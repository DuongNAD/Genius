"""Real-run simulation: providers exercised against REAL fake-CLI subprocesses.

Every other suite mocks ``asyncio.create_subprocess_exec``; these tests do not.
A temp shim dir is prepended to PATH containing Windows-executable fakes
(``<name>.cmd`` batch wrappers around small Python scripts, plus POSIX shell
wrappers) so ``which_external`` resolves them exactly like a real vendor CLI.
The provider ``send_prompt`` implementations then actually spawn them:

* argv / stdin / --prompt-file plumbing is real (incl. cmd.exe wrapping),
* stdout/stderr parsing sees real pipe bytes (incl. non-UTF-8 garbage),
* the timeout path really tree-kills the shim's python process.

Shim output shapes replicate real captures from the vendor CLIs (codex JSONL
stream, grok error envelope with ANSI stderr noise, claude result envelope).
"""

import os
import subprocess
import sys
import time

import pytest

from ag_core.providers.anthropic_provider import AnthropicProvider
from ag_core.providers.grok_provider import GrokProvider
from ag_core.providers.openai_provider import OpenAIProvider
from ag_core.utils.cli_resolver import which_external
from ag_core.utils.cli_runner import CLITimeoutError

IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# Shim infrastructure
# ---------------------------------------------------------------------------


@pytest.fixture
def shim_dir(tmp_path, monkeypatch):
    """Create a temp dir for fake CLIs and prepend it to PATH."""
    d = tmp_path / "cli_shims"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d) + os.pathsep + os.environ.get("PATH", ""))
    if IS_WINDOWS:
        # which() needs .CMD in PATHEXT to find the batch shims.
        pathext = os.environ.get("PATHEXT", "")
        if ".CMD" not in pathext.upper():
            monkeypatch.setenv("PATHEXT", pathext + os.pathsep + ".CMD")
    return d


def install_shim(shim_dir, name, py_body):
    """Install a fake CLI ``name`` whose behavior is the Python code ``py_body``.

    On Windows this is a ``<name>.cmd`` batch wrapper (so the providers'
    ``cmd.exe /c`` wrapping path is exercised); on POSIX an executable shell
    script. Both invoke the current interpreter on the scripted body.
    """
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
    return wrapper


def _pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
        ).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_shims_resolve_via_which_external(shim_dir):
    """Sanity: the shim (not the repo wrapper, not a real install) wins."""
    install_shim(shim_dir, "grok", "print('hi')\n")
    resolved = which_external("grok")
    assert resolved is not None
    assert os.path.normcase(os.path.dirname(resolved)) == os.path.normcase(
        str(shim_dir)
    )


# ---------------------------------------------------------------------------
# a/b. Codex (OpenAIProvider): stdin prompt -> JSONL stream
# ---------------------------------------------------------------------------

# Replica of a real captured `codex exec - --json` success stream.
CODEX_HAPPY = r"""
import sys, json
prompt = sys.stdin.buffer.read().decode("utf-8")
out = sys.stdout.buffer
def emit(obj):
    out.write((json.dumps(obj) + "\n").encode("utf-8"))
emit({"type": "thread.started", "thread_id": "019f207d-3fa5-7cf2-b45f-a51a3ee3f5e2"})
emit({"type": "turn.started"})
emit({"type": "item.completed",
      "item": {"id": "item_0", "type": "agent_message", "text": "PONG: " + prompt}})
emit({"type": "turn.completed",
      "usage": {"input_tokens": 13054, "cached_input_tokens": 1920,
                "output_tokens": 6, "reasoning_output_tokens": 0}})
"""

CODEX_ERROR = r"""
import sys, json
out = sys.stdout.buffer
def emit(obj):
    out.write((json.dumps(obj) + "\n").encode("utf-8"))
emit({"type": "thread.started", "thread_id": "019f207d-err"})
emit({"type": "turn.started"})
emit({"type": "error", "message": "model overloaded: please retry later"})
emit({"type": "turn.failed"})
"""


@pytest.mark.asyncio
async def test_codex_happy_path_real_subprocess(shim_dir):
    install_shim(shim_dir, "codex", CODEX_HAPPY)
    provider = OpenAIProvider(api_key="test-key")

    res = await provider.send_prompt("ping over stdin")

    assert res["content"] == "PONG: ping over stdin"
    assert res["usage"]["prompt_tokens"] == 13054
    assert res["usage"]["completion_tokens"] == 6
    assert res["usage"]["total_tokens"] == 13060


@pytest.mark.asyncio
async def test_codex_error_event_raises_with_cause(shim_dir):
    install_shim(shim_dir, "codex", CODEX_ERROR)
    provider = OpenAIProvider(api_key="test-key")

    with pytest.raises(RuntimeError) as exc_info:
        await provider.send_prompt("anything")

    assert "model overloaded" in str(exc_info.value)


