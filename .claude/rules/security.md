---
paths:
  - "ag_core/utils/security.py"
  - "ag_core/utils/jwt.py"
  - "conftest.py"
  - "serve.py"
  - ".agents/skills/**"
---

# Security model

Inter-service calls are authenticated with a JWT in `X-API-Key`/`Authorization` and integrity-checked with an `X-Payload-SHA256` HMAC header. The secret is `SKILL_API_KEY`. Production code (`ag_core/utils/security.py`) is **HMAC-only — no plain SHA-256 fallback**. `conftest.py` monkeypatches `verify_checksum`/`verify_raw_body_checksum` to additionally accept plain SHA-256 so legacy tests pass; keep the production path HMAC-only and do not "fix" it to match the test patch.
