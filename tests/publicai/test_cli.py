"""Exercise the public CLI while replacing only external model/acquisition boundaries."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Self

import pytest
from pydantic_ai import models
from test_pipeline import RetainedCrawler, fixture_agents
from typer.testing import CliRunner

from publicai.agents import FactoryAgents
from publicai.cli import app
from publicai.config import Settings


@pytest.fixture(autouse=True)
def restore_cli_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not leave a logger pointing to CliRunner's closed capture stream."""
    logger = logging.getLogger("publicai")
    monkeypatch.setattr(logger, "handlers", list(logger.handlers))
    monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.setattr(logger, "propagate", logger.propagate)


def test_normal_run_creates_discovery_and_tested_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    agents, retained = fixture_agents(Settings())

    @asynccontextmanager
    async def model_boundary(settings: Settings) -> AsyncIterator[FactoryAgents]:
        yield agents

    class OfflineCrawler(RetainedCrawler):
        def __init__(self, settings: object) -> None:
            super().__init__(retained.sources)

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr("publicai.pipeline.live_agents", model_boundary)
    monkeypatch.setattr("publicai.pipeline.SafeCrawler", OfflineCrawler)
    result = CliRunner().invoke(app, ["run", "https://www.ausserberg.ch/", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Discovery:" in result.output and "Package:" in result.output
    assert len(list(tmp_path.glob("discovery-*/discovery.json"))) == 1
    assert len(list(tmp_path.glob("build-*/manifest.json"))) == 1


def test_cli_rejects_external_url_before_provider_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    result = CliRunner().invoke(app, ["discover", "https://evil.example/", "--out", str(tmp_path)])
    assert result.exit_code == 1
    assert not list(tmp_path.iterdir())


def test_build_requires_no_provider_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fixture = Path(__file__).parents[2] / "src/publicai/fixtures/representative.json"
    result = CliRunner().invoke(app, ["build", str(fixture), "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(list(tmp_path.glob("build-*/manifest.json"))) == 1
