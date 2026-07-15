"""Preflight diagnostics - ``python serve.py --doctor`` (``--deep`` opt-in).

Verifies the brittle parts of a real run *before* the pipeline starts, so a
missing Codex desktop install, an unauthenticated agy CLI, or a missing
``SKILL_API_KEY`` surfaces immediately with an actionable message instead of
failing deep inside a stage.

For each vendor CLI it reports one of:

* ``OK``      - resolved to a real executable and ``--version`` ran cleanly.
* ``WARN``    - executable located but ``--version`` failed (some CLIs lack the
  flag; it may still work for ``exec``).
* ``MISSING`` - no real executable found (only a bare literal name).

``--deep`` additionally runs a LIVE one-prompt canary through every unique
(backend, model) pair the effective role chains reference — ``--version``
alone cannot catch an invalid model pin (a real agy upgrade renamed every
model id: the shallow doctor kept saying READY while every agy call failed
and silently burned the fallback chain).
"""

import asyncio
import os
import shutil
import sys
import time

from ag_core import provider_factory
from ag_core.providers.agy_provider import resolve_agy_cli
from ag_core.providers.grok_provider import resolve_grok_cli
from ag_core.providers.anthropic_provider import resolve_claude_cli
from ag_core.providers.openai_provider import resolve_codex_cli
from ag_core.providers.notebooklm_provider import resolve_notebooklm_cli
from ag_core.utils.cli_runner import (
    communicate_with_timeout,
    DEFAULT_AUX_TIMEOUT,
    DEFAULT_CLI_TIMEOUT,
)

# (display name, resolver, agents that depend on this CLI)
CLI_CHECKS = [
    (
        "grok",
        resolve_grok_cli,
        ["optional backend (opt-in via GENIUS_PROVIDER_<ROLE>)"],
    ),
    ("claude", resolve_claude_cli, ["Claude Architect"]),
    (
        "codex",
        resolve_codex_cli,
        ["Codex Reviewer", "Tester", "Security", "DevOps"],
    ),
    ("agy", resolve_agy_cli, ["Researcher (default primary, Antigravity 2.0)"]),
    (
        # Named for the backend ("notebooklm"), not the executable ("nlm"), so
        # the status aligns with the chain entries provider_chain_lines /
        # _dead_roles reason about. The resolver locates the `nlm` binary.
        "notebooklm",
        resolve_notebooklm_cli,
        ["optional NotebookLM backend + MCP notebooklm_* tools (needs nlm login)"],
    ),
]

# Backends whose absence never makes the doctor NOT READY. grok and notebooklm
# are opt-in only (no default chain contains them), and every default chain
# also contains claude + codex, so agy going missing only degrades the
# researcher chain. When a missing optional backend sits in an effective chain,
# a [warn] line is emitted instead (see provider_chain_lines).
OPTIONAL_CLIS = {"agy", "grok", "notebooklm"}


def _is_located(path: str) -> bool:
    """True if ``path`` points at a real, runnable executable."""
    if os.path.isabs(path) and os.path.exists(path):
        return True
    return shutil.which(path) is not None


