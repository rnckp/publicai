"""Bounded Exa search and extracted page evidence through Pydantic AI's Exa toolset."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Self
from urllib.parse import unquote, urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ToolReturn
from pydantic_ai_harness.exa import ExaSearch

from publicai.config import ExaSettings
from publicai.crawler import (
    ALLOWED_HOST,
    CrawlError,
    CrawlFailure,
    CrawlSettings,
    FetchedPage,
    _link,
    validate_url,
)


class ExaConfigurationError(ValueError):
    """A safe, actionable retrieval credential error."""


class _Extras(BaseModel):
    links: list[str] = Field(default_factory=list, max_length=1000)


class _Result(BaseModel):
    url: str = Field(max_length=4096)
    title: str | None = Field(default=None, max_length=10000)
    text: str | None = None
    highlights: list[str] = Field(default_factory=list, max_length=100)
    extras: _Extras = Field(default_factory=_Extras)
    # Pydantic AI's formatter reads these optional metadata attributes.
    published_date: str | None = Field(default=None, alias="publishedDate")
    author: str | None = None


class _Status(BaseModel):
    id: str
    status: str


class _Response(BaseModel):
    results: list[_Result] = Field(default_factory=list, max_length=100)
    statuses: list[_Status] = Field(default_factory=list, max_length=100)
    output: None = None


def _page_url(url: str, allowed_host: str) -> str:
    """Accept only municipal HTML-page candidates, never document or JSON retrieval."""
    url = validate_url(url, allowed_host)
    if unquote(urlsplit(url).path).lower().endswith((".pdf", ".ics", ".xml", ".json", ".zip")):
        raise CrawlError("blocked", "Exa retrieval is restricted to municipal HTML pages")
    return url


async def _invoke(
    tool: Callable[[str], Awaitable[ToolReturn[str]]],
    value: str,
) -> ToolReturn[str]:
    """Translate the toolset's ModelRetry wrapper back to safe acquisition failures."""
    try:
        return await tool(value)
    except ModelRetry as error:
        if isinstance(error.__cause__, CrawlError):
            raise CrawlError(error.__cause__.reason, str(error.__cause__)) from error
        if isinstance(error.__cause__, ExaConfigurationError):
            raise ExaConfigurationError(str(error.__cause__)) from error
        raise CrawlError("inaccessible", "Exa returned no usable page content") from error


