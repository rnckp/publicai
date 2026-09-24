"""Two bounded Pydantic AI agents: website discovery and offline evidence review."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict
from urllib.parse import unquote, urlsplit

from ddgs.ddgs import DDGS
from ddgs.exceptions import DDGSException
from httpx import AsyncClient, Request
from openai import AsyncOpenAI
from pydantic import Field, ValidationError
from pydantic_ai import Agent, ModelRetry, RunContext, ToolOutput
from pydantic_ai.capabilities import WebFetch, WebSearch
from pydantic_ai.common_tools.duckduckgo import DuckDuckGoSearchTool
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from publicai.config import AgentMode, Settings
from publicai.contracts import (
    CONTRACT_VERSION,
    Capability,
    Discovery,
    EvidenceRef,
    Fact,
    LinkedDocument,
    MunicipalityIdentity,
    ObservedFormField,
    SourceSnapshot,
    StrictModel,
)
from publicai.crawler import ALLOWED_HOST, CrawlError, FetchedPage, SafeCrawler, validate_url

PROMPTS = Path(__file__).with_name("prompts")
_SERVICE_TERMS = (
    "abfall",
    "kehricht",
    "recycling",
    "entsorgung",
    "sammelstelle",
    "zuzug",
    "wegzug",
    "anmeldung",
    "abmeldung",
    "umzug",
    "öffnungszeit",
    "oeffnungszeit",
    "kanzlei",
    "kontakt",
    "impressum",
    "schaden",
    "mängel",
    "maengel",
    "meldung",
)


def rank_source_links(links: list[dict[str, str]], query: str = "") -> list[dict[str, str]]:
    """Rank specific labels and URL leaves above shared paths; retain every matching lead.

    This only orders discovery candidates. It never classifies services or changes
    acquisition permissions, and it permits one page to support multiple services.
    """
    terms = query.casefold().split()

    def score(link: dict[str, str]) -> tuple[int, int]:
        label = link["label"].casefold()
        leaf = unquote(urlsplit(link["url"]).path).rstrip("/").rsplit("/", 1)[-1].casefold()
        specific = sum(2 * (term in label) + (term in leaf) for term in terms)
        service = sum(2 * (term in label) + (term in leaf) for term in _SERVICE_TERMS)
        return specific, service

    candidates = [
        link
        for link in links
        if not terms
        or any(term in (unquote(link["url"]) + " " + link["label"]).casefold() for term in terms)
    ]
    return sorted(candidates, key=score, reverse=True)


def review_prompt(inventory: Inventory, sources: dict[str, SourceSnapshot]) -> str:
    """Build the same review input for production and independently labeled evaluations."""
    payload = inventory.model_dump(mode="json")
    return "Review this inventory and every listed claim path.\n" + json.dumps(
        {
            "inventory": payload,
            "claims": claim_records(payload),
            "sources": [
                {"id": source.id, "url": source.url, "kind": source.kind}
                for source in sources.values()
            ],
        },
        ensure_ascii=False,
    )


class ModelConfigurationError(ValueError):
    """An actionable local configuration error with no sensitive payload."""


class ReviewValidationError(ValueError):
    """A trusted review rejection message without model-authored text or payloads."""


class CapabilityInventory(TypedDict):
    """Fixed keys let the model provider enforce the complete catalogue during generation."""

    office_hours: Capability
    garbage_collection: Capability
    recycling: Capability
    move_in: Capability
    move_out: Capability
    problem_reporting: Capability


class Inventory(StrictModel):
    """Only the municipal claims are model-generated; acquisition metadata is trusted code."""

    identity: MunicipalityIdentity
    capabilities: CapabilityInventory


class ClaimCheck(StrictModel):
    """The review agent's explicit decision on one factual leaf."""

    path: str
    status: Literal["supported", "unsupported", "conflicting"]
    reason: str = ""


