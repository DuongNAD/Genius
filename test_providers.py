import pytest
import asyncio
import json
import shutil
from unittest.mock import AsyncMock, patch
from ag_core.providers.openai_provider import OpenAIProvider
from ag_core.providers.anthropic_provider import AnthropicProvider
from ag_core.providers.grok_provider import GrokProvider


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
                "--dangerously-bypass-approvals-and-sandbox",
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
            assert args[0] == "codex.exe"
            assert args[1:] == (
                "exec",
                "-",
                "--dangerously-bypass-approvals-and-sandbox",
                "--json",
            )
            assert kwargs["stdin"] == asyncio.subprocess.PIPE
            mock_process.communicate.assert_called_once_with(input=b"Test prompt")

    asyncio.run(run_test())


def test_openai_provider_invalid_json():
    async def run_test():
        provider = OpenAIProvider()

        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"not a valid json\n", b"")

        with (
            patch.dict("os.environ", {}),
            patch("glob.glob", return_value=[]),
            patch("os.path.exists", return_value=False),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
        ):
            mock_exec.return_value = mock_process

            response = await provider.send_prompt("Test prompt")

            assert response["content"] == ""
            assert response["usage"]["prompt_tokens"] == 0
            assert response["usage"]["completion_tokens"] == 0
            assert response["usage"]["total_tokens"] == 0

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
            assert args[1:] == (
                "-p",
                "Test prompt",
                "--bare",
                "--tools",
                '""',
                "--output-format",
                "json",
            )

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

            assert response["content"] == "Hello from Grok CLI!"
            assert response["usage"]["prompt_tokens"] == 20
            assert response["usage"]["completion_tokens"] == 10
            assert response["usage"]["total_tokens"] == 30

            mock_exec.assert_called_once()
            args, kwargs = mock_exec.call_args
            assert args[0] == "/usr/local/bin/grok"
            assert args[1:] == ("-p", "Test prompt", "--output-format", "json")

    asyncio.run(run_test())


def test_grok_provider_login_when_no_key():
    async def run_test():
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

            # The second call should be prompt execution
            second_args, second_kwargs = mock_exec.call_args_list[1]
            assert second_args[0] == "/usr/local/bin/grok"
            assert second_args[1:] == ("-p", "Test prompt", "--output-format", "json")

    asyncio.run(run_test())
