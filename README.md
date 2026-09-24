# Municipality MCP server factory

A CLI-first factory that turns municipal HTML into an evidence-backed, read-only
MCP server. Discovery is currently restricted to **`www.ausserberg.ch`**. The
restriction is enforced on Exa requests and returned page URLs; it cannot be
widened by a prompt or configuration file. Exa performs remote crawling, so its
DNS, robots, and intermediate redirect handling are outside this application.

## Run

Requires Python 3.14 and `uv`:

```sh
uv sync
cp .env.example .env  # only when .env does not already exist
```

Set `OPENAI_API_KEY` and `EXA_API_KEY` in `.env`. Both Pydantic AI agents use the OpenAI API directly:
`gpt-6-sol` for discovery and `gpt-6-luna` for evidence review, with low reasoning
effort. Model IDs and reasoning effort are configurable in `config.yaml`. Discovery
uses Sol because live Luna runs failed the schema and evidence checks.

Run discovery and build in one step, including evidence review and offline
conformance checks:

```sh
uv run factory run https://www.ausserberg.ch/ --out artifacts
```

While running, the CLI prints elapsed-time milestones, source/request counts,
agent token usage as each agent finishes, and build/conformance stages. Interactive
terminals also show a spinner and running timer during waits. Redirected output
keeps plain progress lines on stderr.

Or run the steps separately to build from an existing discovery:

```sh
uv run factory discover https://www.ausserberg.ch/ --out artifacts
uv run factory build artifacts/discovery-<id>/discovery.json --out artifacts
```

`publicai` is an alias for the same CLI. Set each step's default in `config.yaml`:

```yaml
model:
  discovery_model: gpt-6-sol
  review_model: gpt-6-luna
```

Both `discover` and `run` accept independent overrides:

```sh
uv run factory discover https://www.ausserberg.ch/ --out artifacts \
  --discovery-model gpt-6-sol --review-model gpt-6-luna
```

Use `--model MODEL` to override both steps. A step-specific option takes precedence
over `--model`; otherwise, the configured value is used. Use `--config PATH` to load
another configuration file. The `build` step is deterministic and uses no model.

Both model modes use Pydantic AI's Exa search and page-retrieval tools, backed by
`EXA_API_KEY`. `web_search` returns scoped highlights; `web_fetch` calls Exa's
`get_page` tool and retains validated extracted text with evidence IDs. Initial
homepage/contact retrieval also uses Exa. Search highlights alone cannot be cited.
Set `web_search_enabled: false` to disable search; page retrieval still needs Exa.
Neither agent gets a browser, shell, filesystem, form submission, or external-portal
fetching. Credentials are never included in prompts or tool results.

Shared `exa:` settings in `config.yaml` control results (5), page text (10,000
characters), search calls (8), timeout (20 seconds), and request spacing (1 second).
`crawl.max_requests`, `crawl.run_timeout`, and `crawl.max_bytes` bound all Exa
requests, elapsed acquisition time, and API response bytes. The legacy
`website_requests` metric counts Exa requests, including searches and failed calls.
Exa's search and contents APIs may incur charges independently of the model API.

Each run writes a new directory; existing discoveries and packages are never
overwritten. Failed discovery/review/build attempts produce diagnostic artifacts
and a nonzero exit code. Local artifacts are ignored by Git.
Successful discoveries include `metrics.json` with duration, website requests and
per-agent token/tool usage. Diagnostics retain safe validation details, not API payloads.
Successful and failed attempts also write `discovery-report.json`: publication status,
stop reason, acquisition-budget status, inspected/cited source counts, acquisition
failures, and per-capability coverage and discovery status. `agent_finished` means
the agent returned an inventory, not that the website was exhaustively searched.
Coverage in a failed run is an unapproved candidate; before a valid candidate exists,
the unresolved-capability list is `null`. Request/time budgets are distinguished
from page size, nesting, and redirect limits.
For semantic review rejections, inspect `review.json` beside `diagnostic.json` for
the rejected claim paths and reasons; no discovery is published until review passes.

## Try Apertus 1.5 through Swisscom

Set `SWISSCOM_KEY` in `.env`, then run either command:

```sh
uv run factory discover https://www.ausserberg.ch/ --out artifacts --mode apertus
uv run factory run https://www.ausserberg.ch/ --out artifacts --mode apertus
```

