"""Offline maintained conformance checks using independently authored fictional examples."""

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

from publicai.contracts import CAPABILITY_IDS, Discovery, load_discovery
from publicai.runtime import TOOL_NAMES, ResponseEnvelope, SnapshotRuntime, create_server

_FAILURE_MESSAGES = {
    "unknown_office": "Unknown office filters must be explicit.",
    "office_normalization": "Office labels must match after case and whitespace normalization.",
    "missing_zone": "Zone-dependent dates must not be returned without a zone.",
    "zone_choices": "Missing zone choices.",
    "date_boundaries": "Default inclusive 30-day interval lost boundary dates.",
    "empty_interval": "Explicitly covered empty intervals must return no_matching_dates.",
    "uncovered_interval": (
        "Uncovered intervals must retain guidance without claiming no collection."
    ),
    "conditions": "Conditional requirements lost their conditions.",
    "undated_snapshot": "Undated snapshots must not be labelled current.",
    "expired_snapshot": "Expired validity must be explicit.",
    "invalid_interval": "Invalid or oversized date ranges must be tool errors.",
    "tool_catalogue": "Package does not expose exactly the six standard tools.",
    "tool_signature": "Tool signature differs from the fixed catalogue: {tool}.",
    "tool_schema": "Tool schema differs from the maintained contract: {tool}.",
    "tool_annotations": "Tool read-only/offline annotations missing: {tool}.",
    "tool_response": "Tool has no successful structured and readable response: {tool}.",
    "tool_identity": "Tool response has incorrect snapshot identity: {tool}.",
    "tool_evidence": "Tool response lacks evidence or an explicit snapshot label: {tool}.",
    "input_error": "Conformance could not complete: invalid snapshot or input.",
    "filesystem_error": "Conformance could not complete: snapshot filesystem error.",
}
_MAX_DIAGNOSTIC_CHARACTERS = 64 * 1024


def safe_failure_messages(output: str) -> list[str]:
    """Select bounded, deduplicated maintained messages without forwarding arbitrary output."""
    allowed = {
        message.format(tool=tool)
        for message in _FAILURE_MESSAGES.values()
        for tool in TOOL_NAMES.values()
    }
    return list(
        dict.fromkeys(
            line for line in output[:_MAX_DIAGNOSTIC_CHARACTERS].splitlines() if line in allowed
        )
    )


def check_conformance(discovery_path: Path | None = None) -> list[str]:
    """Check fixed expected behavior and each package's real MCP tool contracts.

    Args:
        discovery_path: Optional generated inventory to check alongside the maintained fixture.

    Returns:
        Diagnostic failures; an empty list means every check passed.
    """
    fixture_path = Path(__file__).parent / "fixtures" / "representative.json"
    fixture = load_discovery(fixture_path)
    failures = _fixture_checks(fixture)
    candidate = load_discovery(discovery_path) if discovery_path else fixture
    failures.extend(asyncio.run(_mcp_checks(fixture, candidate)))
    return failures


def _fixture_checks(discovery: Discovery) -> list[str]:
    """Use explicit expectations based on the fictional source text, not extraction output."""
    failures = []
    runtime = SnapshotRuntime(discovery, today=date(2026, 10, 1))
    if runtime.query("office_hours", office="unknown").outcome != "unknown_filter":
        failures.append(_FAILURE_MESSAGES["unknown_office"])
    if (
        runtime.query("office_hours", office=" KANZLEI ").data.entries[0].hours[0].value
        != "Montag 09:00–11:00"
    ):
        failures.append(_FAILURE_MESSAGES["office_normalization"])
    unzoned = runtime.query("garbage_collection")
    if unzoned.outcome != "needs_input" or unzoned.data.entries:
        failures.append(_FAILURE_MESSAGES["missing_zone"])
    if unzoned.available_choices.get("zone") != ["Nord", "Süd"]:
        failures.append(_FAILURE_MESSAGES["zone_choices"])
    waste = runtime.query("garbage_collection", zone="Nord")
    if [item.value for item in waste.data.entries[0].dates] != [
        date(2026, 10, 1),
        date(2026, 10, 30),
    ]:
        failures.append(_FAILURE_MESSAGES["date_boundaries"])
    complete_empty = runtime.query(
        "garbage_collection", zone="Nord", date_from="2026-10-02", date_to="2026-10-03"
    )
    if complete_empty.outcome != "no_matching_dates":
        failures.append(_FAILURE_MESSAGES["empty_interval"])
    incomplete = runtime.query(
        "garbage_collection", zone="Nord", date_from="2026-11-01", date_to="2026-11-02"
    )
    if (
        incomplete.outcome != "information_unavailable"
        or not incomplete.data.entries[0].instructions
    ):
        failures.append(_FAILURE_MESSAGES["uncovered_interval"])
    moving = runtime.query("move_in")
    if moving.data.entries[0].requirements[0].conditions[0].value != "Bei Zuzug aus dem Ausland.":
        failures.append(_FAILURE_MESSAGES["conditions"])
    if moving.snapshot.status != "undated":
        failures.append(_FAILURE_MESSAGES["undated_snapshot"])
    expired = SnapshotRuntime(discovery, today=date(2027, 1, 1)).query(
        "garbage_collection", zone="Nord"
    )
    if expired.snapshot.status != "expired":
        failures.append(_FAILURE_MESSAGES["expired_snapshot"])
    for start, end in [("2026-10-03", "2026-10-02"), ("2026-10-01", "2026-12-30")]:
        try:
            runtime.query("garbage_collection", zone="Nord", date_from=start, date_to=end)
        except ValueError:
            pass
        else:
            failures.append(_FAILURE_MESSAGES["invalid_interval"])
    return failures


