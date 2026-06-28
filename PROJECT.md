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

## Code Layout
- `ag_core/`: Core library containing `agents/`, `interfaces/`, `providers/`, `scanner/`, `utils/`, `memory/`.
- `.agents/skills/`: Custom skills containing the CLI entrypoints (`run.py`) and FastAPI endpoints (`api.py`) for all agents (including `security_agent` and `devops_agent`).
- `serve.py`: Unified startup menu.
- `orchestrator.py`: Main asynchronous pipeline runner.
- `.github/workflows/ci.yml`: CI/CD automation workflow.
- `test_*.py`: Test suite (including test_e2e.py, test_db.py, etc.).
