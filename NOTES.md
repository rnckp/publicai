# Durable implementation notes

- The requested two Pydantic AI agents are discovery and evidence review. Package
  generation remains deterministic, as specified by the MVP plan; there is no
  code-generating builder agent.
- The user's explicit instruction to use the OpenAI API directly overrides the
  OpenRouter default in `AGENTS.md`. Both agents use low reasoning effort and
  read `OPENAI_API_KEY` from the environment. Existing local
  credentials are authorized for the Ausserberg workflow test; validation results
  must be reported from actual runs.
- Live Luna discovery runs produced malformed/truncated inventories, invalid
  contact-source citations and unsupported absence claims. The output now uses
  six explicit required capability keys with OpenAI strict structured output,
  concise-citation instructions and `gpt-6-sol` for discovery; review uses Luna.
  Do not weaken evidence checks to compensate for model failures.
- Retained snapshots are minimized after full-context review to cited excerpts
  and relevant observed links. Their hashes identify that retained text, not the
  original HTML response. Evidence checks remain reproducible.
- Ausserberg publishes PDFs through its root `action=get_file` route with numeric
  `id` and alphanumeric `resource_link_id`, marked by PDF icons. Only that exact
  observed parameter shape is allowed as a handoff. Fetching query URLs remains
  forbidden; document titles/URLs are trusted acquisition metadata, always
  labeled uninspected and preserved through snapshot minimization.
- The authorized website hostname is exactly `www.ausserberg.ch`. External portal
  URLs may be retained as handoffs but never fetched. The direct OpenAI API is
  a separate, explicitly authorized infrastructure connection.
- `mcp` 2.x names its high-level Python server `MCPServer`; older `FastMCP` imports
  are incompatible with the installed, locked SDK.
- The repository's original OpenAI minimum version was younger than the required
  seven-day supply-chain cooldown. Dependencies were resolved under that cooldown
  and pinned in the lockfile while preserving Python >=3.14.
- Ausserberg's move-out page is an HTML form. Visible fields are evidence of form
  shape, not evidence of complete procedural requirements. Shared navigation can
  contain unrelated permit records; the crawler excludes those navigation texts.
