"""Typer/Rich commands for municipality discovery and deterministic MCP packaging."""

import asyncio
import json
from collections import Counter
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
    # Accept the profile shorthand without sending "apertus" as a provider model ID.
    if model == AgentMode.APERTUS:
        if mode is not None and mode != AgentMode.APERTUS:
            from publicai.agents import ModelConfigurationError

            raise ModelConfigurationError(
                f"--model apertus conflicts with --mode {mode.value}; use --mode apertus."
            )
        mode = AgentMode.APERTUS
        model = None
    if mode is not None:
        settings.mode = mode
    if model:
        settings.active_model.discovery_model = model
        settings.active_model.review_model = model
    if discovery_model:
        settings.active_model.discovery_model = discovery_model
    if review_model:
        settings.active_model.review_model = review_model
    configure(settings.telemetry, console=console)
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
    from publicai.agents import ReviewDecision, claim_records
    from publicai.contracts import evidence_refs

    discovery = load_discovery(path)
    review_path = path.with_name("review.json")
    review = (
        ReviewDecision.model_validate_json(review_path.read_text(encoding="utf-8"))
        if review_path.is_file()
        else None
    )
    claims = claim_records(
        {
            "identity": discovery.identity.model_dump(mode="json"),
            "capabilities": {
                key: capability.model_dump(mode="json")
                for key, capability in discovery.capabilities.items()
            },
        }
    )
    checks = {check.path: check.status for check in review.checks} if review else {}
    table = Table(title=Text(f"{discovery.identity.name.value} — capability coverage"))
    table.add_column("Capability")
    table.add_column("Coverage")
    table.add_column("Entries", justify="right")
    table.add_column("Gaps / limitations")
    evidence_table = Table(title="Evidence review")
    evidence_table.add_column("Capability")
    evidence_table.add_column("Claims", justify="right")
    evidence_table.add_column("Citations", justify="right")
    evidence_table.add_column("Sources", justify="right")
    evidence_table.add_column("Review")
    for name, capability in discovery.capabilities.items():
        prefix = f"capabilities.{name}."
        paths = [claim for claim in claims if claim.startswith(prefix)]
        refs = evidence_refs(capability)
        verdicts = Counter(checks.get(claim, "missing") for claim in paths)
        review_label = f"{verdicts['supported']} supported" if review else discovery.review.status
        if review and verdicts["conflicting"]:
            review_label += f", {verdicts['conflicting']} conflicting"
        gaps = ", ".join(capability.missing_reasons) or "—"
        if capability.limitations:
            gaps += f"; {len(capability.limitations)} limitation(s)"
        table.add_row(
            name,
            capability.coverage,
            str(len(capability.entries)),
            gaps,
        )
        evidence_table.add_row(
            name,
            str(len(paths)),
            str(len(refs)),
            str(len({ref.source_id for ref in refs})),
            review_label,
        )
    all_refs = evidence_refs(discovery)
    evidence_table.add_section()
    evidence_table.add_row(
        "Total incl. identity",
        str(len(claims)),
        str(len(all_refs)),
        str(len({ref.source_id for ref in all_refs})),
        f"{len(review.checks)} checked" if review else discovery.review.status,
    )
    console.print(table)
    console.print(evidence_table)
    identity = (
        "not included in package"
        if review is None
        else "consistent"
        if review.identity_consistent
        else "inconsistent"
    )
    checked = len(review.checks) if review else "not bundled"
    issues = len(review.issues) if review else "not bundled"
    console.print(
        f"Identity: {identity} | Sources retained: {len(discovery.sources)} | "
        f"Claims checked: {checked} | Blocking issues: {issues}",
        markup=False,
    )
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
            trace = None
            if error.diagnostic_path.is_file():
                diagnostic = json.loads(error.diagnostic_path.read_text(encoding="utf-8"))
                trace = diagnostic.get("review_trace")
            if trace:
                table = Table(title="Evidence review failures")
                table.add_column("Claim path")
                table.add_column("Finding")
                for finding in trace["findings"]:
                    table.add_row(finding["path"], finding["status"])
                console.print(table)
                console.print(
                    f"Identity consistent: {trace['identity_consistent']} | "
                    f"Unexpected checks: {trace['unexpected_checks']} | "
                    f"Blocking issues: {trace['blocking_issues']}",
                    markup=False,
                )
                console.print(
                    f"Reviewer explanations: {error.diagnostic_path.parent / 'review.json'}",
                    markup=False,
                )
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
        AgentMode | None, typer.Option(help="Select openai, apertus (Swisscom), or publicai.")
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(help="Override both agent model IDs; 'apertus' selects the Apertus profile."),
    ] = None,
    discovery_model: Annotated[
        str | None, typer.Option(help="Discovery model; takes precedence over --model.")
    ] = None,
    review_model: Annotated[
        str | None, typer.Option(help="Evidence-review model; takes precedence over --model.")
    ] = None,
) -> None:
    """Inspect an allowed municipality and retain a reviewed discovery artifact."""
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
        AgentMode | None, typer.Option(help="Select openai, apertus (Swisscom), or publicai.")
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(help="Override both agent model IDs; 'apertus' selects the Apertus profile."),
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
        _coverage(path)
        console.print(f"Discovery: {path}\nPackage: {package}", markup=False)
    except Exception as error:
        _failure(error)
