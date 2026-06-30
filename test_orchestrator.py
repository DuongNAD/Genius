import os
import sys
import pytest
import asyncio
import httpx
from unittest.mock import patch, MagicMock

# Add current workspace to path to import orchestrator
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orchestrator import (
    run_pipeline,
    main,
    PipelineError,
    resolve_antigravity_cmd,
    resolve_claude_cmd,
)


@pytest.fixture
def temp_workspace(tmp_path, monkeypatch):
    """Fixture that moves to a temp directory and returns it as a workspace."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.asyncio
@patch("httpx.AsyncClient.post", new_callable=MagicMock)
@patch("httpx.AsyncClient.get", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_pipeline_sequencing_and_success(
    mock_exec, mock_get, mock_post, temp_workspace
):
    """Verify that all 5 steps execute in sequence and generate expected files."""

    async def post_side_effect(url, **kwargs):
        if "8001" in str(url):
            task_id = "grok-task"
        elif "8002" in str(url):
            task_id = "claude-task"
        elif "8003" in str(url):
            task_id = "codex-task"
        elif "8004" in str(url):
            task_id = "tester-task"
        else:
            task_id = "unknown-task"
        import hashlib
        import json

        body = {"status": "processing", "task_id": task_id}
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"X-Payload-SHA256": checksum},
            request=httpx.Request("POST", str(url)),
        )

    mock_post.side_effect = post_side_effect

    async def get_side_effect(url, **kwargs):
        if "grok-task" in str(url):
            content = "Mock research details on the prompt."
        elif "claude-task" in str(url):
            content = "Mock design architecture and requirements."
        elif "codex-task" in str(url):
            content = "Mock review: Code quality looks good!"
        elif "tester-task" in str(url):
            content = "def test_generated(): pass"
        else:
            content = "Default output"

        import hashlib
        import json

        body = {"status": "completed", "result": content}
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"X-Payload-SHA256": checksum},
            request=httpx.Request("GET", str(url)),
        )

    mock_get.side_effect = get_side_effect

    async def exec_side_effect(*args, **kwargs):
        output_file = "app.py"
        for i, val in enumerate(args):
            if val == "--output" and i + 1 < len(args):
                output_file = args[i + 1]
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("print('Hello Antigravity')")

        proc = MagicMock()

        async def mock_communicate():
            val = b"print('Hello " + b"Antigravity" + b"')"
            return (val, b"")

        proc.communicate = mock_communicate
        proc.returncode = 0
        return proc

    mock_exec.side_effect = exec_side_effect

    await run_pipeline(prompt="Build a calculator app", workspace=str(temp_workspace))

    assert mock_post.call_count == 6
    assert mock_get.call_count == 6
    assert mock_exec.call_count == 1

    project_dir = temp_workspace / "projects" / "build_a_calculator_app"
    research_path = project_dir / "research.md"
    design_path = project_dir / "design.md"
    app_path = project_dir / "app.py"
    review_path = project_dir / "review.md"
    test_generated_path = project_dir / "test_generated.py"

    assert research_path.exists()
    assert design_path.exists()
    assert app_path.exists()
    assert review_path.exists()
    assert test_generated_path.exists()

    assert (
        research_path.read_text(encoding="utf-8")
        == "Mock research details on the prompt."
    )
    assert (
        design_path.read_text(encoding="utf-8")
        == "Mock design architecture and requirements."
    )
    assert app_path.read_text(encoding="utf-8") == "print('Hello Antigravity')"
    assert (
        review_path.read_text(encoding="utf-8")
        == "Mock review: Code quality looks good!"
    )
    assert (
        test_generated_path.read_text(encoding="utf-8") == "def test_generated(): pass"
    )


@pytest.mark.asyncio
@patch("httpx.AsyncClient.post", new_callable=MagicMock)
@patch("httpx.AsyncClient.get", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_pipeline_stdout_redirection(
    mock_exec, mock_get, mock_post, temp_workspace
):
    """Verify that stdout is written to the output file if the command doesn't create it."""

    async def post_side_effect(url, **kwargs):
        if "8001" in str(url):
            task_id = "grok-task"
        elif "8002" in str(url):
            task_id = "claude-task"
        elif "8003" in str(url):
            task_id = "codex-task"
        elif "8004" in str(url):
            task_id = "tester-task"
        else:
            task_id = "unknown-task"
        import hashlib
        import json

        body = {"status": "processing", "task_id": task_id}
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"X-Payload-SHA256": checksum},
            request=httpx.Request("POST", str(url)),
        )

    mock_post.side_effect = post_side_effect

    async def get_side_effect(url, **kwargs):
        if "grok-task" in str(url):
            content = "Mock research details on the prompt."
        elif "claude-task" in str(url):
            content = "Mock design architecture and requirements."
        elif "codex-task" in str(url):
            content = "Mock review: Code quality looks good!"
        elif "tester-task" in str(url):
            content = "def test_generated(): pass"
        else:
            content = "Default output"

        import hashlib
        import json

        body = {"status": "completed", "result": content}
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"X-Payload-SHA256": checksum},
            request=httpx.Request("GET", str(url)),
        )

    mock_get.side_effect = get_side_effect

    async def exec_side_effect(*args, **kwargs):
        proc = MagicMock()

        async def mock_communicate():
            val = b"print('Hello " + b"Antigravity" + b"')"
            return (val, b"")

        proc.communicate = mock_communicate
        proc.returncode = 0
        return proc

    mock_exec.side_effect = exec_side_effect

    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        antigravity_args=["--design", "{input}"],
    )

    project_dir = temp_workspace / "projects" / "build_a_calculator_app"
    research_path = project_dir / "research.md"
    design_path = project_dir / "design.md"
    app_path = project_dir / "app.py"
    review_path = project_dir / "review.md"
    test_generated_path = project_dir / "test_generated.py"

    assert research_path.exists()
    assert design_path.exists()
    assert app_path.exists()
    assert review_path.exists()
    assert test_generated_path.exists()

    assert (
        research_path.read_text(encoding="utf-8")
        == "Mock research details on the prompt."
    )
    assert (
        design_path.read_text(encoding="utf-8")
        == "Mock design architecture and requirements."
    )
    assert app_path.read_text(encoding="utf-8") == "print('Hello Antigravity')"
    assert (
        review_path.read_text(encoding="utf-8")
        == "Mock review: Code quality looks good!"
    )
    assert (
        test_generated_path.read_text(encoding="utf-8") == "def test_generated(): pass"
    )


