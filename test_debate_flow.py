import os
import sys
import pytest
import asyncio
from unittest.mock import patch, MagicMock

# Add current workspace to path to import orchestrator
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orchestrator import run_pipeline, PipelineError


@pytest.fixture
def temp_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# Helper to mock subprocess for Step 3 Antigravity
async def mock_exec_impl(*args, **kwargs):
    proc = MagicMock()

    async def mock_communicate():
        # Write a dummy app.py file so verification checks pass
        app_file = os.path.join("projects", "build_a_calculator_app", "app.py")
        os.makedirs(os.path.dirname(app_file), exist_ok=True)
        with open(app_file, "w") as f:
            f.write("print('hello')")
        return (b"print('hello')", b"")

    proc.communicate = mock_communicate
    proc.returncode = 0
    return proc


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_debate_flow_sequence_no_approval(
    mock_exec, mock_call_api, temp_workspace
):
    # No approval, runs the full 2 rounds
    responses = [
        "Grok research report",  # 1. Grok research
        "Claude design v1",  # 2. Claude design initial
        "Grok criticism v1",  # 3. Grok critic round 1
        "Claude design v2",  # 4. Claude refine round 1
        "Grok criticism v2",  # 5. Grok critic round 2
        "Claude design v3 (final)",  # 6. Claude refine round 2
        "Codex review report",  # 7. Codex review
        "def test_app(): pass",  # 8. Tester test generation
        "Security audit report",  # 9. Security audit
        "DevOps deploy report",  # 10. DevOps deploy
    ]

    call_idx = 0
    calls_recorded = []

    async def mock_call_api_impl(
        url, api_key, prompt, context=None, client=None, poll_timeout=60.0
    ):
        nonlocal call_idx
        calls_recorded.append((url, prompt))
        resp = responses[call_idx]
        call_idx += 1
        return resp

    mock_call_api.side_effect = mock_call_api_impl
    mock_exec.side_effect = mock_exec_impl

    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        max_debate_rounds=2,
    )

    # 10 calls in total
    assert len(calls_recorded) == 10
    assert (
        "Claude design v3 (final)"
        in open(
            os.path.join(
                str(temp_workspace), "projects", "build_a_calculator_app", "design.md"
            ),
            "r",
            encoding="utf-8",
        ).read()
    )


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_debate_flow_instant_approval(mock_exec, mock_call_api, temp_workspace):
    # Instantly approved in round 1
    responses = [
        "Grok research report",  # 1. Grok research
        "Claude design v1",  # 2. Claude design initial
        "Looks good [APPROVED]",  # 3. Grok critic round 1 (instantly approved!)
        "Codex review report",  # 4. Codex review
        "def test_app(): pass",  # 5. Tester test generation
        "Security audit report",  # 6. Security audit
        "DevOps deploy report",  # 7. DevOps deploy
    ]

    call_idx = 0
    calls_recorded = []

    async def mock_call_api_impl(
        url, api_key, prompt, context=None, client=None, poll_timeout=60.0
    ):
        nonlocal call_idx
        calls_recorded.append((url, prompt))
        resp = responses[call_idx]
        call_idx += 1
        return resp

    mock_call_api.side_effect = mock_call_api_impl
    mock_exec.side_effect = mock_exec_impl

    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        max_debate_rounds=2,
    )

    # 7 calls total because we exit early in round 1
    assert len(calls_recorded) == 7

    # Check sequence
    assert "8001" in calls_recorded[0][0]  # Grok research
    assert "8002" in calls_recorded[1][0]  # Claude initial
    assert "8001" in calls_recorded[2][0]  # Grok critic round 1
    assert "You are GrokReviewer" in calls_recorded[2][1]

    # Next call should skip Claude refine round 1 and Grok critic round 2, going straight to Codex review (8003)
    assert "8003" in calls_recorded[3][0]

    # Check that design.md contains the initial design v1 (unmodified)
    design_content = open(
        os.path.join(
            str(temp_workspace), "projects", "build_a_calculator_app", "design.md"
        ),
        "r",
        encoding="utf-8",
    ).read()
    assert design_content == "Claude design v1"


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_debate_flow_round_2_approval(mock_exec, mock_call_api, temp_workspace):
    # Criticized in round 1, refined, approved in round 2
    responses = [
        "Grok research report",  # 1. Grok research
        "Claude design v1",  # 2. Claude design initial
        "Criticism round 1",  # 3. Grok critic round 1
        "Claude design v2",  # 4. Claude refine round 1
        "Excellent [APPROVED]",  # 5. Grok critic round 2 (approved!)
        "Codex review report",  # 6. Codex review
        "def test_app(): pass",  # 7. Tester test generation
        "Security audit report",  # 8. Security audit
        "DevOps deploy report",  # 9. DevOps deploy
    ]

    call_idx = 0
    calls_recorded = []

    async def mock_call_api_impl(
        url, api_key, prompt, context=None, client=None, poll_timeout=60.0
    ):
        nonlocal call_idx
        calls_recorded.append((url, prompt))
        resp = responses[call_idx]
        call_idx += 1
        return resp

    mock_call_api.side_effect = mock_call_api_impl
    mock_exec.side_effect = mock_exec_impl

    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        max_debate_rounds=2,
    )

    # 9 calls total because we exit early in round 2
    assert len(calls_recorded) == 9

    # Check sequence
    assert "8001" in calls_recorded[0][0]  # Grok research
    assert "8002" in calls_recorded[1][0]  # Claude initial
    assert "8001" in calls_recorded[2][0]  # Grok critic 1
    assert "8002" in calls_recorded[3][0]  # Claude refine 1
    assert "8001" in calls_recorded[4][0]  # Grok critic 2

    # Next call should skip Claude refine round 2, going straight to Codex review (8003)
    assert "8003" in calls_recorded[5][0]

    # Check that design.md contains the design v2 (refined from round 1)
    design_content = open(
        os.path.join(
            str(temp_workspace), "projects", "build_a_calculator_app", "design.md"
        ),
        "r",
        encoding="utf-8",
    ).read()
    assert design_content == "Claude design v2"
