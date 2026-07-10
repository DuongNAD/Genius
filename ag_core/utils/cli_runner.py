"""Resilient subprocess helpers shared by the provider CLI integrations.

The providers shell out to local vendor CLIs (grok/claude/codex). Failure
modes guarded here:

* A CLI that hangs (waiting for input, a stuck network call, a hidden login
  prompt) froze ``await process.communicate()`` - and therefore the whole
  pipeline - forever. :func:`communicate_with_timeout` bounds every call and
  kills the process when it overruns. On Windows the direct child is often
  ``cmd.exe`` (wrapping a ``.cmd`` shim), so the whole process *tree* is
  killed via ``taskkill /T /F`` - a plain ``kill()`` would orphan the real
  node/exe grandchild.
* A non-zero exit surfaced the raw stderr with no guidance.
  :func:`explain_cli_failure` recognises the common signatures (missing CLI,
  auth/login, out-of-credits) in *both* streams - several CLIs print their
  errors to stdout - and appends an actionable hint.
* CLIs wrap their JSON result in warning banners / log noise.
  :func:`extract_json_object` tolerates that instead of silently yielding
  an empty "success".
"""

import asyncio
import json
import os
import signal
import sys

# LLM agent CLIs (notably ``codex exec``) can legitimately run for minutes, so
# the default ceiling is generous; tune with GENIUS_CLI_TIMEOUT (seconds).
DEFAULT_CLI_TIMEOUT = 600.0
# Auxiliary, non-generative calls (login, --version) should be quick.
DEFAULT_AUX_TIMEOUT = 60.0
# How much of a CLI's stdout/stderr to embed in raised error messages.
TAIL_CHARS = 2000


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


# Verification subprocesses (flake8 / pytest on generated or reviewed code)
# must also be bounded: LLM-generated code can contain an infinite loop or a
# blocking call (input(), socket.accept()), which would otherwise hang the
# self-healing loop — and, via the MCP review tool, the whole server — forever.
DEFAULT_TEST_TIMEOUT = 300.0


def test_timeout(default: float = DEFAULT_TEST_TIMEOUT) -> float:
    """Resolve the verification-subprocess timeout from ``GENIUS_TEST_TIMEOUT``.

    Separate from :func:`cli_timeout` (which bounds the LLM CLIs): a generated
    test suite should finish in seconds, so the default ceiling is tighter.
    """
    raw = os.getenv("GENIUS_TEST_TIMEOUT")
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return default


def tail_text(text: str, limit: int = TAIL_CHARS) -> str:
    """Return the last ``limit`` characters of ``text`` (for error messages)."""
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


def extract_json_object(text: str):
    """Best-effort extraction of a single JSON object from noisy CLI output.

    Vendor CLIs wrap their JSON result in warning banners, ANSI log lines or
    plain-text trailers. Tries, in order: the whole text, the span from the
    first ``{`` to the last ``}``, then each individual line that looks like
    an object. Returns the parsed dict, or ``None`` when nothing parses.
    """
    text = (text or "").strip()
    if not text:
        return None
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            candidates.append(line)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, RecursionError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def wrap_windows_cmd(cmd: list, cli_path: str) -> list:
    """Wrap ``.cmd``/``.bat`` shims with ``cmd.exe /c`` on Windows.

    Decided from the already-resolved path — never via a raw ``shutil.which``
    on a bare name, which searches the cwd first and would re-introduce the
    repo-wrapper recursion.
    """
    if sys.platform == "win32" and cli_path.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c"] + cmd
    return cmd


