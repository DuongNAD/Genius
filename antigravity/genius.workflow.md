# /genius — drive the Genius multi-agent pipeline

Treat everything after `/genius` as the build request. Orchestrate it through the
**Genius** MCP server's **custom** pipeline and report back — do NOT implement the
work yourself; Genius builds it, your job is to drive it and summarize.

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

2. Call the `genius_orchestrate` tool with `prompt` = the rewritten golden
   prompt and `pipeline` = `"custom"` (plan-first Claude Opus →
   codex-gpt5.6-sol debate → gemini-3.5-flash coding + tests →
   codex-gpt5.6-sol final review). Add `require_approval: true` only if the
   user wants to approve each stage. **Do NOT pass a `workspace`** — Genius
   uses its own writable jobs dir (keeps your project clean; a
   relative/non-writable workspace is ignored anyway). It returns a `job_id`.
3. Poll `genius_orchestrate_status` with that `job_id` about every 20 seconds
   until `status` is `completed` or `failed`; report `current_stage` (what is
   running now — the code stage is the long one) and each finished stage.
4. On completion, read the artifacts from the `artifacts_ready` URIs (exact
   URIs, `.md` suffix included) and summarize what was built, the final-review
   verdict, and where the files live (the `workspace` field). On failure,
   report the error and the last completed stage. A `status: "interrupted"`
   means the MCP server restarted mid-job: artifacts of finished stages are
   still in `workspace`; re-submit to build again.

If the tools are unavailable, enable the `genius` MCP server (… → Manage MCP
Servers) and retry.
