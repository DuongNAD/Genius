---
name: genius
description: Build or refactor a request end-to-end with the Genius multi-agent pipeline (custom flow), then report the artifacts.
---

# /genius — drive the Genius multi-agent pipeline

Treat everything after `/genius` as the request. FIRST pick the mode from the
user's message, then follow that mode's steps. Do NOT implement build work
yourself; Genius builds it, your job is to drive it and summarize.

**MODE SELECTION:**
- **BUILD** — the message describes something NEW to create → steps 1–6.
- **DEBUG** — the message reports that something ALREADY BUILT is wrong (a
  pasted error/traceback/wrong output, "chưa đúng ý", "sai rồi", "sửa lại",
  an existing file/job named, a single behavior to tweak) → do NOT
  re-orchestrate; use the DEBUG LOOP at the bottom.

BUILD steps:

1. REWRITE the user's request (any language) into the Genius **golden prompt**
   (English) before submitting — unless it already follows this shape:
   - **Tiny utility** (≤3 product files, one clear function): the COMPACT form —
     ONE paragraph **under 600 characters**: goal + exact public API
     (signatures) + file cap ("AT MOST N files: ...") + `Done when:` with
     commands and exit codes. Under 600 chars the plan stage runs fast.
   - **Anything bigger**: the DETAILED form —

     ```
     Build <what> '<name>': <one-sentence goal>.
     FILES (at most <N>): <product files only>.
     BEHAVIOR (exact): <signatures>; <2-3 input -> output examples>;
       <error contract: stderr, exit codes>; <semantics decisions chosen
       explicitly — e.g. "ASCII-only" vs "Unicode casefold", timezone, float
       tolerance>.
     CONSTRAINTS: <stdlib-only | allowed deps>, <language/version>, <no network>.
     ACCEPTANCE (done when): <observable checks with exact commands/exit codes>.
     NON-GOALS: <explicitly out of scope>.
     ORIGINAL REQUEST (verbatim): "<the user's message, untranslated>"
     ```
   - Rules: NEVER list test files in FILES (the pipeline generates `tests/`
     itself). NEVER invent requirements — the user's words are the contract;
     details you add are defaults the architect will list under Assumptions.
     If the request is too vague to fill BEHAVIOR at all, ask the user ONE
     clarifying question, then proceed.

2. Call the `genius_orchestrate` tool with:
   - `prompt`: the rewritten golden prompt.
   - `pipeline`: `"custom"` — plan-first (Claude Opus) → codex-gpt5.6-sol debate →
     gemini-3.5-flash coding + tests → codex-gpt5.6-sol final review.
   - `require_approval`: `true` ONLY if the user asked to approve each stage
     (otherwise omit it). When true, resume with `genius_orchestrate_approve` /
     `genius_orchestrate_reject` at each `awaiting_approval` pause.
   - **Do NOT pass a `workspace` argument.** Genius writes to its own writable
     jobs directory, so your project stays clean and artifacts never fail to
     save. (A relative/non-writable workspace is ignored anyway.)
   It returns a `job_id`.

3. Poll `genius_orchestrate_status` with that `job_id` roughly every 20 seconds
   until `status` is `completed` or `failed`. Report progress from the response:
   `current_stage` says what the pipeline is working on RIGHT NOW (the code
   stage is the long one — often 10+ minutes), `stages` lists what already
   finished (research → design → code → review → deploy), and `workspace` is
   the absolute directory the files land in.

4. On `completed`: read the artifacts (research / design / review / audit / deploy)
   from the `artifacts_ready` URIs (exact URIs, including the `.md` suffix) and
   summarize: what was built, the final-review verdict (approved, or the
   blocking issues), and where the files live (`workspace`). When the run had
   `GENIUS_HACKATHON_MODE` on, two extra submission artifacts sit at the
   workspace root: `pitch.md` (narrative, demo script, Marp slides, judge Q&A)
   and `ai_collaboration_log.md` (the AI collaboration log) — read them from
   `workspace` directly and mention both in the summary.

5. On `failed`: report the `error` and the last completed stage.

6. If a poll returns `status: "interrupted"` (with `recovered_from_journal`),
   the MCP server restarted while the job was in flight: the pipeline is no
   longer running, but every finished stage's artifacts are still in
   `workspace`. Tell the user and re-submit `genius_orchestrate` if they want
   the build finished.

DEBUG LOOP (user hand-tested and something is wrong):

1. LOCATE the file(s) — the last job's `workspace` (from
   `genius_orchestrate_status`) or the path the user names — and READ the
   current content yourself.
2. DIAGNOSE only if the cause is unclear: `gdbg_review` (preferred — the
   debug server runs codex) or `genius_review`, with the file content plus
   the user's evidence.
3. FIX with `gdbg_code` (or `genius_code`) using the FIX prompt:

   ```
   Fix the file '<path>' so that <desired behavior, from the user's words>,
   WITHOUT changing its public API or unrelated behavior.
   OBSERVED: <error/traceback/wrong output, verbatim>.
   EXPECTED: <exact behavior, with one input -> output example>.
   EVIDENCE: <the failing command or test and its output>.
   Return the COMPLETE corrected file content.

   <full current file content>
   ```

4. APPLY the returned file to the workspace, re-run the user's failing
   command/tests if runnable, and report the diff + result. Lock the fix with
   a regression test via `gdbg_unit_test` when the user wants one.
5. ESCALATE to BUILD mode (fresh orchestrate: golden prompt + a CONTEXT
   section describing what already exists) ONLY when the fix means redesign
   across multiple files or a changed public contract.

If the tools are unavailable, tell the user to enable the `genius` /
`genius-debug` MCP servers (**… → Manage MCP Servers**) and retry.
