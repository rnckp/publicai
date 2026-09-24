"""Discovery metadata must explain uncertainty without asserting service absence."""

from hashlib import sha256
from pathlib import Path

import pytest

from publicai.contracts import Capability, Discovery, EvidenceRef, load_discovery
from publicai.runtime import SnapshotRuntime, tool_result


@pytest.mark.parametrize(
    ("reasons", "status"),
    [
        ([], "not_observed"),
        (["not_found"], "not_observed"),
        (["blocked"], "blocked"),
        (["inaccessible"], "blocked"),
        (["crawl_limit"], "acquisition_limited"),
        (["explicitly_not_offered"], "explicitly_not_offered"),
    ],
)
def test_runtime_distinguishes_missing_information_from_service_denial(
    reasons: list[str],
    status: str,
) -> None:
    discovery = load_discovery(
        Path(__file__).parents[2] / "src/publicai/fixtures/representative.json"
    )
    denial_text = "Ein Wegzugsservice wird nicht angeboten."
    denial = EvidenceRef(source_id="contact", excerpt=denial_text)
    if status == "explicitly_not_offered":
        for source in discovery.sources:
            source.text = source.text.replace(
                "Melden Sie Ihren Wegzug bei der Kanzlei.", denial_text
            )
            source.sha256 = sha256(source.text.encode()).hexdigest()
    discovery.capabilities["move_out"] = Capability(
        coverage="unavailable",
        missing_reasons=reasons,
        not_offered_evidence=[denial] if status == "explicitly_not_offered" else [],
    )
    discovery = Discovery.model_validate(discovery.model_dump())
    result = SnapshotRuntime(discovery).query("move_out")
    assert result.coverage == "unavailable"
    assert result.discovery_status == status
    assert result.missing_reasons == reasons
    assert status in tool_result(result).content[0].text
    if status == "explicitly_not_offered":
        assert any(citation.excerpt == denial_text for citation in result.evidence)


def test_supported_service_with_a_gap_is_still_observed() -> None:
    discovery = load_discovery(
        Path(__file__).parents[2] / "src/publicai/fixtures/representative.json"
    )
    discovery.capabilities["move_out"].missing_reasons = ["blocked"]
    result = SnapshotRuntime(discovery).query("move_out")
    assert result.discovery_status == "observed"
    assert result.missing_reasons == ["blocked"]


def test_link_ranking_keeps_specific_service_ahead_of_generic_parent_paths() -> None:
    from publicai.agents import rank_source_links

    base = "https://www.ausserberg.ch"
    noise = [
        {"url": f"{base}/verwaltung/galerie/{i}", "label": "Galerie", "kind": "html"}
        for i in range(160)
    ]
    useful = {"url": f"{base}/abfall", "label": "Abfall und Recycling", "kind": "html"}
    links = rank_source_links([*noise, useful])
    assert links[0] == useful
    assert len(links) == 161  # Ranking never discards possible evidence.
    assert rank_source_links([*noise, useful], "Recycling")[0] == useful


def test_link_ranking_prefers_label_and_leaf_over_shared_parent_match() -> None:
    from publicai.agents import rank_source_links

    parent = {"url": "https://www.ausserberg.ch/abfall/archiv", "label": "Archiv", "kind": "html"}
    leaf = {"url": "https://www.ausserberg.ch/entsorgung", "label": "Abfall", "kind": "html"}
    assert rank_source_links([parent, leaf], "abfall") == [leaf, parent]
    assert rank_source_links([parent, leaf], "unknown") == []
