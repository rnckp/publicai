"""Typer/Rich commands for municipality discovery and deterministic MCP packaging."""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

from publicai.config import AgentMode, Settings, load_settings
from publicai.contracts import load_discovery
from publicai.observability import configure

app = typer.Typer(
    no_args_is_help=True, help="Build evidence-backed, read-only municipality MCP servers."
)
console = Console(stderr=True)


def _settings(
    config: Path | None,
    model: str | None,
    discovery_model: str | None,
    review_model: str | None,
    mode: AgentMode | None = None,
) -> Settings:
    load_dotenv()
    settings = load_settings(config)
    if mode is not None:
        settings.mode = mode
    if model:
        settings.active_model.discovery_model = model
        settings.active_model.review_model = model
    if discovery_model:
        settings.active_model.discovery_model = discovery_model
    if review_model:
        settings.active_model.review_model = review_model
    configure(settings.telemetry)
    return settings


@contextmanager
def _live_progress() -> Iterator[Callable[[str], None]]:
    """Stream lasting milestones and show elapsed time while a stage is busy."""
    started = monotonic()
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}", markup=False),
        TimeElapsedColumn(),
        console=console,
        transient=True,
        disable=not console.is_terminal,
    ) as progress:
        task = progress.add_task("Starting", total=None)

        def report(message: str) -> None:
            console.print(
                f"[{monotonic() - started:6.1f}s elapsed] {message}",
                style="cyan",
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
            progress.update(task, description=message)

        yield report


def _coverage(path: Path) -> None:
    discovery = load_discovery(path)
    table = Table(title=Text(f"{discovery.identity.name.value} — build-time snapshot"))
    table.add_column("Capability")
    table.add_column("Coverage")
    table.add_column("Entries", justify="right")
    for name, capability in discovery.capabilities.items():
        table.add_row(name, capability.coverage, str(len(capability.entries)))
    console.print(table)
    if any(cap.coverage != "supported" for cap in discovery.capabilities.values()):
        console.print(
            "Partial coverage: see report.md for gaps and official handoffs.", style="yellow"
        )


def _failure(error: Exception) -> None:
    from publicai.agents import ModelConfigurationError
    from publicai.builder import BuildError
    from publicai.crawler import CrawlError
    from publicai.pipeline import DiscoveryError
    from publicai.retrieval import ExaConfigurationError

    if isinstance(error, (BuildError, DiscoveryError)):
        if isinstance(error, DiscoveryError):
            console.print(str(error), style="red", markup=False)
        console.print(f"Failed. Diagnostic: {error.diagnostic_path}", style="red", markup=False)
    elif isinstance(error, (ModelConfigurationError, ExaConfigurationError, CrawlError)):
        console.print(str(error), style="red", markup=False)
    else:
        # Do not print SDK/config exception payloads, which can contain credentials.
        console.print(
            f"Failed ({type(error).__name__}). Check configuration and required API key.",
            style="red",
        )
    raise typer.Exit(1) from error


@app.command()
def discover(
    url: str,
    out: Annotated[Path, typer.Option(help="Artifact root; each run creates a unique directory.")],
    config: Annotated[Path | None, typer.Option()] = None,
    mode: Annotated[
        AgentMode | None, typer.Option(help="Select the OpenAI or Apertus profile.")
    ] = None,
    model: Annotated[
        str | None, typer.Option(help="Override both configured agent models.")
    ] = None,
    discovery_model: Annotated[
        str | None, typer.Option(help="Discovery model; takes precedence over --model.")
    ] = None,
    review_model: Annotated[
        str | None, typer.Option(help="Evidence-review model; takes precedence over --model.")
    ] = None,
) -> None:
    """Inspect www.ausserberg.ch and retain a reviewed discovery artifact."""
    from publicai.pipeline import discover as discover_pipeline

    try:
        settings = _settings(config, model, discovery_model, review_model, mode)
        with _live_progress() as progress:
            progress("Connecting discovery services")
            path = asyncio.run(discover_pipeline(url, out, settings, progress))
        _coverage(path)
        console.print(f"Discovery: {path}", markup=False)
    except Exception as error:
        _failure(error)


@app.command()
def build(discovery_json: Path, out: Annotated[Path, typer.Option()]) -> None:
    """Validate and package an existing discovery without model calls or website access."""
    from publicai.builder import build as build_package

    try:
        with _live_progress() as progress:
            package = build_package(discovery_json, out, progress=progress)
        _coverage(package / "discovery.json")
        console.print(f"Package: {package}", markup=False)
    except Exception as error:
        _failure(error)


@app.command()
def run(
    url: str,
    out: Annotated[Path, typer.Option()],
    config: Annotated[Path | None, typer.Option()] = None,
    mode: Annotated[
        AgentMode | None, typer.Option(help="Select the OpenAI or Apertus profile.")
    ] = None,
    model: Annotated[
        str | None, typer.Option(help="Override both configured agent models.")
    ] = None,
    discovery_model: Annotated[
        str | None, typer.Option(help="Discovery model; takes precedence over --model.")
    ] = None,
    review_model: Annotated[
        str | None, typer.Option(help="Evidence-review model; takes precedence over --model.")
    ] = None,
) -> None:
    """Run discovery, evidence review, deterministic packaging and offline conformance."""
    from publicai.builder import build as build_package
    from publicai.pipeline import discover as discover_pipeline

    try:
        settings = _settings(config, model, discovery_model, review_model, mode)
        with _live_progress() as progress:
            progress("Connecting discovery services")
            path = asyncio.run(discover_pipeline(url, out, settings, progress))
            progress(f"Discovery saved: {path}")
            package = build_package(path, out, progress=progress)
        _coverage(package / "discovery.json")
        console.print(f"Discovery: {path}\nPackage: {package}", markup=False)
    except Exception as error:
        _failure(error)
