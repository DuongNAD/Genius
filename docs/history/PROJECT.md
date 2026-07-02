# Project: Antigravity 2.0 Enterprise Core Framework (Microservices Edition)

## Architecture
The framework has transitioned from a monolithic/CLI architecture to a distributed microservices model:
$$\text{Orchestrator} \xrightarrow{\text{Async HTTP + Checksums}} \text{FastAPI Skill Server} \xrightarrow{\text{Stateless Payload}} \text{Agent} \longrightarrow \text{Provider} \longrightarrow \text{API}$$

- **Orchestrator (`orchestrator.py`)**: Runs an asynchronous pipeline calling the agent web services via `httpx`. Supports SHA-256 integrity checksums, background task polling, and tenacity retry logic.
- **FastAPI Skill Servers (`api.py` under skills)**: Independent web service wrappers that run agent logic stateless, validating API Keys and payload checksums.
- **Unified Startup Menu (`serve.py`)**: Entrypoint script to launch specific API services or the orchestrator interactively or via CLI flags.

## Port & Role Assignments
- **Grok Researcher API**: Port `8001`
- **Claude Architect API**: Port `8002`
- **Codex Reviewer API**: Port `8003`
- **Tester Agent API**: Port `8004`
- **Security Agent API**: Port `8005`
- **DevOps Agent API**: Port `8006`

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | Monolith Core & CLI | Initial layered framework, CLI wrappers, rate limiters, tenacity API retries. | None | DONE |
| 2 | FastAPI Skill APIs | Expose skills via FastAPI with X-API-Key and X-Payload-SHA256 headers. | M1 | DONE |
| 3 | Stateless Payloads | Support optional `context_data` and bypass disk scanning/writes on servers. | M2 | DONE |
| 4 | Async Orchestration | Async httpx calling, task polling, and tenacity retry loops. | M3 | DONE |
| 5 | Startup Menu CLI | Implement `serve.py` for dynamic role bootup. | M2, M4 | DONE |
| 6 | Tests & Verification | Mock HTTP test coverage and verification. All 144 tests passing. | All | DONE |
| 7 | Vector Memory (R1) | Implement local Vector Database (simple fallback + chroma) and integrate into BaseAgent, Claude & Codex. | M4 | DONE |
| 8 | DevOps & Security (R2) | Add Security & DevOps Agents, integrate into serve.py/orchestrator routing and CLI menu. | M5 | DONE |
| 9 | CI/CD Pipeline (R3) | Setup GitHub Actions workflow (.github/workflows/ci.yml) for automated test runs. | M6 | DONE |
| 10| E2E Testing & Verification | Validate all Phase 5 capabilities through integration/E2E test suite. | M7, M8, M9 | DONE |
| 11| Swarm Upgrades Planning & Exploration | Investigate codebase and verify existing tests | None | DONE |
| 12| Skill Layer & CLI Hang Resolution | Implement missing api.py/run.py for 6 agents; resolve serve.py CLI hang | M11 | DONE |
| 13| Core Distribution & Security | Unify serialization, fix orphan tasks, upgrade HMAC-SHA256, jti JWT, bounded caches | M11 | DONE |
| 14| Database & Microservices | SQLite write queue, Dynamic port discovery | M11 | DONE |
| 15| Quality & Logging | Jitter backoff, type annotations, structured logging | M11 | DONE |
| 16| E2E Verification & Audit Gate | Run pytest on all test suites; perform forensic integrity audit | M12, M13, M14, M15 | DONE |
| 17| Analysis & Exploration (Upgrade V2) | Gather requirements, examine CLI providers, rate limiters, memory, and tests. | None | DONE |
| 18| Grok & Codex CLI Providers (Upgrade V2) | Fix GrokProvider to use grok CLI, fix OpenAIProvider to parse Codex JSONL on Windows. | M17 | DONE |
| 19| Config, Rate Limiter & Cleanups (Upgrade V2) | Update config.yaml models, fix TokenBucket asyncio loop warnings, remove python paths, add gitignore/pre-commit. | M18 | DONE |
| 20| Memory, Concurrency & History (Upgrade V2) | Upgrade VectorMemory to sentence-transformers, parallelize steps in orchestrator, add context history. | M18 | DONE |
| 21| Advanced Features (Upgrade V2) | Implement streaming, WebSocket dashboard, MCP server, Dockerization. | M19, M20 | DONE |
| 22| Final Integration & Audit Gate (Upgrade V2) | Verify 100% pass on all test suites and perform forensic integrity audit. | M21 | DONE |

## Code Layout
- `ag_core/`: Core library containing `agents/`, `interfaces/`, `providers/`, `scanner/`, `utils/`, `memory/`.
- `.agents/skills/`: Custom skills containing the CLI entrypoints (`run.py`) and FastAPI endpoints (`api.py`) for all agents (including `security_agent` and `devops_agent`).
- `serve.py`: Unified startup menu.
- `orchestrator.py`: Main asynchronous pipeline runner.
- `.github/workflows/ci.yml`: CI/CD automation workflow.
- `test_*.py`: Test suite (including test_e2e.py, test_db.py, etc.).