# ---------------------------------------------------------------------------
# c/d. Grok (GrokProvider): --prompt-file -> JSON envelope
# ---------------------------------------------------------------------------

GROK_HAPPY = r"""
import sys, json
args = sys.argv[1:]
prompt = ""
for i, a in enumerate(args):
    if a == "--prompt-file":
        with open(args[i + 1], "r", encoding="utf-8") as f:
            prompt = f.read()
envelope = {"type": "result", "result": "ECHO: " + prompt,
            "usage": {"input_tokens": 11, "output_tokens": 4}}
sys.stdout.buffer.write((json.dumps(envelope) + "\n").encode("utf-8"))
"""

# Byte-faithful replica of the captured out-of-credits failure: ANSI-colored
# stderr tracing (with an unrelated leading MCP noise line), an error JSON on
# stdout followed by a plain-text trailer, exit code 1.
GROK_403 = r"""
import sys, json
stderr_noise = (
    "\x1b[2m2026-07-02T02:20:11.532Z\x1b[0m \x1b[31mERROR\x1b[0m "
    "failed to spawn MCP stdio process: program not found\n"
    "\x1b[2m2026-07-02T02:20:14.104Z\x1b[0m \x1b[31mERROR\x1b[0m "
    "request failed\n"
)
sys.stderr.buffer.write(stderr_noise.encode("utf-8"))
inner = ('Internal error: {\n  "message": "API error (status 403 Forbidden): '
         'personal-team-blocked:spending-limit: You have run out of credits. '
         'To continue using the API, purchase more credits.",\n'
         '  "http_status": 403\n}')
sys.stdout.buffer.write(
    (json.dumps({"type": "error", "message": inner}) + "\n").encode("utf-8"))
sys.stdout.buffer.write(("Error: " + inner + "\n").encode("utf-8"))
sys.exit(1)
"""


@pytest.mark.asyncio
async def test_grok_happy_path_unicode_roundtrip(shim_dir):
    install_shim(shim_dir, "grok", GROK_HAPPY)
    provider = GrokProvider(api_key="test-key")

    res = await provider.send_prompt("Xin chào thế giới ✓")

    # The prompt went CLI-ward through a real --prompt-file tempfile and came
    # back byte-identical (UTF-8 both ways).
    assert res["content"] == "ECHO: Xin chào thế giới ✓"
    assert res["usage"]["prompt_tokens"] == 11
    assert res["usage"]["completion_tokens"] == 4


@pytest.mark.asyncio
async def test_grok_out_of_credits_replica_raises_actionable_error(shim_dir):
    install_shim(shim_dir, "grok", GROK_403)
    provider = GrokProvider(api_key="test-key")

    with pytest.raises(RuntimeError) as exc_info:
        await provider.send_prompt("any prompt")

    msg = str(exc_info.value)
    # The real cause (403 / out of credits) must survive the noise and be
    # actionable, not a generic "exit code 1".
    assert "403" in msg
    assert "spending-limit" in msg or "run out of credits" in msg
    assert "Hint" in msg
    assert "credits" in msg.lower()


# ---------------------------------------------------------------------------
# e/f/g. Claude (AnthropicProvider): stdin prompt -> result envelope
# ---------------------------------------------------------------------------

CLAUDE_HAPPY = r"""
import sys, json
prompt = sys.stdin.buffer.read().decode("utf-8")
envelope = {
    "type": "result", "subtype": "success", "is_error": False,
    "result": "ANSWER len=%d head=%s" % (len(prompt), prompt[:12]),
    "usage": {"input_tokens": 25, "output_tokens": 7},
}
sys.stdout.buffer.write((json.dumps(envelope) + "\n").encode("utf-8"))
"""

CLAUDE_IS_ERROR = r"""
import sys, json
sys.stdin.buffer.read()
envelope = {"type": "result", "subtype": "error_during_execution",
            "is_error": True,
            "result": "Invalid API key. Please run /login."}
sys.stdout.buffer.write((json.dumps(envelope) + "\n").encode("utf-8"))
"""

CLAUDE_NOISY = r"""
import sys, json
prompt = sys.stdin.buffer.read().decode("utf-8")
out = sys.stdout.buffer
out.write(b"[warn] telemetry endpoint unreachable, continuing offline\n")
out.write(b"Loading workspace...\n")
envelope = {"type": "result", "subtype": "success", "is_error": False,
            "result": "CLEAN: " + prompt,
            "usage": {"input_tokens": 3, "output_tokens": 2}}
out.write((json.dumps(envelope) + "\n").encode("utf-8"))
out.write(b"Session closed. Goodbye!\n")
"""


@pytest.mark.asyncio
async def test_claude_happy_path_prompt_via_stdin(shim_dir):
    install_shim(shim_dir, "claude", CLAUDE_HAPPY)
    provider = AnthropicProvider(api_key="test-key")

    res = await provider.send_prompt("hello claude")

    assert res["content"] == "ANSWER len=12 head=hello claude"
    assert res["usage"]["prompt_tokens"] == 25
    assert res["usage"]["completion_tokens"] == 7


