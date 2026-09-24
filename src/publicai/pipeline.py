"""Trusted orchestration from bounded acquisition to reviewed immutable artifacts."""

import asyncio
import hashlib
import json
import logging
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from pydantic_ai import capture_run_messages
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart

from publicai.agents import (
    DiscoveryContext,
    FactoryAgents,
    ReviewValidationError,
    claim_records,
    live_agents,
    review_prompt,
    usage_limits,
    validate_review,
)
from publicai.builder import render_report
from publicai.config import Settings
from publicai.contracts import (
    Discovery,
    DiscoveryFailure,
    ReviewMetadata,
    discovery_status,
    evidence_refs,
    facts,
    normalize_whitespace,
)
from publicai.crawler import SafeCrawler, validate_url
from publicai.retrieval import ExaRetriever

logger = logging.getLogger(__name__)


def discovery_report(
    context: DiscoveryContext,
    discovery: Discovery | None,
    settings: Settings,
    *,
    stop_reason: str,
    acquisition_stop_reason: str | None,
    published: bool,
) -> dict:
    """Summarize trusted acquisition and candidate coverage without claiming search completeness."""
    capabilities = discovery.capabilities if discovery else {}
    return {
        "discovery_id": context.discovery_id,
        "publication_status": "published" if published else "failed",
        "review_status": discovery.review.status if discovery else "not_run",
        "stop_reason": stop_reason,
        "acquisition_stop_reason": acquisition_stop_reason,
        "website_requests": context.crawler.request_count,
        "request_budget": settings.crawl.max_requests,
        "sources_inspected": len(context.sources),
        "sources_cited": len({ref.source_id for ref in evidence_refs(discovery)})
        if discovery
        else 0,
        "capabilities": {
            key: {
                "coverage": capability.coverage,
                "discovery_status": discovery_status(capability),
                "missing_reasons": capability.missing_reasons,
            }
            for key, capability in capabilities.items()
        },
        "unresolved_capabilities": [
            key
            for key, capability in capabilities.items()
            if capability.coverage != "supported"
            and discovery_status(capability) != "explicitly_not_offered"
        ]
        if discovery
        else None,
        "failures": [failure.model_dump() for failure in context.crawler.failures],
    }


class DiscoveryError(ValueError):
    """A failed discovery with a safe diagnostic artifact."""

    def __init__(self, message: str, diagnostic_path: Path) -> None:
        super().__init__(message)
        self.diagnostic_path = diagnostic_path


def failure_message(error: Exception) -> str:
    """Explain known failures without exposing untrusted provider response bodies."""
    if isinstance(error, ReviewValidationError):
        return str(error)
    if isinstance(error, ModelHTTPError):
        guidance = {
            401: "Check or renew the selected provider's API credentials.",
            403: "Check the selected provider's model access permissions.",
            404: "Check the configured model ID and provider endpoint.",
            429: "Provider rate limit reached. Wait before retrying; check account/token quotas "
            "and concurrent clients. Raising requests_per_second will not resolve a quota limit.",
            503: "Provider temporarily unavailable. Try again later.",
            504: "Provider gateway timed out. Try again later.",
        }.get(
            error.status_code, "Check provider availability and the selected model configuration."
        )
        return f"Model request failed (HTTP {error.status_code}). {guidance}"
    return "Discovery failed validation, acquisition, model execution, or review."


def minimize_sources(discovery: Discovery) -> Discovery:
    """Retain only cited public text and relevant links after full-context review.

    Hashes describe the minimized retained text, not the original HTML response.
    All supporting excerpts remain reproducible through whitespace normalization.
    """
    refs = evidence_refs(discovery)
    claim_values = {str(fact.value) for fact in facts(discovery)}
    retained = []
    for source in discovery.sources:
        excerpts = list(
            dict.fromkeys(
                normalize_whitespace(ref.excerpt) for ref in refs if ref.source_id == source.id
            )
        )
        if not excerpts and not source.authentication_observed:
            continue
        original = normalize_whitespace(source.text)
        # Extract from trusted text instead of persisting model-authored replacements.
        text = "\n".join(
            original[original.index(excerpt) : original.index(excerpt) + len(excerpt)]
            for excerpt in excerpts
        )
        if not text:
            text = "Authentication interface observed; no procedural requirements inferred."
        retained.append(
            source.model_copy(
                update={
                    "text": text,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "links": [url for url in source.links if url in claim_values],
                }
            )
        )
    return Discovery.model_validate(discovery.model_copy(update={"sources": retained}).model_dump())


