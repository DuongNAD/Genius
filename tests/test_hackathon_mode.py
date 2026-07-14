"""Hackathon mode (GENIUS_HACKATHON_MODE) — opt-in custom-flow augmentation.

Always off under pytest (same construction as project gates / auto-install),
so the default custom flow stays byte-identical: these tests drive the gate
via ``monkeypatch.setattr(orchestrator, "hackathon_mode_enabled", ...)`` —
NOT ``under_pytest`` (that would also flip design self-heal and poll-timeout
clamping). Scaffolding mirrors test_custom_flow.py: ``orchestrator.call_api``
and the subprocess layer are faked; mocks route on ``in``-markers, never
prompt equality, because guidance is appended to the prompts.
"""

import glob
import os
import sys

import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orchestrator  # noqa: E402
from orchestrator import PipelineError, run_pipeline  # noqa: E402


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

_DESIGN_ONE_FILE = (
    '{"project_name":"p","description":"d",'
    '"files":[{"path":"foo.py","specification":"make foo"}]}'
)

_DEPLOY_MARKER = "deploy content DEPLOYMARKER"
_PITCH_MARKER = "Write the complete contents of pitch.md"


def _happy_mock(critic_reply="Looks good [APPROVED]", pitch_reply="# The Pitch"):
    """A green custom-flow mock; records ``(url, prompt)`` pairs."""
    calls = []

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        calls.append((url, prompt))
        if prompt.startswith("/code"):
            return "```python\ndef foo():\n    return 1\n```"
        if prompt.startswith(_PITCH_MARKER):
            if isinstance(pitch_reply, Exception):
                raise pitch_reply
            return pitch_reply
        if "You are CriticReviewer" in prompt:
            return critic_reply
        if "Review the implemented project" in prompt:
            return '```json\n{"blocking": false, "findings": []}\n```'
        if url == URLS["tester_url"]:
            return "```python\ndef test_foo():\n    assert True\n```"
        if url == URLS["security_url"]:
            return '```json\n{"blocking": false}\n```'
        if url == URLS["devops_url"]:
            return _DEPLOY_MARKER
        return f"```json\n{_DESIGN_ONE_FILE}\n```"

    return calls, impl


def _enable(monkeypatch):
    monkeypatch.setattr(orchestrator, "hackathon_mode_enabled", lambda: True)


async def _run(temp_workspace, flow="custom"):
    kwargs = dict(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        max_debate_rounds=1,
        **URLS,
    )
    if flow is not None:
        kwargs["flow"] = flow
    await run_pipeline(**kwargs)


# --- the env gate itself ----------------------------------------------------


def test_hackathon_mode_always_off_under_pytest(monkeypatch):
    monkeypatch.setenv("GENIUS_HACKATHON_MODE", "1")
    assert orchestrator.hackathon_mode_enabled() is False


def test_hackathon_mode_env_parsing(monkeypatch):
    monkeypatch.setattr(orchestrator, "under_pytest", lambda: False)
    monkeypatch.delenv("GENIUS_HACKATHON_MODE", raising=False)
    assert orchestrator.hackathon_mode_enabled() is False
    for truthy in ("1", "true", "yes", "TRUE"):
        monkeypatch.setenv("GENIUS_HACKATHON_MODE", truthy)
        assert orchestrator.hackathon_mode_enabled() is True
    monkeypatch.setenv("GENIUS_HACKATHON_MODE", "0")
    assert orchestrator.hackathon_mode_enabled() is False


# --- byte-identity when off -------------------------------------------------


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_custom_flow_default_prompts_carry_no_hackathon_guidance(
    mock_exec, mock_call_api, temp_workspace
):
    """The executable byte-identity contract: gate off (as it always is under
    pytest) => no prompt carries any hackathon content, no extra artifacts."""
    calls, impl = _happy_mock()
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    await _run(temp_workspace)
    assert calls
    for _url, prompt in calls:
        assert "HACKATHON" not in prompt
        assert "Target Users & Pain" not in prompt
    assert not os.path.exists(os.path.join(str(temp_workspace), "pitch.md"))
    assert not os.path.exists(
        os.path.join(str(temp_workspace), "ai_collaboration_log.md")
    )