class ExaRetriever:
    """Exa client adapter and acquisition state, shared by search, fetch and seeding.

    Implements the client methods used by Pydantic AI's ExaSearch. The HTTP boundary
    validates provider results before its tools format them or evidence is retained.
    Exa owns crawling; this adapter cannot enforce remote DNS, robots or redirect policy.
    """

    def __init__(
        self,
        settings: CrawlSettings,
        exa: ExaSettings,
        *,
        search_enabled: bool = True,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        allowed_host: str = ALLOWED_HOST,
    ) -> None:
        key = api_key or os.getenv("EXA_API_KEY")
        if not key or not key.strip() or key.startswith("your_"):
            raise ExaConfigurationError("EXA_API_KEY is missing; configure it in .env.")
        self.settings = settings
        self.allowed_host = allowed_host
        self.exa = exa
        self.search_enabled = search_enabled
        self.request_count = 0
        self.search_requests = 0
        self.failures: list[CrawlFailure] = []
        self.discovered_urls: list[str] = []
        self._pages: dict[str, FetchedPage] = {}
        self._contents: dict[str, _Result] = {}
        self._started = time.monotonic()
        self._next_request = 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            base_url="https://api.exa.ai",
            headers={"x-api-key": key},
            timeout=exa.timeout,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        self._tools = ExaSearch(
            client=self,
            include_domains=[allowed_host],
            num_results=exa.max_results,
            max_text_chars=exa.max_text_chars,
            guidance="",
            include_deep_search=False,
        ).get_toolset()

    async def __aenter__(self) -> Self:
        await self._http.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._http.aclose()

    @property
    def budget_stop_reason(self) -> str | None:
        """Distinguish exhausted global acquisition budgets from individual failures."""
        if time.monotonic() - self._started >= self.settings.run_timeout:
            return "time_budget_exhausted"
        if self.request_count >= self.settings.max_requests:
            return "request_budget_exhausted"
        return None

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> _Response:
        async with self._lock:
            if self.budget_stop_reason:
                raise CrawlError("crawl_limit", "Exa acquisition budget exhausted")
            remaining = self.settings.run_timeout - (time.monotonic() - self._started)
            try:
                async with asyncio.timeout(min(self.exa.timeout, remaining)):
                    delay = self._next_request - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    self.request_count += 1
                    self._next_request = time.monotonic() + self.exa.request_interval
                    async with self._http.stream("POST", endpoint, json=payload) as response:
                        if response.status_code in {401, 403}:
                            raise ExaConfigurationError(
                                "Exa authentication failed; check EXA_API_KEY."
                            )
                        response.raise_for_status()
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(data) + len(chunk) > self.settings.max_bytes:
                                raise CrawlError(
                                    "crawl_limit", "Exa response exceeds the byte limit"
                                )
                            data.extend(chunk)
                        return _Response.model_validate_json(data)
            except (httpx.HTTPError, ValidationError, TimeoutError) as error:
                raise CrawlError(
                    "inaccessible", "Exa request failed or returned invalid data"
                ) from error

    async def search(self, query: str, **kwargs: Any) -> _Response:
        """Serve Pydantic AI search with a hard domain filter and bounded highlights."""
        if not self.search_enabled or self.search_requests >= self.exa.search_request_limit:
            raise CrawlError("search_limit", "Exa search is disabled or its budget is exhausted")
        if not query.strip() or len(query) > 200:
            raise CrawlError("blocked", "Search query must contain 1–200 characters")
        self.search_requests += 1
        response = await self._post(
            "/search",
            {
                "query": query,
                "includeDomains": [self.allowed_host],
                "numResults": self.exa.max_results,
                "contents": {"highlights": True},
            },
        )
        matches = []
        for result in response.results:
            try:
                result.url = _page_url(result.url, self.allowed_host)
            except CrawlError:
                continue
            result.title = (result.title or "")[:1000]
            result.highlights = [excerpt[:1000] for excerpt in result.highlights[:3]]
            matches.append(result)
        response.results = matches[: self.exa.max_results]
        return response

    async def get_contents(self, urls: str, *, text: dict[str, Any]) -> _Response:
        """Validate requested and returned page URLs, including per-URL API status."""
        url = _page_url(urls, self.allowed_host)
        response = await self._post(
            "/contents",
            {
                "urls": [url],
                "text": {"maxCharacters": self.exa.max_text_chars},
                "extras": {"links": 100},
            },
        )
        if response.statuses and not any(
            status.id == url and status.status == "success" for status in response.statuses
        ):
            raise CrawlError("inaccessible", "Exa could not retrieve the requested page")
        if len(response.results) != 1:
            raise CrawlError("inaccessible", "Exa returned no unique page result")
        result = response.results[0]
        if (
            _page_url(result.url, self.allowed_host) != url
            or not result.text
            or not result.text.strip()
        ):
            raise CrawlError("inaccessible", "Exa returned an empty or mismatched page")
        result.text = result.text[: self.exa.max_text_chars]
        self._contents[url] = result
        return response

    async def web_search(self, query: str) -> ToolReturn[str]:
        """Use Pydantic AI's Exa search tool; search highlights never become evidence."""
        try:
            return await _invoke(self._tools.web_search, query)
        except CrawlError as error:
            self.failures.append(
                CrawlFailure(
                    url=f"https://{self.allowed_host}/",
                    reason=error.reason,
                    detail="Exa search failed; query and provider payload omitted",
                )
            )
            raise

    async def fetch(self, url: str) -> FetchedPage:
        """Use Pydantic AI's get_page tool and retain its validated Exa extraction."""
        try:
            url = _page_url(url, self.allowed_host)
            if url in self._pages:
                return self._pages[url]
            await _invoke(self._tools.get_page, url)
            result = self._contents.pop(url)
            text = result.text or ""
            candidates = re.findall(r"\[([^\]\n]*)\]\(([^\s)]+)\)", text)
            candidates.extend(("", link) for link in result.extras.links)
            links = {}
            for label, destination in candidates:
                link = _link(destination, label, url)
                if link is not None:
                    links.setdefault(link.url, link)
            page = FetchedPage(
                url=url,
                title=(result.title or "")[:1000],
                text=text,
                sha256=hashlib.sha256(text.encode()).hexdigest(),
                retrieved_at=datetime.now(UTC),
                links=list(links.values()),
            )
            self._pages[url] = page
            return page
        except CrawlError as error:
            try:
                safe_url = validate_url(url, self.allowed_host)
            except CrawlError:
                safe_url = "<blocked-url>"
            self.failures.append(CrawlFailure(url=safe_url, reason=error.reason, detail=str(error)))
            raise

    async def seed(self, url: str) -> list[FetchedPage]:
        """Retrieve the homepage and up to two contact pages, entirely through Exa."""
        homepage = await self.fetch(url)
        candidates = [link.url for link in homepage.links if link.kind == "html"]
        contacts = [
            candidate
            for candidate in candidates
            if re.search(r"kontakt|contact|impressum|kanzlei", candidate, re.I)
        ]
        if not contacts and self.search_enabled:
            try:
                results = await self.web_search(f"{self.allowed_host} Gemeinde Kontakt Impressum")
                contacts = [source["url"] for source in results.metadata["sources"]]
            except CrawlError:
                contacts = []
        self.discovered_urls = list(dict.fromkeys(candidates + contacts))
        for contact in list(dict.fromkeys(contacts))[:2]:
            try:
                await self.fetch(contact)
            except CrawlError:
                continue
        return list(self._pages.values())
