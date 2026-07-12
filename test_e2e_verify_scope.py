"""M6: the E2E per-file verification must run pytest scoped to each file's own
test, never the whole tests/ directory (which races across the concurrently
processed sibling files)."""

import hashlib
import json
import os
import sys

import httpx
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orchestrator import run_e2e_pipeline  # noqa: E402


def _resp(body):
    body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return httpx.Response(
        200,
        content=body_bytes,
        headers={"X-Payload-SHA256": hashlib.sha256(body_bytes).hexdigest()},
        request=httpx.Request("POST", "http://localhost/mock"),
    )


@pytest.mark.asyncio
@patch("httpx.AsyncClient.post", new_callable=MagicMock)
@patch("httpx.AsyncClient.get", new_callable=MagicMock)
@patch("orchestrator.run_subprocess", new_callable=MagicMock)
async def test_e2e_pytest_is_scoped_per_file(mock_sub, mock_get, mock_post, tmp_path):
    async def post_side_effect(url, **kwargs):
        payload = json.loads(kwargs.get("content", b"{}").decode("utf-8"))
        prompt = payload.get("prompt", "")
        if "plan" in prompt:
            tid = "claude-plan"
        elif "CriticReviewer" in prompt:
            tid = "grok-critique"
        elif "unit-test" in prompt:
            tid = "tester-test"
        else:
            tid = "codex-code"
        return _resp({"status": "processing", "task_id": tid})

    mock_post.side_effect = post_side_effect

    # A two-file plan so the concurrent per-file processing is exercised.
    plan = {
        "files": [
            {"path": "src/alpha.py", "specification": "alpha"},
            {"path": "src/beta.py", "specification": "beta"},
        ]
    }

    async def get_side_effect(url, **kwargs):
        u = str(url)
        if "claude-plan" in u:
            content = "```json\n" + json.dumps(plan) + "\n```"
        elif "grok-critique" in u:
            content = "[APPROVED]"
        elif "codex-code" in u:
            content = "```python\ndef f():\n    return 1\n```"
        else:  # tester-test
            content = "```python\ndef test_f():\n    assert True\n```"
        return _resp({"status": "completed", "result": content})

    mock_get.side_effect = get_side_effect

    pytest_targets = []

    async def sub_record(cmd, env=None, cwd=None):
        # cmd: [python, "-m", "pytest"|"flake8", <target>]
        if "pytest" in cmd:
            pytest_targets.append(cmd[-1])
        return (0, "ok")

    mock_sub.side_effect = sub_record

    result = await run_e2e_pipeline(
        prompt="build two files",
        workspace=str(tmp_path),
        max_debate_rounds=0,
        max_retries=2,
    )
    assert result == "E2E Pipeline execution completed successfully."

    # At least one pytest run happened (the Tester verification per file).
    assert pytest_targets, "expected pytest to run during verification"

    project_dir = os.path.join(str(tmp_path), "projects", "build_two_files")
    tests_dir = os.path.join(project_dir, "tests")

    for target in pytest_targets:
        # Never the whole tests/ directory (the racy behavior M6 removes).
        assert target != tests_dir
        # Always a concrete per-file test module.
        base = os.path.basename(target)
        assert base.startswith("test_") and base.endswith(".py"), target

    # Both files' own tests were exercised.
    bases = {os.path.basename(t) for t in pytest_targets}
    assert "test_src_alpha.py" in bases
    assert "test_src_beta.py" in bases


@pytest.mark.asyncio
@patch("httpx.AsyncClient.post", new_callable=MagicMock)
@patch("httpx.AsyncClient.get", new_callable=MagicMock)
@patch("orchestrator.run_subprocess", new_callable=MagicMock)
async def test_e2e_skips_testgen_for_nonpython_infra_and_test_modules(
    mock_sub, mock_get, mock_post, tmp_path
):
    """The E2E Tester loop must generate tests ONLY for regular Python
    sources: never for non-Python files (a pytest module "importing" README.md
    can never pass and dooms the run), pytest modules (no tests-for-tests), or
    pytest infrastructure (conftest.py/__init__.py)."""
    prompts = []

    async def post_side_effect(url, **kwargs):
        payload = json.loads(kwargs.get("content", b"{}").decode("utf-8"))
        prompt = payload.get("prompt", "")
        prompts.append(prompt)
        if "plan" in prompt:
            tid = "claude-plan"
        elif "unit-test" in prompt:
            tid = "tester-test"
        else:
            tid = "codex-code"
        return _resp({"status": "processing", "task_id": tid})

    mock_post.side_effect = post_side_effect

    plan = {
        "files": [
            {"path": "README.md", "specification": "project readme"},
            {"path": "conftest.py", "specification": "shared fixtures"},
            {"path": "tests/test_gamma.py", "specification": "gamma checks"},
            {"path": "src/gamma.py", "specification": "gamma module"},
        ]
    }

    async def get_side_effect(url, **kwargs):
        u = str(url)
        if "claude-plan" in u:
            content = "```json\n" + json.dumps(plan) + "\n```"
        elif "codex-code" in u:
            content = "```python\ndef f():\n    return 1\n```"
        else:  # tester-test
            content = "```python\ndef test_f():\n    assert True\n```"
        return _resp({"status": "completed", "result": content})

    mock_get.side_effect = get_side_effect

    async def sub_ok(cmd, env=None, cwd=None):
        return (0, "ok")

    mock_sub.side_effect = sub_ok

    result = await run_e2e_pipeline(
        prompt="build mixed files",
        workspace=str(tmp_path),
        max_debate_rounds=0,
        max_retries=2,
    )
    assert result == "E2E Pipeline execution completed successfully."

    # Exactly one Tester call, and it targets the regular Python source.
    testgen = [p for p in prompts if p.startswith("/unit-test")]
    assert len(testgen) == 1, testgen
    assert "src/gamma.py" in testgen[0]

    # Only gamma's generated test module exists on disk — under the
    # pipeline-internal dir, not in the deliverable project.
    tests_dir = os.path.join(
        str(tmp_path), ".genius", "build_mixed_files", "tests"
    )
    generated = {
        name
        for name in os.listdir(tests_dir)
        # The plan's own tests/test_gamma.py lives here too; generated
        # modules are the flattened test_<path>.py names.
        if name.startswith("test_") and name != "test_gamma.py"
    }
    assert generated == {"test_src_gamma.py"}