async def _version_cmd(path: str):
    """Launch ``<path> --version``, wrapping .cmd/.bat via cmd.exe on Windows."""
    cmd = [path, "--version"]
    actual = cmd
    if sys.platform == "win32":
        resolved = shutil.which(path) or path
        if resolved.lower().endswith((".cmd", ".bat")):
            actual = ["cmd.exe", "/c"] + cmd
    try:
        return await asyncio.create_subprocess_exec(
            *actual,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        if sys.platform == "win32" and actual == cmd:
            actual = ["cmd.exe", "/c"] + cmd
            return await asyncio.create_subprocess_exec(
                *actual,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        raise


async def check_cli(name: str, resolver, dependents) -> dict:
    """Resolve a CLI and probe ``--version``; return a structured result."""
    result = {
        "cli": name,
        "dependents": dependents,
        "path": None,
        "status": "MISSING",
        "detail": "",
    }
    try:
        path = resolver()
    except Exception as exc:  # noqa: BLE001 - report, don't crash the doctor
        result["detail"] = f"resolution raised: {exc}"
        return result
    result["path"] = path

    if not _is_located(path):
        result["detail"] = (
            f"no real '{name}' executable found (only the bare name '{path}') - "
            "install it or add it to PATH"
        )
        return result

    try:
        proc = await _version_cmd(path)
    except OSError as exc:
        result["status"] = "WARN"
        result["detail"] = f"located at {path} but could not launch: {exc}"
        return result

    try:
        out, err = await communicate_with_timeout(
            proc, timeout=DEFAULT_AUX_TIMEOUT, cli_name=f"{name} --version"
        )
    except Exception as exc:  # noqa: BLE001
        result["status"] = "WARN"
        result["detail"] = f"located at {path} but version check failed: {exc}"
        return result

    text = (
        out.decode("utf-8", errors="ignore").strip()
        or err.decode("utf-8", errors="ignore").strip()
    )
    first_line = text.splitlines()[0] if text else ""
    if proc.returncode == 0:
        result["status"] = "OK"
        result["detail"] = f"{path} ({first_line})" if first_line else path
    else:
        result["status"] = "WARN"
        result["detail"] = (
            f"located at {path} but `--version` exited {proc.returncode} "
            f"(may still work for exec)"
        )
    return result


async def run_doctor_async() -> list:
    return list(await asyncio.gather(*(check_cli(n, r, d) for n, r, d in CLI_CHECKS)))


def _header_lines():
    """Build the header lines; return (lines, skill_key_present)."""
    lines = ["Genius preflight doctor", "=" * 60]
    skill_key = os.getenv("SKILL_API_KEY")
    if skill_key:
        lines.append("[OK]      SKILL_API_KEY is set (inter-service auth)")
    else:
        lines.append(
            "[MISSING] SKILL_API_KEY is not set - orchestrator <-> skill server "
            "calls will be rejected. Set it in .env (same value both sides)."
        )
    # Display the GENERATIVE default (what a real agent call gets), not the
    # auxiliary --version timeout — the old line said "60+" while LLM calls
    # actually run under a 600s ceiling.
    timeout = (
        os.getenv("GENIUS_CLI_TIMEOUT") or f"{int(DEFAULT_CLI_TIMEOUT)} (default)"
    )
    lines.append(f"[info]    CLI timeout: GENIUS_CLI_TIMEOUT={timeout}")
    lines.append("-" * 60)
    return lines, bool(skill_key)


def _dead_roles(results) -> list:
    """Roles whose ENTIRE effective chain failed to resolve.

    A single-backend override (e.g. GENIUS_PROVIDER_RESEARCHER=agy with agy
    not installed) leaves the role with zero working backends; "optional"
    means safe to lack as a fallback, not safe to be the whole chain.
    """
    statuses = {r["cli"]: r["status"] for r in results}
    dead = []
    for role in provider_factory.DEFAULT_CHAINS:
        try:
            chain = provider_factory.resolve_chain(role)
        except ValueError:
            dead.append(role)
            continue
        # Only judge backends the check run actually covered; an empty
        # intersection means we cannot claim the role is dead.
        known = [b for b in chain if b in statuses]
        if known and all(statuses[b] == "MISSING" for b in known) and known == chain:
            dead.append(role)
    return dead


def provider_chain_lines(results):
    """Render each role's effective provider chain (env-knob aware).

    Pure (reads only env + the supplied check ``results``). Flags a role whose
    PRIMARY backend CLI failed to resolve when a resolvable fallback backend
    exists further down its chain, and errors a role whose whole chain is
    missing.
    """
    lines = ["Provider chains (defaults; override with GENIUS_PROVIDER_<ROLE>=a,b):"]
    statuses = {r["cli"]: r["status"] for r in results}
    dead = set(_dead_roles(results))
    for role in provider_factory.DEFAULT_CHAINS:
        try:
            chain = provider_factory.resolve_chain(role)
        except ValueError as exc:
            lines.append(f"[ERROR]   role {role}: {exc}")
            continue
        source = provider_factory.chain_source(role)
        suffix = f" ({source})" if source else ""
        lines.append(f"[info]    role {role:8} -> {', '.join(chain)}{suffix}")
        if role in dead:
            lines.append(
                f"[ERROR]   role {role}: every backend in its chain is "
                "MISSING - the role cannot run at all"
            )
            continue
        if len(chain) > 1 and statuses.get(chain[0]) == "MISSING":
            alive = next((b for b in chain[1:] if statuses.get(b) != "MISSING"), None)
            if alive:
                lines.append(
                    f"[warn]    {chain[0]} CLI missing; role {role} will "
                    f"fall back to {alive}"
                )
        for backend in chain[1:]:
            if statuses.get(backend) == "MISSING":
                lines.append(
                    f"[warn]    {backend} CLI missing; role {role} cannot "
                    f"fall back to it"
                )
    return lines


def report_lines(results, skill_key_ok: bool):
    """Render the full report and an exit code from check results.

    Pure (no I/O) so it can be unit-tested; returns ``(lines, exit_code)``.
    """
    lines, _ = _header_lines()
    tag = {"OK": "[OK]     ", "WARN": "[WARN]   ", "MISSING": "[MISSING]"}
    for r in results:
        deps = ", ".join(r["dependents"])
        lines.append(f"{tag.get(r['status'], '[?]')} {r['cli']:7} -> {r['detail']}")
        lines.append(f"            used by: {deps}")

    lines.append("-" * 60)
    lines.extend(provider_chain_lines(results))

    lines.append("=" * 60)
    # Optional backends (grok, agy) never fail the doctor: grok is opt-in
    # only, and every default chain still contains claude + codex, so the
    # chain report above already carries a [warn] line when a missing
    # optional backend sits in an effective chain.
    missing = [
        r for r in results if r["status"] == "MISSING" and r["cli"] not in OPTIONAL_CLIS
    ]
    optional_missing = [
        r for r in results if r["status"] == "MISSING" and r["cli"] in OPTIONAL_CLIS
    ]
    dead_roles = _dead_roles(results)
    if missing or dead_roles or not skill_key_ok:
        if dead_roles:
            lines.append(
                "Roles with no working backend: " + ", ".join(sorted(dead_roles))
            )
        lines.append(
            "Result: NOT READY - resolve the items above before running the "
            "real pipeline."
        )
        return lines, 1
    if any(r["status"] == "WARN" for r in results) or optional_missing:
        lines.append(
            "Result: READY (with warnings) - optional/backup items above are "
            "degraded but the pipeline can run."
        )
        return lines, 0
    lines.append("Result: READY - all CLIs resolved and responded.")
    return lines, 0


# ---------------------------------------------------------------------------
# Deep doctor (--deep): live model canaries.
#
# The shallow checks above only prove the CLI binaries exist and answer
# --version — they cannot catch an invalid model pin, a logged-out CLI, or a
# dead account. The deep pass sends one tiny prompt through every unique
# (backend, model) pair the effective role chains reference and reports which
# pairs actually answer, then judges each role by whether ANY backend in its
# chain is alive. Costs one real (cheap) inference per unique pair.
# ---------------------------------------------------------------------------

DEEP_CANARY_PROMPT = "Reply with the single word: pong"
_DEEP_DETAIL_CHARS = 220


def _canary_timeout() -> float:
    """Hard cap per canary call (seconds); GENIUS_DOCTOR_CANARY_TIMEOUT
    overrides, floor-guarded, default 240. The providers self-bound at
    cli_timeout() anyway — this keeps an interactive doctor from sitting on a
    hung CLI for the full 600s generative ceiling. On a cap the CLI child may
    finish (and be reaped) on its own; a five-word canary exits quickly."""
    raw = os.getenv("GENIUS_DOCTOR_CANARY_TIMEOUT")
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return 240.0


def collect_canary_pairs(config):
    """(backend, model) -> {"roles": [...], "primary_for": [...]} across every
    role's effective chain, model resolved EXACTLY as build_backend will."""
    pairs = {}
    role_chains = {}
    for role in provider_factory.DEFAULT_CHAINS:
        try:
            chain = provider_factory.resolve_chain(role)
        except ValueError:
            role_chains[role] = []
            continue
        role_chains[role] = chain
        for idx, backend in enumerate(chain):
            try:
                model = provider_factory.resolve_model(backend, config, role=role)
            except Exception:  # noqa: BLE001 - an unknown backend name
                model = ""
            entry = pairs.setdefault(
                (backend, model), {"roles": [], "primary_for": []}
            )
            if role not in entry["roles"]:
                entry["roles"].append(role)
            if idx == 0:
                entry["primary_for"].append(role)
    return pairs, role_chains


async def _canary_call(backend: str, model: str, role: str, config) -> dict:
    """One live prompt through one backend+model; never raises."""
    result = {
        "backend": backend,
        "model": model,
        "status": "FAIL",
        "detail": "",
        "elapsed": 0.0,
    }
    started = time.monotonic()
    try:
        provider = provider_factory.build_backend(backend, config, role=role)
        response = await asyncio.wait_for(
            provider.send_prompt(DEEP_CANARY_PROMPT), timeout=_canary_timeout()
        )
        content = ((response or {}).get("content") or "").strip()
        result["elapsed"] = time.monotonic() - started
        if content:
            result["status"] = "OK"
            first_line = content.splitlines()[0]
            result["detail"] = first_line[:_DEEP_DETAIL_CHARS]
        else:
            result["detail"] = "empty response"
    except asyncio.TimeoutError:
        result["elapsed"] = time.monotonic() - started
        result["detail"] = (
            f"no answer within {_canary_timeout():.0f}s "
            "(GENIUS_DOCTOR_CANARY_TIMEOUT)"
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as exc:  # noqa: BLE001 - report, don't crash the doctor
        result["elapsed"] = time.monotonic() - started
        detail = str(exc).strip() or exc.__class__.__name__
        result["detail"] = detail[:_DEEP_DETAIL_CHARS]
    return result


async def run_deep_doctor_async() -> dict:
    """Run every unique canary concurrently. Returns
    ``{"pairs": [result+roles...], "role_chains": {role: [backend...]}}``."""
    from ag_core.config import load_config

    config = load_config()
    pairs, role_chains = collect_canary_pairs(config)
    ordered = sorted(pairs)  # deterministic output
    results = await asyncio.gather(
        *(
            _canary_call(backend, model, pairs[(backend, model)]["roles"][0], config)
            for backend, model in ordered
        )
    )
    enriched = []
    for (backend, model), result in zip(ordered, results):
        result["roles"] = pairs[(backend, model)]["roles"]
        result["primary_for"] = pairs[(backend, model)]["primary_for"]
        enriched.append(result)
    return {"pairs": enriched, "role_chains": role_chains}


def deep_report_lines(deep: dict):
    """Render the deep-canary section; pure. Returns ``(lines, exit_code)``.

    Exit 1 only when some role has NO live backend left in its chain — a
    failed primary with a live fallback is degraded (warn), not dead,
    matching the shallow doctor's READY-with-warnings philosophy.
    """
    lines = ["Deep doctor - live model canaries (one prompt per pair):"]
    for r in deep["pairs"]:
        tag = "[OK]     " if r["status"] == "OK" else "[FAIL]   "
        shown_model = r["model"] or "(CLI default)"
        lines.append(
            f"{tag} {r['backend']:7} model {shown_model}: "
            f"{r['detail']} ({r['elapsed']:.1f}s)"
        )
        lines.append(f"            used by roles: {', '.join(r['roles'])}")

    dead_roles = []
    for role, chain in deep["role_chains"].items():
        if not chain:
            dead_roles.append(role)
            lines.append(
                f"[ERROR]   role {role}: chain failed to resolve - cannot canary"
            )
            continue
        alive = None
        for backend in chain:
            ok = any(
                r["status"] == "OK"
                for r in deep["pairs"]
                if r["backend"] == backend and role in r["roles"]
            )
            if ok:
                alive = backend
                break
        if alive is None:
            dead_roles.append(role)
            lines.append(
                f"[ERROR]   role {role}: NO backend in its chain "
                f"({', '.join(chain)}) answered the canary"
            )
        elif alive != chain[0]:
            lines.append(
                f"[warn]    role {role}: primary {chain[0]} failed its canary; "
                f"live traffic will fall back to {alive}"
            )
    if dead_roles:
        lines.append(
            "Deep result: NOT READY - roles with no live backend: "
            + ", ".join(sorted(dead_roles))
        )
        return lines, 1
    lines.append("Deep result: every role has at least one live backend.")
    return lines, 0


async def run_doctor_report_async(deep: bool = False) -> int:
    """Run checks + print the report from inside an existing event loop."""
    _, skill_ok = _header_lines()
    results = await run_doctor_async()
    lines, code = report_lines(results, skill_ok)
    print("\n".join(lines))
    if deep:
        deep_results = await run_deep_doctor_async()
        deep_lines, deep_code = deep_report_lines(deep_results)
        print("\n".join(deep_lines))
        code = max(code, deep_code)
    return code


def run_doctor(deep: bool = False) -> int:
    """Standalone entry point (own event loop). Returns a process exit code."""
    return asyncio.run(run_doctor_report_async(deep=deep))
