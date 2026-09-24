# Codebase review (2026-09-24)

The review found five medium-severity issues; all five were subsequently fixed:

| Finding | Current behavior |
| --- | --- |
| Deeply nested HTML stalled synchronous parsing | HTML nesting above 128 elements is rejected as an acquisition gap. |
| An empty optional page aborted discovery | Unusable optional pages are recorded as gaps; identity and useful-evidence checks still apply. |
| Contact links lost their destination before citation | Sanitized `mailto:` and `tel:` destinations are retained as citable text, including after snapshot minimization. |
| Generated releases shared a Docker image tag | Generated Compose files omit an explicit tag and use project-scoped image names. |
| Container health checked only a new server object | `--health-check` probes the running loopback MCP endpoint and lists its six tools. `--check` remains offline snapshot validation. |

Regression tests cover these fixes. The post-fix run reported 148 passing tests, Ruff
formatting and lint checks, and `git diff --check`. Those are historical results,
not a claim about later commits. The loopback health tests did not launch Docker.
Docker image builds and container launches remain unverified in this review.
Packages built before the fixes must be rebuilt to receive the updated runtime
and container templates.

The review did not run live website discovery or model-quality evaluation.
Deterministic tests establish workflow behavior, not the factual accuracy of a
live model inventory. Source hashes identify retained text, not independent
website authenticity. See [README.md](README.md) for current behavior and
[PLAN.md](PLAN.md) for outstanding validation and scope.
