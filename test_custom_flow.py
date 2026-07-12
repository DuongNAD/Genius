"""flow='custom' pipeline variant (Phase 4): plan-first + codex debate critic.

Default (flow='sequential') must be byte-identical: research -> design -> critic
(researcher) -> refine (claude). Custom: plan (claude) -> research -> critic
(codex) -> refine (claude). Debate is forced to 0 rounds under pytest unless
max_debate_rounds is passed explicitly, so these tests pass 1.
"""

import asyncio
import os
import sys

import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orchestrator import run_pipeline, process_single_file  # noqa: E402
from ag_core.config import load_config  # noqa: E402
from ag_core.utils.message_bus import MessageBus  # noqa: E402


@pytest.fixture
def temp_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


async def _mock_exec(*args, **kwargs):
    proc = MagicMock()

    async def _comm():
        app = os.path.join("projects", "build_a_calculator_app", "app.py")
        os.makedirs(os.path.dirname(app), exist_ok=True)
        with open(app, "w") as f:
            f.write("print('hi')")
        return (b"print('hi')", b"")

    proc.communicate = _comm
    proc.returncode = 0
    return proc


URLS = dict(
    researcher_url="http://researcher",
    claude_url="http://claude",
    codex_url="http://codex",
    tester_url="http://tester",
    security_url="http://security",
    devops_url="http://devops",
)


def _recording_mock():
    calls = []

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        calls.append(url)
        return "plan / design / critique content"

    return calls, impl


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_default_flow_research_first_researcher_critic(
    mock_exec, mock_call_api, temp_workspace
):
    calls, impl = _recording_mock()
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        max_debate_rounds=1,
        **URLS,
    )
    assert calls[0] == URLS["researcher_url"]  # research first
    assert calls[1] == URLS["claude_url"]       # design from research
    assert calls[2] == URLS["researcher_url"]   # default critic = researcher
    assert calls[3] == URLS["claude_url"]       # refiner = claude


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_custom_flow_plan_first_codex_critic(
    mock_exec, mock_call_api, temp_workspace
):
    calls, impl = _recording_mock()
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        max_debate_rounds=1,
        flow="custom",
        **URLS,
    )
    assert calls[0] == URLS["claude_url"]       # plan FIRST (before research)
    assert calls[1] == URLS["researcher_url"]   # research after the plan
    assert calls[2] == URLS["codex_url"]        # custom critic = codex
    assert calls[3] == URLS["claude_url"]       # refiner = claude


def _psf_args(proj, urls, **extra):
    """Build process_single_file positional args with a recording mock's urls."""
    os.makedirs(os.path.join(proj, "logs"), exist_ok=True)
    mb = MessageBus(db_path=os.path.join(proj, "logs", "mb.db"))
    return dict(
        file_info={"path": "foo.py", "specification": "spec"},
        project_dir=proj,
        config=load_config(),
        codex_url="http://codex",
        tester_url="http://tester",
        security_url="http://security",
        api_key="key",
        client=MagicMock(),
        poll_timeout=60.0,
        max_retries=2,
        semaphore=asyncio.Semaphore(1),
        message_bus=mb,
        parent_art_id=None,
        **extra,
    )


def _invalid_code_mock():
    urls = []

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        urls.append(url)
        # invalid Python in a fence -> invalid_python_feedback fires -> retry
        return "```python\ndef (((\n```"

    return urls, impl


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
async def test_custom_self_heal_calls_claude_diagnose(mock_call_api, temp_workspace):
    urls, impl = _invalid_code_mock()
    mock_call_api.side_effect = impl
    # Invalid code fails after all retries -> process_single_file raises; we only
    # care about the call sequence it made along the way.
    with pytest.raises(Exception):
        await process_single_file(
            **_psf_args(
                str(temp_workspace), urls, flow="custom", claude_url="http://claude"
            )
        )
    assert "http://claude" in urls          # Claude diagnosed the retry
    assert urls.count("http://codex") == 2  # two code attempts


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
async def test_default_self_heal_no_claude_diagnose(mock_call_api, temp_workspace):
    urls, impl = _invalid_code_mock()
    mock_call_api.side_effect = impl
    with pytest.raises(Exception):
        await process_single_file(**_psf_args(str(temp_workspace), urls))
    assert "http://claude" not in urls      # default: raw logs, no diagnose call
    assert urls.count("http://codex") == 2


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_custom_flow_final_review_and_per_stage_gates(
    mock_exec, mock_call_api, temp_workspace
):
    design = (
        '{"project_name":"p","description":"d",'
        '"files":[{"path":"foo.py","specification":"make foo"}]}'
    )
    gates = []

    async def gate(stage):
        gates.append(stage)

    review_calls = []

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        if prompt.startswith("/code"):
            return "```python\ndef foo():\n    return 1\n```"
        if "Review the implemented project" in prompt:
            review_calls.append(url)
            return "All good [APPROVED]"
        if url == URLS["tester_url"]:
            return "```python\ndef test_foo():\n    assert True\n```"
        if url == URLS["security_url"]:
            return '```json\n{"blocking": false}\n```'
        return f"```json\n{design}\n```"  # plan / design / research

    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        flow="custom",
        stage_gate=gate,
        **URLS,
    )
    # Custom flow gates after EVERY stage, incl. the new review + devops.
    assert "review" in gates
    assert "devops" in gates
    # The final review ran on the codex reviewer.
    assert review_calls and review_calls[0] == URLS["codex_url"]


