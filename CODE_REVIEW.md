# Codebase review

Reviewed 2026-09-24. **Five findings: zero critical, zero high, five medium, zero low.**

## Scope and validation

Reviewed repository guidance, configuration, documentation and durable notes; the crawler and its network boundary; agent tools, evidence contracts and semantic review; discovery publication; package generation and conformance; all six runtime tools; tests, container templates and CI. Findings below distinguish reproduced defects from deployment risks established by configuration. No application code or tests were changed.

Local validation used the existing environment through `uv`, with offline mode and no synchronization or dependency installation:

- `pytest -q -p no:cacheprovider`: **136 passed** in 4.02 seconds, including agent fakes, stdio MCP integration and package conformance subprocesses.
- `ruff check --no-cache .`: **passed**.
- `ruff format --check --no-cache .`: **30 files already formatted**. Check mode preserved the requested no-code-change scope.
- Additional in-memory reproductions exercised contact retention, an empty optional acquisition result, parser scaling and timeout behavior. The runtime `--check` command also exited successfully against the fictional fixture.

Commands used `UV_OFFLINE=1`, `--frozen --no-sync` and a temporary writable UV cache after the default cache was inaccessible. Temporary reproduction artifacts and the temporary cache were removed. No website, model, telemetry service or package registry was contacted; `.env` contents were not read.

## Findings

### 1. Deeply nested HTML causes quadratic work outside effective timeout enforcement

**Category:** Performance / reliability / input handling. **Severity:** medium. **Status:** reproduced defect; exposure is limited to responses from the authorized website.

**Locations:** `src/publicai/crawler.py:334–340`, `349–423`, `443–447`, `717–718`; `src/publicai/pipeline.py:111`.

**Issue and impact:** The HTML parser repeatedly scans its entire open-element stack to determine hidden/navigation state. A page containing deeply nested elements therefore takes quadratic work even when its body is far below the 5 MiB limit. Parsing runs synchronously on the async discovery thread. The surrounding `asyncio.timeout` cannot interrupt that computation, so a malformed or hostile response can stall the whole discovery and delay cancellation well beyond the configured budget. The network semaphore and response-byte limit do not bound this CPU work.

**Evidence:** Parsing nested `<div>` elements took approximately 0.019 seconds at depth 1,000, 0.071 seconds at 2,000 and 0.276 seconds at 4,000. The largest body was only 44,017 bytes. Wrapping that same parser call in `asyncio.timeout(0.01)` exited normally after 0.275 seconds rather than enforcing the 10 ms deadline. `fetch()` directly calls this synchronous parser after reading the response. Existing crawler tests cover byte limits but not structural parsing limits.

**Smallest robust fix:** Enforce a conservative internal HTML nesting limit before pushing onto the stack and report overflow as a sanitized `CrawlError`. Add a regression proving deeply nested input is rejected promptly while normal pages remain usable. Cached hidden/navigation depth counters can further remove repeated scans, but are not required to introduce a bounded failure path. Merely moving parsing to a thread would not stop the underlying work on cancellation.

**Trade-off:** An excessive-depth page becomes an explicit acquisition gap. No such hostile page was fetched during this review; the performance defect was reproduced with synthetic input.

### 2. An optional page with no retainable content aborts the entire discovery

**Category:** Correctness / reliability. **Severity:** medium. **Status:** reproduced defect.

**Locations:** `src/publicai/crawler.py:443–464`, `717–726`; `src/publicai/agents.py:144–158`, `256–266`; `src/publicai/contracts.py:93`; `src/publicai/pipeline.py:113–115`.

**Issue and impact:** A successful HTML response can legitimately yield no retained text—for example, a JavaScript-only service page. `fetch()` accepts and caches it as a successful `FetchedPage`, but `DiscoveryContext.retain()` then constructs a `SourceSnapshot` whose `text` requires at least one character. Its `ValidationError` escapes the acquisition/tool boundary. Consequently, one unusable optional page prevents publication even when the homepage, contact identity and useful service evidence are already available. This defeats the otherwise supported partial-discovery behavior.

**Evidence:** The existing network-free pipeline fixture succeeds in the test suite. Adding one optional HTML page containing only a title and script to its seed results caused `discover_with_agents()` to raise `DiscoveryError`, produce no discovery, and write a diagnostic with `stage: acquisition`, `error_type: ValidationError`, and an empty `failures` list. The same `retain()` call appears in `inspect_page`, which catches only `CrawlError`. Existing pipeline tests do not cover an unusable optional page.

**Smallest robust fix:** Detect pages with no usable retained evidence at the acquisition/retention boundary, record a sanitized page-specific failure, and let optional-page processing continue. Apply the same handling to seeded pages and tool-driven inspection. Preserve useful link/form/authentication observations when present, and keep the existing final identity and utility gates: an unusable required identity page must still prevent publication.

**Trade-off:** Do not label every empty response `javascript_required`; absence of text alone does not establish that cause. A generic inaccessible/empty-content gap is safer unless rendering dependence is actually observed.

### 3. Contact destinations disappear before they can become supporting evidence

**Category:** Correctness / evidence completeness. **Severity:** medium. **Status:** reproduced defect.

**Locations:** `src/publicai/crawler.py:270–271`; `src/publicai/agents.py:104–113`, `155–157`; `src/publicai/contracts.py:233–238`.

