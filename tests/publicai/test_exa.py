"""Exa's provider boundary preserves municipal evidence and acquisition limits."""

import hashlib
import json

import httpx
import pytest

from publicai.config import ExaSettings
from publicai.crawler import CrawlError, CrawlSettings
from publicai.retrieval import ExaRetriever


async def test_search_then_fetch_uses_pydantic_exa_and_retains_page() -> None:
    calls = []
    url = "https://www.ausserberg.ch/kontakt"

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(request.url.path)
        assert request.url.host == "api.exa.ai"
        assert request.headers["x-api-key"] == "test-key"
        if request.url.path == "/search":
            assert payload["includeDomains"] == ["www.ausserberg.ch"]
            assert payload["contents"] == {"highlights": True}
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": url, "title": "Kontakt", "highlights": ["Gemeindekanzlei"]},
                        {
                            "url": "https://evil.example/",
                            "title": "Untrusted",
                            "highlights": ["secret"],
                        },
                    ]
                },
            )
        assert payload["urls"] == [url]
        assert "text" in payload and "highlights" not in payload
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": url, "title": "Kontakt", "text": "Gemeinde Ausserberg\nMontag 9–12"}
                ],
                "statuses": [{"id": url, "status": "success"}],
            },
        )

    async with ExaRetriever(
        CrawlSettings(),
        ExaSettings(request_interval=0.01),
        api_key="test-key",
        transport=httpx.MockTransport(respond),
    ) as retriever:
        result = await retriever.web_search("Kontakt")
        assert "Gemeindekanzlei" in result.return_value
        assert "secret" not in result.return_value
        assert result.metadata["sources"] == [{"url": url, "title": "Kontakt"}]
        page = await retriever.fetch(url)
        assert page.text == "Gemeinde Ausserberg\nMontag 9–12"
        assert page.sha256 == hashlib.sha256(page.text.encode()).hexdigest()
        assert await retriever.fetch(url) is page
    assert calls == ["/search", "/contents"]


@pytest.mark.parametrize(
    "url", ["https://evil.example/", "http://127.0.0.1/", "https://www.ausserberg.ch/calendar.pdf"]
)
async def test_unsafe_fetch_never_reaches_exa(url: str) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("Unsafe URL reached Exa")

    async with ExaRetriever(
        CrawlSettings(), ExaSettings(), api_key="test-key", transport=httpx.MockTransport(forbidden)
    ) as retriever:
        with pytest.raises(CrawlError, match="authorized|HTML"):
            await retriever.fetch(url)
        assert retriever.request_count == 0


@pytest.mark.parametrize(
    "bad_response",
    [
        {"results": [{"url": "https://evil.example/", "text": "Injected"}]},
        {"results": [{"url": "https://www.ausserberg.ch/", "text": ""}]},
        {
            "results": [{"url": "https://www.ausserberg.ch/", "text": "Partial"}],
            "statuses": [{"id": "https://www.ausserberg.ch/", "status": "error"}],
        },
    ],
)
async def test_invalid_or_failed_contents_are_not_retained(bad_response: dict) -> None:
    async with ExaRetriever(
        CrawlSettings(),
        ExaSettings(),
        api_key="test-key",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=bad_response)),
    ) as retriever:
        with pytest.raises(CrawlError):
            await retriever.fetch("https://www.ausserberg.ch/")
        assert retriever.failures