@pytest.mark.asyncio
async def test_claude_prompt_longer_than_cmd_arg_limit(shim_dir):
    """>8191 chars exceeds the cmd.exe command-line limit: only the stdin
    path can deliver it. A real subprocess proves the plumbing."""
    install_shim(shim_dir, "claude", CLAUDE_HAPPY)
    provider = AnthropicProvider(api_key="test-key")
    prompt = "x" * 20000

    res = await provider.send_prompt(prompt)

    assert res["content"] == "ANSWER len=20000 head=xxxxxxxxxxxx"


@pytest.mark.asyncio
async def test_claude_is_error_envelope_raises(shim_dir):
    install_shim(shim_dir, "claude", CLAUDE_IS_ERROR)
    provider = AnthropicProvider(api_key="test-key")

    with pytest.raises(RuntimeError) as exc_info:
        await provider.send_prompt("anything")

    assert "Invalid API key" in str(exc_info.value)


@pytest.mark.asyncio
async def test_claude_banner_noise_around_json_still_parses(shim_dir):
    install_shim(shim_dir, "claude", CLAUDE_NOISY)
    provider = AnthropicProvider(api_key="test-key")

    res = await provider.send_prompt("noisy")

    assert res["content"] == "CLEAN: noisy"


# ---------------------------------------------------------------------------
# h. Timeout: a hanging shim is killed, including its process TREE
# ---------------------------------------------------------------------------

GROK_HANG = r"""
import os, sys, time
with open(os.environ["GENIUS_TEST_SHIM_PIDFILE"], "w") as f:
    f.write(str(os.getpid()))
    f.flush()
    os.fsync(f.fileno())
time.sleep(60)
"""


@pytest.mark.asyncio
async def test_hanging_cli_times_out_and_tree_is_killed(
    shim_dir, tmp_path, monkeypatch
):
    pidfile = tmp_path / "shim.pid"
    monkeypatch.setenv("GENIUS_TEST_SHIM_PIDFILE", str(pidfile))
    monkeypatch.setenv("GENIUS_CLI_TIMEOUT", "5")
    install_shim(shim_dir, "grok", GROK_HANG)
    provider = GrokProvider(api_key="test-key")

    start = time.monotonic()
    with pytest.raises(CLITimeoutError) as exc_info:
        await provider.send_prompt("hang forever")
    elapsed = time.monotonic() - start

    assert elapsed < 12, f"timeout was not enforced promptly ({elapsed:.1f}s)"
    assert "GENIUS_CLI_TIMEOUT" in str(exc_info.value)

    # On Windows the direct child is cmd.exe running the .cmd shim; the python
    # grandchild (whose pid the shim wrote) must be dead too - that is the
    # taskkill /T tree-kill guarantee. Grant a short grace period for the kill
    # to propagate.
    assert pidfile.exists(), "shim never started - test setup is broken"
    shim_pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _pid_alive(shim_pid):
            break
        time.sleep(0.25)
    assert not _pid_alive(shim_pid), (
        f"shim python process {shim_pid} survived the timeout kill "
        f"(process tree was not terminated)"
    )


# ---------------------------------------------------------------------------
# i. Non-UTF-8 bytes on the pipes
# ---------------------------------------------------------------------------

GROK_BINARY_NOISE = r"""
import sys, json
out = sys.stdout.buffer
out.write(b"\x80\xfe\xff binary banner \xc3\x28\n")
envelope = {"type": "result", "result": "ok despite binary noise",
            "usage": {"input_tokens": 1, "output_tokens": 1}}
out.write((json.dumps(envelope) + "\n").encode("utf-8"))
sys.stderr.buffer.write(b"\xff\xfe garbage on stderr too\n")
"""

GROK_PURE_GARBAGE = r"""
import sys
sys.stdout.buffer.write(b"\x00\x80\xfe\xff\xde\xad\xbe\xef no json here\n")
"""


@pytest.mark.asyncio
async def test_non_utf8_noise_before_json_is_tolerated(shim_dir):
    install_shim(shim_dir, "grok", GROK_BINARY_NOISE)
    provider = GrokProvider(api_key="test-key")

    res = await provider.send_prompt("binary noise")

    assert res["content"] == "ok despite binary noise"


@pytest.mark.asyncio
async def test_pure_non_utf8_garbage_raises_cleanly(shim_dir):
    """Undecodable, JSON-free output must raise (never a silent '' success),
    and must not crash with a UnicodeDecodeError."""
    install_shim(shim_dir, "grok", GROK_PURE_GARBAGE)
    provider = GrokProvider(api_key="test-key")

    with pytest.raises(RuntimeError) as exc_info:
        await provider.send_prompt("garbage")

    assert "no result" in str(exc_info.value)
