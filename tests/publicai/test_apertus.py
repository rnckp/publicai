"""Swisscom's wire contract, tool loop and conservative retry pacing, without network."""

import json
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai import models
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ToolReturn
from test_pipeline import RetainedCrawler

from publicai.agents import (
    Inventory,
    ModelConfigurationError,
    claim_records,
    live_agents,
    usage_limits,
)
from publicai.config import Settings
from publicai.pipeline import discover_with_agents


@pytest.mark.parametrize(
    ("mode", "thinking"), [("apertus", False), ("publicai", False), ("publicai", True)]
)
async def test_apertus_tool_workflow_and_retry_pacing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, thinking: bool
) -> None:
    fixture = json.loads(
        (Path(__file__).parents[2] / "src/publicai/fixtures/representative.json").read_text()
    )
    candidate = json.loads(
        json.dumps({key: fixture[key] for key in ("identity", "capabilities")})
        .replace('"source_id": "home"', '"source_id": "source-1"')
        .replace('"source_id": "contact"', '"source_id": "source-2"')
    )
    candidate = Inventory.model_validate(candidate).model_dump(mode="json")
    now = 0.0
    attempts = []
    tool_names = []
    clients = []
    progress = []

    async def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    def respond(request: httpx.Request) -> httpx.Response:
        attempts.append(now)
        assert str(request.url) == (
            "https://api.swisscom.com/products/swiss-ai-weeks/apertus-1.5-70b/v1/chat/completions"
            if mode == "apertus"
            else "https://api.publicai.co/v1/chat/completions"
        )
        assert request.headers["authorization"] == (
            "Bearer test-swisscom-key" if mode == "apertus" else "Bearer test-publicai-key"
        )
        if mode == "publicai":
            # PublicAI requires a User-Agent; Pydantic AI supplies its versioned identifier.
            assert request.headers["user-agent"].startswith("pydantic-ai/")
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body["tools"]}
        suffix = "-thinking" if thinking and "list_sources" in names else ""
        assert body["model"] == (
            "swiss-ai/Apertus-v1.5-70B"
            if mode == "apertus"
            else "swiss-ai/apertus-v1.5-70b" + suffix
        )
        assert "reasoning_effort" not in body
        assert "response_format" not in body
        assert all(not tool["function"].get("strict") for tool in body["tools"])
        assert names == (
            {"web_search", "web_fetch", "list_sources", "final_result"}
            if "list_sources" in names
            else {"read_source", "final_result"}
        )
        if len(attempts) == 1:
            return httpx.Response(429, headers={"retry-after": "0.01"}, json={"error": "limited"})
        discovery = "list_sources" in names
        if not any(message["role"] == "tool" for message in body["messages"]):
            name = "web_search" if discovery else "read_source"
            arguments = {"query": "Kontakt"} if discovery else {"source_id": "source-1"}
        elif discovery and len([m for m in body["messages"] if m["role"] == "tool"]) == 1:
            searched = json.loads(body["messages"][-1]["content"])
            assert searched["scope"] == "exa_municipal_index"
            name = "web_fetch"
            arguments = {"url": fixture["sources"][0]["url"]}
        else:
            if discovery:
                fetched = json.loads(body["messages"][-1]["content"])
                assert fetched["source"]["id"] == "source-1"
                assert fetched["untrusted_evidence"] is True
            name = next(name for name in names if name.startswith("final_result"))
            arguments = (
                candidate
                if discovery
                else {
                    "identity_consistent": True,
                    "checks": [
                        {"path": path, "status": "supported"} for path in claim_records(candidate)
                    ],
                    "issues": [],
                }
            )
        tool_names.append(name)
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "created": 0,
                "object": "chat.completion",
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"call-{len(attempts)}",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(arguments)},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    original_client = httpx.AsyncClient

    def http_client(**kwargs: object) -> httpx.AsyncClient:
        client = original_client(transport=httpx.MockTransport(respond), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setenv("SWISSCOM_KEY", "test-swisscom-key")
    monkeypatch.setenv("PUBLICAI_API_KEY", "test-publicai-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    monkeypatch.setattr("publicai.agents.AsyncClient", http_client)
    monkeypatch.setattr("publicai.agents.time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr("publicai.agents.asyncio.sleep", advance)
    settings = Settings(mode=mode, web_search_enabled=True, **{mode: {"http_retries": 2}})
    if thinking:
        settings.publicai.discovery_model = "swiss-ai/apertus-v1.5-70b-thinking"
    crawler = RetainedCrawler(fixture["sources"])
    crawler.settings = settings.crawl
    fetched_urls = []

    async def fetch(url: str) -> object:
        fetched_urls.append(url)
        return (await crawler.seed(fixture["official_url"]))[0]

    async def web_search(query: str) -> ToolReturn[str]:
        return ToolReturn("No leads", metadata={"sources": []})

    crawler.web_search = web_search
    crawler.fetch = fetch
    async with live_agents(settings, progress.append) as agents:
        path = await discover_with_agents(
            "https://www.ausserberg.ch/",
            tmp_path,
            settings,
            agents,
            crawler,
        )
    assert json.loads(path.read_text())["review"]["status"] == "passed"
    assert tool_names == [
        "web_search",
        "web_fetch",
        "final_result",
        "read_source",
        "final_result",
    ]
    assert len(attempts) == 6
    assert fetched_urls == [fixture["sources"][0]["url"]]
    interval = 0.5 if mode == "apertus" else 1.0
    assert all(later - earlier >= interval for earlier, later in pairwise(attempts))
    assert all(client.is_closed for client in clients)
    assert any("HTTP 429" in message and "retry 1/2" in message for message in progress)
    assert any(("Swisscom" if mode == "apertus" else "PublicAI") in msg for msg in progress)


async def test_missing_swisscom_key_does_not_fall_back_to_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SWISSCOM_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    with pytest.raises(ModelConfigurationError, match="SWISSCOM_KEY is missing"):
        async with live_agents(Settings(mode="apertus")):
            pytest.fail("Missing Swisscom credentials must fail before a connection")


async def test_missing_publicai_key_does_not_fall_back_to_other_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PUBLICAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("SWISSCOM_KEY", "test-swisscom-key")
    with pytest.raises(ModelConfigurationError, match="PUBLICAI_API_KEY is missing"):
        async with live_agents(Settings(mode="publicai")):
            pytest.fail("Missing PublicAI credentials must fail before a connection")


def test_apertus_uses_its_own_usage_limits() -> None:
    settings = Settings(mode="apertus", apertus={"request_limit": 3, "tool_calls_limit": 6})
    limits = usage_limits(settings)
    assert limits.request_limit == 3
    assert limits.tool_calls_limit == 6


async def test_apertus_http_retries_can_be_disabled_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = []

    def respond(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        body = json.loads(request.content)
        assert body["max_completion_tokens"] == 8192
        return httpx.Response(429, json={"error": "limited"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "publicai.agents.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setenv("SWISSCOM_KEY", "test-swisscom-key")
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    settings = Settings(mode="apertus", apertus={"http_retries": 0})
    assert settings.apertus.retries == 2
    async with live_agents(settings) as agents:
        with pytest.raises(ModelHTTPError) as error:
            await agents.reviewer.run("Synthetic rate-limit test", deps={})
    assert error.value.status_code == 429
    assert len(attempts) == 1