async def spawn_cli(
    cmd: list,
    cli_path: str,
    *,
    stdin=asyncio.subprocess.PIPE,
    cwd: str | None = None,
    env: dict | None = None,
):
    """Spawn a resolved vendor CLI, absorbing the Windows shim quirks once.

    ``.cmd``/``.bat`` shims are wrapped with ``cmd.exe /c`` up front (from
    ``cli_path``); if an unwrapped spawn still fails with ``OSError`` on
    Windows (an extensionless shim CreateProcess cannot launch), it is retried
    once through ``cmd.exe /c``. stdout/stderr are always PIPEd; callers pick
    ``stdin`` (PIPE to feed the prompt, DEVNULL for file-fed CLIs).

    ``env`` overrides the child's environment (``None`` = inherit the parent's
    unchanged, the default for every CLI); callers pass a scoped dict to hide
    specific vars from one subprocess (e.g. codex login mode strips the proxy
    OPENAI_* vars) without touching the process-wide environment.
    """
    actual_cmd = wrap_windows_cmd(cmd, cli_path)
    # start_new_session makes the CLI its own process-group/session leader
    # (POSIX setsid; ignored on Windows, where the tree is killed via
    # taskkill /T). This lets _terminate kill the WHOLE group so grandchildren
    # the CLI spawned die with it instead of being orphaned.
    try:
        return await asyncio.create_subprocess_exec(
            *actual_cmd,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except OSError:
        if sys.platform == "win32" and actual_cmd[0] != "cmd.exe":
            return await asyncio.create_subprocess_exec(
                *(["cmd.exe", "/c"] + cmd),
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
        raise


async def _terminate(process) -> None:
    """Best-effort kill + reap so a timed-out CLI leaves no zombie.

    On Windows the direct child may be ``cmd.exe`` running a ``.cmd`` shim;
    ``process.kill()`` would leave the real node/exe grandchild running, so
    the whole tree is killed with ``taskkill /PID <pid> /T /F`` first, falling
    back to a plain ``kill()`` if taskkill is unavailable or fails.
    """
    killed = False
    pid = getattr(process, "pid", None)
    if sys.platform == "win32" and isinstance(pid, int):
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            killed = (await killer.wait()) == 0
        except Exception:
            killed = False
    if not killed:
        if sys.platform != "win32" and isinstance(pid, int):
            # Kill the whole process GROUP so grandchildren the CLI spawned die
            # with it — but ONLY when the child is its own group leader (it was
            # spawned with start_new_session=True, so pgid == pid). Never signal
            # a group we might share with the orchestrator itself.
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGKILL)
                else:
                    process.kill()
            except (ProcessLookupError, OSError):
                return
        else:
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
    :class:`CLITimeoutError` (after terminating the process tree) on timeout.
    """
    timeout = timeout if timeout is not None else cli_timeout()
    # Call communicate() with no argument when there's nothing to send on stdin,
    # so callers (and test doubles) that define a zero-arg communicate() keep
    # working; only pass input= when we actually have bytes to write. For a real
    # subprocess communicate(input=None) and communicate() are equivalent.
    if input is not None:
        comm = process.communicate(input=input)
    else:
        comm = process.communicate()
    try:
        return await asyncio.wait_for(comm, timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate(process)
        raise CLITimeoutError(
            f"{cli_name} timed out after {timeout:.0f}s and was terminated. "
            f"Increase GENIUS_CLI_TIMEOUT if this CLI legitimately needs longer."
        )
    except BaseException:
        # External cancellation (the orchestrator dropping this call, a task
        # group tearing down) must not orphan the running CLI. Kill it before
        # propagating. process.kill() inside _terminate fires synchronously, so
        # the child dies even if the subsequent wait() is itself cancelled.
        await _terminate(process)
        raise


def explain_cli_failure(
    cli_name: str, returncode, stderr: str, stdout: str = ""
) -> str:
    """Build an actionable error message for a non-zero CLI exit.

    Hints are matched against both streams because several CLIs (claude, grok)
    print auth / quota errors to *stdout*; both tails are included in the
    message so the real failure is never hidden.
    """
    stderr = tail_text(stderr)
    stdout = tail_text(stdout)
    low = f"{stderr}\n{stdout}".lower()
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
    msg = f"{cli_name} failed with exit code {returncode}: {stderr}"
    if stdout:
        msg += f" | stdout: {stdout}"
    return msg + hint
