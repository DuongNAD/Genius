---
paths:
  - "config.yaml"
  - "ag_core/config.py"
  - ".env"
  - ".env.*"
---

# Configuration

- **`config.yaml`** — single source for app metadata, model names per provider, scanner exclude patterns, and the service URL map.
- **`.env`** — secrets, loaded by `ag_core/config.py` (walks up the tree to find it): `SKILL_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROK_API_KEY`, `GIT_USERNAME`, `GIT_TOKEN`.
- **`GENIUS_DB_PATH`** — overrides the SQLite path (default `genius.db` at repo root; Docker maps it into the volume).
- **Default provider chains** (no env vars needed; `DEFAULT_CHAINS` in `ag_core/provider_factory.py`): Researcher (role `grok`) → `agy, claude, codex`; Architect (role `claude`) → `claude, agy, codex`; `codex`/`tester`/`security`/`devops` → `codex, claude, agy`. A backend that fails at runtime (`RuntimeError`/CLI timeout) is retried on the next backend, with sticky success within the process. The grok *backend* is in no default chain (opt-in only); the MCP `deploy` tool uses a claude-first default chain (`claude, codex, agy`). All three construction sites (skill servers via `ag_core/skill_app.py`, `mcp_server.py`, distributed worker) build through `make_provider`; `python serve.py --doctor` prints each role's effective chain.
- **`GENIUS_PROVIDER_<ROLE>`** (e.g. `GENIUS_PROVIDER_GROK=grok,agy`) — explicit, comma-separated backend chain for one role (roles: `grok`/`claude`/`codex`/`tester`/`security`/`devops`; backends: `grok`, `claude`, `codex`, `agy`). Overrides the default chain — including bringing the grok backend back. First backend is primary, the rest are runtime fallbacks; unknown names raise an actionable `ValueError`. Blank env values are treated as unset.
- **`GENIUS_PROVIDER_FALLBACK`** — **deprecated no-op**: fallback chains are the default now, so this variable is accepted for backward compatibility but ignored (truthy or falsy makes no difference).
- **`GENIUS_AGY_PATH`** — explicit path to the Antigravity `agy` CLI (backend `agy`, Gemini). Default resolution: PATH via `which_external`, then `%LOCALAPPDATA%\agy\bin\agy.exe`. No API key: auth is shared with the Antigravity IDE login. Model name comes from `config.models.agy` (empty = account default).
- **`GENIUS_AGY_SANDBOX`** — `agy` runs with `--sandbox` by default (print mode requires `--dangerously-skip-permissions`, so the sandbox is the remaining guardrail); set `0`/`false` to drop it. Its `--print-timeout` is derived from `GENIUS_CLI_TIMEOUT` minus 10s (floor 30s).
- **`GENIUS_CODEX_SANDBOX`** — Codex CLI sandbox policy: `read-only` (default — `--sandbox read-only --skip-git-repo-check`, Codex can reason but not execute/write), `workspace-write`, or `danger` (the old `--dangerously-bypass-approvals-and-sandbox`). Legacy `1`/`true`/`yes` and unknown values fail safe to read-only. `OpenAIProvider.send_prompt` also accepts a `workdir` kwarg that adds `--cd <dir>` to scope Codex's working root.
- `config.py` rewrites service URLs (adds `/role` suffixes) when running under pytest — be aware tests and production resolve URLs differently.
