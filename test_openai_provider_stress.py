import pytest
from unittest.mock import AsyncMock, patch
from ag_core.providers.openai_provider import OpenAIProvider


@pytest.mark.asyncio
async def test_openai_provider_missing_event_or_type():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    # Missing event or type entirely
    jsonl_output = '{"item": {"text": "Hello"}}\n'
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test prompt")
        # Since there's no event or type, it should not identify it as agent message or turn completed
        assert response["content"] == ""
        assert response["usage"]["prompt_tokens"] == 0


@pytest.mark.asyncio
async def test_openai_provider_agent_message_missing_item():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    # Event is agent_message but "item" is missing
    jsonl_output = '{"event": "agent_message"}\n'
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test prompt")
        assert response["content"] == ""


@pytest.mark.asyncio
async def test_openai_provider_agent_message_item_not_dict():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    # Item is a string instead of a dict
    jsonl_output = '{"event": "agent_message", "item": "not-a-dict"}\n'
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test prompt")
        assert response["content"] == ""


@pytest.mark.asyncio
async def test_openai_provider_agent_message_missing_text():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    # Item is a dict but lacks "text"
    jsonl_output = '{"event": "agent_message", "item": {"other": "field"}}\n'
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test prompt")
        assert response["content"] == ""


@pytest.mark.asyncio
async def test_openai_provider_agent_message_null_agent_message():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    # "agent_message" is present but is null.
    jsonl_output = '{"agent_message": null}\n'
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test prompt")
        assert response["content"] == ""


@pytest.mark.asyncio
async def test_openai_provider_agent_message_non_dict_agent_message():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    # "agent_message" is present but is a string.
    jsonl_output = '{"agent_message": "not-a-dict"}\n'
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test prompt")
        assert response["content"] == ""


@pytest.mark.asyncio
async def test_openai_provider_turn_completed_non_dict_usage():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    # "turn.completed" event with a list/string usage or tokens
    jsonl_output = (
        '{"event": "turn.completed", "turn.completed": {"usage": "not-a-dict"}}\n'
    )
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test prompt")
        assert response["usage"]["prompt_tokens"] == 0


@pytest.mark.asyncio
async def test_openai_provider_turn_completed_negative_tokens():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    jsonl_output = '{"event": "turn.completed", "turn.completed": {"usage": {"input_tokens": -5, "output_tokens": -10}}}\n'
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test prompt")
        assert response["usage"]["prompt_tokens"] == -5
        assert response["usage"]["completion_tokens"] == -10
        assert response["usage"]["total_tokens"] == -15


@pytest.mark.asyncio
async def test_openai_provider_turn_completed_invalid_token_type_list():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    # Token value is a list.
    jsonl_output = '{"event": "turn.completed", "turn.completed": {"usage": {"input_tokens": []}}}\n'
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test prompt")
        assert response["usage"]["prompt_tokens"] == 0


@pytest.mark.asyncio
async def test_openai_provider_turn_completed_invalid_token_type_dict():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    # Token value is a dict.
    jsonl_output = '{"event": "turn.completed", "turn.completed": {"usage": {"input_tokens": {}}}}\n'
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test prompt")
        assert response["usage"]["prompt_tokens"] == 0


@pytest.mark.asyncio
async def test_openai_provider_turn_completed_float_tokens():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    jsonl_output = '{"event": "turn.completed", "turn.completed": {"usage": {"input_tokens": 12.5, "output_tokens": "5.5"}}}\n'
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test prompt")
        assert response["usage"]["prompt_tokens"] == 12
        assert response["usage"]["completion_tokens"] == 0


@pytest.mark.asyncio
async def test_openai_provider_json_list():
    provider = OpenAIProvider()
    mock_process = AsyncMock()
    jsonl_output = '[{"event": "agent_message"}]\n'
    mock_process.communicate.return_value = (jsonl_output.encode("utf-8"), b"")

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        response = await provider.send_prompt("Test prompt")
        assert response["content"] == ""


@pytest.mark.asyncio
async def test_openai_provider_cli_not_found():
    provider = OpenAIProvider()
    # Simulate FileNotFoundError when trying to execute codex.exe
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("No such file or directory"),
    ):
        with pytest.raises(FileNotFoundError):
            await provider.send_prompt("Test prompt")


@pytest.mark.asyncio
async def test_openai_provider_cli_permission_error():
    provider = OpenAIProvider()
    # Simulate PermissionError when trying to execute codex.exe
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=PermissionError("Permission denied"),
    ):
        with pytest.raises(PermissionError):
            await provider.send_prompt("Test prompt")