class ReviewDecision(StrictModel):
    """Semantic decisions cannot override deterministic validation or missing checks."""

    identity_consistent: bool
    checks: list[ClaimCheck]
    issues: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class DiscoveryContext:
    """State available to the discovery tools, never exposing provider credentials."""

    crawler: SafeCrawler
    official_url: str
    discovery_id: str
    created_at: datetime
    sources: dict[str, SourceSnapshot] = field(default_factory=dict)
    links: dict[str, dict[str, str]] = field(default_factory=dict)
    validation_issues: list[dict[str, Any]] = field(default_factory=list)
    search_requests: int = 0
    search_next_request: float = 0
    search_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    progress: Callable[[str], None] = lambda _: None

    def retain(self, page: FetchedPage) -> SourceSnapshot:
        """Attach trusted fetch metadata and reuse IDs for cached pages."""
        for source in self.sources.values():
            if source.url == page.url:
                return source
        path = urlsplit(page.url).path.rstrip("/")
        kind = "service"
        if not path:
            kind = "homepage"
        elif any(
            word in path.casefold() for word in ("kontakt", "impressum", "kanzlei", "contact")
        ):
            kind = "contact"
        observed_links = [
            f"{link.label or 'Unlabelled link'}: {link.url}"
            for link in page.links
            if urlsplit(link.url).scheme in {"http", "https", "mailto", "tel"}
        ]
        retained_text = page.text
        if observed_links:
            retained_text += "\nObserved link labels and destinations:\n" + "\n".join(
                observed_links
            )
        if page.authentication_required:
            retained_text += (
                "\nAuthentication interface observed; no procedural requirements inferred."
            )
        source_id = f"source-{len(self.sources) + 1}"
        observed_fields = []
        for item in page.form_fields:
            excerpt = f"Observed form label: {item.label}; required marker: {item.required}"
            retained_text += "\n" + excerpt
            observed_fields.append(
                ObservedFormField(
                    label=Fact(
                        value=item.label,
                        evidence=[EvidenceRef(source_id=source_id, excerpt=excerpt)],
                    ),
                    required_marker=item.required,
                )
            )
        documents = []
        for link in page.links:
            if link.kind not in {"pdf", "calendar"}:
                continue
            evidence = [
                EvidenceRef(
                    source_id=source_id, excerpt=f"{link.label or 'Unlabelled link'}: {link.url}"
                )
            ]
            documents.append(
                LinkedDocument(
                    title=Fact(value=link.label, evidence=evidence) if link.label else None,
                    url=Fact(value=link.url, evidence=evidence),
                    kind=link.kind,
                )
            )
        source = SourceSnapshot(
            id=source_id,
            url=page.url,
            title=page.title[:1000],
            text=retained_text,
            retrieved_at=page.retrieved_at,
            sha256=hashlib.sha256(retained_text.encode()).hexdigest(),
            kind=kind,
            documents=documents,
            form_fields=observed_fields,
            authentication_observed=page.authentication_required,
            links=[
                link.url for link in page.links if urlsplit(link.url).scheme in {"http", "https"}
            ],
        )
        self.sources[source.id] = source
        for link in page.links:
            self.links[link.url] = {"url": link.url, "label": link.label, "kind": link.kind}
        return source

    def inventory(self, candidate: Inventory) -> Discovery:
        """Validate the candidate against only sources actually retained by tools."""
        return Discovery(
            contract_version=CONTRACT_VERSION,
            discovery_id=self.discovery_id,
            created_at=self.created_at,
            official_url=self.official_url,
            identity=candidate.identity,
            capabilities=candidate.capabilities,
            sources=list(self.sources.values()),
        )


@dataclass(frozen=True, slots=True)
class FactoryAgents:
    """Separate agents with distinct dependencies and permissions."""

    discoverer: Agent[DiscoveryContext, Inventory]
    reviewer: Agent[dict[str, SourceSnapshot], ReviewDecision]


def claim_records(value: Any, prefix: str = "") -> dict[str, dict[str, Any]]:
    """Enumerate fact paths independently so the reviewer cannot omit difficult claims."""
    if isinstance(value, dict):
        if "value" in value and "evidence" in value:
            return {prefix: value}
        records = {}
        for key, child in value.items():
            path = f"{prefix}.{key}".strip(".")
            if key in {
                "coverage",
                "zone_required",
                "limitations",
                "missing_reasons",
                "delivery_shapes",
            }:
                records[path] = {"value": child, "evidence": [], "kind": "coverage_or_gap_metadata"}
            elif key == "not_offered_evidence" and child:
                records[path] = {"value": "Service explicitly not offered", "evidence": child}
            else:
                records.update(claim_records(child, path))
        return records
    if isinstance(value, list):
        return {
            path: claim
            for index, child in enumerate(value)
            for path, claim in claim_records(child, f"{prefix}.{index}").items()
        }
    return {}


def validate_review(decision: ReviewDecision, claims: dict[str, dict[str, Any]]) -> None:
    """Require complete affirmative review; do not silently repair a failed review."""
    paths = [check.path for check in decision.checks]
    if len(paths) != len(set(paths)) or set(paths) != set(claims):
        raise ReviewValidationError("Semantic review must check every claim exactly once.")
    if not decision.identity_consistent:
        raise ReviewValidationError("Semantic review found inconsistent municipality identity.")
    if any(
        check.status == "unsupported"
        or (check.status == "conflicting" and ".conflicts." not in check.path)
        for check in decision.checks
    ):
        raise ReviewValidationError(
            "Semantic review found unsupported or conflicting claims. "
            "See review.json beside the diagnostic for claim paths and reasons."
        )
    if decision.issues:
        raise ReviewValidationError(
            "Semantic review reported blocking issues. "
            "See review.json beside the diagnostic for details."
        )


