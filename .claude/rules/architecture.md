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

- **`orchestrator.py`** — the async pipeline runner. `run_pipeline()` (sequential) and `run_e2e_pipeline()` chain the agents: Grok (research) → Claude (architect/design) → Codex (code) → Tester + Security + DevOps. Independent stages run concurrently via `asyncio.gather` (e.g. Tester and Security run in parallel per file in `process_single_file`). Handles tenacity retries, `Retry-After`-aware backoff, background-task polling, and response checksum verification. Per file, a self-healing loop (`--max-retries`, default 3) feeds Tester/Security failures back to Codex; an optional Grok↔Claude design debate (`--max-debate-rounds`, default 2 but **0 under pytest**) exits early on an `[APPROVED]` marker.
- **`serve.py`** — unified entrypoint. Boots agent API servers by role, the central hub (`--distributed`), or delegates to the orchestrator. Owns the `WorkerRegistry` (idle-worker selection by role) and bounded pending-task tracking.
- **`ag_core/`** — the core library:
  - `interfaces/base_agent.py`, `interfaces/base_provider.py` — ABCs. `BaseProvider` carries a `TokenBucket` rate limiter + `asyncio.Semaphore(5)`; `BaseAgent` wires in `VectorMemory` and `GitManager`.
  - `agents/` — the six agent implementations (grok_researcher, claude_architect, codex_reviewer, tester, security_agent, devops_agent).
  - `providers/` — LLM backends. Each `send_prompt` resolves a local CLI (Windows-aware: `.cmd`/`.bat` are wrapped with `cmd.exe /c`, falls back to `%APPDATA%\npm\`), runs it as a subprocess, and parses JSON/JSONL output. Grok auto-runs `grok login` when no API key is present.
  - `distributed/hub.py` + `distributed/worker.py` — production `CentralHub` (task queue, routing, heartbeat sweeper, bounded task dict) and `ClientWorker`. These same classes are used in tests via a mock network protocol — do not fork them into test-only variants.
  - `memory/vector_store.py` — `VectorMemory`, with a simple fallback and optional Chroma / sentence-transformers backend.
  - `utils/` — `db.py` (SQLite + WAL, threaded write queue), `security.py` (HMAC-SHA256 checksums + JWT auth), `message_bus.py` (A2A `Artifact` mailbox), `rate_limiter.py`, `git.py`, `jwt.py`, `logger.py`.
- **`.agents/skills/<agent>/`** — per-agent service wrappers: `api.py` (FastAPI app, the `/run` + `/status/{task_id}` background-task pattern, lifespan `init_db`) and `run.py` (standalone CLI runner). `serve.py` loads these dynamically.

## Ports / roles
Hub `8000`, Grok `8001`, Claude `8002`, Codex `8003`, Tester `8004`, Security `8005`, DevOps `8006`, Dashboard `8080`. Service URLs come from `config.yaml`; a `.agents/service_registry.json` (dynamic port discovery) overrides them at load time.
