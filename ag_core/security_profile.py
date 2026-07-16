"""Opt-in production security profile — ``GENIUS_SECURE_DEFAULTS``.

The convenience defaults (no admin key, cwd config/.env walk, plaintext
transport) are correct for the local single-user and trusted-LAN dev cases,
so they stay the DEFAULT. A production operator flips
``GENIUS_SECURE_DEFAULTS=1`` to make those same conveniences FAIL CLOSED: the
process then refuses to boot unless each one is explicitly overridden with a
conscious, auditable env var.

``secure_defaults_violations`` is the pure, unit-tested checker;
``enforce_secure_defaults`` is the shared startup gate that EVERY entrypoint
calls before serving anything (serve.py, mcp_server.py — stdio and HTTP —,
the orchestrator CLI, dashboard.py, control_panel.py). Off by default (and
thus under pytest) — the returned list is empty and startup is
byte-identical.

Trust bootstrap: the profile can only be as trustworthy as the environment it
reads. ``ag_core.config`` therefore refuses the cwd-upward ``.env`` walk
while the profile is on (an explicitly pinned ``GENIUS_ENV_FILE`` is still
honored — the operator vouched for that file), and the checker additionally
verifies that ``GENIUS_SECURE_DEFAULTS`` itself came from the real process
environment (the pre-dotenv snapshot ``ag_core.config._original_env``) rather
than from a loaded ``.env`` — the profile must never bootstrap trust from the
very input it exists to distrust.
"""

import os
from typing import List, Mapping, Optional


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def secure_defaults_enabled() -> bool:
    return _truthy(os.getenv("GENIUS_SECURE_DEFAULTS"))


def _baseline_env() -> Mapping[str, str]:
    # Lazy import: ag_core.config imports this module (for the secure-mode
    # dotenv gate), so the reverse import must stay at call time.
    from ag_core import config

    return config._original_env


def secure_defaults_violations(
    *, distributed: bool, original_env: Optional[Mapping[str, str]] = None
) -> List[str]:
    """Return the list of fail-open settings the production profile forbids.

    Empty when the profile is off (the default) — enforcement is a no-op and
    every existing deployment keeps working unchanged. When on:

    - ``GENIUS_SECURE_DEFAULTS`` itself must come from the real process
      environment (``original_env``, default: the pre-dotenv snapshot in
      ``ag_core.config._original_env``) — a workspace ``.env`` must not be
      able to define the trust boundary it is subject to;
    - a fixed, trusted config path is mandatory (no cwd-upward walk that an
      untrusted working directory could hijack) and must be an absolute path
      to an existing file — a relative pin still resolves against the
      untrusted cwd, and a missing pin would silently fall back to defaults;
    - ``GENIUS_ENV_FILE``, when set, must likewise be an absolute path to an
      existing file;
    - running the hub additionally requires a separate admin credential (so
      the shared worker key cannot administer the hub) and an explicit
      transport decision — either TLS fronts the hub (``GENIUS_HUB_TLS=1``) or
      the operator knowingly accepts plaintext (``GENIUS_ALLOW_PLAINTEXT=1``).
      The transport gate is an explicit assertion rather than a bind-address
      check because a container legitimately binds ``0.0.0.0`` while its port
      is published only on loopback.
    """
    if not secure_defaults_enabled():
        return []

    problems: List[str] = []

    baseline = original_env if original_env is not None else _baseline_env()
    if os.environ.get("GENIUS_SECURE_DEFAULTS") != baseline.get(
        "GENIUS_SECURE_DEFAULTS"
    ):
        problems.append(
            "GENIUS_SECURE_DEFAULTS was not set in the real process "
            "environment (it appeared after startup, e.g. from a loaded "
            ".env) — the profile must not bootstrap trust from the very "
            "input it distrusts; export it from the shell / service manager "
            "/ container env instead."
        )

    config_path = (os.getenv("GENIUS_CONFIG_PATH") or "").strip()
    if not config_path:
        problems.append(
            "GENIUS_CONFIG_PATH must be set: production must load config from a "
            "fixed trusted path, not by walking the current directory."
        )
    elif not os.path.isabs(config_path):
        problems.append(
            f"GENIUS_CONFIG_PATH must be an absolute path (got {config_path!r}): "
            "a relative pin still resolves against an untrusted working "
            "directory."
        )
    elif not os.path.isfile(config_path):
        problems.append(
            f"GENIUS_CONFIG_PATH points to a missing file: {config_path!r} — "
            "a missing pin would silently fall back to built-in defaults."
        )

    env_file = (os.getenv("GENIUS_ENV_FILE") or "").strip()
    if env_file:
        if not os.path.isabs(env_file):
            problems.append(
                f"GENIUS_ENV_FILE must be an absolute path (got {env_file!r}): "
                "a relative pin still resolves against an untrusted working "
                "directory."
            )
        elif not os.path.isfile(env_file):
            problems.append(
                f"GENIUS_ENV_FILE points to a missing file: {env_file!r} — "
                "no secrets would load and the run would limp along "
                "misconfigured."
            )

    if distributed:
        if not (os.getenv("GENIUS_HUB_ADMIN_KEY") or "").strip():
            problems.append(
                "GENIUS_HUB_ADMIN_KEY must be set: the shared worker key must "
                "not also be able to administer the hub."
            )
        if not (
            _truthy(os.getenv("GENIUS_HUB_TLS"))
            or _truthy(os.getenv("GENIUS_ALLOW_PLAINTEXT"))
        ):
            problems.append(
                "the hub transport is plaintext HTTP/ws (shared secret, JWTs "
                "and every prompt/result are readable on the wire); set "
                "GENIUS_HUB_TLS=1 to assert a TLS proxy fronts it, or "
                "GENIUS_ALLOW_PLAINTEXT=1 to knowingly accept plaintext."
            )
    return problems


def enforce_secure_defaults(*, distributed: bool) -> None:
    """Shared startup gate: refuse to boot on any profile violation.

    A no-op while the profile is off. Raises ``SystemExit`` with every
    violation and its fix so a misconfigured production start is one readable
    message, not a stack trace. On the MCP stdio transport the message goes
    to stderr (SystemExit's default), keeping stdout pure JSON-RPC.
    """
    problems = secure_defaults_violations(distributed=distributed)
    if problems:
        raise SystemExit(
            "Refusing to start — GENIUS_SECURE_DEFAULTS is on and these "
            "fail-open settings are not overridden:\n- " + "\n- ".join(problems)
        )