**Issue and impact:** HTML extraction deliberately recognizes `mailto:` and `tel:` links, but retention appends only HTTP(S) destinations to source text and stores only HTTP(S) links. When a contact link is labeled “E-Mail” or “Anrufen”, its actual address or number is absent from the retained source. The discovery tool can show the destination to the model, yet there is no literal retained evidence supporting that contact value. A correct inventory must omit useful published contact details; trying to cite the actual destination fails excerpt validation. This can reduce office/procedure coverage or prevent usable contact extraction.

**Evidence:** For `<a href="mailto:office@example.test">E-Mail</a>` and `<a href="tel:+41000000000">Anrufen</a>`, extraction returned both destinations. After `retain()`, source text was only `Gemeindekanzlei\nE-Mail\nAnrufen` and `source.links` was empty. Identity/contact validators and the semantic reviewer consume retained evidence, not the original HTML. The existing observed-link retention test covers an HTTPS destination only.

**Smallest robust fix:** Include sanitized, bounded contact labels and destinations in retained textual evidence, alongside the existing observed HTTP links. Keep fetch permissions and HTTP handoff validation unchanged; contact links need not enter the HTTP-only `SourceSnapshot.links` collection. Add a regression where the address/number appears only in `href` and remains citable through minimization.

**Trade-off:** Preserve the existing removal of mail query parameters and validate contact values before retaining them. This concerns published organizational contact channels, not permission to collect unrelated personal details.

### 4. All generated releases share one mutable Docker image tag

**Category:** Deployment correctness / release isolation. **Severity:** medium. **Status:** confirmed configuration defect; container recreation scenario not executed.

**Locations:** `src/publicai/templates/compose.yaml:3`; `src/publicai/builder.py:353–368`; `src/publicai/templates/Dockerfile:15`.

**Issue and impact:** Every generated package copies the literal image name `municipality-mcp:1.0.0`, although each image embeds its own `discovery.json` and manifest. Building release B on the same Docker daemon replaces the tag previously used by release A. Later recreating A without rebuilding can resolve the shared tag to B's snapshot, serving a different discovery from the one in A's package directory. Distinct build directories and Compose project names do not isolate an explicitly shared image name.

**Evidence:** `_stage_package()` copies `compose.yaml` unchanged for every unique build ID. The Dockerfile copies snapshot data into the image. Existing immutable-publication tests verify package files and hashes but do not check image identity. Thus filesystem immutability does not extend to the deployment artifact referenced by Compose.

**Smallest robust fix:** Remove the explicit image name and use Compose's project-scoped build image name, or render an image tag containing the generated build ID. Add a package-generation assertion that two releases do not reference the same explicit image tag.

**Trade-off and uncertainty:** Existing running containers retain their original image; this risk concerns later image resolution/recreation. Always rebuilding the intended directory avoids the immediate scenario, including the documented `up --build` command, but ordinary recreation or rollback should not silently select another snapshot. Docker behavior was not exercised in this review.

### 5. Container health checks validate a new object instead of the running service

**Category:** Observability / operational reliability. **Severity:** medium. **Status:** confirmed by code and local command behavior; container execution not performed.

**Locations:** `src/publicai/templates/Dockerfile:19`; `src/publicai/runtime.py:406–416`.

**Issue and impact:** Docker's health command invokes `municipality-mcp --check`, which reads the snapshot, constructs a fresh server object and immediately returns. It never connects to the running HTTP listener or exercises its request loop. An alive but stalled or nonresponsive server can therefore continue receiving successful health results as long as a separate process can load the package. This does not satisfy the repository's requirement for behavioral health checks.

**Evidence:** The local `--check` command succeeded against the fictional fixture without starting a server. The unconditional return precedes both transport launch branches. Existing tests exercise stdio calls and construction/conformance, but do not verify that the Docker probe fails when the HTTP service is unavailable.

**Smallest robust fix:** Keep `--check` for offline package validation, and use a separate bounded loopback probe for container health. Prefer a lightweight readiness endpoint served by the same HTTP event loop, or a minimal MCP protocol exchange. Test that the probe succeeds against a responsive process and fails with no listener or an unresponsive process.

**Trade-off:** Keep the probe internal and credential-free; no external health service or new dependency is needed. Health status alone does not imply Docker will restart an unhealthy container, so recovery policy should remain explicit.

## Important uncertainties and exclusions

- No live discovery or model-quality evaluation was run. Passing deterministic agent tests establishes orchestration behavior, not factual accuracy of live model output. Semantic review remains explicitly best effort.
- No Docker build/launch, independent generated-environment installation, dependency vulnerability audit or hosted CI run was performed. Deployment findings above identify exactly which conclusions come from configuration and control flow. Existing subprocess conformance did run through the local tests.
- The direct OpenAI provider is an established choice explicitly explained in `NOTES.md` as a prior user override of the OpenRouter default. It is not reported as a provider-policy defect. No provider migration or external skill installation was attempted.
- Deferred PDF/calendar parsing, JavaScript execution, a second municipality, public authentication and runtime refresh are documented scope exclusions, not findings. The absence of a general-purpose crawler or code-generating agent is intentional.
- Source hashes establish retained-text integrity, not independent authenticity; documentation correctly describes this limitation. The published-period rule is explicitly constrained by both agent prompts to complete interval coverage, so ordinary date-window handling was not reported as an unsupported completeness inference.
- No additional critical/high-severity vulnerability or materially justified broad refactor was established. This is not a guarantee that none exists; findings were limited to reproducible behavior or concrete configuration evidence.
