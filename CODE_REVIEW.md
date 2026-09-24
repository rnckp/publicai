# Codebase review

Reviewed on 2026-09-24 at commit `f61bcd5a2371ed494c651f5ad960309909e23082`.

## Scope and approach

Reviewed the factory CLI, configuration, discovery/review agents and prompts, Exa
retrieval and legacy direct crawler, snapshot contracts, MCP runtime, deterministic
builder, generated templates, conformance checks, tests, Docker/Compose, CI,
dependency declarations, and operational documentation. Findings were traced through
call sites and relevant validation before inclusion. Reproductions used fictional
fixtures, in-memory telemetry, or mock HTTP boundaries; no external services were
contacted and no dependencies were installed.

`AGENTS.md` governed the review. The formatting command was run in check mode to
respect the instruction not to modify code. No source, test, configuration, or
existing documentation files were changed. This report is the only repository
change. Existing documented limitations are distinguished from defects below.

**Findings: 10 total — 0 critical, 0 high, 6 medium, 4 low.** Severity reflects
demonstrated impact and reachability, including defaults and affected entry points.

## Findings

### R01 — Provider error bodies escape metadata-only telemetry

**Category:** Security / data protection. **Severity:** Medium.
**Status:** Confirmed locally; telemetry must be enabled.

**Location:** `src/publicai/pipeline.py:306-325`; related
`src/publicai/observability.py:104-115` and `pipeline.py:378-412`.

The manually created `logfire.span("Evidence validation", ...)` surrounds the
reviewer call. An exception leaving that context is automatically recorded with its
message and traceback. The Pydantic AI `include_content=False` configuration protects
its own instrumentation, but does not protect this enclosing application span.

**Evidence and impact:** An offline reproduction ran the actual discovery pipeline
with a `FunctionModel` reviewer raising `ModelHTTPError(500, ..., body={"message":
"FICTIONAL_PRIVATE_BODY_4242"})`. Logfire used an in-memory `TestExporter` with the
same three content-capture exclusions as production. The marker was absent from
`diagnostic.json`, but present in the `exception` event of the `Evidence validation`
span. With an external exporter configured, provider bodies can therefore leave the
process despite the promised metadata-only policy. No actual credential disclosure
was observed, and telemetry is disabled by default.

**Smallest robust fix:** Prevent untrusted exceptions from escaping through this
auto-recording span: capture the exception inside the span, record only approved
type/status fields, and re-raise after the span has closed. Add an in-memory exporter
regression checking both exception messages and stack traces for a synthetic payload.
Preserve the existing sanitized diagnostic behavior.

### R02 — Conflicted collection dates can become “no matching dates”

**Category:** Correctness. **Severity:** Medium.
**Status:** Confirmed with a valid, packageable snapshot.

**Location:** `src/publicai/runtime.py:229-236`; related conflict handling at
`runtime.py:141-142` and `src/publicai/contracts.py:274-288,441-456`.

The contracts correctly require disputed dates to be omitted from definitive
entries. The runtime then decides `no_matching_dates` using only the published
interval and the remaining definitive dates, without considering those conflicts.
A generic conflict limitation does not correct the machine-readable false negative.

**Evidence and impact:** Starting with the fictional representative fixture, add
evidence-backed alternative Nord collection dates of 2026-10-29 and 2026-10-30 to
`garbage_collection.conflicts`, remove the disputed date from Nord's definitive
dates, and set coverage to `partial`. Retain the published October validity bounds
and recompute the modified source hash. `Discovery.model_validate` succeeds and
`packaging_issues` returns `[]`. Querying Nord for 2026-10-30 returns
`outcome="no_matching_dates"`, `interval_covered=True`, and no dates, although the
snapshot explicitly records uncertainty about a collection on that day.

**Smallest robust fix:** Return `information_unavailable` when unresolved collection
conflicts could affect the requested dates, while retaining general guidance and
undisputed dates. Because conflict scope is currently free text, conservatively
treating any garbage-collection conflict as incomplete date knowledge is safer than
guessing its scope. Add this case alongside the existing empty-interval tests;
preserve `no_matching_dates` for genuinely complete, conflict-free intervals.

### R03 — Review can pass without examining cited source context

**Category:** Reliability / evidence review. **Severity:** Medium.
**Status:** Confirmed context-coverage gap; live false-approval frequency is unknown.

