import asyncio
import json
import os
import pytest
from unittest.mock import AsyncMock, patch

from ag_core.providers import grok_provider
from ag_core.providers.openai_provider import (
    OpenAIProvider,
    _newest,
    resolve_codex_cli,
)
from ag_core.providers.anthropic_provider import AnthropicProvider, resolve_claude_cli
from ag_core.providers.grok_provider import GrokProvider, resolve_grok_cli
from ag_core.interfaces.base_provider import wait_retry_after


def test_newest_picks_most_recent_codex_and_tolerates_missing(tmp_path):
    # The Codex desktop app can leave several content-addressed bin/<hash> dirs
    # behind after an update; _newest must select the freshest codex.exe.
    old = tmp_path / "old" / "codex.exe"
    new = tmp_path / "new" / "codex.exe"
    old.parent.mkdir()
    new.parent.mkdir()
    old.write_text("")
    new.write_text("")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))

    assert _newest([str(old), str(new)]) == str(new)
    assert _newest([str(new), str(old)]) == str(new)
    # A path that no longer exists must sort last, never raise.
    missing = str(tmp_path / "gone" / "codex.exe")
    assert _newest([missing, str(old)]) == str(old)
    assert _newest([missing]) == missing


def test_openai_provider_success():
    async def run_test():
        provider = OpenAIProvider()

        mock_process = AsyncMock()
        jsonl_output = (
            '{"event": "agent_message", "item": {"text": "Hello from "}}\n'
            '{"event": "agent_message", "item": {"text": "OpenAI CLI!"}}\n'
            '{"event": "turn.completed", "turn.completed": {"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}}\n'
        )
        mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

        fake_path = r"C:\fake_localappdata\OpenAI\Codex\bin\v1.0.0\codex.exe"
        with (
            patch.dict("os.environ", {"LOCALAPPDATA": r"C:\fake_localappdata"}),
            patch("glob.glob", return_value=[fake_path]),
            patch("shutil.which", return_value=None),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process

            response = await provider.send_prompt(
                "Test prompt", system="You are helpful"
            )

            assert response["content"] == "Hello from OpenAI CLI!"
            assert response["usage"]["prompt_tokens"] == 10
            assert response["usage"]["completion_tokens"] == 5
            assert response["usage"]["total_tokens"] == 15

            mock_exec.assert_called_once()
            args, kwargs = mock_exec.call_args
            assert args[0] == fake_path
            assert args[1:] == (
                "exec",
                "-",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--json",
            )
            # Prompt is piped via stdin (the "-" arg), not passed positionally.
            assert kwargs["stdin"] == asyncio.subprocess.PIPE
            assert kwargs["stdout"] == asyncio.subprocess.PIPE
            assert kwargs["stderr"] == asyncio.subprocess.PIPE
            mock_process.communicate.assert_called_once_with(
                input=b"You are helpful\n\nTest prompt"
            )

    asyncio.run(run_test())


def test_openai_provider_codex_cli_format():
    async def run_test():
        provider = OpenAIProvider()

        mock_process = AsyncMock()
        jsonl_output = (
            '{"event": "item.completed", "item": '
            '{"type": "agent_message", "text": "Hello from "}}\n'
            '{"event": "item.completed", "item": '
            '{"type": "agent_message", "text": "Codex CLI!"}}\n'
            '{"event": "turn.completed", "turn.completed": '
            '{"usage": {"input_tokens": 12, "output_tokens": 6, '
            '"total_tokens": 18}}}\n'
        )
        mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

        fake_path = r"C:\fake_localappdata\OpenAI\Codex\bin\v1.0.0\codex.exe"
        env_patch = patch.dict("os.environ", {"LOCALAPPDATA": r"C:\fake_localappdata"})
        with (
            env_patch,
            patch("glob.glob", return_value=[fake_path]),
            patch("shutil.which", return_value=None),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process

            response = await provider.send_prompt(
                "Test prompt", system="You are helpful"
            )

            assert response["content"] == "Hello from Codex CLI!"
            assert response["usage"]["prompt_tokens"] == 12
            assert response["usage"]["completion_tokens"] == 6
            assert response["usage"]["total_tokens"] == 18

            mock_exec.assert_called_once()

    asyncio.run(run_test())


def test_openai_provider_fallback_path():
    async def run_test():
        provider = OpenAIProvider()

        mock_process = AsyncMock()
        jsonl_output = (
            '{"event": "agent_message", "item": {"text": "Fallback test"}}\n'
            '{"event": "turn.completed", "turn.completed": {"usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}\n'
        )
        mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

        # Test fallback to "codex.exe" when all paths are missing
        with (
            patch.dict("os.environ", {}),
            patch("glob.glob", return_value=[]),
            patch("shutil.which", return_value=None),
            patch("os.path.exists", return_value=False),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process

            response = await provider.send_prompt("Test prompt")

            assert response["content"] == "Fallback test"
            mock_exec.assert_called_once()
            args, kwargs = mock_exec.call_args
            assert args[0] == ("codex.exe" if os.name == "nt" else "codex")
            assert args[1:] == (
                "exec",
                "-",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--json",
            )
            assert kwargs["stdin"] == asyncio.subprocess.PIPE
            mock_process.communicate.assert_called_once_with(input=b"Test prompt")

    asyncio.run(run_test())


def _codex_ok_process():
    """AsyncMock process whose stdout is a minimal successful codex JSONL run."""
    mock_process = AsyncMock()
    jsonl_output = (
        '{"event": "agent_message", "item": {"text": "OK"}}\n'
        '{"event": "turn.completed", "turn.completed": {"usage": '
        '{"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}\n'
    )
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")
    return mock_process


async def _codex_invocation_args(env_overrides=None, model_name="", **send_kwargs):
    """Run send_prompt with a mocked subprocess and return the CLI argv tuple."""
    provider = OpenAIProvider(model_name=model_name)
    # Pin GENIUS_CODEX_SANDBOX (empty = unset) so a value in the developer's
    # real environment can't leak into the default-behaviour assertions.
    env = {"LOCALAPPDATA": r"C:\fake_localappdata", "GENIUS_CODEX_SANDBOX": ""}
    env.update(env_overrides or {})
    fake_path = r"C:\fake_localappdata\OpenAI\Codex\bin\v1.0.0\codex.exe"
    with (
        patch.dict("os.environ", env),
        patch("glob.glob", return_value=[fake_path]),
        patch("shutil.which", return_value=None),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
    ):
        mock_exec.return_value = _codex_ok_process()
        await provider.send_prompt("Test prompt", **send_kwargs)
        args, _kwargs = mock_exec.call_args
        return args


def test_openai_provider_default_sandbox_is_read_only():
    # The dangerous bypass must NOT be the default: codex gets a read-only
    # sandbox so it can think but never execute commands or write files.
    args = asyncio.run(_codex_invocation_args())
    assert "--sandbox" in args and "read-only" in args
    assert "--skip-git-repo-check" in args
    assert "--dangerously-bypass-approvals-and-sandbox" not in args


def test_openai_provider_sandbox_danger_env_restores_bypass():
    args = asyncio.run(
        _codex_invocation_args(env_overrides={"GENIUS_CODEX_SANDBOX": "danger"})
    )
    assert "--dangerously-bypass-approvals-and-sandbox" in args
    assert "--sandbox" not in args


def test_openai_provider_sandbox_workspace_write_env():
    args = asyncio.run(
        _codex_invocation_args(
            env_overrides={"GENIUS_CODEX_SANDBOX": "workspace-write"}
        )
    )
    idx = args.index("--sandbox")
    assert args[idx + 1] == "workspace-write"
    assert "--skip-git-repo-check" in args
    assert "--dangerously-bypass-approvals-and-sandbox" not in args


def test_openai_provider_legacy_sandbox_values_map_to_read_only():
    # Old semantics ("1" = keep sandbox on) fail safe to the read-only default.
    for legacy in ("1", "true", "yes", "bogus-value"):
        args = asyncio.run(
            _codex_invocation_args(env_overrides={"GENIUS_CODEX_SANDBOX": legacy})
        )
        idx = args.index("--sandbox")
        assert args[idx + 1] == "read-only", legacy
        assert "--dangerously-bypass-approvals-and-sandbox" not in args


def test_openai_provider_workdir_kwarg_adds_cd():
    args = asyncio.run(_codex_invocation_args(workdir=r"C:\tmp\job1"))
    idx = args.index("--cd")
    assert args[idx + 1] == r"C:\tmp\job1"
    # --json stays last, after the --cd pair.
    assert args[-1] == "--json"


def test_openai_provider_no_workdir_means_no_cd():
    args = asyncio.run(_codex_invocation_args())
    assert "--cd" not in args


def test_openai_provider_passes_model_flag_when_configured():
    args = asyncio.run(_codex_invocation_args(model_name="gpt-5.5"))
    idx = args.index("-m")
    assert args[idx + 1] == "gpt-5.5"


def test_openai_provider_default_model_omits_flag():
    # Empty model = the codex CLI's own default; never inject a -m flag.
    args = asyncio.run(_codex_invocation_args())
    assert "-m" not in args


def test_openai_provider_invalid_json():
    # Unparseable output must raise (never a silent empty "success").
    async def run_test():
        provider = OpenAIProvider()

        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"not a valid json\n", b"boom")

        with (
            patch.dict("os.environ", {}),
            patch("glob.glob", return_value=[]),
            patch("os.path.exists", return_value=False),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process

            with pytest.raises(RuntimeError) as exc_info:
                await provider.send_prompt("Test prompt")
            # The tails of both streams are surfaced for debugging.
            assert "not a valid json" in str(exc_info.value)
            assert "boom" in str(exc_info.value)

    asyncio.run(run_test())


def test_anthropic_provider_success():
    async def run_test():
        provider = AnthropicProvider()

        mock_process = AsyncMock()
        mock_process.communicate.return_value = (
            json.dumps(
                {
                    "result": "Hello from Claude CLI!",
                    "usage": {"input_tokens": 12, "output_tokens": 8},
                }
            ).encode("utf-8"),
            b"",
        )

        with (
            patch("shutil.which", return_value="/usr/local/bin/claude"),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process

            response = await provider.send_prompt("Test prompt")

            assert response["content"] == "Hello from Claude CLI!"
            assert response["usage"]["prompt_tokens"] == 12
            assert response["usage"]["completion_tokens"] == 8
            assert response["usage"]["total_tokens"] == 20

            mock_exec.assert_called_once()
            args, kwargs = mock_exec.call_args
            assert args[0] == "/usr/local/bin/claude"
            # The prompt never appears in argv (cmd.exe metacharacter safety);
            # it is piped via stdin. `--tools` gets a real empty string.
            # No --bare: it skips stored OAuth credentials ("Not logged in").
            assert args[1:] == (
                "-p",
                "--tools",
                "",
                "--output-format",
                "json",
            )
            assert kwargs["stdin"] == asyncio.subprocess.PIPE
            mock_process.communicate.assert_called_once_with(input=b"Test prompt")

    asyncio.run(run_test())


def test_anthropic_provider_passes_model_flag_when_configured():
    async def run_test():
        provider = AnthropicProvider(model_name="claude-sonnet-5")

        mock_process = AsyncMock()
        mock_process.communicate.return_value = (
            json.dumps({"result": "ok", "usage": {}}).encode("utf-8"),
            b"",
        )

        with (
            patch("shutil.which", return_value="/usr/local/bin/claude"),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process
            await provider.send_prompt("Test prompt")

            args, _kwargs = mock_exec.call_args
            arg_list = list(args)
            assert "--model" in arg_list
            assert arg_list[arg_list.index("--model") + 1] == "claude-sonnet-5"

    asyncio.run(run_test())


def test_grok_provider_success():
    async def run_test():
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (
            json.dumps(
                {
                    "result": "Hello from Grok CLI!",
                    "usage": {"input_tokens": 20, "output_tokens": 10},
                }
            ).encode("utf-8"),
            b"",
        )

        captured = {}

        async def fake_exec(*args, **kwargs):
            captured["args"] = args
            # Read the prompt file before send_prompt's finally deletes it.
            idx = args.index("--prompt-file")
            with open(args[idx + 1], "r", encoding="utf-8") as f:
                captured["prompt"] = f.read()
            return mock_process

        with (
            patch("shutil.which", return_value="/usr/local/bin/grok"),
            patch.dict("os.environ", {"GROK_API_KEY": "fake_key"}),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            os.environ.pop("GENIUS_GROK_MODEL", None)
            provider = GrokProvider()
            response = await provider.send_prompt("Test prompt")

            assert response["content"] == "Hello from Grok CLI!"
            assert response["usage"]["prompt_tokens"] == 20
            assert response["usage"]["completion_tokens"] == 10
            assert response["usage"]["total_tokens"] == 30

            # The prompt never appears in argv (cmd.exe metacharacter
            # safety); it is always passed through a temp file.
            args = captured["args"]
            assert args[0] == "/usr/local/bin/grok"
            assert args[1] == "--prompt-file"
            assert args[3:] == ("--output-format", "json")
            assert captured["prompt"] == "Test prompt"

    asyncio.run(run_test())


def test_grok_provider_login_when_no_key():
    async def run_test():
        grok_provider._LOGIN_ATTEMPTED = False

        mock_process = AsyncMock()
        mock_process.communicate.return_value = (
            json.dumps(
                {
                    "result": "Hello from Grok CLI login test!",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
            ).encode("utf-8"),
            b"",
        )

        with (
            patch("shutil.which", return_value="/usr/local/bin/grok"),
            patch.dict("os.environ", {}, clear=True),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process

            provider = GrokProvider(api_key=None)
            response = await provider.send_prompt("Test prompt")

            assert response["content"] == "Hello from Grok CLI login test!"
            assert mock_exec.call_count == 2

            # The first call should be grok login
            first_args, first_kwargs = mock_exec.call_args_list[0]
            assert first_args[0] == "/usr/local/bin/grok"
            assert first_args[1] == "login"

            # The second call should be prompt execution (via prompt file)
            second_args, second_kwargs = mock_exec.call_args_list[1]
            assert second_args[0] == "/usr/local/bin/grok"
            assert second_args[1] == "--prompt-file"
            assert second_args[3:] == ("--output-format", "json")

    asyncio.run(run_test())


def test_grok_provider_login_runs_at_most_once_per_process():
    # M1: `grok login` used to auto-run on EVERY send_prompt without a key.
    async def run_test():
        grok_provider._LOGIN_ATTEMPTED = False

        mock_process = AsyncMock()
        mock_process.communicate.return_value = (
            json.dumps(
                {"result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}
            ).encode("utf-8"),
            b"",
        )

        with (
            patch("shutil.which", return_value="/usr/local/bin/grok"),
            patch.dict("os.environ", {}, clear=True),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process

            provider = GrokProvider(api_key=None)
            await provider.send_prompt("first")
            await provider.send_prompt("second")

            login_calls = [c for c in mock_exec.call_args_list if c.args[1] == "login"]
            assert len(login_calls) == 1
            # login + two prompt executions
            assert mock_exec.call_count == 3

    asyncio.run(run_test())


def test_grok_provider_login_failure_is_logged_not_fatal(caplog):
    async def run_test():
        grok_provider._LOGIN_ATTEMPTED = False

        mock_process = AsyncMock()
        mock_process.communicate.return_value = (
            json.dumps(
                {"result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}
            ).encode("utf-8"),
            b"",
        )

        call_count = {"n": 0}

        async def fake_exec(*args, **kwargs):
            call_count["n"] += 1
            if "login" in args:
                raise OSError("login blew up")
            return mock_process

        with (
            patch("shutil.which", return_value="/usr/local/bin/grok"),
            patch.dict("os.environ", {}, clear=True),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            provider = GrokProvider(api_key=None)
            response = await provider.send_prompt("Test prompt")
            assert response["content"] == "ok"

    import logging

    with caplog.at_level(logging.WARNING, logger="ag_core"):
        asyncio.run(run_test())
    assert any("Grok login attempt failed" in r.message for r in caplog.records)


# --- Silent empty "success" is now a raise (H4 / M2) ----------------------


def test_grok_provider_403_error_on_stdout_raises_actionably():
    # Real failure shape captured on this machine: exit code 1, the error JSON
    # on *stdout* plus a plain-text trailer, ANSI noise on stderr.
    async def run_test():
        mock_process = AsyncMock()
        mock_process.returncode = 1
        stdout = (
            '{"type":"error","message":"Internal error: API error 403 Forbidden: '
            '{\\"error\\":\\"personal-team-blocked:spending-limit\\"}"}\n'
            "Error: {Internal error: API error 403 Forbidden}"
        ).encode("utf-8")
        stderr = b"\x1b[31mINFO unrelated leading noise\x1b[0m"
        mock_process.communicate.return_value = (stdout, stderr)

        with (
            patch("shutil.which", return_value="/usr/local/bin/grok"),
            patch.dict("os.environ", {"GROK_API_KEY": "fake_key"}),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process
            provider = GrokProvider()
            with pytest.raises(RuntimeError) as exc_info:
                await provider.send_prompt("Test prompt")

        msg = str(exc_info.value)
        assert "403" in msg
        assert "spending-limit" in msg
        assert "credits" in msg.lower()  # actionable hint

    asyncio.run(run_test())


def test_grok_provider_exit_zero_error_json_raises():
    # Grok can also report errors with exit code 0; never return "" as success.
    async def run_test():
        mock_process = AsyncMock()
        mock_process.returncode = 0
        stdout = b'{"type":"error","message":"quota exceeded"}'
        mock_process.communicate.return_value = (stdout, b"")

        with (
            patch("shutil.which", return_value="/usr/local/bin/grok"),
            patch.dict("os.environ", {"GROK_API_KEY": "fake_key"}),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process
            provider = GrokProvider()
            with pytest.raises(RuntimeError, match="quota exceeded"):
                await provider.send_prompt("Test prompt")

    asyncio.run(run_test())


def test_grok_provider_plain_text_stdout_used_as_content():
    # Some Grok CLI builds ignore --output-format json and print a plain-text
    # answer on a clean exit; the raw stdout is used as the content instead of
    # being discarded as "no result" (real-world Grok builds do exactly this).
    async def run_test():
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = (b"plain text answer", b"")

        with (
            patch("shutil.which", return_value="/usr/local/bin/grok"),
            patch.dict("os.environ", {"GROK_API_KEY": "fake_key"}),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process
            provider = GrokProvider()
            result = await provider.send_prompt("Test prompt")

        assert result["content"] == "plain text answer"

    asyncio.run(run_test())


def test_grok_provider_text_key_envelope_extracts_answer_only():
    # The current xAI Grok CLI returns {"text": ..., "stopReason": ...,
    # "sessionId": ..., "thought": ...} instead of {"result": ...}. The answer
    # lives in "text"; "thought" (internal reasoning) and the ids must NOT leak
    # into the content.
    async def run_test():
        mock_process = AsyncMock()
        mock_process.returncode = 0
        envelope = json.dumps(
            {
                "text": "Python la ngon ngu lap trinh bac cao.",
                "stopReason": "EndTurn",
                "sessionId": "019f270f-370e-7c11-9139-2c10da3934ce",
                "requestId": "e5630313-b27a-482b-9fbe-580590f6f16a",
                "thought": "The user is asking a simple question in Vietnamese.",
            }
        )

        with (
            patch("shutil.which", return_value="/usr/local/bin/grok"),
            patch.dict("os.environ", {"GROK_API_KEY": "fake_key"}),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process
            mock_process.communicate.return_value = (envelope.encode(), b"")
            provider = GrokProvider()
            result = await provider.send_prompt("Test prompt")

        assert result["content"] == "Python la ngon ngu lap trinh bac cao."
        assert "thought" not in result["content"]
        assert "sessionId" not in result["content"]

    asyncio.run(run_test())


def test_grok_provider_empty_stdout_raises():
    # A clean exit with NO output is still an error — there is nothing to return.
    async def run_test():
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = (b"", b"stderr blob")

        with (
            patch("shutil.which", return_value="/usr/local/bin/grok"),
            patch.dict("os.environ", {"GROK_API_KEY": "fake_key"}),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process
            provider = GrokProvider()
            with pytest.raises(RuntimeError) as exc_info:
                await provider.send_prompt("Test prompt")

        assert "stderr blob" in str(exc_info.value)

    asyncio.run(run_test())


def _grok_invocation_cmd(env_overrides):
    """Run send_prompt with a mocked CLI and return the argv passed to it."""

    async def run():
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = (
            json.dumps({"result": "ok"}).encode(),
            b"",
        )
        with (
            patch("shutil.which", return_value="/usr/local/bin/grok"),
            patch.dict("os.environ", {"GROK_API_KEY": "fake_key"}),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            os.environ.pop("GENIUS_GROK_MODEL", None)
            os.environ.update(env_overrides)
            mock_exec.return_value = mock_process
            provider = GrokProvider()
            await provider.send_prompt("Test prompt")
            args, _kwargs = mock_exec.call_args
            return list(args)

    return asyncio.run(run())


def test_grok_provider_no_model_flag_by_default():
    # With GENIUS_GROK_MODEL unset, no -m flag is added — the Grok CLI uses its
    # own configured default model (grok model ids are install-specific).
    cmd = _grok_invocation_cmd({})
    assert "-m" not in cmd
    assert cmd[1] == "--prompt-file"


def test_grok_provider_model_flag_override_via_env():
    cmd = _grok_invocation_cmd({"GENIUS_GROK_MODEL": "grok-code-fast-1"})
    assert cmd[cmd.index("-m") + 1] == "grok-code-fast-1"


def test_grok_provider_empty_model_env_adds_no_flag():
    # An explicit empty value falls back to the CLI's own default (no -m).
    cmd = _grok_invocation_cmd({"GENIUS_GROK_MODEL": ""})
    assert "-m" not in cmd


def test_grok_provider_json_with_noise_banner_still_parses():
    # Warning banners around the JSON envelope must not break parsing (H4a).
    async def run_test():
        mock_process = AsyncMock()
        mock_process.returncode = 0
        stdout = (
            "WARN: update available\n"
            '{"result": "Hello", "usage": {"input_tokens": 2, "output_tokens": 3}}\n'
            "some trailing log line"
        ).encode("utf-8")
        mock_process.communicate.return_value = (stdout, b"")

        with (
            patch("shutil.which", return_value="/usr/local/bin/grok"),
            patch.dict("os.environ", {"GROK_API_KEY": "fake_key"}),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process
            provider = GrokProvider()
            response = await provider.send_prompt("Test prompt")

        assert response["content"] == "Hello"
        assert response["usage"]["total_tokens"] == 5

    asyncio.run(run_test())


def test_anthropic_provider_is_error_envelope_raises():
    async def run_test():
        provider = AnthropicProvider()
        mock_process = AsyncMock()
        mock_process.returncode = 0
        stdout = json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "API Error: credit balance too low",
            }
        ).encode("utf-8")
        mock_process.communicate.return_value = (stdout, b"")

        with (
            patch("shutil.which", return_value="/usr/local/bin/claude"),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process
            with pytest.raises(RuntimeError, match="credit balance too low"):
                await provider.send_prompt("Test prompt")

    asyncio.run(run_test())


def test_anthropic_provider_unparseable_stdout_raises_with_tails():
    async def run_test():
        provider = AnthropicProvider()
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = (b"garbled banner", b"auth prompt")

        with (
            patch("shutil.which", return_value="/usr/local/bin/claude"),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process
            with pytest.raises(RuntimeError) as exc_info:
                await provider.send_prompt("Test prompt")

        msg = str(exc_info.value)
        assert "garbled banner" in msg
        assert "auth prompt" in msg

    asyncio.run(run_test())


def test_openai_provider_error_events_raise():
    # M2: `error` / `turn.failed` events with no content must raise with the
    # collected error messages, not return "" as a success.
    async def run_test():
        provider = OpenAIProvider()
        mock_process = AsyncMock()
        mock_process.returncode = 0
        jsonl_output = (
            '{"type":"thread.started","thread_id":"t1"}\n'
            '{"type":"turn.started"}\n'
            '{"type":"error","message":"stream disconnected"}\n'
            '{"type":"turn.failed","error":{"message":"turn exploded"}}\n'
        )
        mock_process.communicate.return_value = (
            jsonl_output.encode("utf-8"),
            b"stderr detail",
        )

        with patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = mock_process
            with pytest.raises(RuntimeError) as exc_info:
                await provider.send_prompt("Test prompt")

        msg = str(exc_info.value)
        assert "stream disconnected" in msg
        assert "turn exploded" in msg
        assert "stderr detail" in msg

    asyncio.run(run_test())


def test_openai_provider_real_codex_stream_shape():
    # Real codex success stream captured on this machine.
    async def run_test():
        provider = OpenAIProvider()
        mock_process = AsyncMock()
        mock_process.returncode = 0
        jsonl_output = (
            '{"type":"thread.started","thread_id":"t1"}\n'
            '{"type":"turn.started"}\n'
            '{"type":"item.completed","item":{"id":"item_0",'
            '"type":"agent_message","text":"Real answer"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":7,"output_tokens":3}}\n'
        )
        mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

        with patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = mock_process
            response = await provider.send_prompt("Test prompt")

        assert response["content"] == "Real answer"
        assert response["usage"]["prompt_tokens"] == 7
        assert response["usage"]["completion_tokens"] == 3

    asyncio.run(run_test())


# --- No bare-name CLI fallback (H1) ---------------------------------------


def test_resolve_grok_cli_raises_instead_of_bare_name():
    with (
        patch("shutil.which", return_value=None),
        patch("os.path.exists", return_value=False),
        patch.dict(os.environ, {}, clear=True),
    ):
        with pytest.raises(RuntimeError, match="doctor"):
            resolve_grok_cli()


def test_resolve_claude_cli_raises_instead_of_bare_name():
    with (
        patch("shutil.which", return_value=None),
        patch("os.path.exists", return_value=False),
        patch.dict(os.environ, {}, clear=True),
    ):
        with pytest.raises(RuntimeError, match="doctor"):
            resolve_claude_cli()


def test_resolve_codex_cli_raises_instead_of_bare_name():
    with (
        patch("shutil.which", return_value=None),
        patch("glob.glob", return_value=[]),
        patch("os.path.exists", return_value=False),
        patch.dict(os.environ, {}, clear=True),
    ):
        with pytest.raises(RuntimeError, match="doctor"):
            resolve_codex_cli()


def test_resolvers_keep_benign_literal_under_pytest():
    # Unit tests stub the subprocess layer, so with PYTEST_CURRENT_TEST set
    # (as it is right now) an uninstalled CLI still resolves to a literal.
    with (
        patch("shutil.which", return_value=None),
        patch("glob.glob", return_value=[]),
        patch("os.path.exists", return_value=False),
    ):
        assert resolve_grok_cli() == "grok"
        assert resolve_claude_cli() == "claude"
        assert resolve_codex_cli() in ("codex", "codex.exe")


# --- base_provider: per-loop semaphore + Retry-After cap ------------------


def test_semaphore_recreated_per_event_loop():
    provider = OpenAIProvider()
    seen = []

    async def grab():
        sem = provider.semaphore
        seen.append(sem)
        async with sem:
            # Same loop must reuse the same semaphore.
            assert provider.semaphore is sem

    asyncio.run(grab())
    asyncio.run(grab())
    assert seen[0] is not seen[1]


def test_retry_after_large_value_is_capped_not_raised():
    import httpx

    class MockOutcome:
        failed = True

        def __init__(self, exc):
            self._exc = exc

        def exception(self):
            return self._exc

    class MockRetryState:
        def __init__(self, exc):
            self.outcome = MockOutcome(exc)

    response = httpx.Response(
        status_code=429,
        headers=httpx.Headers({"Retry-After": "30"}),
        request=httpx.Request("GET", "http://test"),
    )
    exc = httpx.HTTPStatusError(
        "429 Too Many Requests", request=response.request, response=response
    )
    waiter = wait_retry_after(lambda state: 1.0)
    # A real 429 with Retry-After: 30 must wait (capped), never crash tenacity.
    assert waiter(MockRetryState(exc)) == 10.0
