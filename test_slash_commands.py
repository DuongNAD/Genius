import os
import sys
import pytest
import asyncio
import httpx
import importlib.util
from unittest.mock import patch, AsyncMock, MagicMock

# Add current workspace to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ag_core.agents.grok_researcher import GrokResearcherAgent
from ag_core.agents.claude_architect import ClaudeArchitectAgent
from ag_core.agents.codex_reviewer import CodexReviewerAgent
from ag_core.agents.tester import TesterAgent
from orchestrator import run_pipeline, ROUTING_TABLE
from serve import main_async as serve_main_async

@pytest.mark.asyncio
async def test_agent_slash_command_rewriting_grok():
    provider = MagicMock()
    provider.send_prompt = AsyncMock(return_value={"content": "mocked", "usage": {}})
    provider.model_name = "grok-2"
    
    agent = GrokResearcherAgent(provider=provider, output_file="None")
    
    # Test /research command
    await agent.run(prompt="/research detailed queries")
    provider.send_prompt.assert_called_once()
    prompt_sent = provider.send_prompt.call_args[0][0]
    assert "Perform an in-depth research" in prompt_sent
    assert "detailed queries" in prompt_sent

    # Test /summarize command
    provider.send_prompt.reset_mock()
    await agent.run(prompt="/summarize details")
    prompt_sent = provider.send_prompt.call_args[0][0]
    assert "Provide a clear and concise summary" in prompt_sent
    assert "details" in prompt_sent

    # Test /fact-check command
    provider.send_prompt.reset_mock()
    await agent.run(prompt="/fact-check details")
    prompt_sent = provider.send_prompt.call_args[0][0]
    assert "Verify facts, check assumptions" in prompt_sent
    assert "details" in prompt_sent


@pytest.mark.asyncio
async def test_agent_slash_command_rewriting_claude():
    provider = MagicMock()
    provider.send_prompt = AsyncMock(return_value={"content": "mocked", "usage": {}})
    provider.model_name = "claude-3-5"
    
    agent = ClaudeArchitectAgent(provider=provider, output_file="None")
    
    # Test /plan command
    await agent.run(prompt="/plan detailed queries")
    provider.send_prompt.assert_called_once()
    prompt_sent = provider.send_prompt.call_args[0][0]
    assert "Develop a comprehensive, step-by-step implementation plan" in prompt_sent
    assert "detailed queries" in prompt_sent

    # Test /design command
    provider.send_prompt.reset_mock()
    await agent.run(prompt="/design details")
    prompt_sent = provider.send_prompt.call_args[0][0]
    assert "Design the high-level architecture" in prompt_sent
    assert "details" in prompt_sent

    # Test /review-architecture command
    provider.send_prompt.reset_mock()
    await agent.run(prompt="/review-architecture details")
    prompt_sent = provider.send_prompt.call_args[0][0]
    assert "Analyze the current project architecture" in prompt_sent
    assert "details" in prompt_sent


@pytest.mark.asyncio
async def test_agent_slash_command_rewriting_codex():
    provider = MagicMock()
    provider.send_prompt = AsyncMock(return_value={"content": "mocked", "usage": {}})
    provider.model_name = "gpt-4o"
    
    agent = CodexReviewerAgent(provider=provider, output_file="None")
    
    # Test /code command
    await agent.run(prompt="/code detailed queries")
    provider.send_prompt.assert_called_once()
    prompt_sent = provider.send_prompt.call_args[0][0]
    assert "Write clean, robust, and well-documented code" in prompt_sent
    assert "detailed queries" in prompt_sent

    # Test /refactor command
    provider.send_prompt.reset_mock()
    await agent.run(prompt="/refactor details")
    prompt_sent = provider.send_prompt.call_args[0][0]
    assert "Refactor the existing code" in prompt_sent
    assert "details" in prompt_sent

    # Test /security command
    provider.send_prompt.reset_mock()
    await agent.run(prompt="/security details")
    prompt_sent = provider.send_prompt.call_args[0][0]
    assert "Perform a security code audit" in prompt_sent
    assert "details" in prompt_sent


