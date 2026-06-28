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
        mock_process.communicate.return_value = (
            json.dumps({
                "result": "Hello from OpenAI CLI!",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5
                }
            }).encode("utf-8"),
            b""
        )
        
        with patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_process
            
            response = await provider.send_prompt("Test prompt", system="You are helpful")
            
            assert response["content"] == "Hello from OpenAI CLI!"
            assert response["usage"]["prompt_tokens"] == 10
            assert response["usage"]["completion_tokens"] == 5
            assert response["usage"]["total_tokens"] == 15
            
            mock_exec.assert_called_once()
            args, kwargs = mock_exec.call_args
            assert args[0] == "/usr/local/bin/claude"
            assert args[1:] == (
                "-p", "Test prompt",
                "--bare",
                "--tools", '""',
                "--output-format", "json",
                "--system-prompt", "You are helpful"
            )
            assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
            assert kwargs["stdout"] == asyncio.subprocess.PIPE
            assert kwargs["stderr"] == asyncio.subprocess.PIPE
            
            mock_process.communicate.assert_called_once_with()
            
    asyncio.run(run_test())

def test_openai_provider_fallback_path():
    async def run_test():
        provider = OpenAIProvider()
        
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (
            json.dumps({
                "result": "Fallback test",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1
                }
            }).encode("utf-8"),
            b""
        )
        
        with patch("shutil.which", return_value=None), \
             patch("os.getenv", side_effect=lambda key: r"C:\Users\FakeUser\AppData\Roaming" if key == "APPDATA" else None), \
             patch("os.path.exists", return_value=True), \
             patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_process
            
            response = await provider.send_prompt("Test prompt")
            
            assert response["content"] == "Fallback test"
            mock_exec.assert_called_once()
            args, kwargs = mock_exec.call_args
            assert args[0] == r"C:\Users\FakeUser\AppData\Roaming\npm\claude.cmd"
            
    asyncio.run(run_test())

def test_openai_provider_invalid_json():
    async def run_test():
        provider = OpenAIProvider()
        
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (
            b"not a valid json",
            b""
        )
        
        with patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
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
            json.dumps({
                "result": "Hello from Claude CLI!",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 8
                }
            }).encode("utf-8"),
            b""
        )
        
        with patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
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
                "-p", "Test prompt",
                "--bare",
                "--tools", '""',
                "--output-format", "json"
            )
            
    asyncio.run(run_test())

def test_grok_provider_success():
    async def run_test():
        provider = GrokProvider()
        
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (
            json.dumps({
                "result": "Hello from Grok CLI!",
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 10
                }
            }).encode("utf-8"),
            b""
        )
        
        with patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_process
            
            response = await provider.send_prompt("Test prompt")
            
            assert response["content"] == "Hello from Grok CLI!"
            assert response["usage"]["prompt_tokens"] == 20
            assert response["usage"]["completion_tokens"] == 10
            assert response["usage"]["total_tokens"] == 30
            
            mock_exec.assert_called_once()
            args, kwargs = mock_exec.call_args
            assert args[0] == "/usr/local/bin/claude"
            assert args[1:] == (
                "-p", "Test prompt",
                "--bare",
                "--tools", '""',
                "--output-format", "json"
            )
            
    asyncio.run(run_test())
