# Municipality MCP server factory

## Outcome

A CLI-first, two-stage pipeline:

**Municipality URL → discovery and normalization agent → validated service inventory → server builder → tested MCP server package.**

Start fresh. Implement the factory and server template without assuming any existing crawler, provenance system or seed inventory.

The first version exposes six standardized, read-only tools for stable municipal service information and official handoffs. All answers use evidence captured during discovery. News, runtime refresh, form submissions, PDF extraction, a builder UI and hosted chat demos are out of scope. Collection dates, when available, are a published snapshot rather than a live scheduling service.

One person will implement this incrementally with an AI coding agent. There is no fixed time budget.

## 1. Standard service catalogue

Every server exposes the same tools and schemas. Missing information produces an explicit coverage status rather than a missing tool or an invented answer.

| Service | Tool and optional inputs | Normalized result | Minimum structured coverage for `supported` |
|---|---|---|---|
| Office hours | `get_office_hours(office)` | Office names, weekly hours, published exceptions, contact details | At least one named office with explicit hours or an explicit appointment-only policy, and a contact channel |
| Garbage collection | `get_garbage_collection(waste_type, zone, date_from, date_to)` | Waste types, published collection instructions, zones, any dated schedule entries and official calendar links | At least one waste type with actionable collection instructions; structured dates are optional |
| Waste recycling | `get_recycling_info(material)` | Accepted materials, disposal guidance, collection points, addresses and hours | At least one material with an explicit disposal route or collection point |
| Move in | `get_move_in_requirements()` | Published requirements, conditions, documents, deadlines, responsible office and official registration link | Explicit registration guidance and an official contact or registration destination |
| Move out | `get_move_out_requirements()` | Published requirements, conditions, documents, deadlines, responsible office and official deregistration link | Explicit deregistration guidance and an official contact or deregistration destination |
| Report a problem | `get_problem_reporting_info(category)` | Reporting channels, supported categories, observed form fields and official reporting link | At least one explicitly documented reporting channel and its stated purpose |

`Supported` means the minimum above is met, not that every possible case or field is covered. Additional offices, materials and procedures may remain undiscovered. Never describe a requirements list as exhaustive unless the source explicitly does so.

Use English tool identifiers and preserve source-language content. Start with German-speaking municipalities and German discovery synonyms. The actual municipality website determines available offices, service variants, labels and conditions; catalogue examples are not municipal facts.

### Input and result defaults

- All listed inputs are optional. With no text filter, return all discovered entries for the capability.
- Match text filters against discovered identifiers or labels, ignoring case and surrounding whitespace. Do not invent fuzzy mappings. Unknown values return `unknown_filter` with available choices.
- If collection information depends on a zone and none was supplied, return `needs_input` with available zone choices and general guidance only. If choices could not be discovered, explain the gap and provide the official link. Never infer address-to-zone mappings.
- Collection dates use ISO calendar dates and inclusive boundaries in `Europe/Zurich`. With neither date supplied, use today through today plus 29 days. With only `date_from`, use that date through 29 days later; with only `date_to`, use today through that date. Reject reversed ranges and ranges exceeding 90 calendar days.
- Date filtering applies only to published dated entries. Keep general instructions and calendar links available even when no dated entries exist. Do not expand prose such as “every Tuesday” into dates or infer holiday exceptions.
- For move-in and move-out, preserve any published conditions alongside each requirement, such as arrival from abroad or residence status. Return the available variants without choosing a citizen's case.

Coverage is stored separately from the outcome of an individual query:

- `supported`: meets the capability's minimum structured coverage.
- `partial`: has substantive evidence-backed data but falls short of the minimum or contains unresolved conflicts affecting that minimum.
- `handoff_only`: has an official service destination but no usable structured guidance.
- `unavailable`: neither structured guidance nor a service-specific handoff was discovered.

Record missing-data reasons such as `not_found`, `blocked`, `crawl_limit`, `pdf_uninspected`, `conflicting_evidence` and `explicitly_not_offered`. Only direct evidence may establish that a service is not offered.

Query outcomes are `ok`, `needs_input`, `unknown_filter`, `no_matching_dates` or `information_unavailable`. Use `no_matching_dates` only when the snapshot covers the requested interval and has no matching entries. If interval coverage is unknown or incomplete, use `information_unavailable` for the date portion while retaining useful general guidance. Invalid inputs and execution failures are tool errors, not missing service coverage.