class DuckDuckGoOnlyClient(DDGS):
    """Use only DuckDuckGo; DDGS otherwise defaults to multiple search providers."""

    def text(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Pin the backend used by Pydantic AI's built-in search callable."""
        return super().text(query, backend="duckduckgo", **kwargs)


def create_agents(
    settings: Settings, discovery_model: Model | str, review_model: Model | str
) -> FactoryAgents:
    """Create the two agents with small, eager, task-relevant tool sets."""
    model_settings = {
        "max_tokens": settings.active_model.max_tokens,
        "timeout": settings.active_model.timeout,
    }
    if settings.mode == AgentMode.OPENAI:
        model_settings["openai_reasoning_effort"] = settings.active_model.reasoning_effort
    if settings.active_model.temperature is not None:
        model_settings["temperature"] = settings.active_model.temperature

    async def web_fetch(ctx: RunContext[DiscoveryContext], url: str) -> dict[str, Any]:
        """Inspect public HTML or explicitly linked JSON on www.ausserberg.ch."""
        ctx.deps.progress("Discovery tool: web_fetch")
        try:
            page = await ctx.deps.crawler.fetch(url)
            source = ctx.deps.retain(page)
        except CrawlError as error:
            return {
                "error": error.reason,
                "guidance": "Record this discovery gap; do not bypass it.",
            }
        return {
            "source": source.model_dump(mode="json"),
            "links": [link.model_dump() for link in page.links],
            "form_fields": [item.model_dump() for item in page.form_fields],
            "authentication_required": page.authentication_required,
            "untrusted_evidence": True,
        }

    async def web_search(
        ctx: RunContext[DiscoveryContext],
        query: Annotated[str, Field(min_length=1, max_length=200)],
    ) -> dict[str, Any]:
        """Search DuckDuckGo for municipal page leads using plain service keywords.

        Results are untrusted snippets, not retained evidence. Fetch promising pages
        with web_fetch before citing them. Search failures do not establish absence.
        """
        words = re.findall(r"[^\W_]+", query)
        if not words:
            raise ModelRetry("Supply at least one municipal service keyword.")
        scoped_query = f"site:{ALLOWED_HOST} " + " ".join(f'"{word}"' for word in words)
        result: dict[str, Any] = {
            "scope": "duckduckgo_municipal_index",
            "results": [],
            "untrusted_evidence": True,
        }
        async with ctx.deps.search_lock:
            if ctx.deps.search_requests >= settings.apertus.search_request_limit:
                return {**result, "error": "search_budget_exhausted"}
            delay = ctx.deps.search_next_request - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            ctx.deps.search_requests += 1
            ctx.deps.search_next_request = time.monotonic() + settings.apertus.search_interval
            ctx.deps.progress("Discovery tool: web_search (DuckDuckGo)")
            try:
                with DuckDuckGoOnlyClient(timeout=settings.apertus.search_timeout) as client:
                    search = DuckDuckGoSearchTool(
                        client=client, max_results=settings.apertus.search_max_results
                    )
                    matches = await search(scoped_query)
            except DDGSException, ValidationError:
                return {
                    **result,
                    "error": "search_unavailable",
                    "guidance": "Continue with known navigation; report the search gap.",
                }
        for match in matches[: settings.apertus.search_max_results]:
            try:
                url = validate_url(match["href"])
            except CrawlError:
                continue
            result["results"].append(
                {
                    "title": match["title"][:500],
                    "href": url,
                    "body": match["body"][:1000],
                }
            )
        return result

    discoverer = Agent(
        discovery_model,
        name="municipality_discovery",
        deps_type=DiscoveryContext,
        output_type=ToolOutput(Inventory, strict=settings.mode == AgentMode.OPENAI),
        instructions=(PROMPTS / "discovery.md").read_text(encoding="utf-8")
        + "\n"
        + (PROMPTS / "catalogue.md").read_text(encoding="utf-8"),
        model_settings=model_settings,
        retries=settings.active_model.retries,
        capabilities=[
            WebFetch(native=False, local=web_fetch),
            *(
                [
                    WebSearch(native=False, local=web_search)
                    if settings.mode == AgentMode.APERTUS
                    else WebSearch(allowed_domains=[ALLOWED_HOST], external_web_access=False)
                ]
                if settings.web_search_enabled
                else []
            ),
        ],
    )

    @discoverer.tool
    def list_sources(ctx: RunContext[DiscoveryContext], query: str = "") -> dict[str, Any]:
        """Find known navigation links or inspected sources by literal German keywords."""
        ctx.deps.progress("Discovery tool: list_sources")
        links = rank_source_links(list(ctx.deps.links.values()), query)
        return {
            "sources": [
                {
                    "source_id": source.id,
                    "url": source.url,
                    "title": source.title,
                    "kind": source.kind,
                }
                for source in ctx.deps.sources.values()
            ],
            "links": links[:150],
            "matching_links": len(links),
            "links_truncated": len(links) > 150,
            "requests_used": ctx.deps.crawler.request_count,
            "request_budget": ctx.deps.crawler.settings.max_requests,
            "budget_stop_reason": ctx.deps.crawler.budget_stop_reason,
        }

    @discoverer.output_validator
    def validate_candidate(ctx: RunContext[DiscoveryContext], output: Inventory) -> Inventory:
        try:
            ctx.deps.inventory(output)
        except ValidationError as error:
            details = [
                {"path": e["loc"], "message": e["msg"]} for e in error.errors(include_input=False)
            ]
            ctx.deps.validation_issues = details
            ctx.deps.progress(
                "Candidate failed deterministic evidence validation; requesting correction"
            )
            raise ModelRetry(
                f"Correct these inventory/evidence errors: {json.dumps(details)}. "
                "Available source IDs and kinds: "
                + json.dumps({sid: source.kind for sid, source in ctx.deps.sources.items()})
            ) from error
        ctx.deps.validation_issues.clear()
        return output

    reviewer = Agent(
        review_model,
        name="municipality_evidence_review",
        deps_type=dict[str, SourceSnapshot],
        output_type=ToolOutput(ReviewDecision, strict=settings.mode == AgentMode.OPENAI),
        instructions=(PROMPTS / "review.md").read_text(encoding="utf-8")
        + "\n"
        + (PROMPTS / "catalogue.md").read_text(encoding="utf-8"),
        model_settings=model_settings,
        retries=settings.active_model.retries,
    )

    @reviewer.tool
    def read_source(ctx: RunContext[dict[str, SourceSnapshot]], source_id: str) -> dict[str, Any]:
        """Read an already retained source by ID; no filesystem or network access."""
        source = ctx.deps.get(source_id)
        if source is None:
            raise ModelRetry("Unknown source ID. Use only source IDs in the supplied claims.")
        return source.model_dump(mode="json")

    return FactoryAgents(discoverer=discoverer, reviewer=reviewer)


def usage_limits(settings: Settings) -> UsageLimits:
    """Bound each agent's model calls, tool calls and total tokens."""
    return UsageLimits(
        request_limit=settings.active_model.request_limit,
        tool_calls_limit=settings.active_model.tool_calls_limit,
        total_tokens_limit=settings.active_model.total_tokens_limit,
    )


@asynccontextmanager
async def live_agents(settings: Settings) -> AsyncIterator[FactoryAgents]:
    """Create and close SDK clients, pacing every Swisscom attempt including retries."""
    apertus = settings.mode == AgentMode.APERTUS
    key_name = "SWISSCOM_KEY" if apertus else "OPENAI_API_KEY"
    key = os.getenv(key_name)
    if not key or not key.strip() or key.startswith("your_"):
        raise ModelConfigurationError(
            f"{key_name} is missing; configure it in .env or the environment."
        )
    lock = asyncio.Lock()
    next_request = 0.0

    async def pace_request(request: Request) -> None:
        """Space HTTP attempts so concurrent calls and SDK retries share one budget."""
        nonlocal next_request
        async with lock:
            delay = next_request - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            next_request = time.monotonic() + 1 / settings.apertus.requests_per_second

    async with AsyncOpenAI(
        api_key=key,
        base_url="https://api.swisscom.com/products/swiss-ai-weeks/apertus-1.5-70b/v1"
        if apertus
        else "https://api.openai.com/v1",
        timeout=settings.active_model.timeout,
        max_retries=settings.active_model.retries,
        http_client=AsyncClient(
            trust_env=False,
            follow_redirects=False,
            event_hooks={"request": [pace_request]} if apertus else None,
        ),
    ) as client:
        adapter = OpenAIProvider(openai_client=client)
        if apertus:
            profile = OpenAIModelProfile(openai_supports_strict_tool_definition=False)
            first = OpenAIChatModel(
                settings.active_model.discovery_model, provider=adapter, profile=profile
            )
            second = OpenAIChatModel(
                settings.active_model.review_model, provider=adapter, profile=profile
            )
        else:
            first = OpenAIResponsesModel(settings.active_model.discovery_model, provider=adapter)
            second = OpenAIResponsesModel(settings.active_model.review_model, provider=adapter)
        yield create_agents(settings, first, second)
