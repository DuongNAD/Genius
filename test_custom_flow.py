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
from orchestrator import run_pipeline, process_single_file, PipelineError  # noqa: E402
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
    # Custom flow gates after EVERY stage, in execution order: the final
    # review runs AFTER the code gate, then devops closes the run.
    assert gates == ["research", "design", "code", "review", "devops"]
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


def _blocking_review_mock():
    """call_api mock whose final review returns an explicit BLOCKING verdict."""
    urls = []

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        urls.append(url)
        if prompt.startswith("/code"):
            return "```python\ndef foo():\n    return 1\n```"
        if prompt.startswith("A reviewer raised issues"):
            return "1. fix the truncated README"
        if "You are CriticReviewer" in prompt:
            return "Looks good [APPROVED]"
        if "Review the implemented project" in prompt:
            return (
                '```json\n{"blocking": true, "findings": '
                '[{"severity": "high", "issue": "README is truncated"}]}\n```'
            )
        if url == URLS["tester_url"]:
            return "```python\ndef test_foo():\n    assert True\n```"
        if url == URLS["security_url"]:
            return '```json\n{"blocking": false}\n```'
        return f"```json\n{_DESIGN_ONE_FILE}\n```"

    return urls, impl


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_custom_flow_blocking_review_fails_pipeline_by_default(
    mock_exec, mock_call_api, temp_workspace
):
    """A parsed {"blocking": true} final-review verdict is a REAL quality gate:
    the Claude fix plan is recorded in review.md, then the pipeline fails
    (default GENIUS_FINAL_REVIEW_STRICT) instead of deploying and reporting
    completed with known-bad output."""
    urls, impl = _blocking_review_mock()
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    with pytest.raises(PipelineError, match="BLOCKING"):
        await run_pipeline(
            prompt="Build a calculator app",
            workspace=str(temp_workspace),
            max_debate_rounds=1,
            flow="custom",
            **URLS,
        )
    # The fix plan WAS produced and recorded (workspace-root review.md is the
    # single canonical copy; the project dir is the artifact-free deliverable)
    review_md = os.path.join(str(temp_workspace), "review.md")
    with open(review_md, encoding="utf-8") as f:
        review = f.read()
    assert "## Claude fix plan" in review
    assert "fix the truncated README" in review
    # ...and the deploy stage never ran.
    assert URLS["devops_url"] not in urls


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_custom_flow_blocking_review_advisory_when_strict_disabled(
    mock_exec, mock_call_api, temp_workspace, monkeypatch
):
    """GENIUS_FINAL_REVIEW_STRICT=0 restores the historical advisory behavior:
    the fix plan is recorded and the pipeline continues to deploy."""
    monkeypatch.setenv("GENIUS_FINAL_REVIEW_STRICT", "0")
    urls, impl = _blocking_review_mock()
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        max_debate_rounds=1,
        flow="custom",
        **URLS,
    )
    review_md = os.path.join(str(temp_workspace), "review.md")
    with open(review_md, encoding="utf-8") as f:
        review = f.read()
    assert "## Claude fix plan" in review
    assert URLS["devops_url"] in urls  # deploy still ran


# --- whole-project pytest gate + conformance + collision-safe test names ----


def _happy_impl(review_response='```json\n{"blocking": false, "findings": []}\n```'):
    """call_api mock for a clean custom run; final review response injectable."""
    urls = []

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        urls.append(url)
        if prompt.startswith("/code"):
            return "```python\ndef foo():\n    return 1\n```"
        if "Review the implemented project" in prompt:
            return review_response
        if url == URLS["tester_url"]:
            return "```python\ndef test_foo():\n    assert True\n```"
        if url == URLS["security_url"]:
            return '```json\n{"blocking": false}\n```'
        return f"```json\n{_DESIGN_ONE_FILE}\n```"

    return urls, impl


