import os
import sys
import pytest
import asyncio
import httpx
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orchestrator import run_e2e_pipeline, PipelineError

@pytest.fixture
def temp_workspace(tmp_path, monkeypatch):
    """Fixture that moves to a temp directory and returns it as a workspace."""
    monkeypatch.chdir(tmp_path)
    return tmp_path

def create_mock_http_response(status_code, content_str, url="http://localhost/mock"):
    import hashlib
    import json
    body = {"status": "completed", "result": content_str}
    body_bytes = json.dumps(body, separators=(',', ':')).encode("utf-8")
    checksum = hashlib.sha256(body_bytes).hexdigest()
    request = httpx.Request("GET", str(url))
    return httpx.Response(status_code, content=body_bytes, headers={"X-Payload-SHA256": checksum}, request=request)

@pytest.mark.asyncio
@patch("httpx.AsyncClient.post", new_callable=MagicMock)
@patch("httpx.AsyncClient.get", new_callable=MagicMock)
@patch("orchestrator.run_subprocess", new_callable=MagicMock)
async def test_e2e_happy_path(mock_run_subproc, mock_get, mock_post, temp_workspace):
    """Test happy path: Claude -> Grok approved -> Codex code -> Tester test -> execution success."""
    
    # 1. Mock call_api posting task
    async def post_side_effect(url, **kwargs):
        import json
        payload = json.loads(kwargs.get("content", b"{}").decode("utf-8"))
        prompt = payload.get("prompt", "")
        
        task_id = "task-id"
        if "plan" in prompt:
            task_id = "claude-plan"
        elif "GrokReviewer" in prompt:
            task_id = "grok-critique"
        elif "unit-test" in prompt:
            task_id = "tester-test"
        elif "code" in prompt:
            task_id = "codex-code"
            
        body = {"status": "processing", "task_id": task_id}
        import hashlib
        body_bytes = json.dumps(body, separators=(',', ':')).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        request = httpx.Request("POST", str(url))
        return httpx.Response(200, content=body_bytes, headers={"X-Payload-SHA256": checksum}, request=request)
    mock_post.side_effect = post_side_effect

    # 2. Mock call_api status poll
    async def get_side_effect(url, **kwargs):
        content = ""
        if "claude-plan" in str(url):
            content = """
            Here is the plan.
            ```json
            {
              "files": [
                {
                  "path": "src/hello.py",
                  "specification": "Implement hello function"
                }
              ]
            }
            ```
            """
        elif "grok-critique" in str(url):
            # Grok approves plan
            content = "[APPROVED] Plan is perfect."
        elif "codex-code" in str(url):
            content = "```python\ndef hello():\n    return 'world'\n```"
        elif "tester-test" in str(url):
            content = "```python\nfrom src.hello import hello\ndef test_hello():\n    assert hello() == 'world'\n```"
            
        return create_mock_http_response(200, content, url=url)
    mock_get.side_effect = get_side_effect

    # 3. Mock run_subprocess to always succeed
    async def subproc_happy(cmd, env=None):
        return (0, "Success/Pass output")
    mock_run_subproc.side_effect = subproc_happy

    # Run E2E pipeline
    result = await run_e2e_pipeline(
        prompt="Write hello world",
        workspace=str(temp_workspace),
        max_debate_rounds=1,
        max_retries=3
    )

    assert result == "E2E Pipeline execution completed successfully."

    # Verify files created
    plan_path = temp_workspace / "plan.md"
    project_dir = temp_workspace / "projects" / "write_hello_world"
    proj_plan_path = project_dir / "plan.md"
    src_file_path = project_dir / "src" / "hello.py"
    test_file_path = project_dir / "tests" / "test_hello.py"
    prog_file_path = temp_workspace / "CURRENT_PROG.md"

    assert plan_path.exists()
    assert proj_plan_path.exists()
    assert src_file_path.exists()
    assert test_file_path.exists()
    assert prog_file_path.exists()

    assert "def hello():" in src_file_path.read_text()
    assert "def test_hello():" in test_file_path.read_text()