@pytest.mark.asyncio
@patch("httpx.AsyncClient.post", new_callable=MagicMock)
@patch("httpx.AsyncClient.get", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_pipeline_early_exit_on_error(
    mock_exec, mock_get, mock_post, temp_workspace
):
    """Verify that if a step fails, the pipeline halts immediately and raises PipelineError."""

    async def post_side_effect(url, **kwargs):
        if "8001" in str(url):
            task_id = "grok-task"
        elif "8002" in str(url):
            task_id = "claude-task"
        else:
            task_id = "unknown-task"
        import hashlib
        import json

        body = {"status": "processing", "task_id": task_id}
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"X-Payload-SHA256": checksum},
            request=httpx.Request("POST", str(url)),
        )

    mock_post.side_effect = post_side_effect

    async def get_side_effect(url, **kwargs):
        import hashlib
        import json

        if "grok-task" in str(url):
            body = {"status": "completed", "result": "Grok mock research content"}
            body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
            checksum = hashlib.sha256(body_bytes).hexdigest()
            return httpx.Response(
                200,
                content=body_bytes,
                headers={"X-Payload-SHA256": checksum},
                request=httpx.Request("GET", str(url)),
            )
        elif "claude-task" in str(url):
            body = {"status": "failed", "error": "Claude internal error"}
            body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
            checksum = hashlib.sha256(body_bytes).hexdigest()
            return httpx.Response(
                200,
                content=body_bytes,
                headers={"X-Payload-SHA256": checksum},
                request=httpx.Request("GET", str(url)),
            )

        body = {"status": "completed", "result": "Default"}
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"X-Payload-SHA256": checksum},
            request=httpx.Request("GET", str(url)),
        )

    mock_get.side_effect = get_side_effect

    with pytest.raises(PipelineError) as exc_info:
        await run_pipeline(
            prompt="Build a calculator app", workspace=str(temp_workspace)
        )

    assert "Task execution failed on server: Claude internal error" in str(
        exc_info.value
    )
    assert mock_post.call_count == 2
    assert mock_get.call_count == 2
    assert mock_exec.call_count == 0

    assert not (temp_workspace / "design.md").exists()
    assert not (temp_workspace / "app.py").exists()
    assert not (temp_workspace / "review.md").exists()


