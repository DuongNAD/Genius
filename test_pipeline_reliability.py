"""Tests for the pipeline-reliability upgrades: the design-format retry
(production-only self-heal), the tests-for-tests skip, and raw response
capture."""

import asyncio
import json
import os

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

import orchestrator
from orchestrator import (
    is_test_module,
    is_pytest_infra,
    save_raw_response,
    PipelineError,
)
from ag_core.config import load_config
from ag_core.utils.message_bus import MessageBus


# --- is_test_module ----------------------------------------------------------


def test_is_test_module_detects_pytest_modules():
    assert is_test_module("tests/test_core.py")
    assert is_test_module("test_app.py")
    assert is_test_module("pkg/tests/test_x.py")
    assert is_test_module("tests\\test_win.py")


def test_is_test_module_ignores_regular_sources():
    assert not is_test_module("src/main.py")
    assert not is_test_module("contest.py")
    assert not is_test_module("latest_results.py")
    assert not is_test_module("src/testing_utils.py")


# --- is_pytest_infra ----------------------------------------------------------


def test_is_pytest_infra_detects_conftest_and_init():
    assert is_pytest_infra("conftest.py")
    assert is_pytest_infra("tests/conftest.py")
    assert is_pytest_infra("pkg/__init__.py")
    assert is_pytest_infra("src\\__init__.py")


def test_is_pytest_infra_ignores_regular_sources():
    assert not is_pytest_infra("src/main.py")
    assert not is_pytest_infra("palindrome.py")
    assert not is_pytest_infra("test_conftest.py")  # a real test module, not infra


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_root_conftest_skips_testgen_and_pytest_run(
    mock_exec, mock_call_api, tmp_path, monkeypatch
):
    """A root conftest.py (pytest infra outside tests/) gets NO generated test
    module AND NO pytest execution: running pytest on the never-written
    tests/test_conftest.py exits 4 and dooms the self-heal loop. Verification
    is the security audit alone (like non-Python files)."""
    monkeypatch.chdir(tmp_path)
    prompts = []

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        prompts.append(prompt)
        if prompt.startswith("/code"):
            return "```python\nimport pytest  # noqa: F401\n```"
        return '```json\n{"blocking": false}\n```'  # security audit

    mock_call_api.side_effect = impl

    proj = str(tmp_path)
    os.makedirs(os.path.join(proj, "logs"), exist_ok=True)
    mb = MessageBus(db_path=os.path.join(proj, "logs", "mb.db"))
    await orchestrator.process_single_file(
        file_info={"path": "conftest.py", "specification": "shared fixtures"},
        project_dir=proj,
        config=load_config(),
        codex_url="http://codex",
        tester_url="http://tester",
        security_url="http://security",
        api_key="key",
        client=MagicMock(),
        poll_timeout=60.0,
        max_retries=1,
        semaphore=asyncio.Semaphore(1),
        message_bus=mb,
        parent_art_id=None,
    )
    # The audit ran; the tester never did; no pytest subprocess was spawned on
    # the never-written test module; the file itself was written.
    assert any(p.startswith("/audit") for p in prompts)
    assert not any(p.startswith("/unit-test") for p in prompts)
    mock_exec.assert_not_called()
    assert not os.path.exists(os.path.join(proj, "tests", "test_conftest.py"))
    assert os.path.exists(os.path.join(proj, "conftest.py"))


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_tester_retry_sees_previous_test_failures(
    mock_exec, mock_call_api, tmp_path, monkeypatch
):
    """When verification fails, the NEXT tester prompt must carry the failing
    pytest log: the coder alone cannot fix a WRONG generated test (a real run
    failed 3/3 attempts on an over-mocked test asserting per-chunk I/O
    internals against correct code)."""
    monkeypatch.chdir(tmp_path)
    prompts = []

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        prompts.append(prompt)
        if prompt.startswith("/code"):
            return "```python\ndef f():\n    return 1\n```"
        if prompt.startswith("/unit-test"):
            return "```python\ndef test_f():\n    assert True\n```"
        return '```json\n{"blocking": false}\n```'  # security audit

    mock_call_api.side_effect = impl

    rcs = iter([1, 0])  # first pytest run fails, second passes

    async def exec_side_effect(*args, **kwargs):
        proc = MagicMock()
        proc.returncode = next(rcs)

        async def comm():
            return (b"FAILED tests/test_foo.py::test_chunk - assert 'abc 1'", b"")

        proc.communicate = comm
        return proc

    mock_exec.side_effect = exec_side_effect

    proj = str(tmp_path)
    os.makedirs(os.path.join(proj, "logs"), exist_ok=True)
    os.makedirs(os.path.join(proj, "tests"), exist_ok=True)  # run_pipeline's job
    mb = MessageBus(db_path=os.path.join(proj, "logs", "mb.db"))
    await orchestrator.process_single_file(
        file_info={"path": "foo.py", "specification": "make foo"},
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
    )
    testgen = [p for p in prompts if p.startswith("/unit-test")]
    assert len(testgen) == 2
    assert "Previous failures" not in testgen[0]
    assert "Previous failures" in testgen[1]
    assert "abc 1" in testgen[1]  # the actual failing log reached the tester


