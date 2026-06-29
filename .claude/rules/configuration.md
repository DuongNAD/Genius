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
- `config.py` rewrites service URLs (adds `/role` suffixes) when running under pytest — be aware tests and production resolve URLs differently.