**Location:** `src/publicai/agents.py:93-106,292-312,442-448`;
`src/publicai/prompts/review.md:7-19`; publication at `src/publicai/pipeline.py:324-338`.

The initial reviewer input contains the discovery agent's selected excerpts and
source identifiers, URLs, and kinds, but no full source text. Reading context through
`read_source` is optional. The acceptance gate checks claim decisions, not whether
the reviewer obtained the source context needed to assess contradictions, omitted
conditions, or the municipality named in a page's main content. Publication then
removes uncited text.

**Evidence and impact:** The existing `conflicting_hours` gold case
(`tests/publicai/fixtures/review_gold.json:59-64`) contains “Montag geschlossen”
outside its affirmative excerpt. A local comparison confirmed that its initial
`review_prompt` is byte-for-byte identical to the prompt for a source containing
only the affirmative excerpt. The contradiction is available only through the
optional tool. The passing pipeline fixture reviewer also returns approval without
reading any sources (`tests/publicai/test_pipeline.py:102-122`). Thus publication
does not ensure the reviewer receives context needed to detect contradictions or
missing conditions outside selected excerpts. That context can become unavailable
after minimization.

**Smallest robust fix:** Require successful source-context inspection for every
distinct cited source, including homepage/contact identity evidence, before accepting
review. Either supply that bounded text in the prompt or track successful
`read_source` calls and reject incomplete inspection. Add a deterministic zero-read
approval regression. Account for context/tool budgets rather than silently skipping
sources. Reading context cannot guarantee correct reasoning; this finding does not
claim that a live reviewer was observed approving the gold-case contradiction.

### R04 — Factory Compose configuration omits required credentials

**Category:** Deployment correctness. **Severity:** Medium.
**Status:** Confirmed configuration/call-path defect; Docker was not launched.

**Location:** `compose.yaml:5-6`; related `.dockerignore:1-2`,
`src/publicai/pipeline.py:455-462`, `src/publicai/retrieval.py:104-106`, and
`src/publicai/agents.py:469-481`.

The factory container receives only `OPENAI_API_KEY`. Every live discovery mode
constructs `ExaRetriever`, which immediately requires `EXA_API_KEY`. The image
excludes `.env`; no environment-file or secret mount supplies it. The Swisscom and
PublicAI provider keys are also absent from the Compose environment mapping.

**Evidence and impact:** With the documented keys in the host `.env`, running the
factory's `discover` or `run` command through the supplied Compose service still
fails with the missing-Exa-key configuration error before discovery. Once Exa is
supplied manually, alternate modes require another manual override for their model
key. Compose's host-side variable substitution does not forward unmapped variables.
Offline `build` remains unaffected.

**Smallest robust fix:** Explicitly forward `EXA_API_KEY`, `SWISSCOM_KEY`, and
`PUBLICAI_API_KEY` alongside the existing OpenAI key. Keep this allowlist limited to
the factory; generated runtime containers should remain credential-free. Validate
the mappings without real secrets and add a local configuration smoke check.

### R05 — Deliberate Exa pacing can expire the request timeout before HTTP starts

**Category:** Reliability / configuration. **Severity:** Medium.
**Status:** Confirmed using immediate mock HTTP responses.

**Location:** `src/publicai/retrieval.py:153-182`; valid settings are defined at
`src/publicai/config.py:66-74`.

`_post` starts `asyncio.timeout(min(exa.timeout, remaining))` before waiting for the
configured request interval. Rate-limit waiting therefore consumes the per-request
timeout. Valid settings permit the interval to exceed that timeout, causing a
subsequent request to fail as `inaccessible` before being sent.

**Evidence and impact:** With `ExaSettings(timeout=0.025, request_interval=0.1)`,
`CrawlSettings(run_timeout=10)`, and instant successful mock responses, the homepage
fetch succeeded but the immediately following contact fetch failed after about
0.026 seconds. Only one HTTP request occurred, `request_count` remained 1, and
`budget_stop_reason` was `None`. The supported 20-second timeout / 60-second interval
combination has the same problem. Contact seeding or evidence retrieval can lose
valid pages and misdiagnose intentional pacing as a provider failure. Defaults do
not produce this complete pre-request failure with instantaneous responses.

**Smallest robust fix:** Keep pacing under the global acquisition deadline, start
the per-request timeout after pacing, and recompute the remaining global allowance
before HTTP. Add an offline regression with interval greater than timeout and an
immediate response; preserve the overall deadline and serialization guarantees.