@pytest.mark.asyncio
@patch("httpx.AsyncClient.post", new_callable=MagicMock)
@patch("httpx.AsyncClient.get", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_pipeline_early_exit_on_missing_or_empty_output(
    mock_exec, mock_get, mock_post, temp_workspace
):
    """Verify that if a step completes but does not generate a non-empty output, the pipeline fails."""

    async def post_side_effect(url, **kwargs):
        import hashlib
        import json

        body = {"status": "processing", "task_id": "grok-task"}
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"X-Payload-SHA256": checksum},
            request=httpx.Request("POST", str(url)),
        )

    mock_post.side_effect = post_side_effect

    async def get_side_effect(url, **kwargs):
        import hashlib
        import json

        body = {"status": "completed", "result": ""}
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"X-Payload-SHA256": checksum},
            request=httpx.Request("GET", str(url)),
        )

    mock_get.side_effect = get_side_effect

    with pytest.raises(PipelineError) as exc_info:
        await run_pipeline(
            prompt="Build a calculator app", workspace=str(temp_workspace)
        )

    assert "Output file for 'Grok' is empty" in str(exc_info.value)
    assert mock_post.call_count == 1


@pytest.mark.asyncio
@patch("httpx.AsyncClient.post", new_callable=MagicMock)
async def test_pipeline_cleanup_old_files(mock_post, temp_workspace):
    """Verify that old context files are deleted before the pipeline runs."""
    project_dir = temp_workspace / "projects" / "build_a_calculator_app"
    os.makedirs(project_dir, exist_ok=True)
    research_file = project_dir / "research.md"
    design_file = project_dir / "design.md"
    app_file = project_dir / "app.py"
    review_file = project_dir / "review.md"
    test_generated_file = project_dir / "test_generated.py"

    research_file.write_text("old", encoding="utf-8")
    design_file.write_text("old", encoding="utf-8")
    app_file.write_text("old", encoding="utf-8")
    review_file.write_text("old", encoding="utf-8")
    test_generated_file.write_text("old", encoding="utf-8")

    async def post_side_effect(url, **kwargs):
        raise Exception("API crashed")

    mock_post.side_effect = post_side_effect

    with pytest.raises(PipelineError):
        await run_pipeline(
            prompt="Build a calculator app", workspace=str(temp_workspace)
        )

    assert not research_file.exists()
    assert not design_file.exists()
    assert not app_file.exists()
    assert not review_file.exists()
    assert not test_generated_file.exists()


@patch("httpx.AsyncClient.post", new_callable=MagicMock)
@patch("httpx.AsyncClient.get", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
def test_cli_main_success(mock_exec, mock_get, mock_post, temp_workspace):
    """Verify that the main CLI execution succeeds with correct exit codes."""

    async def post_side_effect(url, **kwargs):
        import hashlib
        import json

        body = {"status": "processing", "task_id": "task"}
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"X-Payload-SHA256": checksum},
            request=httpx.Request("POST", str(url)),
        )

    mock_post.side_effect = post_side_effect

    async def get_side_effect(url, **kwargs):
        import hashlib
        import json

        body = {"status": "completed", "result": "mock content"}
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"X-Payload-SHA256": checksum},
            request=httpx.Request("GET", str(url)),
        )

    mock_get.side_effect = get_side_effect

    async def exec_side_effect(*args, **kwargs):
        with open("app.py", "w", encoding="utf-8") as f:
            f.write("print('Hello Antigravity')")
        proc = MagicMock()

        async def mock_communicate():
            val = b"print('Hello " + b"Antigravity" + b"')"
            return (val, b"")

        proc.communicate = mock_communicate
        proc.returncode = 0
        return proc

    mock_exec.side_effect = exec_side_effect

    test_args = [
        "orchestrator.py",
        "--prompt",
        "Build a calculator app",
        "--workspace",
        str(temp_workspace),
    ]
    with patch("sys.argv", test_args):
        main()

    assert mock_post.call_count == 6
    assert mock_exec.call_count == 1