# --- F1: guidance reaches the right prompts ---------------------------------


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_hackathon_guidance_reaches_research_design_devops(
    mock_exec, mock_call_api, temp_workspace, monkeypatch
):
    _enable(monkeypatch)
    calls, impl = _happy_mock()
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    await _run(temp_workspace)

    # Plan-first claude call is call #0 and carries the design guidance.
    url0, prompt0 = calls[0]
    assert url0 == URLS["claude_url"]
    assert "AI-NATIVE ARCHITECTURE" in prompt0
    assert prompt0.startswith("Build a calculator app")

    research_prompts = [p for u, p in calls if u == URLS["researcher_url"]]
    assert research_prompts
    assert "Target Users & Pain" in research_prompts[0]
    assert research_prompts[0].startswith("Build a calculator app")

    devops_prompts = [p for u, p in calls if u == URLS["devops_url"]]
    assert devops_prompts
    assert "LIVE public URL" in devops_prompts[0]
    # The fixed file budget must survive, in its original position (before
    # the hackathon append).
    assert "File budget (FIXED by the approved design)" in devops_prompts[0]
    assert devops_prompts[0].index("File budget") < devops_prompts[0].index(
        "LIVE public URL"
    )


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_hackathon_guidance_never_leaks_into_sequential_flow(
    mock_exec, mock_call_api, temp_workspace, monkeypatch
):
    """Even with the gate forced on, only flow='custom' is augmented."""
    _enable(monkeypatch)
    calls, impl = _happy_mock()
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    await _run(temp_workspace, flow=None)  # sequential default
    assert calls
    for _url, prompt in calls:
        assert "HACKATHON" not in prompt
        assert "Target Users & Pain" not in prompt
    assert not os.path.exists(os.path.join(str(temp_workspace), "pitch.md"))
    assert not os.path.exists(
        os.path.join(str(temp_workspace), "ai_collaboration_log.md")
    )


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_debate_refine_prompt_carries_design_guidance(
    mock_exec, mock_call_api, temp_workspace, monkeypatch
):
    """A debate round must not launder the hackathon content out of the plan."""
    _enable(monkeypatch)
    calls, impl = _happy_mock(critic_reply="Needs work: add error handling")
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    await _run(temp_workspace)
    refine_prompts = [
        p
        for _u, p in calls
        if p.startswith("You are Claude, the architect agent. Refine")
    ]
    assert refine_prompts
    assert "AI-NATIVE ARCHITECTURE" in refine_prompts[0]


# --- F2/F3: pitch.md + ai_collaboration_log.md ------------------------------


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_hackathon_pitch_written_and_raw_captured(
    mock_exec, mock_call_api, temp_workspace, monkeypatch
):
    _enable(monkeypatch)
    calls, impl = _happy_mock(pitch_reply="# The Pitch\n\ncontent")
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    await _run(temp_workspace)

    pitch_file = os.path.join(str(temp_workspace), "pitch.md")
    with open(pitch_file, encoding="utf-8") as f:
        assert f.read() == "# The Pitch\n\ncontent"

    raw_traces = glob.glob(
        os.path.join(str(temp_workspace), ".genius", "*", "logs", "raw", "pitch.md")
    )
    assert raw_traces

    # The pitch request goes to the claude role and threads the finished
    # artifacts through (deploy content included).
    pitch_calls = [(u, p) for u, p in calls if p.startswith(_PITCH_MARKER)]
    assert len(pitch_calls) == 1
    url, prompt = pitch_calls[0]
    assert url == URLS["claude_url"]
    assert "Design:" in prompt
    assert "Deploy plan:" in prompt
    assert _DEPLOY_MARKER in prompt


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_hackathon_pitch_failure_is_best_effort(
    mock_exec, mock_call_api, temp_workspace, monkeypatch
):
    """A dead pitch backend cannot fail the build, and the collab log still
    exports (the two halves are independent)."""
    _enable(monkeypatch)
    calls, impl = _happy_mock(pitch_reply=PipelineError("pitch backend down"))
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    await _run(temp_workspace)  # must NOT raise
    assert not os.path.exists(os.path.join(str(temp_workspace), "pitch.md"))
    assert os.path.exists(
        os.path.join(str(temp_workspace), "ai_collaboration_log.md")
    )


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_hackathon_collab_log_autorun(
    mock_exec, mock_call_api, temp_workspace, monkeypatch
):
    _enable(monkeypatch)
    calls, impl = _happy_mock()
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    await _run(temp_workspace)

    log_file = os.path.join(str(temp_workspace), "ai_collaboration_log.md")
    with open(log_file, encoding="utf-8") as f:
        log = f.read()
    assert "## Stage timeline" in log
    assert "generated by AI agents" in log
    # The pitch trace was saved BEFORE the export, so it is in the timeline.
    assert "pitch generation" in log