def _critic_and_review_recorder(design):
    """A call_api mock recording debate-critic and final-review target URLs."""
    critic_calls = []
    review_calls = []

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        if prompt.startswith("/code"):
            return "```python\ndef foo():\n    return 1\n```"
        if "You are CriticReviewer" in prompt:
            critic_calls.append(url)
            return "Looks good [APPROVED]"
        if "Review the implemented project" in prompt:
            review_calls.append(url)
            return "All good [APPROVED]"
        if url == URLS["tester_url"]:
            return "```python\ndef test_foo():\n    assert True\n```"
        if url == URLS["security_url"]:
            return '```json\n{"blocking": false}\n```'
        return f"```json\n{design}\n```"  # plan / design / research

    return critic_calls, review_calls, impl


_DESIGN_ONE_FILE = (
    '{"project_name":"p","description":"d",'
    '"files":[{"path":"foo.py","specification":"make foo"}]}'
)


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_custom_flow_review_url_override_routes_critic_and_review(
    mock_exec, mock_call_api, temp_workspace
):
    """An explicit review_url reroutes BOTH the debate critic and the final
    review off the codex-role service (so codex-gpt5.6-sol can review while the
    codex role stays the gemini coder)."""
    critic_calls, review_calls, impl = _critic_and_review_recorder(_DESIGN_ONE_FILE)
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        max_debate_rounds=1,
        flow="custom",
        review_url="http://review",
        **URLS,
    )
    assert critic_calls and critic_calls[0] == "http://review"
    assert review_calls and review_calls[0] == "http://review"


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_custom_flow_review_role_env_routes_to_security(
    mock_exec, mock_call_api, temp_workspace, monkeypatch
):
    """GENIUS_REVIEW_ROLE=security maps to the security service URL for the
    debate critic + final review, with no explicit review_url passed."""
    monkeypatch.setenv("GENIUS_REVIEW_ROLE", "security")
    critic_calls, review_calls, impl = _critic_and_review_recorder(_DESIGN_ONE_FILE)
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        max_debate_rounds=1,
        flow="custom",
        **URLS,
    )
    assert critic_calls and critic_calls[0] == URLS["security_url"]
    assert review_calls and review_calls[0] == URLS["security_url"]


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_debate_critic_prompt_carries_quality_checklist(
    mock_exec, mock_call_api, temp_workspace
):
    """Every debate round hands the critic the design-quality checklist (the
    five gates mirrored from the architect contract), so the debate hunts for
    contract/algorithm mismatches, layout bloat and unlocked claims."""
    critic_prompts = []

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        if "You are CriticReviewer" in prompt:
            critic_prompts.append(prompt)
            return "Looks good [APPROVED]"
        if prompt.startswith("/code"):
            return "```python\ndef foo():\n    return 1\n```"
        if url == URLS["tester_url"]:
            return "```python\ndef test_foo():\n    assert True\n```"
        if url == URLS["security_url"]:
            return '```json\n{"blocking": false}\n```'
        return f"```json\n{_DESIGN_ONE_FILE}\n```"

    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        max_debate_rounds=1,
        **URLS,
    )
    assert critic_prompts
    assert "Contract-algorithm consistency" in critic_prompts[0]
    assert "Test-locked claims" in critic_prompts[0]


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_custom_flow_nonblocking_review_skips_claude_fix_plan(
    mock_exec, mock_call_api, temp_workspace
):
    """A non-blocking final-review verdict ({"blocking": false}) counts as
    approved: the pipeline records the notes but spends NO Claude fix plan."""
    fix_plan_calls = []

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        if prompt.startswith("/code"):
            return "```python\ndef foo():\n    return 1\n```"
        if prompt.startswith("A reviewer raised issues"):
            fix_plan_calls.append(url)
            return "fix plan"
        if "You are CriticReviewer" in prompt:
            return "Looks good [APPROVED]"
        if "Review the implemented project" in prompt:
            return '```json\n{"blocking": false, "findings": [{"issue": "minor"}]}\n```'
        if url == URLS["tester_url"]:
            return "```python\ndef test_foo():\n    assert True\n```"
        if url == URLS["security_url"]:
            return '```json\n{"blocking": false}\n```'
        return f"```json\n{_DESIGN_ONE_FILE}\n```"

    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        max_debate_rounds=1,
        flow="custom",
        **URLS,
    )
    # Non-blocking verdict => approved => the "raised issues" Claude call is skipped.
    assert fix_plan_calls == []