Both agents use `swiss-ai/Apertus-v1.5-70B` through Swisscom's Chat Completions
endpoint, as specified in the [Swisscom hacker guide](https://zh.ai-weeks.ch/tools/swisscom-hacker-guide).
Set `mode: apertus` in `config.yaml` for the default; tune models, request/token
budgets, timeouts and retries under `apertus:`. The existing `--model`,
`--discovery-model` and `--review-model` options override the selected profile.
As a shorthand, `--model apertus` selects the Apertus profile with its configured
model IDs, just like `--mode apertus`; it cannot be combined with `--mode openai`.
Use `--mode openai` to switch back.

Requests are paced by `apertus.requests_per_second`, including SDK retries,
shared by both agents in a run. It accepts values up to 4, below the
provider's 5/s limit. Run one CLI process at a time with this key; separate processes
and other applications do not share this limiter. SDK retries respect `Retry-After`.
The guide lists a 60-minute bearer-token lifetime; replace expired credentials in `.env`.

Apertus defaults to an **8,192-token output allowance**. A larger allowance can
contribute to provider token-rate limits even when few requests are sent. HTTP 429
means provider rate limiting; raising requests per second does not resolve it.
`apertus.http_retries` defaults to `1` to allow recovery from transient errors;
set it to `0` to fail immediately. Provider HTTP errors and retry attempts appear
in progress output. SDK backoff can include long `Retry-After` waits.
`apertus.retries` independently controls agent validation retries. Failed runs
record provider, HTTP status and elapsed time without saving provider payloads.

Apertus and OpenAI use the same Exa tools and evidence checks. No DuckDuckGo or
OpenAI-hosted search is used. Swisscom model pacing remains separate from Exa
request pacing. OpenAI strict output-schema mode is not requested for Apertus.
Offline tests verify the integration; model quality must be assessed from real runs
and their `metrics.json`, `discovery-report.json`, and failure diagnostics.

## The two agents and their boundaries

1. **Discovery agent** searches Exa for municipal pages and retrieves their
   extracted text using `web_fetch`. Search is domain-filtered, and requested and
   returned page URLs are validated locally. `list_sources` lists retained sources
   and links from Exa's extracted text/link metadata. The agent returns a typed
   inventory with literal excerpts. Trusted code owns source IDs, hashes, retrieval
   timestamps, request limits, and publication permissions.
2. **Evidence-review agent** checks every claim and municipality identity. Its
   only tool, `read_source`, reads retained text by source ID. Missing checks,
   unsupported claims, conflicts in definitive answers or identity disagreement
   block publication. Review is best-effort semantic checking, not proof.

The deterministic builder packages a maintained template; neither agent writes
or executes code. The runtime has no model dependency or credentials and makes
no outbound requests. Both agents' instructions live in `src/publicai/prompts/`.
The small tool sets are loaded eagerly because they are needed throughout each
agent's task; there is no general tool discovery or sub-agent spawning.

The live factory uses Exa for retrieval, including the homepage and up to two
contact pages. It retains Exa's extracted text (which may come from Exa's cache),
not independently verified raw HTML. Retrieval timestamps record when this app
received the extraction. Per-page text may be truncated; absent text does not prove
that guidance is absent. Exa extraction does not provide verified form-required
markers or authentication observations, and the agent must not infer them.
PDF/calendar and external destinations remain uninspected handoffs. Direct JSON,
document parsing, JavaScript interaction, and form submission are outside this
retrieval mode. Query-bearing URLs, credentials, non-default ports, and other hosts
are rejected before Exa is called. Returned page URLs and per-URL statuses are
checked before evidence is retained. Existing direct-crawler code and its security
tests remain available, but live CLI discovery does not use that crawler.

Published email and telephone link destinations remain citable even when their
visible labels contain no address or number.

## Six stable tools

| Tool | Optional filters |
| --- | --- |
| `get_office_hours` | `office` |
| `get_garbage_collection` | `waste_type`, `zone`, `date_from`, `date_to` |
| `get_recycling_info` | `material` |
| `get_move_in_requirements` | — |
| `get_move_out_requirements` | — |
| `get_problem_reporting_info` | `category` |

All tools are read-only and return structured results and readable text. Results
include coverage (`supported`, `partial`, `handoff_only`, `unavailable`), query
outcome, citations, source retrieval timestamps and an explicit build-time
snapshot label. `supported` means minimum structured guidance, not completeness.
`discovery_status` separately identifies `observed`, `not_observed`, `blocked`,
`acquisition_limited`, or `explicitly_not_offered`; `missing_reasons` preserves
individual gaps. `unavailable` coverage means no usable information in the snapshot,
not that the municipality lacks the service. Explicit denials include citations.
These statuses are derived from existing reviewed fields, so older snapshots still load.
Generated template version 1.1.0 adds these two MCP response fields; clients that
validate an exact response-key set must update their schema.
Requirements retain their conditions; form fields do not imply an exhaustive
procedure. Conflicting claims are reported separately from definitive answers.
A package can have partial coverage when identity and review checks pass and at
least one capability has evidence-backed data or an official handoff.

Filters match exact identifiers or labels, ignoring case and surrounding spaces.
Unknown filters return available choices. Zone-dependent waste guidance requires
a zone; no address mapping is inferred. Collection date windows are inclusive,
use Europe/Zurich, default to today through 29 days later, and cannot exceed 90
calendar days. General instructions survive missing dates. Absence of dates is
only `no_matching_dates` when an explicit published interval covers the whole
query. Undated and expired snapshots are labeled accordingly. Rebuild to update.

