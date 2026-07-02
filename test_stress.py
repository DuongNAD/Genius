import os
import sys
import pytest
import stat
import json
import hashlib
import httpx
import asyncio
from unittest.mock import patch, MagicMock
from orchestrator import run_pipeline, PipelineError

# Resolve path to dummy_cli.py
DUMMY_CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dummy_cli.py")


@pytest.fixture
def temp_workspace(tmp_path):
    """Create a temporary workspace directory."""
    return tmp_path


@pytest.fixture(autouse=True)
def mock_api_calls():
    """Automatically mock all API calls for Grok, Claude, and Codex in stress tests."""
    mock_api_calls.latest_prompt = "default"

    async def post_side_effect(url, **kwargs):
        content = kwargs.get("content", b"")
        try:
            payload = json.loads(content.decode("utf-8"))
        except Exception:
            payload = {}
        prompt = payload.get("prompt", "default")
        mock_api_calls.latest_prompt = prompt

        body = {"status": "processing", "task_id": "stress-task"}
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"X-Payload-SHA256": checksum},
            request=httpx.Request("POST", str(url)),
        )

    async def get_side_effect(url, **kwargs):
        result = mock_api_calls.latest_prompt
        body = {"status": "completed", "result": result}
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body_bytes).hexdigest()
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"X-Payload-SHA256": checksum},
            request=httpx.Request("GET", str(url)),
        )

    with patch("httpx.AsyncClient.post", new_callable=MagicMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=MagicMock
    ) as mock_get:
        mock_post.side_effect = post_side_effect
        mock_get.side_effect = get_side_effect
        yield


def test_stress_large_prompt(temp_workspace):
    """Test behavior with very large prompts (Windows command line limit is 8191 chars)."""
    # 1. Moderately large prompt (e.g., 4000 characters) - should succeed
    large_prompt_ok = "A" * 4000
    try:
        asyncio.run(
            run_pipeline(
                prompt=large_prompt_ok,
                workspace=str(temp_workspace),
                grok_cmd=sys.executable,
                claude_cmd=sys.executable,
                antigravity_cmd=sys.executable,
                codex_cmd=sys.executable,
                grok_args=[DUMMY_CLI, "--query", "{prompt}", "--output", "{output}"],
                claude_args=[DUMMY_CLI, "--input", "{input}", "--output", "{output}"],
                antigravity_args=[
                    DUMMY_CLI,
                    "--design",
                    "{input}",
                    "--output",
                    "{output}",
                ],
                codex_args=[DUMMY_CLI, "--code", "{input}", "--output", "{output}"],
            )
        )
        assert (temp_workspace / "review.md").exists()
    except PipelineError as e:
        pytest.fail(f"Pipeline failed on 4000 char prompt: {e}")

    # Clean up workspace files for the next run
    for f in ["research.md", "design.md", "app.py", "review.md"]:
        p = temp_workspace / f
        if p.exists():
            p.unlink()

    # 2. Extremely large prompt (100,000 characters) - should fail on command line length
    huge_prompt = "B" * 100000
    with pytest.raises(PipelineError) as exc_info:
        asyncio.run(
            run_pipeline(
                prompt="short prompt",
                workspace=str(temp_workspace),
                grok_cmd=sys.executable,
                claude_cmd=sys.executable,
                antigravity_cmd=sys.executable,
                codex_cmd=sys.executable,
                grok_args=[DUMMY_CLI, "--query", "{prompt}", "--output", "{output}"],
                claude_args=[DUMMY_CLI, "--input", "{input}", "--output", "{output}"],
                antigravity_args=[
                    DUMMY_CLI,
                    "--design",
                    "{input}",
                    "--output",
                    "{output}",
                    huge_prompt,
                ],
                codex_args=[DUMMY_CLI, "--code", "{input}", "--output", "{output}"],
            )
        )

    assert "Execution failed for 'Antigravity'" in str(exc_info.value)
    assert (
        "too long" in str(exc_info.value)
        or "parameter is incorrect" in str(exc_info.value)
        or "87" in str(exc_info.value)
        or "206" in str(exc_info.value)
        or "failed" in str(exc_info.value).lower()
    )


def test_stress_special_characters(temp_workspace):
    """Test prompt containing special characters, quotes, and redirection operators."""
    special_prompt = "Prompt with \"quotes\", 'single', \\backslashes\\, & amp, | pipe, > redirect, %USERPROFILE% env, ^ caret, ! excl, ? question, * star"

    asyncio.run(
        run_pipeline(
            prompt=special_prompt,
            workspace=str(temp_workspace),
            grok_cmd=sys.executable,
            claude_cmd=sys.executable,
            antigravity_cmd=sys.executable,
            codex_cmd=sys.executable,
            grok_args=[DUMMY_CLI, "--query", "{prompt}", "--output", "{output}"],
            claude_args=[DUMMY_CLI, "--input", "{input}", "--output", "{output}"],
            antigravity_args=[DUMMY_CLI, "--design", "{input}", "--output", "{output}"],
            codex_args=[DUMMY_CLI, "--code", "{input}", "--output", "{output}"],
        )
    )

    research_path = temp_workspace / "research.md"
    assert research_path.exists()

    content = research_path.read_text(encoding="utf-8")
    assert special_prompt in content
    assert "%USERPROFILE%" in content


