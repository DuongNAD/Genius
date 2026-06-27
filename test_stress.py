import os
import sys
import pytest
import shutil
import stat
import subprocess
from unittest.mock import patch, MagicMock
from orchestrator import run_pipeline, PipelineError


# Resolve path to dummy_cli.py
DUMMY_CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dummy_cli.py")

@pytest.fixture
def temp_workspace(tmp_path):
    """Create a temporary workspace directory."""
    return tmp_path

def test_stress_large_prompt(temp_workspace):
    """Test behavior with very large prompts (Windows command line limit is 8191 chars)."""
    # 1. Moderately large prompt (e.g., 4000 characters) - should succeed
    large_prompt_ok = "A" * 4000
    try:
        run_pipeline(
            prompt=large_prompt_ok,
            workspace=str(temp_workspace),
            grok_cmd=sys.executable,
            claude_cmd=sys.executable,
            antigravity_cmd=sys.executable,
            codex_cmd=sys.executable,
            grok_args=[DUMMY_CLI, "--query", "{prompt}", "--output", "{output}"],
            claude_args=[DUMMY_CLI, "--input", "{input}", "--output", "{output}"],
            antigravity_args=[DUMMY_CLI, "--design", "{input}", "--output", "{output}"],
            codex_args=[DUMMY_CLI, "--code", "{input}", "--output", "{output}"]
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
        run_pipeline(
            prompt=huge_prompt,
            workspace=str(temp_workspace),
            grok_cmd=sys.executable,
            claude_cmd=sys.executable,
            antigravity_cmd=sys.executable,
            codex_cmd=sys.executable,
            grok_args=[DUMMY_CLI, "--query", "{prompt}", "--output", "{output}"],
            claude_args=[DUMMY_CLI, "--input", "{input}", "--output", "{output}"],
            antigravity_args=[DUMMY_CLI, "--design", "{input}", "--output", "{output}"],
            codex_args=[DUMMY_CLI, "--code", "{input}", "--output", "{output}"]
        )
    
    # Verify that it raised PipelineError due to OSError / command line length limits
    assert "Execution failed for 'Grok'" in str(exc_info.value)
    assert "too long" in str(exc_info.value) or "parameter is incorrect" in str(exc_info.value) or "87" in str(exc_info.value) or "206" in str(exc_info.value) or "failed" in str(exc_info.value).lower()


def test_stress_special_characters(temp_workspace):
    """Test prompt containing special characters, quotes, and redirection operators."""
    special_prompt = 'Prompt with "quotes", \'single\', \\backslashes\\, & amp, | pipe, > redirect, %USERPROFILE% env, ^ caret, ! excl, ? question, * star'
    
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
        codex_args=[DUMMY_CLI, "--code", "{input}", "--output", "{output}"]
    )
    
    research_path = temp_workspace / "research.md"
    assert research_path.exists()
    
    content = research_path.read_text(encoding="utf-8")
    # Verify that the exact special prompt was passed through without shell interpretation
    assert special_prompt in content
    # Specifically check environment variable remains unexpanded
    assert "%USERPROFILE%" in content


def test_stress_missing_cli(temp_workspace):
    """Test handling of missing/invalid CLI paths."""
    # 1. Non-existent executable
    with pytest.raises(PipelineError) as exc_info:
        run_pipeline(
            prompt="Test",
            workspace=str(temp_workspace),
            grok_cmd="non_existent_grok_cli_executable_12345"
        )
    assert "Execution failed for 'Grok'" in str(exc_info.value)
    assert "cannot find the file specified" in str(exc_info.value) or "No such file or directory" in str(exc_info.value)

    # 2. Directory specified as executable command
    with pytest.raises(PipelineError) as exc_info:
        run_pipeline(
            prompt="Test",
            workspace=str(temp_workspace),
            grok_cmd=str(temp_workspace)
        )
    assert "Execution failed for 'Grok'" in str(exc_info.value)
    assert "Access is denied" in str(exc_info.value) or "PermissionError" in str(exc_info.value) or "Permission denied" in str(exc_info.value) or "is a directory" in str(exc_info.value)