### R06 — `.ical` calendars bypass the HTML-only retrieval restriction

**Category:** Correctness / acquisition boundary. **Severity:** Medium.
**Status:** Confirmed at the live adapter boundary with mock HTTP.

**Location:** `src/publicai/retrieval.py:63-68,254-278`; classification at
`src/publicai/crawler.py:307-310`; policy at `src/publicai/prompts/discovery.md:22-26`.

The retrieval URL guard blocks `.ics` but omits `.ical`, although the shared link
classifier recognizes both as calendars. `web_fetch` does not otherwise require an
HTML link classification before calling Exa and retaining its output.

**Evidence and impact:** The same municipal `calendar.ical` URL was classified as
`calendar` by `_link`, then successfully requested through `/contents` and retained
by `ExaRetriever.fetch` when the mock response contained `BEGIN:VCALENDAR` text.
This permits contents that the workflow explicitly treats as uninspected documents
to enter the source-evidence path. Prompt instructions discourage the call but do
not enforce the boundary. Actual Exa calendar extraction was not tested.

**Smallest robust fix:** Include `.ical` in the retrieval guard and share the
relevant suffix definition with link classification to prevent divergence. Extend
forbidden-fetch tests to require zero HTTP requests for both calendar suffixes,
including uppercase and percent-encoded variants. This closes the demonstrated
gap; it does not establish MIME guarantees for extensionless remote URLs.

### R07 — A service source can satisfy the distinct identity-page check

**Category:** Contract validation. **Severity:** Low.
**Status:** Confirmed for imported discoveries; normal acquisition prevents this state.

**Location:** `src/publicai/contracts.py:251-260`; imported build path at
`src/publicai/builder.py:451-454`.

`distinct_identity_urls` counts every source cited for the municipality name,
including service sources. Its size therefore does not establish that homepage and
contact evidence themselves come from different pages.

**Evidence and impact:** Set the fixture contact source URL equal to the homepage
URL. Add a copy of the homepage source with a new ID, `kind="service"`, and a
`/third-page` URL, and add its name citation. The discovery validates, packaging
checks return `[]`, and candidate MCP conformance reports no failures. An imported
artifact can therefore present the same page as both required identity sources.
This is a contract-integrity defect, not an authentication bypass: hashes and
metadata already do not prove website authenticity.

**Smallest robust fix:** Require an actual homepage/contact pair with distinct
normalized page URLs, excluding service sources from that decision. Extend the
existing duplicate-identity-page regression with the third-source case. Keep the
current URL normalization behavior. Normal `DiscoveryContext.retain` deduplicates
URLs and labels root pages as homepages (`agents.py:164-175`), limiting the defect
to imported-artifact validation.

### R08 — Conformance diagnostics discard the failure that needs fixing

**Category:** Observability / maintainability. **Severity:** Low.
**Status:** Confirmed with a failing local child process.

**Location:** `src/publicai/builder.py:390-400,467-486`; diagnostic producer at
`src/publicai/conformance.py:114-166`.

The builder captures conformance stdout/stderr but discards both and the return code
when the child fails. The diagnostic artifact reports only “Maintained package
conformance failed,” and staging is deleted. Different contract failures, import
errors, and child termination consequently become indistinguishable to the operator.

**Evidence and impact:** A temporary conformance child printed the maintained
failure message `Tool response has incorrect snapshot identity: get_office_hours.`
and exited with status 1. `_run_conformance` exposed only its generic `ValueError`,
without a chained cause. The actual conformance module already produces specific
contract-failure messages that would identify the failing check.

**Smallest robust fix:** Preserve the return code and a bounded, structured set of
safe conformance failure identifiers/messages in the build diagnostic. Do not dump
arbitrary exception bodies or untrusted snapshot content. Test a real failing child
so output preservation is exercised, rather than only replacing the conformance
function with an exception.

### R09 — A CLI test depends on terminal width and temporary-path length

**Category:** Test reliability. **Severity:** Low.
**Status:** Reproduced during the full suite and isolated by a focused rerun.

**Location:** `tests/publicai/test_cli.py:42-56`; output generated at
`src/publicai/cli.py:206`.

The test uses the global Rich console and asserts that `diagnostic.json` appears as
one uninterrupted substring. Rich can wrap inside that filename depending on the
ambient width and pytest temporary path, although the intended message is present.

