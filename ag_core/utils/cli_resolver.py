"""Resolve the *real* vendor CLI for a provider, never the bundled wrapper.

The Genius repo root ships convenience wrappers named exactly like the vendor
CLIs (``grok.cmd``/``grok``, ``claude.cmd``/``claude``, ``codex.cmd``/``codex``)
so external tools can invoke the agents as if they were those CLIs. Those
wrappers must never be picked up by the *providers* themselves.

On Windows ``shutil.which()`` searches the current working directory first, and
the documented way to run Genius is from the repo root. So a naive
``which("grok")`` resolves the repo wrapper instead of the real ``grok.exe``.
The wrapper then re-enters the agent (wrapper -> run.py -> agent -> provider ->
wrapper), which is both an infinite recursion / fork bomb and, when ``python``
is not on PATH, an immediate "Python interpreter not found" failure.

``which_external`` keeps the patch-friendly ``shutil.which`` call (tests stub it)
but discards any match that lives inside the repo root, re-scanning PATH with the
repo excluded so the genuine vendor CLI wins.
"""

import functools
import os
import shutil

from ag_core.runtime import under_pytest

# ag_core/utils/cli_resolver.py -> repo root is three levels up.
_REPO_ROOT = os.path.normcase(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
)

# fn qualname -> resolved CLI path, for memoize_cli_path below.
_CLI_PATH_CACHE: dict = {}


def memoize_cli_path(fn):
    """Cache a ``resolve_*_cli()`` result for the life of the process.

    Without this, every ``send_prompt`` re-walks PATH (and, for codex,
    re-globs install dirs with per-file mtime checks) — pure blocking disk
    work on the event loop. Only successful resolutions are cached, so a
    missing CLI keeps raising and an install mid-process is picked up on the
    next call. Bypassed under pytest: the resolver tests patch
    ``shutil.which``/``glob.glob``/``os.path.exists`` and expect every call to
    re-resolve.
    """

    @functools.wraps(fn)
    def wrapper():
        if under_pytest():
            return fn()
        key = f"{fn.__module__}.{fn.__qualname__}"
        if key not in _CLI_PATH_CACHE:
            _CLI_PATH_CACHE[key] = fn()
        return _CLI_PATH_CACHE[key]

    return wrapper


def _within(path: str, directory: str) -> bool:
    """True if ``path`` is ``directory`` itself or lives underneath it.

    Case-insensitive and drive-aware via ``normcase``; returns False rather than
    raising when the paths sit on different drives.
    """
    try:
        p = os.path.normcase(os.path.abspath(path))
    except (ValueError, OSError):
        return False
    return p == directory or p.startswith(directory + os.sep)


def _scan_path_excluding(name: str, skip_dirs):
    """Reimplement a PATH scan that never injects the current directory.

    Unlike ``shutil.which``, which prepends ``os.curdir`` on Windows, this walks
    only the real PATH entries and skips any directory in ``skip_dirs`` (the repo
    root), so the bundled wrappers are invisible.
    """
    path_entries = (os.environ.get("PATH") or os.defpath).split(os.pathsep)

    if os.name == "nt":
        pathext = [e for e in (os.environ.get("PATHEXT") or "").split(os.pathsep) if e]
        if not pathext:
            pathext = [".COM", ".EXE", ".BAT", ".CMD"]
        _, ext = os.path.splitext(name)
        candidates = [name] if ext else [name + e for e in pathext]
    else:
        candidates = [name]

    skip = [os.path.normcase(os.path.abspath(d)) for d in skip_dirs]
    seen = set()
    for entry in path_entries:
        if not entry:
            continue
        norm = os.path.normcase(os.path.abspath(entry))
        if norm in seen:
            continue
        seen.add(norm)
        if any(norm == s or norm.startswith(s + os.sep) for s in skip):
            continue
        for cand in candidates:
            full = os.path.join(entry, cand)
            if os.path.isfile(full):
                return full
    return None


def which_external(name: str):
    """Like ``shutil.which(name)`` but never returns a Genius bundled wrapper.

    If the first match lands inside the repo root (the wrapper that shadows the
    real CLI on Windows, where ``which`` searches the cwd first), PATH is
    re-scanned with the repo excluded so the genuine vendor CLI is returned.
    Returns ``None`` when no external CLI is found, letting the caller fall back
    to its known install-location candidates.
    """
    found = shutil.which(name)
    if found and _within(found, _REPO_ROOT):
        found = _scan_path_excluding(name, [_REPO_ROOT])
    return found
