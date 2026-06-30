"""Resilient subprocess helpers shared by the provider CLI integrations.

The providers shell out to local vendor CLIs (grok/claude/codex). Two failure
modes were previously unguarded:

* A CLI that hangs (waiting for input, a stuck network call, a hidden login
  prompt) froze ``await process.communicate()`` - and therefore the whole
  pipeline - forever. :func:`communicate_with_timeout` bounds every call and
  kills the process when it overruns.
* A non-zero exit surfaced the raw stderr with no guidance.
  :func:`explain_cli_failure` recognises the common signatures (missing CLI,
  auth/login, out-of-credits) and appends an actionable hint.
"""

import asyncio
import os

# LLM agent CLIs (notably ``codex exec``) can legitimately run for minutes, so
# the default ceiling is generous; tune with GENIUS_CLI_TIMEOUT (seconds).
DEFAULT_CLI_TIMEOUT = 600.0
# Auxiliary, non-generative calls (login, --version) should be quick.
DEFAULT_AUX_TIMEOUT = 60.0


class CLITimeoutError(RuntimeError):
    """Raised when a CLI subprocess overruns its timeout and is killed."""


def cli_timeout(default: float = DEFAULT_CLI_TIMEOUT) -> float:
    """Resolve the CLI timeout (seconds) from ``GENIUS_CLI_TIMEOUT`` or default."""
    raw = os.getenv("GENIUS_CLI_TIMEOUT")
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return default


async def _terminate(process) -> None:
    """Best-effort kill + reap so a timed-out CLI leaves no zombie."""
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        return
    try:
        await process.wait()
    except Exception:
        pass


async def communicate_with_timeout(
    process,
    *,
    input: bytes | None = None,
    timeout: float | None = None,
    cli_name: str = "CLI",
):
    """``process.communicate`` bounded by ``timeout``; kills the CLI on overrun.

    Returns the ``(stdout, stderr)`` tuple on success, raises
    :class:`CLITimeoutError` (after terminating the process) on timeout.
    """
    timeout = timeout if timeout is not None else cli_timeout()
    try:
        return await asyncio.wait_for(process.communicate(input=input), timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate(process)
        raise CLITimeoutError(
            f"{cli_name} timed out after {timeout:.0f}s and was terminated. "
            f"Increase GENIUS_CLI_TIMEOUT if this CLI legitimately needs longer."
        )


def explain_cli_failure(cli_name: str, returncode, stderr: str) -> str:
    """Build an actionable error message for a non-zero CLI exit."""
    stderr = (stderr or "").strip()
    low = stderr.lower()
    hint = ""
    if any(
        k in low
        for k in ("spending-limit", "out of credit", "insufficient", "quota", "billing")
    ):
        hint = (
            " | Hint: the account is out of credits/quota - top up or check the plan."
        )
    elif any(
        k in low for k in ("login", "unauthorized", "forbidden", " 401", " 403", "auth")
    ):
        hint = f" | Hint: authentication failed - re-run the {cli_name} login flow."
    elif any(
        k in low
        for k in (
            "not recognized",
            "not found",
            "no such file",
            "cannot find",
            "is not a valid",
        )
    ):
        hint = (
            f" | Hint: the {cli_name} executable was not found or is not runnable - "
            f"install it or put it on PATH (try `python serve.py --doctor`)."
        )
    return f"{cli_name} failed with exit code {returncode}: {stderr}{hint}"
