import pytest
import json
from unittest.mock import AsyncMock, patch
from ag_core.providers.openai_provider import OpenAIProvider


@pytest.mark.asyncio
async def test_openai_provider_empty_output():
    """
    Test empty stdout from CLI execution.
    Should return empty string for content and 0 for tokens, without crashing.
    """
    provider = OpenAIProvider()

    mock_process = AsyncMock()
    mock_process.communicate.return_value = (b"", b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test prompt")

        assert response["content"] == ""
        assert response["usage"]["prompt_tokens"] == 0
        assert response["usage"]["completion_tokens"] == 0
        assert response["usage"]["total_tokens"] == 0


@pytest.mark.asyncio
async def test_openai_provider_long_output_stream():
    """
    Test excessively long output stream (e.g. 50,000 lines).
    Should parse successfully and handle without high memory/cpu explosion or crashing.
    """
    provider = OpenAIProvider()

    # Generate 50,000 lines of agent_message and token usage at the end
    lines = []
    for i in range(50000):
        lines.append(
            json.dumps({"event": "agent_message", "item": {"text": f"token_{i} "}})
        )
    lines.append(
        json.dumps(
            {
                "event": "turn.completed",
                "turn.completed": {
                    "usage": {"input_tokens": 100, "output_tokens": 50000}
                },
            }
        )
    )

    jsonl_output = "\n".join(lines)
    mock_process = AsyncMock()
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test prompt")

        # Verify content starts correctly and has correct token count
        assert response["content"].startswith("token_0 token_1 ")
        assert response["usage"]["prompt_tokens"] == 100
        assert response["usage"]["completion_tokens"] == 50000
        assert response["usage"]["total_tokens"] == 50100


@pytest.mark.asyncio
async def test_openai_provider_nested_json():
    """
    Test extremely nested JSON structures.
    We must ensure OpenAIProvider handles this gracefully without crashing the whole application.
    """
    provider = OpenAIProvider()

    # Create an extremely nested structure of depth 2000.
    nested_str = "{" * 2000 + "}" * 2000

    # Mixed with a normal valid message at the end
    jsonl_output = (
        nested_str
        + "\n"
        + '{"event": "agent_message", "item": {"text": "Recovered from nested JSON"}}\n'
        + '{"event": "turn.completed", "turn.completed": {"usage": {"input_tokens": 5, "output_tokens": 5}}}\n'
    )

    mock_process = AsyncMock()
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test prompt")

        # It should ignore the deeply nested invalid json line and correctly parse the subsequent lines
        assert response["content"] == "Recovered from nested JSON"
        assert response["usage"]["prompt_tokens"] == 5
        assert response["usage"]["completion_tokens"] == 5


@pytest.mark.asyncio
async def test_openai_provider_malformed_jsonl():
    """
    Test malformed JSONL outputs (e.g. mixed valid/invalid, bad types, missing fields).
    Should ignore bad JSON blocks, parse valid tokens, and return what it can.
    """
    provider = OpenAIProvider()

    jsonl_output = (
        '{"event": "agent_message", "item": {"text": "Part 1 "}}\n'
        "invalid json line here\n"
        '{"event": "agent_message", "item": "not a dict"}\n'
        '{"event": "agent_message", "item": {"text": null}}\n'
        "[1, 2, 3]\n"
        '{"event": "agent_message", "item": {"text": "Part 2"}}\n'
        '{"event": "turn.completed", "turn.completed": {"usage": {"input_tokens": "invalid_int", "output_tokens": 10}}}\n'
    )

    mock_process = AsyncMock()
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test prompt")

        # Should parse "Part 1 " and "Part 2", and ignore the rest
        assert response["content"] == "Part 1 Part 2"
        # Since prompt_tokens was "invalid_int", it should fallback to 0 or try to parse
        assert response["usage"]["prompt_tokens"] == 0
        assert response["usage"]["completion_tokens"] == 10
        assert response["usage"]["total_tokens"] == 10
