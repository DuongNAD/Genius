---
paths:
  - "orchestrator.py"
  - "serve.py"
  - "dashboard.py"
  - "mcp_server.py"
  - "ag_core/**"
  - ".agents/**"
---

# Architecture

## Request flow
```
Orchestrator  --async httpx + HMAC checksums-->  FastAPI Skill Server  -->  Agent  -->  Provider  -->  local CLI / API
```

- **`orchestrator.py`** — the async pipeline runner. `run_pipeline()` (sequential) and `run_e2e_pipeline()` chain the agents: Researcher (role id `researcher`, agy/Gemini-primary chain) → Claude (architect/design) → Codex (code) → Tester + Security + DevOps. Independent stages run concurrently via `asyncio.gather` (e.g. Tester and Security run in parallel per file in `process_single_file`). Handles tenacity retries, `Retry-After`-aware backoff, background-task polling, and response checksum verification. Per file, a self-healing loop (`--max-retries`, default 3) feeds Tester/Security failures back to Codex; an optional critic↔Claude design debate (`--max-debate-rounds`, default 2 but **0 under pytest**) exits early on an `[APPROVED]` marker.
- **`serve.py`** — unified entrypoint. Boots agent API servers by role, the central hub (`--distributed`), or delegates to the orchestrator. Owns the `WorkerRegistry` (idle-worker selection by role) and bounded pending-task tracking.
- **`ag_core/`** — the core library:
  - `interfaces/base_agent.py`, `interfaces/base_provider.py` — ABCs. `BaseProvider` carries a `TokenBucket` rate limiter + `asyncio.Semaphore(5)`; `BaseAgent` wires in `VectorMemory` and `GitManager`.
  - `agents/` — the six agent implementations (researcher, claude_architect, codex_reviewer, tester, security_agent, devops_agent; `grok_researcher.py` is a compat shim re-exporting `ResearcherAgent` as `GrokResearcherAgent`).
  - `providers/` — LLM backends. Each `send_prompt` resolves a local CLI (Windows-aware: `.cmd`/`.bat` are wrapped with `cmd.exe /c`, falls back to `%APPDATA%\npm\`), runs it as a subprocess, and parses JSON/JSONL output. Every role defaults to a `FallbackProvider` chain from `ag_core/provider_factory.py` (Researcher: `agy → claude → codex`; Architect: `claude → agy → codex`; others: `codex → claude → agy`); the grok backend is opt-in only via `GENIUS_PROVIDER_<ROLE>` and auto-runs `grok login` (only when actually invoked) when no API key is present.
  - `distributed/hub.py` + `distributed/worker.py` — production `CentralHub` (task queue, routing, heartbeat sweeper, bounded task dict) and `ClientWorker`. These same classes are used in tests via a mock network protocol — do not fork them into test-only variants.
  - `memory/vector_store.py` — `VectorMemory`, with a simple fallback and optional Chroma / sentence-transformers backend.
  - `utils/` — `db.py` (SQLite + WAL, threaded write queue), `security.py` (HMAC-SHA256 checksums + JWT auth), `message_bus.py` (A2A `Artifact` mailbox), `rate_limiter.py`, `git.py`, `jwt.py`, `logger.py`.
- **`.agents/skills/<agent>/`** — per-agent service wrappers: `api.py` (FastAPI app, the `/run` + `/status/{task_id}` background-task pattern, lifespan `init_db`) and `run.py` (standalone CLI runner). `serve.py` loads these dynamically.
- **`mcp_server.py`** — hand-rolled JSON-RPC MCP server (stdio for Antigravity + optional HTTP). 11 tools: the 6 single agents (research/design/code/unit_test/security_audit/deploy, built in-process via `make_provider`), `orchestrate` (background pipeline job → job_id) and `orchestrate_status` (also reports `elapsed_seconds`, per-stage done/pending `stages` inferred from artifact mtimes vs job start, and `artifacts_ready` URIs), `doctor` (in-process preflight report from `ag_core.diagnostics`), `debate` (in-process researcher-critic ↔ Claude-refiner loop, `[APPROVED]` early exit, max 3 rounds) and `review` (codex-role review of supplied code, no file writes). Also serves MCP **resources**: a fixed whitelist of root artifacts (`research/design/review/audit/deploy/plan.md` + their `.bak` archives) as `genius://artifacts/<name>` — exact-name match only, never glob (`resources/read` on anything else → `-32002`). stdout must stay pure JSON-RPC; logs go to stderr.

## Ports / roles
Hub `8000`, Researcher `8001`, Claude `8002`, Codex `8003`, Tester `8004`, Security `8005`, DevOps `8006`, Dashboard `8080`. Canonical role id for the Researcher is `researcher` (renamed from the legacy `grok` role id); `grok`/`grok_researcher` are accepted everywhere as compat aliases via `ag_core.provider_factory.canonical_role`. Ports and the other role ids are frozen. The grok BACKEND keeps its name and stays opt-in. Service URLs come from `config.yaml`; a `.agents/service_registry.json` (dynamic port discovery) overrides them at load time.
