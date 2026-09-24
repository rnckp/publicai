"""Pydantic AI DuckDuckGo integration retains municipal search boundaries."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from ddgs.ddgs import DDGS
from ddgs.exceptions import RatelimitException
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from publicai.agents import DiscoveryContext, create_agents
from publicai.config import Settings
from publicai.contracts import SourceSnapshot
from publicai.crawler import SafeCrawler


@pytest.mark.parametrize("failure", [False, True])
async def test_duckduckgo_scoping_budget_and_safe_failure(
    monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    fixture = json.loads(
        (Path(__file__).parents[2] / "src/publicai/fixtures/representative.json").read_text()
    )
    calls = []
    now = 0.0
    attempts = []

    async def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr("publicai.agents.time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr("publicai.agents.asyncio.sleep", advance)

    def search(self: DDGS, query: str, **kwargs: Any) -> list[dict[str, str]]:
        calls.append((query, kwargs))
        attempts.append(now)
        assert query == 'site:www.ausserberg.ch "Sperrgut"'
        assert kwargs == {"backend": "duckduckgo", "max_results": 5}
        if failure:
            raise RatelimitException("private provider payload")
        return [
            {"title": "Sperrgut", "href": "https://www.ausserberg.ch/sperrgut", "body": "lead"},
            {"title": "Wrong host", "href": "https://evil.example/", "body": "discard"},
            {"title": "Wrong scheme", "href": "ftp://www.ausserberg.ch/file", "body": "discard"},
        ]

    monkeypatch.setattr(DDGS, "text", search)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert info.model_request_parameters.native_tools == []
        returns = [
            p
            for m in messages
            for p in m.parts
            if isinstance(p, ToolReturnPart) and p.tool_name == "web_search"
        ]
        if len(returns) < 3:
            return ModelResponse(parts=[ToolCallPart("web_search", {"query": "Sperrgut"})])
        first = returns[0].content
        assert first["scope"] == "duckduckgo_municipal_index"
        assert first["untrusted_evidence"] is True
        assert "private provider payload" not in str(first)
        if failure:
            assert first["error"] == "search_unavailable"
            assert first["results"] == []
        else:
            assert first["results"] == [
                {"title": "Sperrgut", "href": "https://www.ausserberg.ch/sperrgut", "body": "lead"}
            ]
        assert returns[2].content["error"] == "search_budget_exhausted"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {key: fixture[key] for key in ("identity", "capabilities")},
                )
            ]
        )

    settings = Settings(
        mode="apertus", web_search_enabled=True, apertus={"search_request_limit": 2}
    )
    async with SafeCrawler() as crawler:
        context = DiscoveryContext(
            crawler=crawler,
            official_url=fixture["official_url"],
            discovery_id="search-test",
            created_at=datetime.now(UTC),
            sources={s["id"]: SourceSnapshot.model_validate(s) for s in fixture["sources"]},
        )
        agents = create_agents(settings, FunctionModel(respond), TestModel())
        await agents.discoverer.run("Discover", deps=context)
        assert crawler.request_count == 0
        assert len(context.sources) == len(fixture["sources"])
        assert len(calls) == 2
        assert attempts == [0, 1]
