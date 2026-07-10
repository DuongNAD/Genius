"""Tests for the Antigravity 2.0 provider (ag_core/providers/agy_provider.py):
CLI resolution precedence, invocation shape (stdin prompt, required flags,
--print-timeout derived from GENIUS_CLI_TIMEOUT, sandbox toggle, --model),
failure handling, real-shim subprocess roundtrips, and the doctor wiring."""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

from ag_core import diagnostics
from ag_core.providers.agy_provider import (
    AgyProvider,
    _print_timeout_seconds,
    _sandbox_enabled,
    resolve_agy_cli,
)

IS_WINDOWS = sys.platform == "win32"

_AGY_ENV_VARS = (
    "GENIUS_AGY_PATH",
    "GENIUS_AGY_SANDBOX",
    "GENIUS_CLI_TIMEOUT",
    "GENIUS_PROVIDER_FALLBACK",
    "GENIUS_PROVIDER_GROK",
)


@pytest.fixture(autouse=True)
def _clean_agy_env(monkeypatch):
    """Start every test from the no-knobs-set default."""
    for var in _AGY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _mock_process(stdout=b"", stderr=b"", returncode=0):
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


# --- CLI resolution ---------------------------------------------------------


def test_resolve_env_path_override_wins(monkeypatch):
    monkeypatch.setenv("GENIUS_AGY_PATH", r"D:\tools\agy.exe")
    with patch("shutil.which", return_value=r"C:\elsewhere\agy.exe"):
        assert resolve_agy_cli() == r"D:\tools\agy.exe"


def test_resolve_blank_env_path_treated_as_unset(monkeypatch):
    # A blank GENIUS_AGY_PATH shipped in .env.example must fall through to
    # the PATH scan, not be returned as the (empty) executable. Use an
    # absolute path outside the repo root for the current platform, or
    # which_external treats the relative Windows literal as an in-repo
    # wrapper (POSIX abspath) and re-scans it away.
    real_cli = r"C:\real\agy.exe" if os.name == "nt" else "/opt/real/agy"
    monkeypatch.setenv("GENIUS_AGY_PATH", "   ")
    with patch("shutil.which", return_value=real_cli):
        assert resolve_agy_cli() == real_cli


def test_resolve_falls_back_to_localappdata_default(monkeypatch):
    fake = os.path.join(r"C:\fake_localappdata", "agy", "bin", "agy.exe")
    with (
        patch("shutil.which", return_value=None),
        patch.dict(os.environ, {"LOCALAPPDATA": r"C:\fake_localappdata"}),
        patch("os.path.exists", side_effect=lambda p: p == fake),
    ):
        assert resolve_agy_cli() == fake


def test_resolve_keeps_benign_literal_under_pytest():
    # PYTEST_CURRENT_TEST is set right now: an uninstalled agy still resolves
    # to a harmless literal (unit tests stub the subprocess layer).
    with (
        patch("shutil.which", return_value=None),
        patch("os.path.exists", return_value=False),
    ):
        assert resolve_agy_cli() == "agy"