@patch("httpx.AsyncClient.post", new_callable=MagicMock)
def test_cli_main_failure(mock_post, temp_workspace):
    """Verify that the main CLI exits with status 1 on failure."""

    async def post_side_effect(url, **kwargs):
        raise Exception("HTTP error")

    mock_post.side_effect = post_side_effect

    test_args = [
        "orchestrator.py",
        "--prompt",
        "Build a calculator app",
        "--workspace",
        str(temp_workspace),
    ]
    with patch("sys.argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1


def test_command_resolvers():
    """Test that command resolvers return strings and don't raise errors."""
    claude = resolve_claude_cmd()
    antigravity = resolve_antigravity_cmd()

    assert isinstance(claude, str)
    assert isinstance(antigravity, str)
    assert len(claude) > 0
    assert len(antigravity) > 0


def test_resolve_antigravity_cmd_env_override():
    """Verify resolve_antigravity_cmd honors ANTIGRAVITY_BIN_PATH environment variable."""
    with patch.dict(
        os.environ, {"ANTIGRAVITY_BIN_PATH": "/custom/path/to/antigravity"}
    ):
        cmd = resolve_antigravity_cmd()
        assert cmd == "/custom/path/to/antigravity"


def test_resolve_antigravity_cmd_user_profile():
    """Verify resolve_antigravity_cmd resolves using USERPROFILE or HOME."""
    with patch.dict(os.environ, {}, clear=True):
        with patch("sys.platform", "win32"):
            with patch("os.path.exists", return_value=True) as mock_exists:
                with patch.dict(os.environ, {"USERPROFILE": "C:\\Users\\MockUser"}):
                    cmd = resolve_antigravity_cmd()
                    assert cmd == os.path.join(
                        "C:\\Users\\MockUser",
                        ".gemini",
                        "antigravity",
                        "bin",
                        "antigravity.cmd",
                    )
                    mock_exists.assert_any_call(
                        os.path.join(
                            "C:\\Users\\MockUser",
                            ".gemini",
                            "antigravity",
                            "bin",
                            "antigravity.cmd",
                        )
                    )


def test_parse_design_for_files():
    from orchestrator import parse_design_for_files

    # Test JSON block extraction
    design_with_json = """
    We will build a simple calculator.
    Here is the files list:
    ```json
    {
      "files": [
        {"path": "src/calc.py", "specification": "Implement add and sub"},
        {"path": "tests/test_calc.py", "specification": "Test add and sub"}
      ]
    }
    ```
    """
    files = parse_design_for_files(design_with_json)
    assert len(files) == 2
    assert files[0]["path"] == "src/calc.py"
    assert files[0]["specification"] == "Implement add and sub"

    # Test codeblocks fallback with annotations
    design_with_codeblocks = """
    Let's write src/utils.py first:
    ```python
    # filepath: src/utils.py
    def helper():
        pass
    ```
    Next, the other file:
    ```javascript
    // path: src/index.js
    console.log("hello");
    ```
    """
    files = parse_design_for_files(design_with_codeblocks)
    assert len(files) == 2
    assert files[0]["path"] == "src/utils.py"
    assert "helper" in files[0]["specification"]
    assert files[1]["path"] == "src/index.js"
    assert 'console.log("hello")' in files[1]["specification"]


@pytest.mark.asyncio
@patch("httpx.AsyncClient.post", new_callable=MagicMock)
@patch("httpx.AsyncClient.get", new_callable=MagicMock)
@patch("builtins.input", side_effect=["First feedback", "Second feedback", ""])
async def test_interactive_design_review_loop(
    mock_input, mock_get, mock_post, temp_workspace
):
    async def post_side_effect(url, **kwargs):
        import hashlib
        import json

        body = {"status": "processing", "task_id": "task-id"}
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"X-Payload-SHA256": checksum},
            request=httpx.Request("POST", str(url)),
        )

    mock_post.side_effect = post_side_effect

    call_index = 0

    async def get_side_effect(url, **kwargs):
        nonlocal call_index
        if "8002" in str(url):
            content = f"Design Version {call_index}"
            call_index += 1
        else:
            content = "Mock Output"

        import hashlib
        import json

        body = {"status": "completed", "result": content}
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"X-Payload-SHA256": checksum},
            request=httpx.Request("GET", str(url)),
        )

    mock_get.side_effect = get_side_effect

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = MagicMock()

        async def mock_communicate():
            return (b"stdout", b"stderr")

        proc.communicate = mock_communicate
        proc.returncode = 0
        mock_exec.return_value = proc

        await run_pipeline(
            prompt="Build a game", workspace=str(temp_workspace), interactive=True
        )

        design_path = temp_workspace / "projects" / "build_a_game" / "design.md"
        assert design_path.exists()
        assert "Design Version 2" in design_path.read_text(encoding="utf-8")
        assert mock_input.call_count == 3