@pytest.mark.asyncio
async def test_agent_slash_command_rewriting_tester():
    provider = MagicMock()
    provider.send_prompt = AsyncMock(return_value={"content": "mocked", "usage": {}})
    provider.model_name = "gpt-4o"
    
    agent = TesterAgent(provider=provider, output_file="None")
    
    # Test /unit-test command
    await agent.run(prompt="/unit-test detailed queries")
    provider.send_prompt.assert_called_once()
    prompt_sent = provider.send_prompt.call_args[0][0]
    assert "Generate comprehensive unit tests" in prompt_sent
    assert "detailed queries" in prompt_sent

    # Test /stress-test command
    provider.send_prompt.reset_mock()
    await agent.run(prompt="/stress-test details")
    prompt_sent = provider.send_prompt.call_args[0][0]
    assert "Create a performance or stress testing script" in prompt_sent
    assert "details" in prompt_sent


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=AsyncMock)
async def test_orchestrator_smart_routing(mock_call_api, tmp_path):
    mock_call_api.return_value = "Mocked routed agent output"
    
    # Run the orchestrator with a routed command
    result = await run_pipeline(
        prompt="/research detailed research query",
        workspace=str(tmp_path),
        grok_url="http://localhost:8001",
        api_key_override="test-key"
    )
    
    # Verify we returned direct response and wrote output to research.md only
    assert result == "Mocked routed agent output"
    mock_call_api.assert_called_once()
    
    research_file = tmp_path / "research.md"
    design_file = tmp_path / "design.md"
    
    assert research_file.exists()
    assert not design_file.exists()
    assert research_file.read_text(encoding="utf-8") == "Mocked routed agent output"


@pytest.mark.asyncio
@patch("serve.start_server", new_callable=AsyncMock)
@patch("serve.run_pipeline", new_callable=AsyncMock)
@patch("argparse.ArgumentParser.parse_args")
async def test_serve_slash_command_dynamic_role(mock_parse_args, mock_run_pipeline, mock_start_server):
    mock_args = MagicMock()
    mock_args.roles = "orchestrator"
    mock_args.prompt = "/plan do a blueprint"
    mock_parse_args.return_value = mock_args

    await serve_main_async()
    
    # Serve should dynamically resolve /plan to "claude" and add "claude" as server to start
    mock_start_server.assert_called_once_with("claude", 8002)
    mock_run_pipeline.assert_called_once_with("/plan do a blueprint")


@patch("sys.argv", ["run.py", "/research", "query"])
@patch("ag_core.agents.grok_researcher.GrokResearcherAgent.run", new_callable=AsyncMock)
def test_grok_run_cli(mock_agent_run):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "grok_run", 
        os.path.join(base_dir, ".agents", "skills", "grok_researcher", "run.py")
    )
    grok_run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grok_run)
    grok_run.main()
    mock_agent_run.assert_called_once_with(prompt="/research query")


@patch("sys.argv", ["run.py", "/plan", "design blueprint"])
@patch("ag_core.agents.claude_architect.ClaudeArchitectAgent.run", new_callable=AsyncMock)
def test_claude_run_cli(mock_agent_run):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "claude_run", 
        os.path.join(base_dir, ".agents", "skills", "claude_architect", "run.py")
    )
    claude_run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(claude_run)
    claude_run.main()
    mock_agent_run.assert_called_once_with(prompt="/plan design blueprint")


@patch("sys.argv", ["run.py", "/code", "write a class"])
@patch("ag_core.agents.codex_reviewer.CodexReviewerAgent.run", new_callable=AsyncMock)
def test_codex_run_cli(mock_agent_run):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "codex_run", 
        os.path.join(base_dir, ".agents", "skills", "codex_reviewer", "run.py")
    )
    codex_run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(codex_run)
    codex_run.main()
    mock_agent_run.assert_called_once_with(prompt="/code write a class")


@patch("sys.argv", ["run.py", "/unit-test", "add tests"])
@patch("ag_core.agents.tester.TesterAgent.run", new_callable=AsyncMock)
def test_tester_run_cli(mock_agent_run):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "tester_run", 
        os.path.join(base_dir, ".agents", "skills", "tester_agent", "run.py")
    )
    tester_run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tester_run)
    tester_run.main()
    mock_agent_run.assert_called_once_with(prompt="/unit-test add tests")
