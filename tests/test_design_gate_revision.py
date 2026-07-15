"""The design approval gate's plan-revision loop (orchestrator side).

``run_pipeline``'s design gate LOOPS: a ``stage_gate("design")`` call that
RETURNS reviewer feedback (a non-empty string — the MCP orchestrate_revise
path) makes the architect revise the DesignPlan, rewrites design.md, updates
``files_to_implement`` and pauses at the SAME gate again. A gate that returns
None keeps the legacy single-pause contract. A revision that fails to produce
a valid DesignPlan keeps the current design and pauses again instead of
failing the job.
"""

import os
import sys

import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from orchestrator import run_pipeline  # noqa: E402


@pytest.fixture
def temp_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


URLS = dict(
    researcher_url="http://researcher",
    claude_url="http://claude",
    codex_url="http://codex",
    tester_url="http://tester",
    security_url="http://security",
    devops_url="http://devops",
)

DESIGN_V1 = (
    "Design v1\n```json\n"
    '{"project_name": "x", "description": "d", "files": '
    '[{"path": "app.py", "specification": "the v1 app"}]}'
    "\n```"
)
DESIGN_V2 = (
    "Design v2\n```json\n"
    '{"project_name": "x", "description": "d", "files": '
    '[{"path": "app.py", "specification": "the v2 app"}, '
    '{"path": "util.py", "specification": "helpers requested by review"}]}'
    "\n```"
)


class _StopAfterReview(Exception):
    """Raised by the test gate to abort the pipeline once the review loop is
    exercised, before the (irrelevant here) code fan-out starts."""


def _design_mock():
    prompts = []

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        prompts.append((url, prompt))
        if "Reviewer feedback (MUST be addressed)" in prompt:
            return DESIGN_V2
        if url == URLS["claude_url"]:
            return DESIGN_V1
        return "research content"

    return prompts, impl


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_design_gate_feedback_revises_plan(
    mock_exec, mock_call_api, temp_workspace
):
    prompts, impl = _design_mock()
    mock_call_api.side_effect = impl

    gate_stages = []

    async def gate(stage):
        if stage != "design":
            return None
        gate_stages.append(stage)
        if len(gate_stages) == 1:
            return "Please add util.py with helper functions"
        raise _StopAfterReview()

    with pytest.raises(_StopAfterReview):
        await run_pipeline(
            prompt="Build x",
            workspace=str(temp_workspace),
            stage_gate=gate,
            **URLS,
        )

    # The gate fired twice: initial pause + the post-revision pause.
    assert gate_stages == ["design", "design"]

    # Exactly one revise call, carrying the reviewer feedback and the
    # current design.
    revise_prompts = [
        p for (_u, p) in prompts if "Reviewer feedback (MUST be addressed)" in p
    ]
    assert len(revise_prompts) == 1
    assert "Please add util.py with helper functions" in revise_prompts[0]
    assert "Design v1" in revise_prompts[0]

    # The revised plan was persisted to design.md before the second pause.
    design_md = (temp_workspace / "design.md").read_text(encoding="utf-8")
    assert "util.py" in design_md

    # And the revision was raw-captured like every other stage attempt.
    raw_dir_hits = []
    for root, _dirs, files in os.walk(str(temp_workspace)):
        raw_dir_hits.extend(f for f in files if f.startswith("design_revision1"))
    assert raw_dir_hits, "design_revision1 raw capture missing"


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_failed_revision_keeps_current_design_and_pauses_again(
    mock_exec, mock_call_api, temp_workspace
):
    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        if "Reviewer feedback (MUST be addressed)" in prompt:
            return "sorry, no fenced DesignPlan here"
        if url == URLS["claude_url"]:
            return DESIGN_V1
        return "research content"

    mock_call_api.side_effect = impl

    gate_stages = []

    async def gate(stage):
        if stage != "design":
            return None
        gate_stages.append(stage)
        if len(gate_stages) == 1:
            return "make it fancier"
        raise _StopAfterReview()

    with pytest.raises(_StopAfterReview):
        await run_pipeline(
            prompt="Build x",
            workspace=str(temp_workspace),
            stage_gate=gate,
            **URLS,
        )

    # Unparseable revision -> the loop pauses again with the ORIGINAL design
    # instead of failing the job or applying garbage.
    assert gate_stages == ["design", "design"]
    design_md = (temp_workspace / "design.md").read_text(encoding="utf-8")
    assert "Design v1" in design_md
    assert "util.py" not in design_md