async def _mcp_checks(fixture: Discovery, candidate: Discovery) -> list[str]:
    """Exercise actual SDK tool registration, JSON schemas and structured tool calls."""
    failures = []
    expected_signatures = {
        "get_office_hours": {"office"},
        "get_garbage_collection": {"waste_type", "zone", "date_from", "date_to"},
        "get_recycling_info": {"material"},
        "get_move_in_requirements": set(),
        "get_move_out_requirements": set(),
        "get_problem_reporting_info": {"category"},
    }
    expected_envelope = {
        "municipality",
        "capability",
        "coverage",
        "discovery_status",
        "missing_reasons",
        "outcome",
        "data",
        "evidence",
        "retrieved_at",
        "snapshot",
        "limitations",
        "available_choices",
    }
    expected_server = create_server(fixture)
    server = create_server(candidate)
    expected = {tool.name: tool for tool in await expected_server.list_tools()}
    actual = {tool.name: tool for tool in await server.list_tools()}
    if set(actual) != set(expected_signatures):
        failures.append(_FAILURE_MESSAGES["tool_catalogue"])
        return failures
    for capability in CAPABILITY_IDS:
        name = TOOL_NAMES[capability]
        tool = actual[name]
        if (
            set(tool.input_schema.get("properties", {})) != expected_signatures[name]
            or tool.input_schema.get("required")
            or set((tool.output_schema or {}).get("properties", {})) != expected_envelope
        ):
            failures.append(_FAILURE_MESSAGES["tool_signature"].format(tool=name))
        if (
            tool.input_schema != expected[name].input_schema
            or tool.output_schema != expected[name].output_schema
        ):
            failures.append(_FAILURE_MESSAGES["tool_schema"].format(tool=name))
        if not tool.annotations or (
            tool.annotations.read_only_hint is not True
            or tool.annotations.destructive_hint is not False
            or tool.annotations.idempotent_hint is not True
            or tool.annotations.open_world_hint is not False
        ):
            failures.append(_FAILURE_MESSAGES["tool_annotations"].format(tool=name))
        result = await server.call_tool(name, {})
        if result.is_error or not result.structured_content or not result.content:
            failures.append(_FAILURE_MESSAGES["tool_response"].format(tool=name))
            continue
        envelope = ResponseEnvelope.model_validate(result.structured_content)
        if (
            envelope.capability != capability
            or envelope.snapshot.discovery_id != candidate.discovery_id
        ):
            failures.append(_FAILURE_MESSAGES["tool_identity"].format(tool=name))
        if not envelope.evidence or "Build-time snapshot" not in result.content[0].text:
            failures.append(_FAILURE_MESSAGES["tool_evidence"].format(tool=name))
    return failures


def main() -> None:
    """Run the maintained checks and emit diagnostics exclusively to stderr."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path)
    args = parser.parse_args()
    try:
        failures = check_conformance(args.discovery)
    except (OSError, ValueError) as error:
        reason = "filesystem_error" if isinstance(error, OSError) else "input_error"
        print(_FAILURE_MESSAGES[reason], file=sys.stderr)
        raise SystemExit(1) from error
    if failures:
        print("\n".join(failures), file=sys.stderr)
        raise SystemExit(1)
    print("Shared conformance checks passed.", file=sys.stderr)


if __name__ == "__main__":
    main()
