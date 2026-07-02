"""Tests for the defensive layers: CLI timeout/kill, actionable errors,
the preflight doctor, and opt-in degraded-mode pipeline behavior."""

import asyncio
import os

import pytest
from unittest.mock import patch

from ag_core.utils.cli_runner import (
    communicate_with_timeout,
    explain_cli_failure,
    extract_json_object,
    cli_timeout,
    CLITimeoutError,
)
from ag_core import diagnostics
from orchestrator import degraded_mode, resolve_degraded_outcome


class _FakeProc:
    """Minimal stand-in for an asyncio subprocess."""

    def __init__(self, hang=False, result=(b"v", b"")):
        self.killed = False
        self._hang = hang
        self._result = result

    async def communicate(self, input=None):
        if self._hang:
            await asyncio.sleep(30)
        return self._result

    def kill(self):
        self.killed = True

    async def wait(self):
        return 0


# --- cli_runner: timeout + kill ------------------------------------------


def test_communicate_returns_on_success():
    proc = _FakeProc(result=(b"out", b"err"))
    out, err = asyncio.run(communicate_with_timeout(proc, cli_name="x"))
    assert (out, err) == (b"out", b"err")


def test_communicate_kills_process_on_timeout():
    proc = _FakeProc(hang=True)
    with pytest.raises(CLITimeoutError):
        asyncio.run(communicate_with_timeout(proc, timeout=0.05, cli_name="HangCLI"))
    assert proc.killed is True


class _FakeKiller:
    """Stand-in for the taskkill subprocess."""

    def __init__(self, returncode=0):
        self.returncode = returncode

    async def wait(self):
        return self.returncode


def test_timeout_tree_kills_via_taskkill_on_windows():
    # H3: when the direct child is cmd.exe (a .cmd shim), plain kill() orphans
    # the node.exe grandchild - the whole tree must go through taskkill /T /F.
    proc = _FakeProc(hang=True)
    proc.pid = 4242
    taskkill_calls = []

    async def fake_exec(*args, **kwargs):
        taskkill_calls.append(args)
        return _FakeKiller(returncode=0)

    with patch("sys.platform", "win32"), patch(
        "asyncio.create_subprocess_exec", side_effect=fake_exec
    ):
        with pytest.raises(CLITimeoutError):
            asyncio.run(
                communicate_with_timeout(proc, timeout=0.05, cli_name="HangCLI")
            )

    assert taskkill_calls == [("taskkill", "/PID", "4242", "/T", "/F")]
    # taskkill succeeded, so the bare kill() fallback must not fire.
    assert proc.killed is False


def test_timeout_falls_back_to_kill_when_taskkill_fails():
    proc = _FakeProc(hang=True)
    proc.pid = 4242

    async def fake_exec(*args, **kwargs):
        return _FakeKiller(returncode=128)  # taskkill: process not found

    with patch("sys.platform", "win32"), patch(
        "asyncio.create_subprocess_exec", side_effect=fake_exec
    ):
        with pytest.raises(CLITimeoutError):
            asyncio.run(
                communicate_with_timeout(proc, timeout=0.05, cli_name="HangCLI")
            )

    assert proc.killed is True


def test_cli_timeout_env_override():
    with patch.dict(os.environ, {"GENIUS_CLI_TIMEOUT": "12.5"}):
        assert cli_timeout() == 12.5
    # Garbage / non-positive values fall back to the supplied default.
    with patch.dict(os.environ, {"GENIUS_CLI_TIMEOUT": "not-a-number"}):
        assert cli_timeout(99.0) == 99.0
    with patch.dict(os.environ, {"GENIUS_CLI_TIMEOUT": "-5"}):
        assert cli_timeout(99.0) == 99.0


# --- cli_runner: actionable error messages -------------------------------


def test_explain_cli_failure_credit_hint():
    msg = explain_cli_failure("Grok CLI", 1, "personal-team-blocked:spending-limit")
    assert "credits" in msg.lower()
    assert "exit code 1" in msg


def test_explain_cli_failure_auth_hint():
    assert "auth" in explain_cli_failure("Grok CLI", 1, "403 Forbidden").lower()


def test_explain_cli_failure_not_found_hint():
    msg = explain_cli_failure("Codex CLI", 1, "'codex' is not recognized")
    assert "path" in msg.lower()


