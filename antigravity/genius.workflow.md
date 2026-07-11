# /genius — drive the Genius multi-agent pipeline

Treat everything after `/genius` as the build request. Orchestrate it through the
**Genius** MCP server's **custom** pipeline and report back — do NOT implement the
work yourself; Genius builds it, your job is to drive it and summarize.

1. Call the `genius_orchestrate` tool with `prompt` = the user's request and
   `pipeline` = `"custom"` (plan-first Claude Opus → codex-gpt5.6-sol debate →
   gemini-3.5-flash coding + tests → codex-gpt5.6-sol final review). Add
   `require_approval: true` only if the user wants to approve each stage. It
   returns a `job_id`.
2. Poll `genius_orchestrate_status` with that `job_id` about every 20 seconds
   until `status` is `completed` or `failed`; report each stage as it finishes.
3. On completion, read the artifacts from the `artifacts_ready` URIs and
   summarize what was built, the final-review verdict, and where the files live.
   On failure, report the error and the last completed stage.

If the tools are unavailable, enable the `genius` MCP server (… → Manage MCP
Servers) and retry.