# --- architect design-quality gates -------------------------------------------


def test_architect_contract_has_design_quality_gates():
    """The architect's system prompt carries the five design-quality gates
    distilled from a real external review of a generated plan (contract vs
    algorithm mismatch on Unicode casing, an unnecessary conftest.py, claimed
    capabilities left as 'optional' tests, no assumptions traceability, and
    heavy repetition)."""
    from ag_core.agents.claude_architect import ARCHITECT_SYSTEM_PROMPT

    for marker in (
        "DESIGN QUALITY GATES",
        "CONTRACT-ALGORITHM CONSISTENCY",
        "MINIMAL LAYOUT",
        "TEST-LOCKED CLAIMS",
        "TRACEABILITY",
        "NO REPETITION",
    ):
        assert marker in ARCHITECT_SYSTEM_PROMPT, marker


def test_architect_examples_cover_both_task_classes():
    """A single application-shaped example anchors the model (a real run
    planned src/ + conftest.py for a one-function utility). The prompt now
    shows a tiny-utility root layout AND an application layout, telling the
    model to classify the task first."""
    from ag_core.agents.claude_architect import ARCHITECT_SYSTEM_PROMPT

    assert "Task class A" in ARCHITECT_SYSTEM_PROMPT
    assert '"palindrome.py"' in ARCHITECT_SYSTEM_PROMPT  # root layout example
    assert "Task class B" in ARCHITECT_SYSTEM_PROMPT
    assert '"src/main.py"' in ARCHITECT_SYSTEM_PROMPT  # app layout example


def test_researcher_brief_must_be_self_contained():
    """The research brief is the architect's ground truth: it must restate the
    verbatim request and inline referenced content, never point elsewhere (a
    real run's research.md was just a pointer to an external artifact, so
    requirements could not be traced)."""
    from ag_core.utils.prompt_templates import RESEARCHER_PROMPT

    assert "SELF-CONTAINED" in RESEARCHER_PROMPT
    assert "Original Request" in RESEARCHER_PROMPT


# --- deterministic design lint -------------------------------------------------


def test_lint_design_plan_flags_blocking_defects():
    files = [
        {"path": "a.py", "specification": "ok"},
        {"path": "a.py", "specification": "dup"},
        {"path": "/abs/b.py", "specification": "abs"},
        {"path": "../escape.py", "specification": "esc"},
        {"path": "c.py", "specification": "   "},
        {"path": "", "specification": "no path"},
    ]
    blocking, warnings = orchestrator.lint_design_plan(files, "")
    text = "; ".join(blocking)
    assert "duplicate file path" in text
    assert "absolute path" in text
    assert "escapes the project root" in text
    assert "empty specification" in text
    assert "empty path" in text
    assert warnings == []


def test_lint_design_plan_clean_plan_and_clarification_warning():
    files = [{"path": "x.py", "specification": "do x"}]
    blocking, warnings = orchestrator.lint_design_plan(
        files, "design text [NEEDS CLARIFICATION: which timezone?]"
    )
    assert blocking == []
    assert len(warnings) == 1 and "NEEDS CLARIFICATION" in warnings[0]