def test_explain_cli_failure_plain_when_unknown():
    msg = explain_cli_failure("X", 2, "some opaque error")
    assert "Hint" not in msg
    assert "exit code 2" in msg


def test_explain_cli_failure_matches_and_includes_stdout():
    # M3: claude/grok print auth and quota errors to *stdout*; hints must
    # match against it and the message must include both tails.
    msg = explain_cli_failure(
        "Grok CLI",
        1,
        "unrelated log noise",
        '{"type":"error","message":"403 Forbidden: spending-limit reached"}',
    )
    assert "credits" in msg.lower()
    assert "spending-limit" in msg
    assert "unrelated log noise" in msg


# --- cli_runner: noise-tolerant JSON extraction ---------------------------


def test_extract_json_object_plain():
    assert extract_json_object('{"result": "hi"}') == {"result": "hi"}


def test_extract_json_object_tolerates_banners_and_trailers():
    noisy = 'WARN: update available\n{"result": "hi"}\ntrailing log line'
    assert extract_json_object(noisy) == {"result": "hi"}


def test_extract_json_object_grok_error_shape():
    # Real grok out-of-credits output: error JSON line plus a plain-text
    # "Error: {...}" trailer whose braces must not confuse extraction.
    raw = (
        '{"type":"error","message":"Internal error: 403 Forbidden"}\n'
        "Error: {Internal error: 403 Forbidden}"
    )
    parsed = extract_json_object(raw)
    assert parsed is not None
    assert parsed["type"] == "error"


def test_extract_json_object_returns_none_for_garbage():
    assert extract_json_object("") is None
    assert extract_json_object("no json here") is None
    assert extract_json_object("[1, 2, 3]") is None  # not an object


# --- diagnostics: report rendering + exit codes --------------------------


def _r(cli, status):
    return {
        "cli": cli,
        "dependents": ["Agent"],
        "path": f"/{cli}",
        "status": status,
        "detail": "d",
    }


def test_report_missing_cli_is_not_ready():
    lines, code = diagnostics.report_lines(
        [_r("grok", "MISSING"), _r("codex", "OK")], skill_key_ok=True
    )
    assert code == 1
    assert any("NOT READY" in ln for ln in lines)


def test_report_all_ok_is_ready():
    lines, code = diagnostics.report_lines([_r("grok", "OK")], skill_key_ok=True)
    assert code == 0
    assert any("READY" in ln for ln in lines)


def test_report_warn_is_ready_with_warnings():
    lines, code = diagnostics.report_lines([_r("grok", "WARN")], skill_key_ok=True)
    assert code == 0
    assert any("warning" in ln.lower() for ln in lines)


def test_report_missing_skill_key_is_not_ready():
    _, code = diagnostics.report_lines([_r("grok", "OK")], skill_key_ok=False)
    assert code == 1


def test_check_cli_missing_for_unresolvable_binary():
    res = asyncio.run(
        diagnostics.check_cli("nope", lambda: "totally-not-a-real-bin-xyz", ["X"])
    )
    assert res["status"] == "MISSING"


# --- orchestrator: degraded-mode toggle ----------------------------------


def test_degraded_mode_toggle():
    with patch.dict(os.environ, {"GENIUS_DEGRADED_MODE": "yes"}):
        assert degraded_mode() is True
    with patch.dict(os.environ, {"GENIUS_DEGRADED_MODE": "0"}):
        assert degraded_mode() is False


def test_degraded_outcome_partial_failure_summarizes():
    paths = ["a.py", "b.py", "c.py"]
    results = [None, RuntimeError("boom"), None]
    failed, summary = resolve_degraded_outcome(paths, results, "E2E Pipeline")
    assert failed == ["b.py"]
    assert "2/3 files verified" in summary
    assert "b.py" in summary


def test_degraded_outcome_no_failure_returns_none():
    paths = ["a.py", "b.py"]
    failed, summary = resolve_degraded_outcome(paths, [None, None], "E2E Pipeline")
    assert failed == []
    assert summary is None


def test_degraded_outcome_total_failure_reraises_first():
    paths = ["a.py", "b.py"]
    first = RuntimeError("first failure")
    results = [first, RuntimeError("second")]
    with pytest.raises(RuntimeError, match="first failure"):
        resolve_degraded_outcome(paths, results, "E2E Pipeline")
