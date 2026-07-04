# Pipeline comparison: `run_pipeline` vs `run_e2e_pipeline`

Reference map for any future attempt to unify the two orchestrator pipelines
in `orchestrator.py`. **Conclusion up front: do not merge them into one
function.** They are two different products that share only scaffolding; the
divergent parts encode genuinely different behavior and merging them risks
silent regressions (several of which the test suite cannot catch — see below).

- `run_pipeline` — full 7-agent sequential build (Researcher — legacy "Grok"
  stage name — → Claude → Antigravity → Codex → Tester → Security → DevOps),
  with MessageBus, slash-command routing, an interactive feedback loop, and a
  multi-file fan-out plus a single-file fallback.
- `run_e2e_pipeline` — lightweight 4-agent E2E flow (Claude plan → Researcher
  critique → Codex implement+self-heal → Tester tests+self-heal). No
  MessageBus, no Security/DevOps, no interactive loop, no fallback.

## Signatures

Common: `prompt, grok_cmd, claude_cmd, codex_cmd, tester_cmd, workspace,
grok_url, claude_url, codex_url, tester_url, api_key_override, poll_timeout,
max_retries, max_debate_rounds, distributed`.

Only in `run_pipeline`: all `*_args` lists; `antigravity_cmd`, `security_cmd`,
`devops_cmd`; `security_url`, `devops_url`; `interactive`.

## Already-shared helpers (extracted)

`derive_project_name`, `resolve_debate_rounds`, `make_http_client`,
`write_progress_md` are module-level helpers both pipelines use.

## Preamble differences (precise)

| Concern | run_pipeline | run_e2e_pipeline |
|---|---|---|
| `project_dir` subdirs | `src, tests, logs, docker` (4) | `src, tests, logs` (3) |
| Output file paths | 7 (research/design/app/review/test_generated/audit/deploy) | 2 (`plan.md` ws + project mirror) |
| Cleanup | slash-cmd interception + 14-path clean | `clean_output_files([plan, proj_plan])` only |
| URL resolution | 6 URLs | 4 URLs (no security/devops) |
| MessageBus | yes (`logs/message_bus.db`) | none |

## Flow (side by side)

| Phase | run_pipeline | run_e2e_pipeline |
|---|---|---|
| Pre | slash-command fast path → single agent → return | (none) |
| 1 | Grok research → `research.md` | Claude planning (`/plan`) |
| 2 | Claude design (input = research) | Grok critique debate |
| Debate | Grok⇄Claude on *design* | the critique loop is step 2 |
| Persist | `design.md` | `plan.md` |
| Interactive | stdin feedback loop | (none) |
| Parse | `parse_design_for_files` | `parse_design_for_files`; empty → early return |
| Fan-out | `process_single_file`: Codex → Tester+Security in parallel → pytest + vuln scan → retry | `process_e2e_file`: two sequential self-heal loops (Codex+flake8/pytest; Tester+pytest); no Security, no MessageBus |
| Post | `review.md` + aggregated `audit.md` + DevOps `deploy.md`; `log_conversation`; return content | `gather` only; return fixed success string |
| Fallback | Antigravity → `app.py` → Codex review → Tester+Security+DevOps | (none) |

## Debate loop — similar shape, different content (TEST-BLIND)

Same control flow and `[APPROVED]` early-exit, but:
- Wording differs ("architecture plan" vs "plan"; "suppressed" typo in
  run_pipeline vs "suggested" in e2e).
- Grounding differs: run_pipeline injects `claude_prompt` (research content);
  e2e injects the raw `prompt`.

**Why this is risky to merge:** the test suite mocks `provider.send_prompt`, so
no test verifies the prompt strings. A wording/grounding regression here would
be invisible to CI. Parameterize only with a manual byte-for-byte diff of the
generated prompts, never trusting a green suite alone.

## Safe-to-extract vs must-stay-separate

Safe scaffolding (low risk, suite-verifiable): preamble; config+api_key+scan+
client setup; `update_progress_md` (done — delegates to `write_progress_md`);
`status_dict` init + `Semaphore(3)`; PYTHONPATH construction; pytest subprocess
plumbing; `parse_design_for_files` + logging.

Must stay separate (structurally different / behavior-defining): slash-command
routing; MessageBus integration; interactive loop; the per-file workers
(`process_single_file` vs `process_e2e_file` — different pass criteria, agents
and retry semantics); post-fan-out review/audit/DevOps; the Antigravity
fallback; the output-file sets; the return/`log_conversation` contracts.

## Merge-risk summary

| Area | Decision required | Risk |
|---|---|---|
| makedirs `docker`, 6-vs-4 URLs | unify or branch | Low (harmless if unified) |
| scaffolding helpers | extract | Low |
| debate loop | unify wording + grounding | Medium, **test-blind** |
| progress file path | `.agents/CURRENT_PROG.md` vs `CURRENT_PROG.md` | Low–Medium |
| slash/interactive/return contract | add to e2e? | Medium–High |
| per-file worker (verify semantics) | which "verified" wins | **Very High** |
| MessageBus, review/audit/DevOps | force onto e2e? | High |