def test_lint_design_plan_windows_paths():
    blocking, _ = orchestrator.lint_design_plan(
        [
            {"path": "C:\\evil.py", "specification": "x"},
            {"path": "src\\ok.py", "specification": "x"},
        ],
        "",
    )
    assert any("absolute path" in b for b in blocking)
    assert not any("ok.py" in b for b in blocking)


_DUP_DESIGN = (
    "```json\n"
    + json.dumps(
        {
            "project_name": "demo",
            "description": "d",
            "files": [
                {"path": "src/thing.py", "specification": "make thing"},
                {"path": "src/thing.py", "specification": "make it again"},
            ],
        }
    )
    + "\n```"
)


@pytest.mark.asyncio
async def test_design_lint_retry_fixes_duplicate_paths(tmp_path, monkeypatch):
    """A parseable-but-defective plan (duplicate paths would double-implement
    the same file concurrently) gets ONE corrective re-prompt carrying the
    lint findings, then the fixed plan is built."""
    monkeypatch.setattr(orchestrator, "design_selfheal_enabled", lambda: True)

    calls = []

    async def fake_call_api(url, api_key, prompt, **kwargs):
        calls.append(prompt)
        n = len(calls)
        if n == 1:
            return "research brief"
        if n == 2:
            return _DUP_DESIGN  # parseable, but lint-blocking
        if n == 3:
            return _GOOD_DESIGN  # the lint retry fixes it
        if "unit-test" in prompt:
            return _PASSING_TEST_MODULE
        if "/audit" in prompt:
            return _CLEAN_AUDIT
        if n == 4:
            return "```python\ndef ok():\n    return 1\n```"  # codex
        return "deploy plan"  # devops

    with patch("orchestrator.call_api", new=AsyncMock(side_effect=fake_call_api)):
        await orchestrator.run_pipeline(
            prompt="build demo",
            workspace=str(tmp_path),
            max_debate_rounds=0,
            max_retries=1,
        )

    # The 3rd call is the lint retry, carrying the concrete findings.
    assert "deterministic checks" in calls[2]
    assert "duplicate file path" in calls[2]
    project_dirs = list((tmp_path / "projects").iterdir())
    assert (project_dirs[0] / "src" / "thing.py").is_file()
    raw_names = os.listdir(project_dirs[0] / "logs" / "raw")
    assert any(n.startswith("design_lint_retry1") for n in raw_names)


