import pytest
import asyncio
import json
from unittest.mock import AsyncMock, patch
from ag_core.providers.openai_provider import OpenAIProvider

@pytest.mark.asyncio
async def test_openai_provider_empty_output():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    mock_process.communicate.return_value = (b"", b"")
    
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test empty")
        assert response["content"] == ""
        assert response["usage"]["prompt_tokens"] == 0
        assert response["usage"]["completion_tokens"] == 0
        assert response["usage"]["total_tokens"] == 0

@pytest.mark.asyncio
async def test_openai_provider_malformed_jsonl():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    
    # Mix of valid lines, malformed JSON, and JSON that are not objects
    malformed_output = (
        'invalid json here\n'
        '{"event": "agent_message", "item": {"text": "Valid part 1"}}\n'
        '[1, 2, 3]\n'
        '{"event": "agent_message", "item": {"text": "Valid part 2"}}\n'
        'null\n'
        '12345\n'
    )
    mock_process.communicate.return_value = (malformed_output.encode("utf-8"), b"")
    
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test malformed")
        assert response["content"] == "Valid part 1Valid part 2"

@pytest.mark.asyncio
async def test_openai_provider_excessively_long_output():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    
    # 10,000 lines of output to verify handling of larger streams
    lines = []
    for i in range(10000):
        lines.append(json.dumps({"event": "agent_message", "item": {"text": f"part_{i} "}}))
    lines.append(json.dumps({"event": "turn.completed", "input_tokens": 100, "output_tokens": 200}))
    long_output = "\n".join(lines) + "\n"
    
    mock_process.communicate.return_value = (long_output.encode("utf-8"), b"")
    
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test long")
        assert response["usage"]["prompt_tokens"] == 100
        assert response["usage"]["completion_tokens"] == 200
        assert response["usage"]["total_tokens"] == 300
        # Verify content starts and ends correctly
        assert response["content"].startswith("part_0 ")
        assert response["content"].endswith("part_9999 ")

@pytest.mark.asyncio
async def test_openai_provider_extremely_nested_json():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    
    # Deeply nested structure
    nested = {"event": "agent_message", "item": {"text": "Nested"}}
    for _ in range(200):
        nested = {"nested": nested}
        
    mock_process.communicate.return_value = (json.dumps(nested).encode("utf-8"), b"")
    
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test nested")
        assert response["content"] == ""

@pytest.mark.asyncio
async def test_openai_provider_agent_message_crash_str():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    # "agent_message" key exists but is a string instead of dict
    crash_line = '{"agent_message": "not a dict"}\n'
    mock_process.communicate.return_value = (crash_line.encode("utf-8"), b"")
    
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test crash str")
        assert response["content"] == ""

@pytest.mark.asyncio
async def test_openai_provider_agent_message_crash_none():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    # "agent_message" key exists but is None
    crash_line = '{"agent_message": null}\n'
    mock_process.communicate.return_value = (crash_line.encode("utf-8"), b"")
    
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test crash null")
        assert response["content"] == ""

@pytest.mark.asyncio
async def test_openai_provider_turn_completed_type_error():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    # "input_tokens" is a dict/list instead of int/str, causing TypeError in int()
    crash_line = '{"event": "turn.completed", "input_tokens": {}}\n'
    mock_process.communicate.return_value = (crash_line.encode("utf-8"), b"")
    
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test turn completed type error")
        assert response["usage"]["prompt_tokens"] == 0

@pytest.mark.asyncio
async def test_openai_provider_recursion_error():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    
    # Deeply nested list/dict string that triggers RecursionError in json.loads
    deep_json = "[" * 2000 + "]" * 2000 + "\n"
    mock_process.communicate.return_value = (deep_json.encode("utf-8"), b"")
    
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test recursion error")
        assert response["content"] == ""

@pytest.mark.asyncio
async def test_openai_provider_non_zero_exit_code():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    mock_process.returncode = 1
    mock_process.communicate.return_value = (b"", b"Some CLI error output")
    
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        with pytest.raises(RuntimeError) as exc_info:
            await provider.send_prompt("Test exit code")
        assert "Codex CLI failed with exit code 1: Some CLI error output" in str(exc_info.value)