async def discover_with_agents(
    url: str,
    out: Path,
    settings: Settings,
    agents: FactoryAgents,
    crawler: ExaRetriever | SafeCrawler,
    progress: Callable[[str], None] = lambda _: None,
) -> Path:
    """Run injected agents and crawler; publish only a validated reviewed discovery.

    Invalid model output, failed identity checks and incomplete semantic reviews
    leave diagnostic artifacts, never a completed discovery or server package.
    """
    discovery_id = f"discovery-{uuid4().hex}"
    out.mkdir(parents=True, exist_ok=True)
    staging = out / f".staging-{discovery_id}"
    staging.mkdir()
    context = DiscoveryContext(crawler, url, discovery_id, datetime.now(UTC), progress=progress)
    decision = None
    discovery = None
    acquisition_stop_reason = None
    started = monotonic()
    messages = []
    stage = "acquisition"

    try:
        url = validate_url(url)
        context.official_url = url
        async with asyncio.timeout(settings.run_timeout):
            progress("Retrieving homepage and contact-page evidence")
            pages = await crawler.seed(url)
            for page in pages:
                context.retain(page)
            for candidate in crawler.discovered_urls:
                context.links.setdefault(candidate, {"url": candidate, "label": "", "kind": "html"})
            progress(
                f"Evidence ready: {len(context.sources)} sources retained; "
                f"{crawler.request_count}/{settings.crawl.max_requests} acquisition requests used; "
                f"{len(crawler.failures)} retrieval failures"
            )
            progress("Discovery agent: extracting the six evidence-backed capabilities")
            stage = "discovery"
            with capture_run_messages() as messages:
                result = await agents.discoverer.run(
                    "Discover the six municipal capabilities at " + url + ".\n"
                    "Initial inspected source evidence:\n"
                    + json.dumps(
                        [source.model_dump(mode="json") for source in context.sources.values()],
                        ensure_ascii=False,
                    ),
                    deps=context,
                    usage_limits=usage_limits(settings),
                )
            discovery = context.inventory(result.output)
            acquisition_stop_reason = crawler.budget_stop_reason
            # Facts and fetch failures come through separate trust boundaries.
            discovery.failures = [
                DiscoveryFailure(**failure.model_dump()) for failure in crawler.failures
            ]
            claims = claim_records(result.output.model_dump(mode="json"))
            progress(
                f"Discovery complete: {len(context.sources)} sources inspected; "
                f"{len(claims)} claims to review; "
                f"{crawler.request_count}/{settings.crawl.max_requests} acquisition requests used"
            )
            progress(
                f"Discovery usage: {result.usage.requests} model requests, "
                f"{result.usage.tool_calls} tool calls, {result.usage.input_tokens} input tokens, "
                f"{result.usage.output_tokens} output tokens"
            )
            progress(f"Review agent: checking {len(claims)} claims against retained evidence")
            stage = "review"
            review_result = await agents.reviewer.run(
                review_prompt(result.output, context.sources),
                deps=context.sources,
                usage_limits=usage_limits(settings),
            )
            decision = review_result.output
            validate_review(decision, claims)
            progress(f"Evidence review passed: {len(claims)} claims checked")
            progress(
                f"Review usage: {review_result.usage.requests} model requests, "
                f"{review_result.usage.tool_calls} tool calls, "
                f"{review_result.usage.input_tokens} input tokens, "
                f"{review_result.usage.output_tokens} output tokens"
            )
            discovery.review = ReviewMetadata(
                status="passed", agent="municipality_evidence_review", checked_at=datetime.now(UTC)
            )
            stage = "publication"
            progress("Publishing reviewed discovery, evidence report and usage metrics")
            # Revalidate after trusted metadata additions before publishing.
            discovery = minimize_sources(Discovery.model_validate(discovery.model_dump()))
            (staging / "discovery.json").write_text(
                discovery.model_dump_json(indent=2), encoding="utf-8"
            )
            (staging / "review.json").write_text(
                decision.model_dump_json(indent=2), encoding="utf-8"
            )
            metrics = {
                "duration_seconds": round(monotonic() - started, 3),
                "website_requests": crawler.request_count,
                "agents": {
                    name: {
                        "model_requests": run.usage.requests,
                        "tool_calls": run.usage.tool_calls,
                        "input_tokens": run.usage.input_tokens,
                        "output_tokens": run.usage.output_tokens,
                    }
                    for name, run in (("discovery", result), ("review", review_result))
                },
            }
            (staging / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
            (staging / "discovery-report.json").write_text(
                json.dumps(
                    discovery_report(
                        context,
                        discovery,
                        settings,
                        stop_reason=acquisition_stop_reason or "agent_finished",
                        acquisition_stop_reason=acquisition_stop_reason,
                        published=True,
                    ),
                    indent=2,
                ),
                encoding="utf-8",
            )
            (staging / "report.md").write_text(render_report(discovery), encoding="utf-8")
            destination = out / discovery_id
            staging.rename(destination)
            logger.info("discovery_completed")
            return destination / "discovery.json"
    except Exception as error:
        # Exception strings from SDKs may include provider payloads; never persist them.
        diagnostic = out / f"diagnostic-{discovery_id}"
        diagnostic.mkdir()
        payload = {
            "status": "failed",
            "discovery_id": discovery_id,
            "error_type": type(error).__name__,
            "stage": stage,
            "message": failure_message(error),
            "provider": settings.active_model.provider,
            "http_status": error.status_code if isinstance(error, ModelHTTPError) else None,
            "duration_seconds": round(monotonic() - started, 3),
            "request_count": crawler.request_count,
            "failures": [failure.model_dump() for failure in crawler.failures],
            "validation_issues": context.validation_issues,
            "model_responses": [
                {
                    "input_tokens": message.usage.input_tokens,
                    "output_tokens": message.usage.output_tokens,
                    "finish_reason": message.finish_reason,
                }
                for message in messages
                if isinstance(message, ModelResponse)
            ],
            "schema_retry_history": [
                {key: issue[key] for key in ("loc", "type", "msg") if key in issue}
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, RetryPromptPart) and isinstance(part.content, list)
                for issue in part.content
            ],
        }
        diagnostic_path = diagnostic / "diagnostic.json"
        diagnostic_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        if stage in {"acquisition", "discovery"}:
            acquisition_stop_reason = crawler.budget_stop_reason
        stop_reason = "run_timeout" if isinstance(error, TimeoutError) else f"{stage}_failed"
        (diagnostic / "discovery-report.json").write_text(
            json.dumps(
                discovery_report(
                    context,
                    discovery,
                    settings,
                    stop_reason=stop_reason,
                    acquisition_stop_reason=acquisition_stop_reason,
                    published=False,
                ),
                indent=2,
            ),
            encoding="utf-8",
        )
        if decision is not None:
            (diagnostic / "review.json").write_text(
                decision.model_dump_json(indent=2), encoding="utf-8"
            )
        logger.error("discovery_failed", extra={"error_type": type(error).__name__})
        progress(
            f"Discovery stopped during {stage}: {type(error).__name__}; "
            f"{crawler.request_count}/{settings.crawl.max_requests} acquisition requests used"
        )
        raise DiscoveryError(payload["message"], diagnostic_path) from error
    finally:
        if staging.exists():
            shutil.rmtree(staging)


async def discover(
    url: str, out: Path, settings: Settings, progress: Callable[[str], None] = lambda _: None
) -> Path:
    """Connect configured models and shared Exa retrieval for live discovery."""
    validate_url(url)
    async with (
        ExaRetriever(
            settings.crawl, settings.exa, search_enabled=settings.web_search_enabled
        ) as crawler,
        live_agents(settings, progress) as agents,
    ):
        return await discover_with_agents(url, out, settings, agents, crawler, progress)
