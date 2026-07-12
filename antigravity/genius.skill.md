---
name: genius
description: Build or refactor a request end-to-end with the Genius multi-agent pipeline (custom flow), then report the artifacts.
---

# /genius — drive the Genius multi-agent pipeline

Treat everything after `/genius` as the build request. Orchestrate it through the
**Genius** MCP server's **custom** pipeline and report back — do NOT implement the
work yourself; Genius builds it, your job is to drive it and summarize.

Steps:

1. Call the `genius_orchestrate` tool with:
   - `prompt`: the user's request, verbatim.
   - `pipeline`: `"custom"` — plan-first (Claude Opus) → codex-gpt5.6-sol debate →
     gemini-3.5-flash coding + tests → codex-gpt5.6-sol final review.
   - `require_approval`: `true` ONLY if the user asked to approve each stage
     (otherwise omit it). When true, resume with `genius_orchestrate_approve` /
     `genius_orchestrate_reject` at each `awaiting_approval` pause.
   - **Do NOT pass a `workspace` argument.** Genius writes to its own writable
     jobs directory, so your project stays clean and artifacts never fail to
     save. (A relative/non-writable workspace is ignored anyway.)
   It returns a `job_id`.

2. Poll `genius_orchestrate_status` with that `job_id` roughly every 20 seconds
   until `status` is `completed` or `failed`. Report progress from the response:
   `current_stage` says what the pipeline is working on RIGHT NOW (the code
   stage is the long one — often 10+ minutes), `stages` lists what already
   finished (research → design → code → review → deploy), and `workspace` is
   the absolute directory the files land in.

3. On `completed`: read the artifacts (research / design / review / audit / deploy)
   from the `artifacts_ready` URIs (exact URIs, including the `.md` suffix) and
   summarize: what was built, the final-review verdict (approved, or the
   blocking issues), and where the files live (`workspace`).

4. On `failed`: report the `error` and the last completed stage.

5. If a poll returns `status: "interrupted"` (with `recovered_from_journal`),
   the MCP server restarted while the job was in flight: the pipeline is no
   longer running, but every finished stage's artifacts are still in
   `workspace`. Tell the user and re-submit `genius_orchestrate` if they want
   the build finished.

If the tools are unavailable, tell the user to enable the `genius` MCP server
(**… → Manage MCP Servers**) and retry.