**Evidence and impact:** At the normal 80-column width with the review's isolated
pytest directory, output contained `diagn\nostic.json`, causing the suite to fail.
Rerunning the unchanged test with `COLUMNS=200` passed. This is a deterministic
environment-sensitive test defect, not a missing diagnostic or a discovery failure.
It makes test results depend on filesystem layout and display configuration.

**Smallest robust fix:** Inject a controlled test console with Rich word wrapping
disabled (`soft_wrap=True`) for this assertion, and assert the complete expected diagnostic
message/path. If wrapping behavior itself needs testing, cover it separately with
explicit widths. Do not skip the test or weaken the safe-failure-message contract.

### R10 — PLAN describes a single-host restriction that has already been removed

**Category:** Documentation. **Severity:** Low.
**Status:** Confirmed against current configuration, implementation, and tests.

**Location:** `PLAN.md:12-15,54-55`; contrary behavior at `config.yaml:4-25`,
`README.md:3-8,27-30`, and `src/publicai/pipeline.py:451-454`.

The plan says the crawler permits only Ausserberg and that other municipalities
still require authorization and code changes. The active discovery path already
accepts the 21 configured hosts, and the README demonstrates Riehen. Offline tests
verify another municipality's search/fetch scoping.

**Impact and evidence:** The deferred-work document conflates an unperformed live
second-municipality trial with an unimplemented host expansion. It can misdirect
future work into repeating a completed change or requesting obsolete approval.
The actual allowlist restriction remains enforced; the problem is the stale stated
prerequisite, not an unauthorized host bypass.

**Smallest robust fix:** Remove the obsolete single-host/code-change prerequisite
and retain the genuinely outstanding live second-municipality validation. Update
both references together. This does not imply any live second-site success.

## Validation performed

All Python/tool commands used the existing environment through `uv run --no-sync`,
with `UV_OFFLINE=1`. A temporary `UV_CACHE_DIR` avoided sandbox restrictions on the
user's normal uv cache. Bytecode writing and pytest's cache provider were disabled.

| Check | Observed result |
| --- | --- |
| `ruff format --check --no-cache .` | Passed; 36 files already formatted. |
| `ruff check --no-cache .` | Passed. |
| Full `pytest -p no:cacheprovider` with isolated `--basetemp` | 258 collected: 254 passed, 3 failed, 1 skipped. |
| Two failed local health-probe tests rerun with loopback binding permitted | Both passed. Initial failures were sandbox socket restrictions, not runtime defects. |
| Failed CLI diagnostic test rerun unchanged with `COLUMNS=200` | Passed; width/path dependence is R09. |
| Targeted offline reproductions | Confirmed telemetry payload capture, conflict/date outcome, reviewer-input equivalence, retrieval pacing/calendar gaps, imported identity validation, and discarded conformance output. |

The one skipped test was the explicitly opt-in paid reviewer gold-set evaluation.
The unmodified full suite did not pass in its original invocation; focused reruns
explain its failures and do not erase R09. No tests or assertions were altered.

## Important uncertainties and verification limits

- No live municipality, Exa, model-provider, telemetry-export, or registry calls were
  made. Provider availability, quotas, live model accuracy, actual source freshness,
  and real-world municipality coverage were not established. In-memory telemetry
  confirms what is exportable, not that any historical export disclosed data.
- Docker images were not built, downloaded, scanned, or launched. Compose and
  Docker findings are configuration/call-path conclusions. CI's dependency audit,
  container scan, and fresh generated-package dependency installation were
  inspected but not rerun; current vulnerability status is outside this review.
- Generated-package conformance ran through the existing builder tests using the
  installed interpreter. That is distinct from a fresh runtime-only environment or
  container verification. Loopback MCP health checks were verified separately.
- Full-context inspection is a necessary input condition, not proof of semantic
  correctness. R03 does not measure false approvals or claim prompt injection has
  succeeded in production.
- Production discovery constructs `ExaRetriever`; direct-crawler implementation was
  inspected without treating its unused production paths as current live defects.
  Exa's remote DNS, robots, intermediate redirects, and extraction remain outside
  local enforcement, as the README already states.
- External client setup instructions and provider documentation were not fetched or
  independently verified. No current-product documentation claim is presented as a
  finding. No broad rewrite, dependency addition, or coverage-percentage target is
  needed for the fixes above.
