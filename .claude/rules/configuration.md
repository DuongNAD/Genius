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
- **`GENIUS_PROVIDER_<ROLE>`** (e.g. `GENIUS_PROVIDER_GROK=claude,codex`) — explicit, comma-separated backend chain for one role (roles: `grok`/`claude`/`codex`/`tester`/`security`/`devops`; backends: `grok`, `claude`, `codex`, `agy`). First backend is primary, the rest are runtime fallbacks; unknown names raise an actionable `ValueError`.
- **`GENIUS_PROVIDER_FALLBACK`** (`1`/`true`/`auto`) — enables the default fallback chains for every role (`DEFAULT_CHAINS` in `ag_core/provider_factory.py`): a backend that fails at runtime (`RuntimeError`/CLI timeout, e.g. grok 403 out-of-credits) is retried on the next backend, with sticky success within the process. With neither knob set, each role uses its single legacy backend and gets the raw provider class — bit-identical to pre-fallback behavior (`agy` never participates in legacy mode). Blank env values are treated as unset. All three construction sites (skill servers via `ag_core/skill_app.py`, `mcp_server.py`, distributed worker) honor these knobs; `python serve.py --doctor` prints each role's effective chain.
- **`GENIUS_AGY_PATH`** — explicit path to the Antigravity `agy` CLI (backend `agy`, Gemini). Default resolution: PATH via `which_external`, then `%LOCALAPPDATA%\agy\bin\agy.exe`. No API key: auth is shared with the Antigravity IDE login. Model name comes from `config.models.agy` (empty = account default).
- **`GENIUS_AGY_SANDBOX`** — `agy` runs with `--sandbox` by default (print mode requires `--dangerously-skip-permissions`, so the sandbox is the remaining guardrail); set `0`/`false` to drop it. Its `--print-timeout` is derived from `GENIUS_CLI_TIMEOUT` minus 10s (floor 30s).
- `config.py` rewrites service URLs (adds `/role` suffixes) when running under pytest — be aware tests and production resolve URLs differently.
