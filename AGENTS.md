# Genius — read this before touching anything

This repository is the **Genius multi-agent framework** (Antigravity 2.0
orchestrator backend). On this machine it is an **installed tool**, already
registered as the MCP server `genius` (config: `~/.gemini/config/mcp_config.json`).

**Do NOT modify, refactor, or "fix" code in this repository** unless the user
explicitly says they want to work on the Genius framework itself. For any normal
task, USE the `genius` tools below and write results into the user's own
workspace — never here.

## Language

Talk to the **user in Vietnamese** — progress updates, technical explanations,
debugging, questions, summaries. The **autonomous pipeline works in English**:
research briefs, the design plan, code, and agent-to-agent artifacts are all
English (that is baked into the Genius agent prompts). So: English inside the
machine, Vietnamese when you speak to the user.

## The `genius_*` tools (namespaced on the wire)

Tools are exposed as `genius_<name>`. Pick by task:

- **`genius_doctor`** — preflight check (which CLIs/keys are ready). **Call this
  first** before `genius_orchestrate`; it has no side effects.
- **`genius_orchestrate`** — run the **full multi-agent pipeline**
  (research → design → code → test + security + deploy) as a background job.
  Returns a `job_id` immediately. Pass `workspace` = an absolute dir for output,
  and `require_approval: true` to pause for human review after research/design/code.
- **`genius_orchestrate_status`** — poll a job by `job_id` (per-stage progress,
  `artifacts_ready`, `elapsed_seconds`). **`genius_orchestrate_approve` /
  `genius_orchestrate_reject`** resume/cancel a paused job.
- Single agents (in-process, no servers needed): **`genius_research`**,
  **`genius_design`**, **`genius_code`**, **`genius_unit_test`**,
  **`genius_security_audit`**, **`genius_deploy`**.
- **`genius_review`** — quick review of pasted code (no file writes).
  **`genius_debate`** — adversarial design refinement (critic ↔ refiner).
- **`genius_code_graph`** — repo-structure queries (where is a symbol defined,
  who imports a file, ranked repo map) **without running any agent** — fast.
- **`genius_eval`** — grade a finished pipeline run against metrics.
- **`genius_notebooklm_list` / `_query` / `_research`** — NotebookLM: list
  notebooks / grounded+cited Q&A over a notebook's sources / deep-research
  web→notebook (`nlm` is installed and logged in on this machine).

## How to get the most out of it

- **Design drives quality.** The pipeline offloads hard reasoning to the
  **design** stage (claude), then a fast coder (agy/gemini-3.5-flash) just
  implements the detailed spec, and a self-heal loop runs the tests. If code
  comes out wrong, the design was too vague — improve the design, not the coder.
- **Start small, then orchestrate.** For a quick answer use a single tool
  (`genius_code`, `genius_review`, `genius_code_graph`); for a real build use
  `genius_orchestrate` and poll `genius_orchestrate_status`.
- **Read artifacts** anytime via MCP resources: `genius://artifacts/design.md`,
  `research.md`, `review.md`, `audit.md`, `deploy.md`.

## Gotchas on this machine

- `genius_orchestrate` needs the six skill servers (ports 8001–8006). It now
  **auto-starts them** if they're down (first run waits ~15–30s for boot); to
  start them manually: `python serve.py --roles researcher,claude,codex,tester,security,devops`.
- The MCP server does **not** read this repo's `.env` — provider/model config
  lives in the `env` block of `~/.gemini/config/mcp_config.json`.
- NotebookLM queries take ~1 minute (grounded, cited) — that's normal.
- Backends available here: **claude** (Claude Code), **agy** (gemini-3.5-flash),
  **grok**, **nlm** (NotebookLM). `codex` (OpenAI) is NOT installed, so the
  coder/tester/security/devops roles are routed to agy/claude.
