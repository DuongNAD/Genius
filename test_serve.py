import sys
import os
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

# Add current workspace to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from serve import normalize_roles, main_async

def test_normalize_roles():
    assert normalize_roles("grok,claude") == ["grok", "claude"]
    assert normalize_roles("grok-api,4") == ["grok", "tester"]
    assert normalize_roles("grok, 2, 3, 5") == ["grok", "claude", "codex", "orchestrator"]

@pytest.mark.asyncio
@patch("serve.start_server", new_callable=AsyncMock)
@patch("serve.run_pipeline", new_callable=AsyncMock)
@patch("argparse.ArgumentParser.parse_args")
async def test_serve_cli_roles_only(mock_parse_args, mock_run_pipeline, mock_start_server):
    # Simulate '--roles grok,claude'
    mock_args = MagicMock()
    mock_args.roles = "grok,claude"
    mock_args.prompt = None
    mock_parse_args.return_value = mock_args

    await main_async()

    assert mock_start_server.call_count == 2
    mock_start_server.assert_any_call("grok", 8001)
    mock_start_server.assert_any_call("claude", 8002)
    assert not mock_run_pipeline.called

@pytest.mark.asyncio
@patch("serve.start_server", new_callable=AsyncMock)
@patch("serve.run_pipeline", new_callable=AsyncMock)
@patch("argparse.ArgumentParser.parse_args")
async def test_serve_cli_orchestrator(mock_parse_args, mock_run_pipeline, mock_start_server):
    # Simulate '--roles orchestrator --prompt "Build a calculator"'
    mock_args = MagicMock()
    mock_args.roles = "orchestrator"
    mock_args.prompt = "Build a calculator"
    mock_parse_args.return_value = mock_args

    await main_async()

    assert not mock_start_server.called
    mock_run_pipeline.assert_called_once_with("Build a calculator")

@pytest.mark.asyncio
@patch("serve.start_server", new_callable=AsyncMock)
@patch("serve.interactive_prompt")
@patch("argparse.ArgumentParser.parse_args")
async def test_serve_interactive_fallback(mock_parse_args, mock_interactive_prompt, mock_start_server):
    # Simulate no args passed, interactive prompt selects 'codex'
    mock_args = MagicMock()
    mock_args.roles = None
    mock_args.prompt = None
    mock_parse_args.return_value = mock_args

    mock_interactive_prompt.return_value = ["codex"]

    await main_async()

    assert mock_interactive_prompt.called
    mock_start_server.assert_called_once_with("codex", 8003)

@pytest.mark.asyncio
@patch("serve.start_server", new_callable=AsyncMock)
@patch("serve.run_pipeline", new_callable=AsyncMock)
@patch("argparse.ArgumentParser.parse_args")
async def test_serve_cli_interactive(mock_parse_args, mock_run_pipeline, mock_start_server):
    # Simulate '--roles orchestrator --prompt "Build a calculator" --interactive'
    mock_args = MagicMock()
    mock_args.roles = "orchestrator"
    mock_args.prompt = "Build a calculator"
    mock_args.interactive = True
    mock_args.auto_pilot = False
    mock_parse_args.return_value = mock_args

    await main_async()

    assert not mock_start_server.called
    mock_run_pipeline.assert_called_once_with("Build a calculator", interactive=True)

@pytest.mark.asyncio
@patch("serve.start_server", new_callable=AsyncMock)
@patch("serve.run_pipeline", new_callable=AsyncMock)
@patch("argparse.ArgumentParser.parse_args")
async def test_serve_cli_auto_pilot(mock_parse_args, mock_run_pipeline, mock_start_server):
    # Simulate '--auto-pilot --prompt "Build a calculator"'
    mock_args = MagicMock()
    mock_args.roles = None
    mock_args.prompt = "Build a calculator"
    mock_args.interactive = False
    mock_args.auto_pilot = True
    mock_parse_args.return_value = mock_args

    await main_async()

    # All 7 microservice servers should have been started
    assert mock_start_server.call_count == 7
    mock_start_server.assert_any_call("grok", 8001)
    mock_start_server.assert_any_call("claude", 8002)
    mock_start_server.assert_any_call("codex", 8003)
    mock_start_server.assert_any_call("tester", 8004)
    mock_start_server.assert_any_call("security", 8005)
    mock_start_server.assert_any_call("devops", 8006)
    mock_start_server.assert_any_call("dashboard", 8080)
    
    # run_pipeline should be called with interactive=False
    mock_run_pipeline.assert_called_once_with("Build a calculator", interactive=False)