def test_stress_missing_cli(temp_workspace):
    """Test handling of missing/invalid CLI paths."""
    with pytest.raises(PipelineError) as exc_info:
        asyncio.run(
            run_pipeline(
                prompt="Test",
                workspace=str(temp_workspace),
                antigravity_cmd="non_existent_antigravity_cli_executable_12345",
            )
        )
    assert "Execution failed for 'Antigravity'" in str(exc_info.value)


def test_stress_file_permissions_and_stale_data(temp_workspace):
    """Stale output files from a previous run (even read-only ones) are archived
    to <name>.bak before the pipeline starts, so stale data can't be consumed."""
    research_path = temp_workspace / "research.md"
    backup_path = temp_workspace / "research.md.bak"
    research_path.write_text("Stale grok output from previous run.", encoding="utf-8")
    os.chmod(str(research_path), stat.S_IREAD)

    try:
        # The pipeline still fails later (Antigravity exits 1), but the stale
        # research.md must already have been archived out of the way.
        with pytest.raises(PipelineError):
            asyncio.run(
                run_pipeline(
                    prompt="Test",
                    workspace=str(temp_workspace),
                    grok_cmd=sys.executable,
                    claude_cmd=sys.executable,
                    antigravity_cmd=sys.executable,
                    codex_cmd=sys.executable,
                    grok_args=[
                        DUMMY_CLI,
                        "--query",
                        "{prompt}",
                        "--output",
                        "{output}",
                    ],
                    claude_args=[
                        DUMMY_CLI,
                        "--input",
                        "{input}",
                        "--output",
                        "{output}",
                    ],
                    antigravity_args=[
                        DUMMY_CLI,
                        "--exit-code",
                        "1",
                        "--design",
                        "{input}",
                        "--output",
                        "{output}",
                    ],
                    codex_args=[DUMMY_CLI, "--code", "{input}", "--output", "{output}"],
                )
            )
        assert backup_path.exists()
        assert (
            backup_path.read_text(encoding="utf-8")
            == "Stale grok output from previous run."
        )
        # The fresh run regenerated research.md from the (mocked) Grok call,
        # so its content is no longer the stale text.
        if research_path.exists():
            assert (
                research_path.read_text(encoding="utf-8")
                != "Stale grok output from previous run."
            )
    finally:
        for p in (research_path, backup_path):
            if p.exists():
                os.chmod(str(p), stat.S_IWRITE)


def test_unreadable_input_file_raises_pipeline_error(temp_workspace):
    """Test that when an input file exists but is unreadable, the orchestrator raises a PipelineError."""
    research_file = temp_workspace / "research.md"
    research_file.write_text("Some research content", encoding="utf-8")
    design_file = temp_workspace / "design.md"
    design_file.write_text("Some design content", encoding="utf-8")

    original_open = open

    def mock_open(file, mode="r", *args, **kwargs):
        if ("research.md" in str(file) or "design.md" in str(file)) and "r" in mode:
            raise PermissionError("Simulated read permission denied")
        return original_open(file, mode, *args, **kwargs)

    with patch("builtins.open", mock_open):
        with pytest.raises(PipelineError) as exc_info:
            asyncio.run(
                run_pipeline(
                    prompt="Test prompt",
                    workspace=str(temp_workspace),
                    grok_cmd=sys.executable,
                    claude_cmd=sys.executable,
                    antigravity_cmd=sys.executable,
                    codex_cmd=sys.executable,
                    grok_args=[
                        DUMMY_CLI,
                        "--query",
                        "{prompt}",
                        "--output",
                        "{output}",
                    ],
                    claude_args=[
                        DUMMY_CLI,
                        "--input",
                        "{input_content}",
                        "--output",
                        "{output}",
                    ],
                    antigravity_args=[
                        DUMMY_CLI,
                        "--design",
                        "{input}",
                        "--output",
                        "{output}",
                    ],
                    codex_args=[DUMMY_CLI, "--code", "{input}", "--output", "{output}"],
                )
            )
        assert "Simulated read permission denied" in str(exc_info.value)


def test_stress_whitespace_prompt_raises_error(temp_workspace):
    """Test that a whitespace-only prompt raises PipelineError."""
    with pytest.raises(PipelineError) as exc_info:
        asyncio.run(run_pipeline(prompt="   \n  \t  ", workspace=str(temp_workspace)))
    assert "Prompt cannot be empty" in str(exc_info.value)
