"""Independent fictional gold cases; live model quality is measured only by explicit opt-in."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from dotenv import load_dotenv
from pydantic import TypeAdapter
from pydantic_ai.models.test import TestModel

from publicai.agents import (
    Inventory,
    ReviewDecision,
    ReviewValidationError,
    claim_records,
    create_agents,
    live_agents,
    review_prompt,
    usage_limits,
    validate_review,
)
from publicai.config import Settings, load_settings
from publicai.contracts import CAPABILITY_IDS, Discovery, SourceSnapshot, StrictModel


class GoldCase(StrictModel):
    """Manually authored evidence and an expected publication decision."""

    id: str
    scenario: Literal["hours", "requirement", "handoff"]
    source_text: str
    excerpt: str
    value: str
    expected_approval: bool


CASES = TypeAdapter(list[GoldCase]).validate_json(
    Path(__file__).with_name("fixtures").joinpath("review_gold.json").read_text()
)


def case_input(case: GoldCase) -> tuple[Inventory, dict[str, SourceSnapshot]]:
    """Make a full minimal inventory with literal evidence, including intentionally false claims."""
    name = "Fiktive Testgemeinde"
    contact = "Kanzlei: Telefon +41 00 000 00 00"

    def fact(value: str, excerpt: str | None = None) -> dict:
        return {"value": value, "evidence": [{"source_id": "contact", "excerpt": excerpt or value}]}

    capabilities = {
        key: {"coverage": "unavailable", "missing_reasons": ["not_found"]} for key in CAPABILITY_IDS
    }
    claim = fact(case.value, case.excerpt)
    if case.scenario == "hours":
        capabilities["office_hours"] = {
            "coverage": "supported",
            "entries": [
                {
                    "id": "kanzlei",
                    "label": fact("Kanzlei"),
                    "hours": [claim],
                    "contacts": [fact(contact)],
                }
            ],
        }
    elif case.scenario == "requirement":
        capabilities["move_in"] = {
            "coverage": "supported",
            "entries": [
                {
                    "id": "zuzug",
                    "label": fact("Zuzug"),
                    "requirements": [{"text": claim, "conditions": []}],
                    "contacts": [fact(contact)],
                }
            ],
        }
    else:
        capabilities["move_in"] = {"coverage": "handoff_only", "handoffs": [claim]}
    inventory = Inventory.model_validate(
        {
            "identity": {
                "name": {
                    "value": name,
                    "evidence": [
                        {"source_id": source_id, "excerpt": name}
                        for source_id in ("home", "contact")
                    ],
                },
                "contact": fact(contact),
            },
            "capabilities": capabilities,
        }
    )
    sources = {}
    for source_id, path, kind in (("home", "/", "homepage"), ("contact", "/kontakt", "contact")):
        text = f"FICTIONAL EVALUATION ONLY\n{name}\n{contact}\nZuzug\n"
        if source_id == "contact":
            text += case.source_text
        sources[source_id] = SourceSnapshot(
            id=source_id,
            url=f"https://www.ausserberg.ch{path}",
            title=name,
            text=text,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
            kind=kind,
            retrieved_at=datetime(2026, 9, 24, tzinfo=UTC),
            links=[case.value] if case.scenario == "handoff" and source_id == "contact" else [],
        )
    return inventory, sources


def approved(decision: ReviewDecision, inventory: Inventory) -> bool:
    """Use the production gate, including missing checks and identity disagreements."""
    try:
        validate_review(decision, claim_records(inventory.model_dump(mode="json")))
    except ReviewValidationError:
        return False
    return True


def summarize(results: list[dict]) -> dict:
    """Keep execution errors separate from false approvals and false rejections."""
    negatives = sum(not result["expected_approval"] for result in results)
    positives = len(results) - negatives
    false_approvals = sum(
        result["actual_approval"] is True and not result["expected_approval"] for result in results
    )
    false_rejections = sum(
        result["actual_approval"] is False and result["expected_approval"] for result in results
    )
    return {
        "cases": len(results),
        "errors": sum(result["actual_approval"] is None for result in results),
        "false_approvals": false_approvals,
        "false_rejections": false_rejections,
        "false_approval_rate": false_approvals / negatives if negatives else None,
        "false_rejection_rate": false_rejections / positives if positives else None,
    }


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_gold_cases_pass_literal_evidence_checks(case: GoldCase) -> None:
    """All cases reach semantic review; rejecting a fabricated source is not this evaluation."""
    inventory, sources = case_input(case)
    Discovery(
        contract_version="1.0",
        discovery_id="gold-case",
        created_at=datetime(2026, 9, 24, tzinfo=UTC),
        official_url="https://www.ausserberg.ch/",
        identity=inventory.identity,
        capabilities=inventory.capabilities,
        sources=list(sources.values()),
    )
    prompt = review_prompt(inventory, sources)
    assert "expected_approval" not in prompt
    assert case.id not in prompt


def test_review_prompt_supplies_context_outside_selected_excerpts() -> None:
    """Contradictions must reach review even when the model makes no source-tool calls."""
    case = next(case for case in CASES if case.id == "conflicting_hours")
    inventory, sources = case_input(case)
    consistent_inventory, consistent_sources = case_input(
        case.model_copy(update={"source_text": case.excerpt})
    )

    prompt = review_prompt(inventory, sources)
    assert prompt != review_prompt(consistent_inventory, consistent_sources)
    payload = json.loads(prompt.split("\n", 1)[1])
    supplied = {source["id"]: source for source in payload["sources"]}
    assert supplied["home"]["text"] == sources["home"].text
    assert supplied["contact"]["text"] == sources["contact"].text
    assert "Montag geschlossen" in supplied["contact"]["text"]


async def test_always_approving_reviewer_is_detected_without_network() -> None:
    results = []
    for case in CASES:
        inventory, sources = case_input(case)
        checks = [
            {"path": path, "status": "supported"}
            for path in claim_records(inventory.model_dump(mode="json"))
        ]
        model = TestModel(
            call_tools=[],
            custom_output_args={
                "identity_consistent": True,
                "checks": checks,
                "issues": [],
            },
        )
        agents = create_agents(Settings(), TestModel(), model)
        result = await agents.reviewer.run(review_prompt(inventory, sources), deps=sources)
        results.append(
            {
                "expected_approval": case.expected_approval,
                "actual_approval": approved(result.output, inventory),
            }
        )
    summary = summarize(results)
    assert summary["false_approvals"] == 6
    assert summary["false_approval_rate"] == 1.0
    assert summary["false_rejections"] == 0


def test_evaluation_errors_are_not_scored_as_rejections() -> None:
    summary = summarize([{"expected_approval": True, "actual_approval": None}])
    assert summary["errors"] == 1
    assert summary["false_rejections"] == 0


def test_scoring_distinguishes_false_rejection_from_correct_rejection() -> None:
    summary = summarize(
        [
            {"expected_approval": True, "actual_approval": False},
            {"expected_approval": True, "actual_approval": True},
            {"expected_approval": False, "actual_approval": False},
        ]
    )
    assert summary["false_rejections"] == 1
    assert summary["false_rejection_rate"] == 0.5
    assert summary["false_approvals"] == 0


async def test_live_review_gold_set(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    """Invoke deliberately with --run-review-evals; never enabled by credentials alone."""
    if not request.config.getoption("--run-review-evals"):
        pytest.skip("Paid reviewer evaluation requires --run-review-evals")
    load_dotenv()
    settings = load_settings()
    results = []
    # No application telemetry setup or crawler is used by this evaluation.
    async with live_agents(settings) as agents:
        for case in CASES:
            inventory, sources = case_input(case)
            record = {
                "id": case.id,
                "expected_approval": case.expected_approval,
                "actual_approval": None,
            }
            try:
                async with asyncio.timeout(settings.run_timeout):
                    result = await agents.reviewer.run(
                        review_prompt(inventory, sources),
                        deps=sources,
                        usage_limits=usage_limits(settings),
                    )
                record["actual_approval"] = approved(result.output, inventory)
            except Exception as error:
                # Provider exceptions can contain sensitive payloads.
                record["error_type"] = type(error).__name__
            results.append(record)
    report = {
        "model": settings.active_model.review_model,
        "summary": summarize(results),
        "results": results,
    }
    path = tmp_path / "review-evaluation.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nReviewer evaluation report: {path}\n{json.dumps(report, indent=2)}")
    assert report["summary"]["errors"] == 0
    assert report["summary"]["false_approvals"] == 0
    assert report["summary"]["false_rejections"] == 0