@pytest.mark.asyncio
@patch("httpx.AsyncClient.post", new_callable=MagicMock)
@patch("httpx.AsyncClient.get", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_self_healing_loop_success_after_retry(
    mock_exec, mock_get, mock_post, temp_workspace
):
    async def post_side_effect(url, **kwargs):
        import hashlib
        import json

        if "8001" in str(url):
            task_id = "grok-task"
        elif "8002" in str(url):
            task_id = "claude-task"
        elif "8003" in str(url):
            task_id = "codex-task"
        elif "8004" in str(url):
            task_id = "tester-task"
        elif "8005" in str(url):
            task_id = "security-task"
        elif "8006" in str(url):
            task_id = "devops-task"
        else:
            task_id = "generic-task"
        body = {"status": "processing", "task_id": task_id}
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"X-Payload-SHA256": checksum},
            request=httpx.Request("POST", str(url)),
        )

    mock_post.side_effect = post_side_effect

    codex_call_count = 0

    async def get_side_effect(url, **kwargs):
        nonlocal codex_call_count
        import hashlib
        import json

        if "grok-task" in str(url):
            content = "Research details"
        elif "claude-task" in str(url):
            content = """
            We need to implement:
            ```json
            {
              "files": [
                {"path": "src/app.py", "specification": "simple function"}
              ]
            }
            ```
            """
        elif "codex-task" in str(url):
            codex_call_count += 1
            content = f"def run(): return {codex_call_count}"
        elif "tester-task" in str(url):
            content = "def test_run(): assert run() == 2"
        elif "security-task" in str(url):
            content = "No vulnerabilities detected."
        elif "devops-task" in str(url):
            content = "Deployment complete"
        else:
            content = "Default output"

        body = {"status": "completed", "result": content}
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"X-Payload-SHA256": checksum},
            request=httpx.Request("GET", str(url)),
        )

    mock_get.side_effect = get_side_effect

    exec_call_count = 0

    async def exec_side_effect(*args, **kwargs):
        nonlocal exec_call_count
        exec_call_count += 1
        proc = MagicMock()

        async def mock_communicate():
            return (b"stdout", b"stderr")

        proc.communicate = mock_communicate
        proc.returncode = 1 if exec_call_count == 1 else 0
        return proc

    mock_exec.side_effect = exec_side_effect

    await run_pipeline(
        prompt="Build a self healer", workspace=str(temp_workspace), max_retries=3
    )

    assert codex_call_count == 2
    assert exec_call_count == 2

    proj_dir = temp_workspace / "projects" / "build_a_self_healer"
    # Generated test/audit/log names derive from the flattened relative path
    # (src/app.py -> src_app) so files with the same basename can't collide.
    assert (proj_dir / "src" / "app.py").exists()
    assert (proj_dir / "tests" / "test_src_app.py").exists()
    assert (proj_dir / "logs" / "audit_src_app.md").exists()
    assert (proj_dir / "logs" / "test_src_app.log").exists()