def test_resolve_raises_actionably_outside_pytest():
    with (
        patch("shutil.which", return_value=None),
        patch("os.path.exists", return_value=False),
        patch.dict(os.environ, {}, clear=True),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            resolve_agy_cli()
    msg = str(exc_info.value)
    assert "GENIUS_AGY_PATH" in msg
    assert "doctor" in msg


# --- knob helpers ------------------------------------------------------------


def test_print_timeout_derived_from_cli_timeout(monkeypatch):
    assert _print_timeout_seconds() == 590  # default 600s budget - 10s margin
    monkeypatch.setenv("GENIUS_CLI_TIMEOUT", "120")
    assert _print_timeout_seconds() == 110
    monkeypatch.setenv("GENIUS_CLI_TIMEOUT", "15")
    assert _print_timeout_seconds() == 30  # floor


def test_sandbox_default_on_and_opt_out(monkeypatch):
    assert _sandbox_enabled() is True
    for off in ("0", "false", "no", "off"):
        monkeypatch.setenv("GENIUS_AGY_SANDBOX", off)
        assert _sandbox_enabled() is False
    monkeypatch.setenv("GENIUS_AGY_SANDBOX", "1")
    assert _sandbox_enabled() is True
    monkeypatch.setenv("GENIUS_AGY_SANDBOX", "")  # blank = unset = default
    assert _sandbox_enabled() is True


# --- invocation shape (mocked subprocess) ------------------------------------


def test_agy_provider_success_invocation_shape(monkeypatch):
    async def run_test():
        monkeypatch.setenv("GENIUS_CLI_TIMEOUT", "120")
        provider = AgyProvider()
        mock_process = _mock_process(stdout=b"Hello from Agy!\n")

        with (
            patch("shutil.which", return_value="/usr/local/bin/agy"),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process

            response = await provider.send_prompt("Test prompt")

            assert response["content"] == "Hello from Agy!"
            # agy reports no token usage in print mode.
            assert response["usage"]["total_tokens"] == 0

            mock_exec.assert_called_once()
            args, kwargs = mock_exec.call_args
            assert args[0] == "/usr/local/bin/agy"
            # No --model (empty model_name = account default); sandbox is on
            # by default; --print-timeout is GENIUS_CLI_TIMEOUT - 10. The
            # prompt is the VALUE of --print (agy ignores stdin), passed last
            # in the =-joined form.
            assert args[1:] == (
                "--dangerously-skip-permissions",
                "--print-timeout",
                "110s",
                "--sandbox",
                "--print=Test prompt",
            )
            # Prompt travels in argv as the --print value; stdin is sent empty.
            assert kwargs["stdin"] == asyncio.subprocess.PIPE
            mock_process.communicate.assert_called_once_with(input=b"")

    asyncio.run(run_test())


def test_agy_provider_model_flag_only_when_set():
    async def run_test():
        provider = AgyProvider(model_name="gemini-3-pro")
        mock_process = _mock_process(stdout=b"ok")

        with (
            patch("shutil.which", return_value="/usr/local/bin/agy"),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process
            await provider.send_prompt("Test prompt")

            args, _ = mock_exec.call_args
            assert "--model" in args
            assert args[args.index("--model") + 1] == "gemini-3-pro"
            # The prompt is always the last arg, as the --print value.
            assert args[-1] == "--print=Test prompt"

    asyncio.run(run_test())


def test_agy_provider_sandbox_opt_out(monkeypatch):
    async def run_test():
        monkeypatch.setenv("GENIUS_AGY_SANDBOX", "0")
        provider = AgyProvider()
        mock_process = _mock_process(stdout=b"ok")

        with (
            patch("shutil.which", return_value="/usr/local/bin/agy"),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process
            await provider.send_prompt("Test prompt")

            args, _ = mock_exec.call_args
            assert "--sandbox" not in args

    asyncio.run(run_test())


def test_agy_provider_system_prompt_is_prepended():
    # agy has no system-prompt flag: the system text is prepended into the
    # prompt (the --print value) with explicit section markers.
    async def run_test():
        provider = AgyProvider()
        mock_process = _mock_process(stdout=b"ok")

        with (
            patch("shutil.which", return_value="/usr/local/bin/agy"),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process
            await provider.send_prompt("Test prompt", system="Be terse")

            args, _ = mock_exec.call_args
            assert args[-1] == (
                "--print=[System instructions]\nBe terse\n\n[Task]\nTest prompt"
            )
            # The prompt travels in argv now, not stdin.
            mock_process.communicate.assert_called_once_with(input=b"")

    asyncio.run(run_test())


def test_agy_provider_empty_stdout_raises_never_empty_success():
    async def run_test():
        provider = AgyProvider()
        mock_process = _mock_process(stdout=b"  \n", stderr=b"quota warning blob")

        with (
            patch("shutil.which", return_value="/usr/local/bin/agy"),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process
            with pytest.raises(RuntimeError) as exc_info:
                await provider.send_prompt("Test prompt")

        msg = str(exc_info.value)
        assert "no output" in msg
        assert "quota warning blob" in msg  # stderr tail surfaced

    asyncio.run(run_test())


def test_agy_provider_nonzero_exit_raises_actionably():
    async def run_test():
        provider = AgyProvider()
        mock_process = _mock_process(
            stdout=b"",
            stderr=b"agy: request failed: 401 unauthorized - run agy login",
            returncode=1,
        )

        with (
            patch("shutil.which", return_value="/usr/local/bin/agy"),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process
            with pytest.raises(RuntimeError) as exc_info:
                await provider.send_prompt("Test prompt")

        msg = str(exc_info.value)
        assert "Agy CLI failed with exit code 1" in msg
        assert "unauthorized" in msg
        assert "Hint" in msg and "authentication" in msg

    asyncio.run(run_test())


# --- real-shim integration (same pattern as tests/test_realrun_simulation.py)


@pytest.fixture
def shim_dir(tmp_path, monkeypatch):
    """Temp dir of fake vendor CLIs, prepended to PATH."""
    d = tmp_path / "cli_shims"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d) + os.pathsep + os.environ.get("PATH", ""))
    if IS_WINDOWS:
        pathext = os.environ.get("PATHEXT", "")
        if ".CMD" not in pathext.upper():
            monkeypatch.setenv("PATHEXT", pathext + os.pathsep + ".CMD")
    return d


def _install_shim(shim_dir, name, py_body):
    script = shim_dir / f"{name}_shim_impl.py"
    script.write_text(py_body, encoding="utf-8")
    if IS_WINDOWS:
        wrapper = shim_dir / f"{name}.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="ascii"
        )
    else:
        wrapper = shim_dir / name
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="ascii"
        )
        wrapper.chmod(0o755)


# Echo shim: plain text on stdout (agy's real output shape), plus the argv it
# received so the flag plumbing through cmd.exe is verifiable end-to-end. The
# prompt arrives as the --print=<value> argv element (agy ignores stdin).
AGY_ECHO = r"""
import sys
prompt = ""
for a in sys.argv[1:]:
    if a.startswith("--print="):
        prompt = a[len("--print="):]
out = "AGY: " + prompt + "\nARGS: " + " ".join(sys.argv[1:])
sys.stdout.buffer.write(out.encode("utf-8"))
"""

AGY_ERROR = r"""
import sys
sys.stdin.buffer.read()
sys.stderr.buffer.write(
    b"agy: request failed: 401 unauthorized - run `agy` and log in\n")
sys.exit(1)
"""


@pytest.mark.asyncio
async def test_agy_shim_roundtrip_unicode_and_flags(shim_dir):
    _install_shim(shim_dir, "agy", AGY_ECHO)
    provider = AgyProvider()

    res = await provider.send_prompt("Xin chào Antigravity ✓")

    # The prompt went CLI-ward as the --print= argv value and came back
    # byte-identical (UTF-8 both ways, through the cmd.exe /c wrapping).
    assert res["content"].startswith("AGY: Xin chào Antigravity ✓")
    assert "--print" in res["content"]
    assert "--dangerously-skip-permissions" in res["content"]
    assert "--print-timeout" in res["content"]
    assert "--sandbox" in res["content"]
    assert res["usage"]["total_tokens"] == 0


@pytest.mark.asyncio
async def test_agy_shim_failure_raises_actionable_error(shim_dir):
    _install_shim(shim_dir, "agy", AGY_ERROR)
    provider = AgyProvider()

    with pytest.raises(RuntimeError) as exc_info:
        await provider.send_prompt("any prompt")

    msg = str(exc_info.value)
    assert "exit code 1" in msg
    assert "unauthorized" in msg
    assert "Hint" in msg


# --- doctor wiring -----------------------------------------------------------


def _r(cli, status):
    return {
        "cli": cli,
        "dependents": ["Agent"],
        "path": f"/{cli}",
        "status": status,
        "detail": "d",
    }


_BASE_OK = [_r("grok", "OK"), _r("claude", "OK"), _r("codex", "OK")]


def test_doctor_probe_table_includes_agy():
    entry = next((c for c in diagnostics.CLI_CHECKS if c[0] == "agy"), None)
    assert entry is not None
    assert entry[1] is resolve_agy_cli
    assert any("Antigravity" in dep for dep in entry[2])


def test_doctor_missing_agy_is_not_fatal():
    # agy is an optional backend: every default chain still contains
    # claude + codex, so a missing agy degrades chains but must not flip the
    # doctor to NOT READY.
    results = _BASE_OK + [_r("agy", "MISSING")]
    lines, code = diagnostics.report_lines(results, skill_key_ok=True)
    text = "\n".join(lines)
    assert code == 0
    assert "NOT READY" not in text
    assert any("agy" in ln and "[MISSING]" in ln for ln in lines)


def test_doctor_warns_when_default_chain_references_missing_agy():
    # The default researcher chain is agy-first: a missing agy must produce a
    # [warn] naming the surviving fallback backend.
    results = _BASE_OK + [_r("agy", "MISSING")]
    lines, code = diagnostics.report_lines(results, skill_key_ok=True)
    assert code == 0  # degraded, not fatal
    assert any(
        "[warn]" in ln and "agy CLI missing" in ln and "fall back" in ln for ln in lines
    )


def test_doctor_missing_grok_is_not_fatal():
    # grok is opt-in only (in no default chain): its absence never makes the
    # doctor NOT READY.
    results = [_r("grok", "MISSING"), _r("claude", "OK"), _r("codex", "OK")]
    lines, code = diagnostics.report_lines(results, skill_key_ok=True)
    assert code == 0
    assert "NOT READY" not in "\n".join(lines)


def test_doctor_missing_required_cli_still_fatal():
    # The optional-backend carve-out must not soften claude/codex.
    results = [_r("grok", "OK"), _r("claude", "OK"), _r("codex", "MISSING")]
    _, code = diagnostics.report_lines(results, skill_key_ok=True)
    assert code == 1


# --- optional live smoke test (never runs in CI) -----------------------------

_REAL_AGY = r"C:\Users\Admin\AppData\Local\agy\bin\agy.exe"


@pytest.mark.skipif(
    not os.path.exists(_REAL_AGY) or os.environ.get("GENIUS_LIVE_AGY") != "1",
    reason="live agy smoke test: needs the real agy.exe and GENIUS_LIVE_AGY=1",
)
@pytest.mark.asyncio
async def test_live_agy_smoke(monkeypatch):
    monkeypatch.setenv("GENIUS_AGY_PATH", _REAL_AGY)
    monkeypatch.setenv("GENIUS_CLI_TIMEOUT", "90")
    provider = AgyProvider()

    res = await provider.send_prompt("Reply with exactly the word: PONG")

    assert "PONG" in res["content"].upper()
