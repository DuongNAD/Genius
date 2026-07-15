"""Opt-in production security profile — ``GENIUS_SECURE_DEFAULTS``.

The convenience defaults (no admin key, cwd config walk, plaintext transport)
are correct for the local single-user and trusted-LAN dev cases, so they stay
the DEFAULT. A production operator flips ``GENIUS_SECURE_DEFAULTS=1`` to make
those same conveniences FAIL CLOSED: the process then refuses to boot unless
each one is explicitly overridden with a conscious, auditable env var.

``secure_defaults_violations`` is the pure, unit-tested checker; ``serve.py``
calls it at startup and exits on any violation. Off by default (and thus under
pytest) — the returned list is empty and startup is byte-identical.
"""

import os
from typing import List


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def secure_defaults_enabled() -> bool:
    return _truthy(os.getenv("GENIUS_SECURE_DEFAULTS"))


def secure_defaults_violations(*, distributed: bool) -> List[str]:
    """Return the list of fail-open settings the production profile forbids.

    Empty when the profile is off (the default) — enforcement is a no-op and
    every existing deployment keeps working unchanged. When on:

    - a fixed, trusted config path is mandatory (no cwd-upward walk that an
      untrusted working directory could hijack);
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
    if not (os.getenv("GENIUS_CONFIG_PATH") or "").strip():
        problems.append(
            "GENIUS_CONFIG_PATH must be set: production must load config from a "
            "fixed trusted path, not by walking the current directory."
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