@pytest.mark.asyncio
@patch("httpx.AsyncClient.post", new_callable=MagicMock)
@patch("httpx.AsyncClient.get", new_callable=MagicMock)
@patch("orchestrator.run_subprocess", new_callable=MagicMock)
async def test_e2e_codex_self_healing_success(mock_run_subproc, mock_get, mock_post, temp_workspace):
    """Test Codex self-healing: first check fails, retry succeeds."""
    
    async def post_side_effect(url, **kwargs):
        body = {"status": "processing", "task_id": "generic-task"}
        import json, hashlib
        body_bytes = json.dumps(body, separators=(',', ':')).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        request = httpx.Request("POST", str(url))
        return httpx.Response(200, content=body_bytes, headers={"X-Payload-SHA256": checksum}, request=request)
    mock_post.side_effect = post_side_effect

    plan_content = '```json\n{"files": [{"path": "src/hello.py", "specification": "hello"}]}\n```'
    code_content = "```python\ndef hello(): pass\n```"
    test_content = "```python\ndef test_hello(): pass\n```"

    get_calls = 0
    async def get_side_effect(url, **kwargs):
        nonlocal get_calls
        get_calls += 1
        # 1st call: plan, 2nd call: 1st code, 3rd call: 2nd code (on retry), 4th call: test
        if get_calls == 1:
            return create_mock_http_response(200, plan_content, url=url)
        elif get_calls in (2, 3):
            return create_mock_http_response(200, code_content, url=url)
        else:
            return create_mock_http_response(200, test_content, url=url)
    mock_get.side_effect = get_side_effect

    # Mock subprocess run:
    # First Codex run runs flake8. Let's make it fail.
    # Second Codex run (retry) runs flake8. Let's make it pass.
    # Tester runs pytest. Let's make it pass.
    subproc_calls = []
    async def subproc_side_effect(cmd, env=None):
        subproc_calls.append(cmd)
        if "flake8" in cmd[2]:
            if len(subproc_calls) == 1:
                return (1, "Flake8 Error: missing space")
            return (0, "")
        return (0, "Success")
    mock_run_subproc.side_effect = subproc_side_effect

    result = await run_e2e_pipeline(
        prompt="Write hello world",
        workspace=str(temp_workspace),
        max_debate_rounds=0,
        max_retries=2
    )

    assert result == "E2E Pipeline execution completed successfully."
    # Flake8 ran twice
    flake8_cmds = [c for c in subproc_calls if "flake8" in c[2]]
    assert len(flake8_cmds) == 2


@pytest.mark.asyncio
@patch("httpx.AsyncClient.post", new_callable=MagicMock)
@patch("httpx.AsyncClient.get", new_callable=MagicMock)
@patch("orchestrator.run_subprocess", new_callable=MagicMock)
async def test_e2e_codex_self_healing_failure(mock_run_subproc, mock_get, mock_post, temp_workspace):
    """Test Codex self-healing: persistent failure raises PipelineError."""
    
    async def post_side_effect(url, **kwargs):
        body = {"status": "processing", "task_id": "generic-task"}
        import json, hashlib
        body_bytes = json.dumps(body, separators=(',', ':')).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        request = httpx.Request("POST", str(url))
        return httpx.Response(200, content=body_bytes, headers={"X-Payload-SHA256": checksum}, request=request)
    mock_post.side_effect = post_side_effect

    plan_content = '```json\n{"files": [{"path": "src/hello.py", "specification": "hello"}]}\n```'
    code_content = "```python\ndef hello(): pass\n```"

    async def get_side_effect(url, **kwargs):
        if "status" in str(url):
            # Always return code content when requested
            if "plan" in str(url) or len(mock_get.call_args_list) == 1:
                return create_mock_http_response(200, plan_content, url=url)
            return create_mock_http_response(200, code_content, url=url)
    mock_get.side_effect = get_side_effect

    # Subprocess runs always fail for flake8
    async def subproc_fail(cmd, env=None):
        return (1, "Persistent Flake8 Error")
    mock_run_subproc.side_effect = subproc_fail

    with pytest.raises(PipelineError) as exc_info:
        await run_e2e_pipeline(
            prompt="Write hello world",
            workspace=str(temp_workspace),
            max_debate_rounds=0,
            max_retries=2
        )
    
    assert "Codex self-healing failed" in str(exc_info.value)


@pytest.mark.asyncio
@patch("httpx.AsyncClient.post", new_callable=MagicMock)
@patch("httpx.AsyncClient.get", new_callable=MagicMock)
@patch("orchestrator.run_subprocess", new_callable=MagicMock)
async def test_e2e_tester_self_healing_success(mock_run_subproc, mock_get, mock_post, temp_workspace):
    """Test Tester self-healing: first check fails, retry succeeds."""
    
    async def post_side_effect(url, **kwargs):
        body = {"status": "processing", "task_id": "generic-task"}
        import json, hashlib
        body_bytes = json.dumps(body, separators=(',', ':')).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        request = httpx.Request("POST", str(url))
        return httpx.Response(200, content=body_bytes, headers={"X-Payload-SHA256": checksum}, request=request)
    mock_post.side_effect = post_side_effect

    plan_content = '```json\n{"files": [{"path": "src/hello.py", "specification": "hello"}]}\n```'
    code_content = "```python\ndef hello(): pass\n```"
    test_content = "```python\ndef test_hello(): pass\n```"

    get_calls = 0
    async def get_side_effect(url, **kwargs):
        nonlocal get_calls
        get_calls += 1
        if get_calls == 1:
            return create_mock_http_response(200, plan_content, url=url)
        elif get_calls == 2:
            return create_mock_http_response(200, code_content, url=url)
        else:
            return create_mock_http_response(200, test_content, url=url)
    mock_get.side_effect = get_side_effect

    # Subprocess logic:
    # Codex flake8 pass.
    # Tester pytest 1st run fails.
    # Tester pytest 2nd run (retry) passes.
    subproc_calls = []
    async def subproc_side_effect(cmd, env=None):
        subproc_calls.append(cmd)
        if "flake8" in cmd[2]:
            return (0, "")
        # Pytest run
        pytest_runs = [c for c in subproc_calls if "pytest" in c[1]]
        if len(pytest_runs) == 1:
            return (1, "AssertionError in tests")
        return (0, "All tests passed")
    mock_run_subproc.side_effect = subproc_side_effect

    result = await run_e2e_pipeline(
        prompt="Write hello world",
        workspace=str(temp_workspace),
        max_debate_rounds=0,
        max_retries=2
    )

    assert result == "E2E Pipeline execution completed successfully."