@pytest.mark.parametrize("mode", ["openai", "apertus"])
async def test_both_agent_modes_search_and_fetch_through_exa(mode: str) -> None:
    from datetime import UTC, datetime
    from pathlib import Path

    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.models.test import TestModel

    from publicai.agents import DiscoveryContext, create_agents
    from publicai.config import Settings
    from publicai.contracts import SourceSnapshot

    fixture = json.loads(
        (Path(__file__).parents[2] / "src/publicai/fixtures/representative.json").read_text()
    )
    extra_url = "https://www.ausserberg.ch/extra"
    requests = []

    def http_response(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": extra_url,
                        "title": "Extra",
                        "text": "Inspected page text",
                        "highlights": ["Search lead only"],
                    }
                ]
            },
        )

    def model_response(messages: list, info: object) -> ModelResponse:
        assert info.model_request_parameters.native_tools == []
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("web_search", {"query": "Kontakt"})])
        if len(returns) == 1:
            assert "Search lead only" in returns[-1].content["content"]
            assert extra_url not in {source.url for source in context.sources.values()}
            return ModelResponse(parts=[ToolCallPart("web_fetch", {"url": extra_url})])
        assert returns[-1].content["source"]["text"] == "Inspected page text"
        assert returns[-1].content["source"]["url"] == extra_url
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {key: fixture[key] for key in ("identity", "capabilities")},
                )
            ]
        )

    settings = Settings(mode=mode, web_search_enabled=True, exa={"request_interval": 0.01})
    async with ExaRetriever(
        settings.crawl,
        settings.exa,
        api_key="test-key",
        transport=httpx.MockTransport(http_response),
    ) as retriever:
        context = DiscoveryContext(
            retriever,
            fixture["official_url"],
            "test-exa",
            datetime.now(UTC),
            sources={s["id"]: SourceSnapshot.model_validate(s) for s in fixture["sources"]},
        )
        agents = create_agents(settings, FunctionModel(model_response), TestModel())
        await agents.discoverer.run("Discover", deps=context)
    assert requests == ["/search", "/contents"]


async def test_exa_search_budget_pacing_and_redacted_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    now = 0.0
    attempts = []

    async def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    def respond(request: httpx.Request) -> httpx.Response:
        attempts.append(now)
        return httpx.Response(429, json={"error": "sensitive-provider-payload"})

    monkeypatch.setattr("publicai.retrieval.time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr("publicai.retrieval.asyncio.sleep", advance)
    async with ExaRetriever(
        CrawlSettings(),
        ExaSettings(search_request_limit=2),
        api_key="test-key",
        transport=httpx.MockTransport(respond),
    ) as retriever:
        for _ in range(2):
            with pytest.raises(CrawlError, match="Exa request failed"):
                await retriever.web_search("query")
        with pytest.raises(CrawlError, match="budget is exhausted"):
            await retriever.web_search("query")
        assert attempts == [0, 1]
        assert "sensitive" not in str(retriever.failures)
        assert retriever.request_count == 2


async def test_exa_fetch_budget_prevents_extra_requests() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        url = json.loads(request.content)["urls"][0]
        return httpx.Response(200, json={"results": [{"url": url, "text": "Evidence"}]})

    async with ExaRetriever(
        CrawlSettings(max_requests=1),
        ExaSettings(),
        api_key="test-key",
        transport=httpx.MockTransport(respond),
    ) as retriever:
        await retriever.fetch("https://www.ausserberg.ch/")
        with pytest.raises(CrawlError, match="budget exhausted"):
            await retriever.fetch("https://www.ausserberg.ch/kontakt")
        assert retriever.budget_stop_reason == "request_budget_exhausted"
        assert retriever.request_count == 1


async def test_seed_fetches_home_and_linked_contact_through_exa() -> None:
    requested = []
    root = "https://www.ausserberg.ch/"
    contact = root + "kontakt"

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/contents"
        url = json.loads(request.content)["urls"][0]
        requested.append(url)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": url,
                        "title": "Gemeinde Ausserberg",
                        "text": "Gemeinde Ausserberg",
                        "extras": {"links": [contact] if url == root else []},
                    }
                ]
            },
        )

    async with ExaRetriever(
        CrawlSettings(),
        ExaSettings(request_interval=0.01),
        search_enabled=False,
        api_key="test-key",
        transport=httpx.MockTransport(respond),
    ) as retriever:
        pages = await retriever.seed(root)
        assert [page.url for page in pages] == [root, contact]
        assert retriever.discovered_urls == [contact]
    assert requested == [root, contact]


def test_missing_exa_key_fails_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    from publicai.retrieval import ExaConfigurationError

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    with pytest.raises(ExaConfigurationError, match="EXA_API_KEY is missing"):
        ExaRetriever(CrawlSettings(), ExaSettings())