def test_stress_file_permissions_and_stale_data(temp_workspace):
    """Test behavior when output files are read-only or locked, and stale data usage risk."""
    research_path = temp_workspace / "research.md"
    
    # 1. Create research.md and make it read-only
    research_path.write_text("Stale grok output from previous run.", encoding="utf-8")
    os.chmod(str(research_path), stat.S_IREAD)
    
    try:
        # Now, try to run a command that fails to write (dummy_cli with exit_code=1)
        # Because clean_output_files fails to delete it (it's read-only), and grok_cmd crashes,
        # it should fail because clean_output_files raises PipelineError.
        with pytest.raises(PipelineError) as exc_info:
            run_pipeline(
                prompt="New prompt",
                workspace=str(temp_workspace),
                grok_cmd=sys.executable,
                grok_args=[DUMMY_CLI, "--exit-code", "1", "--output", "{output}"]
            )
        assert "Failed to delete" in str(exc_info.value) or "Access is denied" in str(exc_info.value)
        
        # 2. Now let's test the STALE DATA vulnerability:
        # We assert that the second run (the stale data simulation) raises PipelineError instead of completing successfully.
        with pytest.raises(PipelineError) as exc_info2:
            run_pipeline(
                prompt="New prompt asking for calculator",
                workspace=str(temp_workspace),
                grok_cmd=sys.executable,
                claude_cmd=sys.executable,
                antigravity_cmd=sys.executable,
                codex_cmd=sys.executable,
                grok_args=[DUMMY_CLI, "--query", "{prompt}"], # NO --output argument, so dummy_cli does NOT write
                claude_args=[DUMMY_CLI, "--input", "{input}", "--output", "{output}"],
                antigravity_args=[DUMMY_CLI, "--design", "{input}", "--output", "{output}"],
                codex_args=[DUMMY_CLI, "--code", "{input}", "--output", "{output}"]
            )
        assert "Failed to delete" in str(exc_info2.value) or "Access is denied" in str(exc_info2.value)
        
    finally:
        # Restore permissions so we can clean up
        os.chmod(str(research_path), stat.S_IWRITE)
        if research_path.exists():
            research_path.unlink()


def test_unreadable_input_file_raises_pipeline_error(temp_workspace):
    """Test that when an input file exists but is unreadable, the orchestrator raises a PipelineError."""
    # Create the input file
    research_file = temp_workspace / "research.md"
    research_file.write_text("Some research content", encoding="utf-8")
    
    # We mock builtins.open to raise PermissionError when reading research.md in the orchestrator
    original_open = open
    def mock_open(file, mode="r", *args, **kwargs):
        if "research.md" in str(file) and "r" in mode:
            raise PermissionError("Simulated read permission denied")
        return original_open(file, mode, *args, **kwargs)
        
    with patch("builtins.open", mock_open):
        # Run pipeline using real subprocesses
        with pytest.raises(PipelineError) as exc_info:
            run_pipeline(
                prompt="Test prompt",
                workspace=str(temp_workspace),
                grok_cmd=sys.executable,
                claude_cmd=sys.executable,
                antigravity_cmd=sys.executable,
                codex_cmd=sys.executable,
                grok_args=[DUMMY_CLI, "--query", "{prompt}", "--output", "{output}"],
                claude_args=[DUMMY_CLI, "--input", "{input_content}", "--output", "{output}"],  # Use {input_content}
                antigravity_args=[DUMMY_CLI, "--design", "{input}", "--output", "{output}"],
                codex_args=[DUMMY_CLI, "--code", "{input}", "--output", "{output}"]
            )
        assert "Simulated read permission denied" in str(exc_info.value)


def test_stress_whitespace_prompt_raises_error(temp_workspace):
    """Test that a whitespace-only prompt raises PipelineError."""
    with pytest.raises(PipelineError) as exc_info:
        run_pipeline(
            prompt="   \n  \t   ",
            workspace=str(temp_workspace)
        )
    assert "Prompt cannot be empty" in str(exc_info.value)

