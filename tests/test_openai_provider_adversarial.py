import pytest
import asyncio
import json
from unittest.mock import AsyncMock, patch
from ag_core.providers.openai_provider import OpenAIProvider


@pytest.mark.asyncio
async def test_openai_provider_blank_and_noise_lines():
    """
    Test empty lines, purely whitespace lines, and random non-JSON CLI noise.
    Ensure they are silently skipped and the rest of the stream is parsed.
    """
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    mock_process.returncode = 0

    noise_output = (
        "\n"
        "   \n"
        "\t\n"
        "DEBUG: starting execution...\n"
        "ERROR: connection retrying\n"
        '{"event": "agent_message", "item": {"text": "Valid Part"}}\n'
        "Some random trailing stdout log\n"
    )
    mock_process.communicate.return_value = (noise_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test noise")
        assert response["content"] == "Valid Part"
        assert response["usage"]["prompt_tokens"] == 0
        assert response["usage"]["completion_tokens"] == 0


@pytest.mark.asyncio
async def test_openai_provider_non_dict_json_lines():
    """
    Test JSON lines that are valid JSON but not objects (e.g. lists, numbers, booleans, null).
    They should be skipped without throwing errors.
    """
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    mock_process.returncode = 0

    json_lines = (
        "null\n"
        "true\n"
        "false\n"
        "123.45\n"
        '"string_value"\n'
        "[1, 2, 3]\n"
        '{"event": "agent_message", "item": {"text": "Actual message"}}\n'
    )
    mock_process.communicate.return_value = (json_lines.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test non-dict JSON")
        assert response["content"] == "Actual message"


@pytest.mark.asyncio
async def test_openai_provider_agent_message_adversarial_structures():
    """
    Test various strange and malformed agent_message structures, verifying
    that the parser is robust and extracts what it can or skips safely.
    """
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    mock_process.returncode = 0

    lines = [
        # event is agent_message, but item is missing
        '{"event": "agent_message"}',
        # event is agent_message, but item is null
        '{"event": "agent_message", "item": null}',
        # event is agent_message, but item is a string
        '{"event": "agent_message", "item": "not_a_dict"}',
        # event is agent_message, but text is a list
        '{"event": "agent_message", "item": {"text": ["part1", "part2"]}}',
        # event is agent_message, but text is a dict
        '{"event": "agent_message", "item": {"text": {"nested": "value"}}}',
        # agent_message as key but value is a string (not dict)
        '{"agent_message": "flat_string_value"}',
        # agent_message as key, item exists but is null
        '{"agent_message": {"item": null}}',
        # agent_message as key, item is string
        '{"agent_message": {"item": "string_item"}}',
        # agent_message as key, correct structure
        '{"agent_message": {"item": {"text": "Nested Key Text"}}}',
        # item.completed event with invalid structures
        '{"event": "item.completed"}',
        '{"event": "item.completed", "item": null}',
        '{"event": "item.completed", "item": "string_item"}',
        '{"event": "item.completed", "item": {"type": "not_agent_message", "text": "ignored"}}',
        '{"event": "item.completed", "item": {"type": "agent_message", "text": "Item Completed Text"}}',
    ]

    jsonl_output = "\n".join(lines) + "\n"
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test adversarial agent_message")

        # What should content be?
        # 1. The list text ["part1", "part2"] -> str() -> "['part1', 'part2']"
        # 2. The dict text {"nested": "value"} -> str() -> "{'nested': 'value'}"
        # 3. "Nested Key Text" from nested key agent_message
        # 4. "Item Completed Text" from event item.completed
        # Let's verify that they are concatenated or handled gracefully.
        content = response["content"]
        assert "['part1', 'part2']" in content
        assert "{'nested': 'value'}" in content
        assert "Nested Key Text" in content
        assert "Item Completed Text" in content


@pytest.mark.asyncio
async def test_openai_provider_tokens_non_scalar_types():
    """
    Test when token values are non-scalar/unexpected types (e.g. dicts, lists, booleans).
    Check that they don't crash and default to fallback values where appropriate.
    """
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    mock_process.returncode = 0

    lines = [
        # turn.completed with input_tokens/output_tokens as dictionaries
        '{"event": "turn.completed", "turn.completed": {"usage": {"input_tokens": {"dict": 1}, "output_tokens": [10]}}}',
        # another line with valid tokens to verify it keeps/overwrites
        '{"event": "turn.completed", "turn.completed": {"usage": {"input_tokens": 15, "output_tokens": 25}}}',
    ]

    jsonl_output = "\n".join(lines) + "\n"
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test non-scalar tokens")
        assert response["usage"]["prompt_tokens"] == 15
        assert response["usage"]["completion_tokens"] == 25
        assert response["usage"]["total_tokens"] == 40


@pytest.mark.asyncio
async def test_openai_provider_tokens_flat_and_nested_combinations():
    """
    Test combinations of flat and nested layouts for tokens.
    """
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    mock_process.returncode = 0

    # Check flat format
    flat_output = (
        '{"event": "turn.completed", "input_tokens": 8, "output_tokens": 12}\n'
    )
    mock_process.communicate.return_value = (flat_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test flat tokens")
        assert response["usage"]["prompt_tokens"] == 8
        assert response["usage"]["completion_tokens"] == 12
        assert response["usage"]["total_tokens"] == 20

    # Check nested format with "tokens" sub-dict
    nested_tokens_output = '{"event": "turn.completed", "turn.completed": {"tokens": {"input_tokens": 30, "output_tokens": 40}}}\n'
    mock_process.communicate.return_value = (nested_tokens_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test nested tokens")
        assert response["usage"]["prompt_tokens"] == 30
        assert response["usage"]["completion_tokens"] == 40
        assert response["usage"]["total_tokens"] == 70

    # Check multiple conflicting turn.completed events, last one should override
    multiple_output = (
        '{"event": "turn.completed", "input_tokens": 5, "output_tokens": 5}\n'
        '{"event": "turn.completed", "turn.completed": {"usage": {"input_tokens": 100, "output_tokens": 200, "total_tokens": 300}}}\n'
    )
    mock_process.communicate.return_value = (multiple_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test multiple tokens")
        assert response["usage"]["prompt_tokens"] == 100
        assert response["usage"]["completion_tokens"] == 200
        assert response["usage"]["total_tokens"] == 300


@pytest.mark.asyncio
async def test_openai_provider_tokens_invalid_strings_and_booleans():
    """
    Test parsing when token values are invalid strings or booleans.
    Note that int("10") is valid, int(True) is 1, and int("invalid") raises ValueError.
    """
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    mock_process.returncode = 0

    lines = [
        '{"event": "turn.completed", "input_tokens": "abc", "output_tokens": "50"}\n'
    ]
    mock_process.communicate.return_value = ("\n".join(lines).encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test invalid strings")
        assert (
            response["usage"]["prompt_tokens"] == 0
        )  # failed to parse "abc", fallback to 0
        assert response["usage"]["completion_tokens"] == 50  # parsed "50" successfully
        assert response["usage"]["total_tokens"] == 50

    # Test booleans (which Python evaluates as 1 or 0 inside int())
    boolean_lines = [
        '{"event": "turn.completed", "input_tokens": true, "output_tokens": false}\n'
    ]
    mock_process.communicate.return_value = (
        "\n".join(boolean_lines).encode("utf-8"),
        b"",
    )

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test booleans")
        # int(True) -> 1, int(False) -> 0 in Python
        assert response["usage"]["prompt_tokens"] == 1
        assert response["usage"]["completion_tokens"] == 0
        assert response["usage"]["total_tokens"] == 1