## Generated package

A successful `build-<id>/` contains its own `pyproject.toml`, maintained `uv.lock`,
`Dockerfile`, `compose.yaml`, `README.md`, manifest with file hashes,
`discovery.json`, evidence report, `llms.txt`, `municipality.md`, runtime source,
conformance tests and independently authored fictional fixtures.

From a generated directory:

```sh
uv sync --frozen --no-dev
uv run --frozen --no-dev municipality-mcp --discovery discovery.json --transport stdio
uv run --frozen --no-dev municipality-mcp --discovery discovery.json --transport streamable-http
# Streamable HTTP endpoint: http://127.0.0.1:8000/mcp

docker compose up --build
```

### Connect from ChatGPT desktop or Codex

Keep the Streamable HTTP server above running, then in the **ChatGPT desktop app**
open **Settings → MCP servers → Add server**. Name it `municipality`, choose
**Streamable HTTP**, and enter `http://127.0.0.1:8000/mcp`. Save and select
**Restart**. In the composer, type `/mcp` to confirm the server and its tools appear.
The Codex CLI and IDE extension share this MCP configuration with the desktop app.

Alternatively, configure it from a terminal on the same computer:

```sh
codex mcp add municipality --url http://127.0.0.1:8000/mcp
codex mcp list
```

These steps are for the local desktop/CLI/IDE clients. ChatGPT **web** cannot
reach your computer's loopback address through this configuration; a private
server needs a [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
or another reachable HTTPS endpoint and a developer-mode connection.
See OpenAI's [MCP setup guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
for the current client steps.

The HTTP launch defaults to loopback. Container port mapping also uses loopback;
production authentication and public hosting are deferred. The generated
container runs as a dedicated non-root user with a read-only filesystem,
capability drops, resource limits and a private runtime network. Docker itself
should run rootlessly. Consult the generated README for runtime commands and
the exact isolation limits of conformance checks.
Compose derives image names from each release's project directory, avoiding a
shared mutable image tag. Container health checks probe the running loopback MCP
server; `--check` remains an offline snapshot validation command.

The repository's factory container can also run the CLI. Model credentials belong
only to the factory process; generated runtime containers do not inherit them.

## Validate

```sh
uv run ruff format .
uv run ruff check .
uv run pytest
# Build a clearly fictional fixture without contacting a model or website:
uv run factory build src/publicai/fixtures/representative.json --out artifacts
```

Tests use deterministic Pydantic AI `FunctionModel` agents, fake HTTP boundaries,
and independently authored source excerpts. They exercise the normal workflow,
identity/evidence checks, filters, zones, date boundaries, malicious source text,
network limits and immutable publication. Every package runs offline conformance
in a subprocess with cleared credentials, denied sockets/process spawning and
resource limits. This local check is defense in depth, not an OS sandbox.
Generated Docker configuration supports a separate container runtime; the
builder's automatic checks do not launch Docker.

### Reviewer gold-set evaluation

Ten independently labeled fictional cases exercise the production reviewer prompt,
retained-source tool, and approval gate: correct/incorrect hours, unrelated quotations,
conditions, handoffs, conflicting evidence, and source-instruction injection. Ordinary
tests validate the fixtures and evaluation scoring offline; they do not measure model
accuracy. The paid evaluation stays skipped even when API credentials are present.

To explicitly run all ten cases against the selected mode’s review model in `config.yaml`:

```sh
uv run pytest tests/publicai/test_review_evals.py -k live_review_gold_set --run-review-evals -s
```

This contacts the model provider, uses per-case configured usage/time limits, and
prints a JSON report plus its pytest temporary-file path. The report includes false
approval/rejection counts and rates and separate execution errors. No crawler or
telemetry exporter is started. A passing small gold set is calibration evidence,
not proof of real-world factual accuracy; live evaluation has not yet been recorded.

The fixture municipality is fictional. Its URLs use the authorized host only to
exercise network policy; its contents are **not facts about Ausserberg**.

Telemetry is off by default. `config.yaml` can opt into Logfire instrumentation
with model/tool content and binary payloads disabled. `send_to_logfire: true`
requires `LOGFIRE_TOKEN`; standard `OTEL_EXPORTER_OTLP_*` settings can target an
Aspire collector. No telemetry service is contacted unless enabled. Application
logs contain event names and exception types on stderr, never SDK payloads.

The original plan also requests a second municipality. That trial is deferred
because the crawler currently permits only `www.ausserberg.ch`. The shared
contracts and fictional fixtures exercise service variants without contacting
another site. See [PLAN.md](PLAN.md) for outstanding validation.

The existing standalone OpenAI connectivity script remains available:
`uv run python scripts/test_openai.py`.
