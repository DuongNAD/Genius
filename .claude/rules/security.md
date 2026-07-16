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

## Distributed hub credentials & transport

- The shared `SKILL_API_KEY` is the **worker/client** credential (register, heartbeat, dispatch, task_status, report_result, deregister, workers).
- **`GENIUS_HUB_ADMIN_KEY`** (optional second credential) — when set, the hub's state-mutating/dumping endpoints `/update_config`, `/tasks`, `/write_workspace_file` (`ADMIN_ENDPOINTS` in `ag_core/distributed/hub.py`) ALSO require it in an `X-Admin-Key` header (constant-time compare, layered on top of the normal auth + checksum + replay checks; missing/wrong → 403). Unset = legacy single-credential behavior, byte-identical (pinned by `tests/test_distributed.py`; the gate by `tests/test_hub_admin_auth.py`).
- **`/write_workspace_file` is disabled outside pytest** unless `GENIUS_HUB_WORKSPACE_WRITE=1` — no production caller uses it. The write root is pinned via `GENIUS_HUB_WORKSPACE_ROOT` (default: hub cwd); the resolved parent is re-verified immediately before an `O_NOFOLLOW` open to narrow the realpath-check→open TOCTOU window.
- **WS worker auth**: workers send the JWT as `Authorization: Bearer <jwt>` (serve.py's `/ws/connect` still accepts legacy `?token=`; `GENIUS_WS_TOKEN_QUERY=1` forces the old query form for pre-header hubs). `GENIUS_HUB_TLS=1` makes workers dial `wss://`. `serve.py --distributed` warns at startup when the hub binds beyond loopback — the transport is plaintext; use a VPN/trusted LAN or a TLS-terminating proxy.
- Dashboard/control-panel browser tokens are still accepted via `?token=` for the FIRST navigation only as a UX affordance — the page immediately moves the token into sessionStorage and scrubs it from the URL/history (`history.replaceState`); requests then carry it in `X-Dashboard-Token`/`X-Panel-Token` headers. Both pages set `<meta name="referrer" content="no-referrer">` and escape all server-derived strings before `innerHTML` (`esc()`/`escapeHtml()`).

## Git credentials

- `GIT_TOKEN` never rides git's argv. `GitManager` (`ag_core/utils/git.py`) hands push/pull/clone a **credential-stripped** URL plus `-c credential.helper=` (resets system helpers so osxkeychain/manager-core can't answer with a different identity) and points `GIT_ASKPASS` at a generated sh script containing **no secrets** — the username/token travel only in the subprocess env (`GENIUS_GIT_ASKPASS_USERNAME`/`_PASSWORD`). One POSIX script covers all platforms (git-for-Windows runs askpass through its bundled sh). Token-only mirrors the old PAT-as-username URL form. Non-http(s) remotes (ssh, local paths) are untouched. Tests: `tests/test_git_askpass.py`.