@pytest.mark.asyncio
@patch("httpx.AsyncClient.post", new_callable=MagicMock)
@patch("httpx.AsyncClient.get", new_callable=MagicMock)
@patch("orchestrator.run_subprocess", new_callable=MagicMock)
async def test_e2e_tester_self_healing_failure(mock_run_subproc, mock_get, mock_post, temp_workspace):
    """Test Tester self-healing: persistent failure raises PipelineError."""
    
    async def post_side_effect(url, **kwargs):
        body = {"status": "processing", "task_id": "generic-task"}
        import json, hashlib
        body_bytes = json.dumps(body, separators=(',', ':')).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        request = httpx.Request("POST", str(url))
        return httpx.Response(200, content=body_bytes, headers={"X-Payload-SHA256": checksum}, request=request)
    mock_post.side_effect = post_side_effect

    plan_content = '```json\n{"files": [{"path": "src/hello.py", "specification": "hello"}]}\n```'
    code_content = "```python\ndef hello(): pass\n```"
    test_content = "```python\ndef test_hello(): pass\n```"

    async def get_side_effect(url, **kwargs):
        if "plan" in str(url) or len(mock_get.call_args_list) == 1:
            return create_mock_http_response(200, plan_content, url=url)
        elif len(mock_get.call_args_list) == 2:
            return create_mock_http_response(200, code_content, url=url)
        else:
            return create_mock_http_response(200, test_content, url=url)
    mock_get.side_effect = get_side_effect

    # Flake8 passes, pytest fails persistently
    async def subproc_side_effect(cmd, env=None):
        if "flake8" in cmd[2]:
            return (0, "")
        return (1, "Persistent AssertionError")
    mock_run_subproc.side_effect = subproc_side_effect

    with pytest.raises(PipelineError) as exc_info:
        await run_e2e_pipeline(
            prompt="Write hello world",
            workspace=str(temp_workspace),
            max_debate_rounds=0,
            max_retries=2
        )
    
    assert "Tester self-healing failed" in str(exc_info.value)


@pytest.mark.asyncio
@patch("httpx.AsyncClient.post", new_callable=MagicMock)
@patch("httpx.AsyncClient.get", new_callable=MagicMock)
@patch("orchestrator.run_subprocess", new_callable=MagicMock)
async def test_e2e_pythonpath_setting(mock_run_subproc, mock_get, mock_post, temp_workspace):
    """Test that PYTHONPATH is correctly set in run_subprocess environment."""
    
    async def post_side_effect(url, **kwargs):
        body = {"status": "processing", "task_id": "generic-task"}
        import json, hashlib
        body_bytes = json.dumps(body, separators=(',', ':')).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        request = httpx.Request("POST", str(url))
        return httpx.Response(200, content=body_bytes, headers={"X-Payload-SHA256": checksum}, request=request)
    mock_post.side_effect = post_side_effect

    plan_content = '```json\n{"files": [{"path": "src/hello.py", "specification": "hello"}]}\n```'
    code_content = "```python\ndef hello(): pass\n```"
    test_content = "```python\ndef test_hello(): pass\n```"

    async def get_side_effect(url, **kwargs):
        if len(mock_get.call_args_list) == 1:
            return create_mock_http_response(200, plan_content, url=url)
        elif len(mock_get.call_args_list) == 2:
            return create_mock_http_response(200, code_content, url=url)
        else:
            return create_mock_http_response(200, test_content, url=url)
    mock_get.side_effect = get_side_effect

    subproc_envs = []
    async def subproc_side_effect(cmd, env=None):
        subproc_envs.append(env)
        return (0, "Success")
    mock_run_subproc.side_effect = subproc_side_effect

    await run_e2e_pipeline(
        prompt="Write hello world",
        workspace=str(temp_workspace),
        max_debate_rounds=0,
        max_retries=1
    )

    # Verify PYTHONPATH exists in captured environment dictionaries
    assert len(subproc_envs) > 0
    for env in subproc_envs:
        assert env is not None
        assert "PYTHONPATH" in env
        pythonpath = env["PYTHONPATH"]
        project_dir_str = str(temp_workspace / "projects" / "write_hello_world")
        project_src_dir_str = str(temp_workspace / "projects" / "write_hello_world" / "src")
        assert project_dir_str in pythonpath
        assert project_src_dir_str in pythonpath
