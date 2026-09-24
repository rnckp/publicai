# Deferred work and validation

- Repeat the Apertus end-to-end run when Swisscom quota permits. The September
  2026 verification hit HTTP 429 after the first model response and two additional
  page fetches; lower output allowance and visible bounded retries are implemented,
  but successful live discovery/review/packaging is not yet established.

- Run the ten-case reviewer gold set explicitly with `--run-review-evals` before
  using it to justify model changes. Offline fixture/scoring checks do not measure
  the live reviewer's false-approval or false-rejection rates.

- A second German-speaking municipality was part of the original MVP acceptance
  plan, but the current crawler permits only `www.ausserberg.ch`. Expanding that
  boundary requires explicit authorization and code changes. No second-site
  result is established by the repository.
- Generated-package Docker build and launch have not been verified locally in
  the recorded review. The builder runs offline subprocess conformance; that is
  distinct from container validation.
- Live model output still needs source spot-checking for any demonstrated
  municipality. Deterministic tests and semantic review cannot establish
  factual accuracy on their own.
- PDF/iCalendar parsing, JavaScript rendering, form submissions, runtime
  refresh, and public hosting/authentication remain outside the implemented
  read-only HTML snapshot workflow.

## TODO: municipality image publishing from the CLI

- Extend `factory run` and standalone package publishing to build and push the
  generated runtime image to GitHub Container Registry (GHCR), after review and
  conformance pass. Start with local Docker Buildx; defer scheduled GitHub Actions
  runs and remote builds until needed.
- Add explicit publishing options (for example, `--push`, `--registry`, and
  `--municipality`) with registry, namespace, and target-platform defaults in
  `config.yaml`. Use a stable, validated lowercase municipality slug, with a canton
  suffix for name collisions: `ghcr.io/<owner>/ausserberg:latest`.
- Update the same municipality's `latest` tag on each successful publication;
  preserve the last published image on failure and keep local build directories
  immutable. Prevent overlapping publications from replacing a newer run with an
  older one. Treat historical registry-version cleanup as a separate retention
  policy; moving `latest` does not delete old images.
- Use Docker registry login with a classic PAT scoped to `write:packages`; keep
  credentials outside generated packages, images, and logs. Default packages to
  private and attach the source-repository label. For future GitHub Actions, use
  `GITHUB_TOKEN` with package-write permission and SHA-pinned actions.
- Support explicit Linux target architectures (AMD64/ARM64 or both), report the
  published image reference and digest, and validate the built container's startup
  and health before updating `latest`. Include image vulnerability scanning.
- Update generated Compose configuration and documentation to consume the stable
  image reference. Explain that publishing does not update running containers:
  deployment requires `docker compose pull` followed by `docker compose up -d`.
  Keep automated deployment separate from publishing.
- Test naming and input validation, successful publication, failed validation/build/
  push, and overlapping runs; verify a real Docker build and authorized GHCR push.
  Supporting municipalities beyond Ausserberg remains dependent on the separately
  authorized discovery-boundary expansion above.

## Planned: selective runtime refresh

Evolve toward a hybrid of reviewed discovery snapshots and bounded live lookups.
Discovery should establish authoritative sources and baseline facts; existing MCP
tools should refresh time-sensitive information behind their current interfaces.
This is deferred work, not implemented behavior.

- Start with `get_garbage_collection`: refresh when cached data exceeds a
  configurable freshness limit or does not cover the requested date interval.
  Include holiday exceptions where supported by official evidence.
- Record approved source URLs and acquisition formats per capability during
  discovery. Inspect the actual waste sources before choosing parsers; prefer
  structured calendars or JSON where available. PDF/iCalendar support may be a
  prerequisite and remains outside the current implementation.
- Fetch only approved sources using the existing crawler restrictions and bounded
  request/time budgets. Validate and review extracted facts before making them
  available; retain citations and the last successfully validated data.
- Cache validated results and configure freshness limits per capability in
  `config.yaml`. Keep runtime network access opt-in and preserve offline operation.
  Decide cache storage and scheduled versus on-demand refresh during implementation.
- Extend responses to distinguish source check time, content retrieval time,
  published validity, and refresh outcome. A newly fetched old calendar must never
  be treated as current solely because retrieval succeeded.
- On refresh failure, clearly label stale information, preserve useful general
  guidance, and provide the official destination when current dates cannot be
  established. Failed lookup or incomplete coverage must not mean “no collection.”
  Preserve zone requirements, date-window rules, and conflict handling.
- After the garbage-collection pilot, consider regular checks for office hours and
  temporary closures, longer caches for recycling information, and periodic checks
  for move-in/out requirements and reporting contacts. Choose intervals from source
  behavior and freshness needs rather than one global expiry.
- Validate with deterministic tests for cache hits, refresh success and failure,
  expired published calendars fetched today, uncovered query intervals, holiday
  exceptions, zone filtering, and rejected sources or invalid evidence. Update
  generated packages, response contracts, conformance checks, and documentation
  together when runtime refresh is implemented.

The implemented commands, boundaries, and tool behavior are documented in
[README.md](README.md). The original design context is summarized in
[ideas-patrick/hackathon-mvp-plan.md](ideas-patrick/hackathon-mvp-plan.md).
