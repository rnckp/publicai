"""Shared snapshot service behavior independent of model providers."""

import sys
from datetime import date
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from publicai.contracts import Discovery, load_discovery
from publicai.runtime import SnapshotRuntime, create_server, date_window


@pytest.fixture
def discovery() -> Discovery:
    return load_discovery(Path(__file__).parents[2] / "src/publicai/fixtures/representative.json")


@pytest.fixture
def runtime(discovery: Discovery) -> SnapshotRuntime:
    return SnapshotRuntime(discovery, today=date(2026, 10, 1))


def test_unknown_filters_offer_exact_choices(runtime: SnapshotRuntime) -> None:
    result = runtime.query("office_hours", office="Unknown")
    assert result.outcome == "unknown_filter"
    assert "Kanzlei" in result.available_choices["office"]
    assert runtime.query("office_hours", office=" KANZLEI ").outcome == "ok"


def test_missing_zone_returns_only_general_guidance(runtime: SnapshotRuntime) -> None:
    result = runtime.query("garbage_collection")
    assert result.outcome == "needs_input"
    assert result.available_choices["zone"] == ["Nord", "Süd"]
    assert not result.data.entries


def test_collection_boundaries_are_inclusive(runtime: SnapshotRuntime) -> None:
    result = runtime.query("garbage_collection", zone="Nord")
    assert result.outcome == "ok"
    assert [item.value for item in result.data.entries[0].dates] == [
        date(2026, 10, 1),
        date(2026, 10, 30),
    ]


def test_partial_coverage_never_claims_no_dates(runtime: SnapshotRuntime) -> None:
    result = runtime.query(
        "garbage_collection", zone="Nord", date_from="2026-11-01", date_to="2026-11-02"
    )
    assert result.outcome == "information_unavailable"
    assert result.data.entries[0].instructions
    result = runtime.query(
        "garbage_collection", zone="Nord", date_from="2026-10-02", date_to="2026-10-03"
    )
    assert result.outcome == "no_matching_dates"


@pytest.mark.parametrize("start,end", [("2026-10-03", "2026-10-02"), ("2026-10-01", "2026-12-30")])
def test_invalid_date_ranges_are_tool_errors(
    runtime: SnapshotRuntime, start: str, end: str
) -> None:
    with pytest.raises(ValueError):
        runtime.query("garbage_collection", zone="Nord", date_from=start, date_to=end)


def test_conditions_and_expired_validity_are_preserved(discovery: Discovery) -> None:
    runtime = SnapshotRuntime(discovery, today=date(2027, 1, 1))
    moving = runtime.query("move_in")
    assert (
        moving.data.entries[0].requirements[0].conditions[0].value == "Bei Zuzug aus dem Ausland."
    )
    assert moving.snapshot.status == "undated"
    waste = runtime.query("garbage_collection", zone="Nord")
    assert waste.snapshot.status == "expired"


@pytest.mark.asyncio
async def test_real_mcp_lists_six_readonly_tools_with_structured_output(
    discovery: Discovery,
) -> None:
    server = create_server(discovery)
    tools = await server.list_tools()
    assert len(tools) == 6
    assert all(tool.annotations.read_only_hint for tool in tools)
    assert all(tool.output_schema for tool in tools)
    result = await server.call_tool("get_move_in_requirements", {})
    assert not result.is_error
    assert result.structured_content["data"]["entries"][0]["requirements"]
    assert result.content[0].text


@pytest.mark.parametrize(
    "start,end,expected",
    [
        (None, None, (date(2026, 10, 1), date(2026, 10, 30))),
        ("2026-10-15", None, (date(2026, 10, 15), date(2026, 11, 13))),
        (None, "2026-10-05", (date(2026, 10, 1), date(2026, 10, 5))),
        ("2026-10-01", "2026-12-29", (date(2026, 10, 1), date(2026, 12, 29))),
    ],
)
def test_date_defaults_and_ninety_day_inclusive_limit(
    start: str | None, end: str | None, expected: tuple[date, date]
) -> None:
    assert date_window(date(2026, 10, 1), start, end) == expected


def test_undated_collection_keeps_guidance(discovery: Discovery) -> None:
    payload = discovery.model_dump(mode="json")
    waste = payload["capabilities"]["garbage_collection"]
    waste["valid_from"] = waste["valid_to"] = None
    for entry in waste["entries"]:
        entry["dates"] = []
    runtime = SnapshotRuntime(Discovery.model_validate(payload), today=date(2026, 10, 1))
    result = runtime.query("garbage_collection", zone="Nord")
    assert result.outcome == "information_unavailable"
    assert result.coverage == "supported"
    assert result.data.entries[0].instructions
    assert result.snapshot.status == "undated"


@pytest.mark.asyncio
async def test_stdio_transport_negotiates_and_invalid_dates_are_protocol_tool_errors() -> None:
    fixture = Path(__file__).parents[2] / "src/publicai/fixtures/representative.json"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "publicai.runtime", "--discovery", str(fixture)],
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams, read_timeout_seconds=10) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert len(tools.tools) == 6
            response = await client.call_tool("get_office_hours", {"office": "Kanzlei"})
            assert not response.is_error
            assert response.structured_content["data"]["entries"][0]["hours"][0]["value"] == (
                "Montag 09:00–11:00"
            )
            invalid = await client.call_tool(
                "get_garbage_collection",
                {"date_from": "2026-10-02", "date_to": "2026-10-01"},
            )
            assert invalid.is_error


def test_missing_zone_never_returns_unqualified_collection_dates(discovery: Discovery) -> None:
    payload = discovery.model_dump(mode="json")
    payload["capabilities"]["garbage_collection"]["entries"][0]["zone"] = None
    runtime = SnapshotRuntime(Discovery.model_validate(payload), today=date(2026, 10, 1))
    result = runtime.query("garbage_collection")
    assert result.outcome == "needs_input"
    assert result.data.entries[0].instructions
    assert not result.data.entries[0].dates


def test_unknown_material_offers_discovered_materials_and_collection_point_labels(
    discovery: Discovery,
) -> None:
    payload = discovery.model_dump(mode="json")
    entry = payload["capabilities"]["recycling"]["entries"][0]
    entry["materials"] = [entry["label"]]
    entry["id"] = "sammelstelle-dorfplatz"
    entry["label"] = {
        "value": "Sammelstelle Dorfplatz",
        "evidence": entry["instructions"][0]["evidence"],
    }
    runtime = SnapshotRuntime(Discovery.model_validate(payload))

    result = runtime.query("recycling", material="Unknown")

    assert result.outcome == "unknown_filter"
    assert set(result.available_choices["material"]) == {
        "Glas",
        "Sammelstelle Dorfplatz",
        "sammelstelle-dorfplatz",
    }
    for choice in result.available_choices["material"]:
        assert runtime.query("recycling", material=choice).outcome == "ok"