def _exec_side_with_full_suite_rc(returncode):
    """Subprocess mock: the whole-project pytest run (the only call passing
    cwd=) gets ``returncode``; per-file pytest runs keep succeeding."""

    async def side(*args, **kwargs):
        if kwargs.get("cwd"):
            proc = MagicMock()

            async def _comm():
                return (b"whole-project pytest output", b"")

            proc.communicate = _comm
            proc.returncode = returncode
            return proc
        return await _mock_exec(*args, **kwargs)

    return side


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_custom_full_suite_gate_fails_job(
    mock_exec, mock_call_api, temp_workspace
):
    """Per-file verification can pass while the ASSEMBLED project fails one
    plain pytest run (e.g. duplicate test-module basenames). The custom flow
    now runs pytest once from the project root and fails the job (default
    strict) — a false-positive 'completed' shipped exactly this way."""
    urls, impl = _happy_impl()
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _exec_side_with_full_suite_rc(1)
    from orchestrator import PipelineError as PE

    with pytest.raises(PE, match="Whole-project pytest"):
        await run_pipeline(
            prompt="Build a calculator app",
            workspace=str(temp_workspace),
            flow="custom",
            **URLS,
        )
    # Evidence recorded in the canonical workspace-root review.md before the
    # gate fired; the deliverable project dir carries no artifact copy.
    review_md = os.path.join(str(temp_workspace), "review.md")
    with open(review_md, encoding="utf-8") as f:
        review = f.read()
    assert "## Whole-project pytest" in review
    assert "exit code: 1" in review
    assert not os.path.exists(
        os.path.join(
            str(temp_workspace), "projects", "build_a_calculator_app", "review.md"
        )
    )
    # The job stopped before deploy.
    assert URLS["devops_url"] not in urls


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_custom_full_suite_gate_report_only_when_disabled(
    mock_exec, mock_call_api, temp_workspace, monkeypatch
):
    monkeypatch.setenv("GENIUS_FULL_SUITE_GATE", "0")
    urls, impl = _happy_impl(review_response="All good [APPROVED]")
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _exec_side_with_full_suite_rc(1)
    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        flow="custom",
        **URLS,
    )
    review_md = os.path.join(str(temp_workspace), "review.md")
    with open(review_md, encoding="utf-8") as f:
        review = f.read()
    assert "exit code: 1" in review  # still reported
    assert URLS["devops_url"] in urls  # but the run continued


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_custom_full_suite_exit_5_no_tests_is_a_pass(
    mock_exec, mock_call_api, temp_workspace
):
    """pytest exit 5 = nothing collected (docs-only project): not a failure."""
    urls, impl = _happy_impl(review_response="All good [APPROVED]")
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _exec_side_with_full_suite_rc(5)
    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        flow="custom",
        **URLS,
    )
    assert URLS["devops_url"] in urls


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_custom_workspace_review_md_carries_conformance_and_final_review(
    mock_exec, mock_call_api, temp_workspace
):
    """The workspace-root review.md (what the genius:// artifact serves) must
    tell the same story as the project copy: conformance report, whole-project
    pytest result AND the final-review section (a real run's root copy held
    only the one-line base summary)."""
    urls, impl = _happy_impl(
        review_response='```json\n{"blocking": false, "findings": [{"issue": "note"}]}\n```'
    )
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _exec_side_with_full_suite_rc(0)
    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        flow="custom",
        **URLS,
    )
    with open(os.path.join(str(temp_workspace), "review.md"), encoding="utf-8") as f:
        root_review = f.read()
    assert "## File conformance (design vs disk)" in root_review
    assert "## Whole-project pytest" in root_review
    assert "## Final review (non-blocking)" in root_review
    # Sequential default keeps the historical one-line review (byte-compat):
    # covered by the default-flow tests above, which never see these sections.


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_designed_test_modules_run_after_implementation_wave(
    mock_exec, mock_call_api, temp_workspace
):
    """Designed test modules execute directly against the real modules, so
    they fan out in a SECOND wave: a real run burned the designed test's
    whole retry budget racing mid-rewrite implementations (the final
    implementation was correct and would have passed)."""
    design = (
        '{"project_name":"p","description":"d","files":['
        '{"path":"test_foo.py","specification":"tests for foo"},'
        '{"path":"foo.py","specification":"make foo"}]}'
    )
    code_order = []

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        if prompt.startswith("/code"):
            name = prompt.split("'")[1]
            code_order.append(name)
            if name == "test_foo.py":
                return "```python\ndef test_foo():\n    assert True\n```"
            return "```python\ndef foo():\n    return 1\n```"
        if "Review the implemented project" in prompt:
            return "All good [APPROVED]"
        if url == URLS["tester_url"]:
            return "```python\ndef test_gen():\n    assert True\n```"
        if url == URLS["security_url"]:
            return '```json\n{"blocking": false}\n```'
        return f"```json\n{design}\n```"

    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        flow="custom",
        **URLS,
    )
    # The design lists test_foo.py FIRST, yet foo.py is implemented first.
    assert code_order == ["foo.py", "test_foo.py"]
    # Bonus cross-check: foo.py's generated test dodged the designed
    # test_foo.py basename AND lives in the internal dir, not the deliverable.
    internal = os.path.join(
        str(temp_workspace), ".genius", "build_a_calculator_app"
    )
    assert os.path.exists(os.path.join(internal, "tests", "test_foo_gen.py"))


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_designed_test_wave_skipped_when_impl_wave_failed(
    mock_exec, mock_call_api, temp_workspace
):
    """Strict mode: once an implementation file fails all retries the job is
    doomed, so the designed-test wave is skipped instead of burning its
    retry budget (worst case 3 x 300s timeouts) against known-broken code."""
    design = (
        '{"project_name":"p","description":"d","files":['
        '{"path":"foo.py","specification":"make foo"},'
        '{"path":"test_foo.py","specification":"tests for foo"}]}'
    )
    code_order = []

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        if prompt.startswith("/code"):
            name = prompt.split("'")[1]
            code_order.append(name)
            # foo.py is UNFIXABLE garbage -> fails invalid_python_feedback
            # on every attempt; the designed test must never be implemented.
            return "```python\ndef broken(((\n```"
        if "You are CriticReviewer" in prompt:
            return "Looks good [APPROVED]"
        if url == URLS["tester_url"]:
            return "```python\ndef test_gen():\n    assert True\n```"
        if url == URLS["security_url"]:
            return '```json\n{"blocking": false}\n```'
        return f"```json\n{design}\n```"

    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    from orchestrator import PipelineError as PE

    with pytest.raises(PE, match="Self-healing loop failed"):
        await run_pipeline(
            prompt="Build a calculator app",
            workspace=str(temp_workspace),
            flow="custom",
            max_retries=2,
            **URLS,
        )
    assert "foo.py" in code_order
    assert "test_foo.py" not in code_order  # designed-test wave skipped


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_generated_test_module_dodges_designed_basename_collision(
    mock_exec, mock_call_api, temp_workspace
):
    """When the design itself ships test_foo.py, the tester-generated module
    for foo.py must NOT reuse that basename (pytest 'import file mismatch' /
    silent overwrite): it becomes tests/test_foo_gen.py."""
    calls, impl = _happy_impl()
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    proj = str(temp_workspace / "proj")
    await process_single_file(
        **_psf_args(proj, calls),
        designed_basenames={"foo.py", "test_foo.py"},
    )
    # Fallback internal layout (proj is not a projects/<slug> dir).
    gen_tests = os.path.join(proj, ".genius", "tests")
    assert os.path.exists(os.path.join(gen_tests, "test_foo_gen.py"))
    assert not os.path.exists(os.path.join(gen_tests, "test_foo.py"))


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_generated_test_module_keeps_legacy_name_without_collision(
    mock_exec, mock_call_api, temp_workspace
):
    calls, impl = _happy_impl()
    mock_call_api.side_effect = impl
    mock_exec.side_effect = _mock_exec
    proj = str(temp_workspace / "proj2")
    await process_single_file(
        **_psf_args(proj, calls),
        designed_basenames={"foo.py"},
    )
    assert os.path.exists(os.path.join(proj, ".genius", "tests", "test_foo.py"))
