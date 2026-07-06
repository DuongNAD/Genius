"""Preflight diagnostics - ``python serve.py --doctor``.

Verifies the brittle parts of a real run *before* the pipeline starts, so a
missing Codex desktop install, an unauthenticated agy CLI, or a missing
``SKILL_API_KEY`` surfaces immediately with an actionable message instead of
failing deep inside a stage.

For each vendor CLI it reports one of:

* ``OK``      - resolved to a real executable and ``--version`` ran cleanly.
* ``WARN``    - executable located but ``--version`` failed (some CLIs lack the
  flag; it may still work for ``exec``).
* ``MISSING`` - no real executable found (only a bare literal name).
"""

import asyncio
import os
import shutil
import sys

from ag_core import provider_factory
from ag_core.providers.agy_provider import resolve_agy_cli
from ag_core.providers.grok_provider import resolve_grok_cli
from ag_core.providers.anthropic_provider import resolve_claude_cli
from ag_core.providers.openai_provider import resolve_codex_cli
from ag_core.providers.notebooklm_provider import resolve_notebooklm_cli
from ag_core.utils.cli_runner import communicate_with_timeout, DEFAULT_AUX_TIMEOUT

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
    timeout = (
        os.getenv("GENIUS_CLI_TIMEOUT") or f"{int(DEFAULT_AUX_TIMEOUT)}+ (default)"
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


async def run_doctor_report_async() -> int:
    """Run checks + print the report from inside an existing event loop."""
    _, skill_ok = _header_lines()
    results = await run_doctor_async()
    lines, code = report_lines(results, skill_ok)
    print("\n".join(lines))
    return code


def run_doctor() -> int:
    """Standalone entry point (own event loop). Returns a process exit code."""
    return asyncio.run(run_doctor_report_async())