@pytest.mark.asyncio
async def test_design_lint_exhaustion_raises(tmp_path, monkeypatch):
    """If the corrective retry still fails lint, the run aborts BEFORE any
    coder tokens are spent."""
    monkeypatch.setattr(orchestrator, "design_selfheal_enabled", lambda: True)

    calls = []

    async def fake_call_api(url, api_key, prompt, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            return "research brief"
        return _DUP_DESIGN  # the design AND the lint retry stay defective

    with patch("orchestrator.call_api", new=AsyncMock(side_effect=fake_call_api)):
        with pytest.raises(PipelineError, match="deterministic lint"):
            await orchestrator.run_pipeline(
                prompt="build demo",
                workspace=str(tmp_path),
                max_debate_rounds=0,
                max_retries=1,
            )
    # research + design + one lint retry — never a /code call.
    assert len(calls) == 3
    assert not any(c.startswith("/code") for c in calls)


# --- save_raw_response --------------------------------------------------------


def test_save_raw_response_writes_sanitized_file(tmp_path):
    save_raw_response(str(tmp_path), "codex a/b:attempt#1", "payload")
    raw_dir = tmp_path / "logs" / "raw"
    files = list(raw_dir.iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".md"
    assert files[0].read_text(encoding="utf-8") == "payload"
    # No path separators survive in the file name.
    assert "/" not in files[0].name and ":" not in files[0].name


def test_save_raw_response_never_raises(tmp_path):
    # Point at an unwritable location (a FILE where the logs dir should be).
    (tmp_path / "logs").write_text("blocker", encoding="utf-8")
    save_raw_response(str(tmp_path), "x", "y")  # must not raise


# --- design-format retry ------------------------------------------------------

_GOOD_DESIGN = (
    "```json\n"
    + json.dumps(
        {
            "project_name": "demo",
            "description": "d",
            "files": [{"path": "src/thing.py", "specification": "A function ok()."}],
        }
    )
    + "\n```"
)

_PASSING_TEST_MODULE = "```python\ndef test_ok():\n    assert True\n```"
_CLEAN_AUDIT = '```json\n{"blocking": false, "findings": []}\n```'


def test_design_selfheal_disabled_under_pytest():
    # The suite itself must see the knob off (legacy branch stays reachable).
    assert orchestrator.design_selfheal_enabled() is False


@pytest.mark.asyncio
async def test_design_retry_reprompts_architect_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "design_selfheal_enabled", lambda: True)

    calls = []

    async def fake_call_api(url, api_key, prompt, **kwargs):
        calls.append(prompt)
        n = len(calls)
        if n == 1:
            return "research brief"
        if n == 2:
            return "a markdown design with no json block"
        if n == 3:
            return _GOOD_DESIGN  # the retry fixes the format
        if "unit-test" in prompt:
            return _PASSING_TEST_MODULE
        if "/audit" in prompt:
            return _CLEAN_AUDIT
        if n == 4:
            return "```python\ndef ok():\n    return 1\n```"  # codex
        return "deploy plan"  # devops

    with patch("orchestrator.call_api", new=AsyncMock(side_effect=fake_call_api)):
        await orchestrator.run_pipeline(
            prompt="build demo",
            workspace=str(tmp_path),
            max_debate_rounds=0,
            max_retries=1,
        )

    # The 3rd call is the format retry, carrying explicit feedback.
    assert "could not be parsed" in calls[2]
    assert "DesignPlan" in calls[2]
    # The pipeline went on to implement the planned file.
    project_dirs = list((tmp_path / "projects").iterdir())
    assert len(project_dirs) == 1
    assert (project_dirs[0] / "src" / "thing.py").is_file()
    # Raw responses were captured for the retry and the codex attempt.
    raw_names = os.listdir(project_dirs[0] / "logs" / "raw")
    assert any(n.startswith("design_retry1") for n in raw_names)
    assert any(n.startswith("codex_") for n in raw_names)


_README_DESIGN = (
    "```json\n"
    + json.dumps(
        {
            "project_name": "demo",
            "description": "d",
            "files": [{"path": "README.md", "specification": "Project readme."}],
        }
    )
    + "\n```"
)


@pytest.mark.asyncio
async def test_non_python_file_skips_tester_and_pytest(tmp_path):
    # Regression: a designed README.md used to get a generated pytest module
    # against a markdown "module" and failed every self-heal attempt.
    calls = []

    async def fake_call_api(url, api_key, prompt, **kwargs):
        calls.append(prompt)
        n = len(calls)
        if n == 1:
            return "research brief"
        if n == 2:
            return _README_DESIGN
        if "/audit" in prompt:
            return _CLEAN_AUDIT
        if n == 3:
            return "# Demo\n\nA readme."  # codex writes the file content
        return "deploy plan"

    with patch("orchestrator.call_api", new=AsyncMock(side_effect=fake_call_api)):
        await orchestrator.run_pipeline(
            prompt="build demo",
            workspace=str(tmp_path),
            max_debate_rounds=0,
            max_retries=1,
        )

    project_dirs = list((tmp_path / "projects").iterdir())
    assert (project_dirs[0] / "README.md").is_file()
    # No unit-test generation was requested for the markdown file.
    assert not any("unit-test" in c for c in calls)
    # The security audit still ran.
    assert any("/audit" in c for c in calls)


@pytest.mark.asyncio
async def test_design_retry_exhaustion_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "design_selfheal_enabled", lambda: True)

    async def fake_call_api(url, api_key, prompt, **kwargs):
        if "research" not in getattr(fake_call_api, "seen", []):
            fake_call_api.seen = ["research"]
            return "research brief"
        return "still not a design plan"

    with patch("orchestrator.call_api", new=AsyncMock(side_effect=fake_call_api)):
        with pytest.raises(PipelineError, match="no parseable DesignPlan"):
            await orchestrator.run_pipeline(
                prompt="build demo",
                workspace=str(tmp_path),
                max_debate_rounds=0,
                max_retries=1,
            )
