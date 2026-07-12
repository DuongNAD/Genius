# /genius — drive the Genius multi-agent pipeline

Treat everything after `/genius` as the build request. Orchestrate it through the
**Genius** MCP server's **custom** pipeline and report back — do NOT implement the
work yourself; Genius builds it, your job is to drive it and summarize.

1. Call the `genius_orchestrate` tool with `prompt` = the user's request and
   `pipeline` = `"custom"` (plan-first Claude Opus → codex-gpt5.6-sol debate →
   gemini-3.5-flash coding + tests → codex-gpt5.6-sol final review). Add
   `require_approval: true` only if the user wants to approve each stage. **Do
   NOT pass a `workspace`** — Genius uses its own writable jobs dir (keeps your
   project clean; a relative/non-writable workspace is ignored anyway). It
   returns a `job_id`.
2. Poll `genius_orchestrate_status` with that `job_id` about every 20 seconds
   until `status` is `completed` or `failed`; report `current_stage` (what is
   running now — the code stage is the long one) and each finished stage.
3. On completion, read the artifacts from the `artifacts_ready` URIs (exact
   URIs, `.md` suffix included) and summarize what was built, the final-review
   verdict, and where the files live (the `workspace` field). On failure,
   report the error and the last completed stage. A `status: "interrupted"`
   means the MCP server restarted mid-job: artifacts of finished stages are
   still in `workspace`; re-submit to build again.

If the tools are unavailable, enable the `genius` MCP server (… → Manage MCP
Servers) and retry.
