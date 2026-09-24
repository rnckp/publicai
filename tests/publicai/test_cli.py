"""Exercise the public CLI while replacing only external model/acquisition boundaries."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import StringIO
from pathlib import Path
from typing import Self

import pytest
from pydantic_ai import models
from rich.console import Console
from test_pipeline import RetainedCrawler, fixture_agents
from typer.testing import CliRunner

from publicai.agents import FactoryAgents
from publicai.cli import app
from publicai.config import Settings


@pytest.mark.parametrize("terminal", [False, True])
def test_progress_is_visible_before_work_finishes_and_cleans_up_on_error(
    monkeypatch: pytest.MonkeyPatch, terminal: bool
) -> None:
    from publicai.cli import _live_progress

    output = StringIO()
    console = Console(file=output, force_terminal=terminal, width=160)
    monkeypatch.setattr("publicai.cli.console", console)
    with pytest.raises(ValueError, match="stage failed"):
        with _live_progress() as progress:
            progress("Waiting for evidence [literal]")
            assert "Waiting for evidence [literal]" in output.getvalue()
            assert "elapsed" in output.getvalue()
            raise ValueError("stage failed")
    console.print("Failure diagnostic")
    assert output.getvalue().endswith("Failure diagnostic\n")
    if not terminal:
        assert "\x1b" not in output.getvalue()


def test_cli_reports_safe_discovery_failure_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import typer

    from publicai.cli import _failure
    from publicai.pipeline import DiscoveryError

    with pytest.raises(typer.Exit):
        _failure(
            DiscoveryError("Semantic review rejected the inventory.", tmp_path / "diagnostic.json")
        )
    output = capsys.readouterr().err
    assert "Semantic review rejected the inventory." in output
    assert "diagnostic.json" in output


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
    async def model_boundary(settings: Settings, progress: object) -> AsyncIterator[FactoryAgents]:
        yield agents

    class OfflineCrawler(RetainedCrawler):
        def __init__(self, settings: object, exa: object, **kwargs: object) -> None:
            super().__init__(retained.sources)

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr("publicai.pipeline.live_agents", model_boundary)
    monkeypatch.setattr("publicai.pipeline.ExaRetriever", OfflineCrawler)
    result = CliRunner().invoke(app, ["run", "https://www.ausserberg.ch/", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Discovery:" in result.output and "Package:" in result.output
    milestones = [
        "Connecting discovery services",
        "Retrieving homepage",
        "Evidence ready:",
        "Discovery agent:",
        "Discovery complete:",
        "Review agent:",
        "Evidence review passed:",
        "Publishing reviewed discovery",
        "Build: validating discovery",
        "Build: rendering MCP package",
        "Build: running offline conformance",
        "Build: conformance passed",
        "Build: package published",
    ]
    positions = [result.output.index(message) for message in milestones]
    assert positions == sorted(positions)
    assert "input tokens" in result.output
    assert "elapsed" in result.output
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


@pytest.mark.parametrize("command", ["discover", "run"])
@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ([], ("configured-discovery", "configured-review")),
        (["--model", "shared"], ("shared", "shared")),
        (["--discovery-model", "custom-discovery"], ("custom-discovery", "configured-review")),
        (["--review-model", "custom-review"], ("configured-discovery", "custom-review")),
        (
            ["--model", "shared", "--discovery-model", "specific"],
            ("specific", "shared"),
        ),
        (
            ["--model", "shared", "--review-model", "specific"],
            ("shared", "specific"),
        ),
        (
            ["--discovery-model", "first", "--review-model", "second"],
            ("first", "second"),
        ),
    ],
)
def test_cli_model_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    options: list[str],
    expected: tuple[str, str],
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "model:\n  discovery_model: configured-discovery\n  review_model: configured-review\n"
    )
    selected = []

    async def capture_settings(url: str, out: Path, settings: Settings, progress: object) -> Path:
        selected.append((settings.model.discovery_model, settings.model.review_model))
        return Path(__file__).parents[2] / "src/publicai/fixtures/representative.json"

    monkeypatch.setattr("publicai.pipeline.discover", capture_settings)
    monkeypatch.setattr("publicai.builder.build", lambda path, out, **kwargs: path.parent)
    monkeypatch.setattr("publicai.cli._coverage", lambda path: None)
    result = CliRunner().invoke(
        app,
        [
            command,
            "https://www.ausserberg.ch/",
            "--out",
            str(tmp_path),
            "--config",
            str(config),
            *options,
        ],
    )
    assert result.exit_code == 0, result.output
    assert selected == [expected]


@pytest.mark.parametrize("command", ["discover", "run"])
@pytest.mark.parametrize("mode", ["openai", "apertus"])
def test_cli_mode_overrides_config_and_preserves_model_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, mode: str
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("mode: apertus\napertus:\n  review_model: configured-apertus-review\n")
    selected = []

    async def capture_settings(url: str, out: Path, settings: Settings, progress: object) -> Path:
        selected.append(
            (
                settings.mode,
                settings.active_model.provider,
                settings.active_model.discovery_model,
                settings.active_model.review_model,
            )
        )
        return Path(__file__).parents[2] / "src/publicai/fixtures/representative.json"

    monkeypatch.setattr("publicai.pipeline.discover", capture_settings)
    monkeypatch.setattr("publicai.builder.build", lambda path, out, **kwargs: path.parent)
    monkeypatch.setattr("publicai.cli._coverage", lambda path: None)
    result = CliRunner().invoke(
        app,
        [
            command,
            "https://www.ausserberg.ch/",
            "--out",
            str(tmp_path),
            "--config",
            str(config),
            "--mode",
            mode,
            "--model",
            "shared",
            "--review-model",
            "specific",
        ],
    )
    assert result.exit_code == 0, result.output
    assert selected == [(mode, "swisscom" if mode == "apertus" else "openai", "shared", "specific")]


@pytest.mark.parametrize("command", ["discover", "run"])
@pytest.mark.parametrize("explicit_mode", [False, True])
def test_model_apertus_selects_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, explicit_mode: bool
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("mode: openai\napertus:\n  discovery_model: configured-apertus\n")
    selected = []

    async def capture_settings(url: str, out: Path, settings: Settings, progress: object) -> Path:
        selected.append(
            (
                settings.mode,
                settings.active_model.provider,
                settings.active_model.discovery_model,
                settings.active_model.review_model,
            )
        )
        return Path(__file__).parents[2] / "src/publicai/fixtures/representative.json"

    monkeypatch.setattr("publicai.pipeline.discover", capture_settings)
    monkeypatch.setattr("publicai.builder.build", lambda path, out, **kwargs: path.parent)
    monkeypatch.setattr("publicai.cli._coverage", lambda path: None)
    result = CliRunner().invoke(
        app,
        [
            command,
            "https://www.ausserberg.ch/",
            "--out",
            str(tmp_path),
            "--config",
            str(config),
            "--model",
            "apertus",
            "--review-model",
            "specific",
            *(["--mode", "apertus"] if explicit_mode else []),
        ],
    )
    assert result.exit_code == 0, result.output
    assert selected == [("apertus", "swisscom", "configured-apertus", "specific")]


@pytest.mark.parametrize("command", ["discover", "run"])
def test_conflicting_apertus_selector_fails_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    def unexpected_discovery(*args: object, **kwargs: object) -> None:
        pytest.fail("Conflicting selectors must fail before discovery")

    monkeypatch.setattr("publicai.pipeline.discover", unexpected_discovery)
    result = CliRunner().invoke(
        app,
        [
            command,
            "https://www.ausserberg.ch/",
            "--out",
            str(tmp_path),
            "--mode",
            "openai",
            "--model",
            "apertus",
        ],
    )
    assert result.exit_code != 0
    assert "--model apertus conflicts with --mode openai" in result.output
