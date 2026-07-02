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
- **`GENIUS_PROVIDER_<ROLE>`** (e.g. `GENIUS_PROVIDER_GROK=claude,codex`) — explicit, comma-separated backend chain for one role (roles: `grok`/`claude`/`codex`/`tester`/`security`/`devops`; backends: `grok`, `claude`, `codex`). First backend is primary, the rest are runtime fallbacks; unknown names raise an actionable `ValueError`.
- **`GENIUS_PROVIDER_FALLBACK`** (`1`/`true`/`auto`) — enables the default fallback chains for every role (`DEFAULT_CHAINS` in `ag_core/provider_factory.py`): a backend that fails at runtime (`RuntimeError`/CLI timeout, e.g. grok 403 out-of-credits) is retried on the next backend, with sticky success within the process. With neither knob set, each role uses its single legacy backend and gets the raw provider class — bit-identical to pre-fallback behavior. Blank env values are treated as unset. All three construction sites (skill servers via `ag_core/skill_app.py`, `mcp_server.py`, distributed worker) honor these knobs; `python serve.py --doctor` prints each role's effective chain.
- `config.py` rewrites service URLs (adds `/role` suffixes) when running under pytest — be aware tests and production resolve URLs differently.