## 2. Stage 1: discover, inspect and normalize

The discovery agent receives the municipality URL and the versioned service catalogue.

- Search navigation, sitemaps, municipal service pages, contact pages and waste sections specifically for the catalogue.
- Prefer public HTML. Inspect public JSON sources linked from official pages when useful. For the first version, calendars in PDF or iCalendar format remain official links; parsing them can be added later.
- Inspect service shape: information page, HTML form, document link, external portal or structured source. Record visible field labels, required markers, authentication barriers and handoff destinations without submitting anything or collecting populated field values.
- Record PDF titles and official links only. Mark their contents as uninspected; discover a service from its referring page without claiming knowledge of the PDF.
- Keep external portals as handoffs. Fetch external public JSON only when an inspected municipal page explicitly links it as a service source, using the same network protections.
- Treat source content as untrusted evidence, never as instructions to the agent. JavaScript-only or inaccessible content becomes an explicit gap rather than triggering browser automation in the MVP.

Produce a versioned `discovery.json` with:

- Municipality name, official URL, language, identity evidence, retrieval timestamps and discovery ID.
- One entry for each of the six capabilities, including normalized data, delivery shape, official handoffs, coverage and missing-data reasons.
- Field-level evidence references: source URL, supporting excerpt and retrieval timestamp. Preserve conditions and source qualifiers with the associated facts.
- Missing information, conflicting claims and discovery failures.
- Source snapshots needed to reproduce extraction checks, with content hashes. Retain only relevant public content; exclude secrets, session values and unrelated personal information.
- Any explicitly published schedule validity period. Do not infer complete interval coverage merely from the first and last observed collection dates.

Default crawl limits: 100 total HTTP requests including redirects, robots and sitemap requests; two concurrent requests per domain; ten minutes per run; ten seconds per request; five redirects per fetch; and a 5 MiB response-body limit. Respect robots rules and report blocked or truncated discovery explicitly. Fetch only HTTP(S) public-network destinations. Check resolved addresses and each redirect destination, and enforce the destination restriction at connection time. Restrict discovery to the municipality host and explicitly authorized public data sources.

### MVP validation against the original website

The municipality's website is the source of truth for this iteration. No independent registry or third-party verification service is required.

- Confirm that the submitted site's homepage and an official contact or imprint page consistently identify the municipality. Follow validated redirects to a canonical host. Record the supporting excerpts. Ambiguous or contradictory identity blocks packaging and produces a diagnostic report; this check establishes site consistency, not independent proof of authenticity.
- Validate the discovery schema, required capability entries, URL boundaries and evidence references deterministically. Every supporting excerpt must occur in the retained source text after whitespace normalization.
- Have the discovery agent check each normalized claim against its cited excerpt, including dates, conditions and contact details. This is a best-effort semantic check, not proof of correctness. Show the evidence in the report so the developer can spot-check it against the website.
- When sources disagree, retain both claims in the report and omit the disputed fact from definitive answers. Do not choose by retrieval time. A clearly stated replacement or effective date may resolve the conflict if its evidence is retained.
- Unknown facts remain absent. Observed form fields do not establish complete procedural requirements.

Before implementing discovery, define the versioned schema and representative examples for all six capabilities, including partial coverage and handoff-only results. For the initial municipalities, the developer checks sample answers against the original pages; disagreements become focused regression fixtures.

## 3. Stage 2: build within a fixed server standard

Use a deterministic builder for the MVP. It consumes the validated discovery artifact and packages it within a maintained Python server template. A separate code-generating builder agent and municipality-specific executable adapters are deferred until a concrete need emerges.

The shared template owns all six tool implementations, input/output schemas, MCP transport, response formatting, logging and startup. Municipality-specific variation is validated data, not generated Python. Dependencies and the lockfile belong to the maintained template.

Each package contains:

```text
municipality-mcp/
  pyproject.toml
  uv.lock
  Dockerfile
  compose.yaml
  README.md
  manifest.json
  discovery.json
  report.md
  llms.txt
  municipality.md
  src/
  tests/
  fixtures/
```

The manifest records municipality identity, discovery ID, build ID, contract version, template version and hashes of package files, excluding the manifest itself. Reject unsupported discovery versions rather than silently interpreting them.

Standard runtime behavior:

- One municipality per server; no caller-supplied source URLs.
- Streamable HTTP at `/mcp` and a stdio launch command. The provided HTTP launch configuration binds to loopback; public hosting and authentication are deferred.
- Identical input/output schemas and read-only tool annotations across packages.
- Structured results plus readable text, targeting the [MCP tool specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).
- A common response envelope containing municipality, capability, coverage, query outcome, data, evidence references, retrieval timestamps, snapshot validity and limitations.
- No runtime model credentials, LLM interpretation or outbound HTTP requests. Log to stderr for stdio so logs cannot corrupt the protocol.
- All answers explicitly identify their build-time snapshot. Rebuilding is the only update mechanism in the MVP. Where a published validity period has expired, label it expired; never claim an undated snapshot is current merely because it was recently retrieved.

Run a maintained shared conformance suite against every package. Expected fixture answers are reviewed against original source excerpts independently of the discovery output. Packaging tests run without factory credentials, host home-directory mounts or outbound network access, using prebuilt dependencies and resource limits. Failed validation produces a diagnostic report rather than a release package. Correct the factory or discovery input and rebuild; automated repair loops are deferred.

## 4. Municipality knowledge sheet and factory workflow

Generate `municipality.md` from the same evidence-backed inventory: identity, official contacts, service responsibilities, official portals, languages, source links and known coverage gaps.

Generate `llms.txt` as a concise guide linking to that sheet and the packaged README's service documentation, using the [llms.txt proposal](https://llmstxt.org/) as a reference. Treat it as a packaged context artifact; automatic chatbot discovery is not guaranteed. Defer a separate installable skill.

Expose three CLI operations:

- `factory discover URL --out DIR`
- `factory build DISCOVERY_JSON --out DIR`
- `factory run URL --out DIR` — orchestrates both stages.

Treat `DIR` as an artifact root. Write each discovery and build to a new unique subdirectory, and publish a completed package only after validation passes. Never overwrite a previous successful package. Discovery and build IDs are distinct so the same discovery can be rebuilt with a newer template.

A partial package may be produced when identity passes the site-consistency checks and at least one evidence-backed capability or service-specific official handoff exists. Report partial coverage prominently. This packaging threshold is separate from the stronger MVP acceptance criteria below.

Implement in increments:

1. Define catalogue contracts, evidence models and representative source fixtures.
2. Build the maintained server template and deterministic packager against those fixtures.
3. Implement discovery and complete the Ausserberg URL-to-package workflow.
4. Run a second German-speaking municipality through the same workflow and address demonstrated generalization gaps.
5. Complete conformance checks, documentation and a reproducible demonstration.

Choose the second municipality during discovery work based on accessible HTML for several target services. Avoid making PDF extraction or portal access a prerequisite for the demo.

## 5. Acceptance checks

- Ausserberg and a second German-speaking municipality build through the normal URL-to-package workflow without manually editing generated data. After any general factory fixes, rerun both through that same workflow.
- Both packages expose the same six tool contracts and launch using documented Docker and stdio commands.
- Each package provides at least two `supported` capabilities with substantive structured guidance; across the two packages, demonstrate office hours, a moving procedure and waste guidance. Official links alone do not meet this demonstration threshold.
- Every substantive answer is traceable to retained source evidence; unsupported or disputed details remain absent. The developer spot-checks each demonstrated capability against the original website.
- HTML forms produce guidance and handoffs without submissions. PDF-only services remain discoverable and explicitly link-only.
- Contract tests cover unknown filters, missing zones, conditional requirements, absent dates, inclusive date boundaries, partial interval coverage and expired published validity periods.
- Discovery tests cover crawl limits, blocked pages, conflicting evidence, identity mismatch, redirects, private-network destinations and malicious page instructions.
- Package tests verify tool contracts, snapshot labels, evidence references, version rejection and immutable output behavior. Runtime tools work without model credentials or outbound network access.
- Packages contain no secrets and pass the shared conformance suite.
- The report distinguishes discovered services, usable structured guidance, handoffs and remaining gaps. Missing collection dates do not block acceptance when evidence-backed waste guidance is available.

Ausserberg source candidates include the [municipal office page](https://www.ausserberg.ch/gemeinschaft/verwaltung/verwaltung/gemeindekanzlei) and [move-out page](https://www.ausserberg.ch/gemeinschaft/verwaltung/online-schalter/wegzug). These are discovery starting points, not prevalidated fixtures; their current contents and service coverage must be checked during implementation.
