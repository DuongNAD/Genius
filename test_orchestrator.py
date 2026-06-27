import os
import sys
import pytest
import subprocess
from unittest.mock import patch, MagicMock

# Add current workspace to path to import orchestrator
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orchestrator import run_pipeline, main, PipelineError, resolve_antigravity_cmd, resolve_claude_cmd


@pytest.fixture
def temp_workspace(tmp_path, monkeypatch):
    """Fixture that moves to a temp directory and returns it as a workspace."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def create_mock_run_success():
    """Create a side_effect function for subprocess.run that simulates success and generates files."""
    def side_effect(cmd_args, *args, **kwargs):
        executable = cmd_args[0]
        
        # Determine output file by checking if --output is in args
        output_file = None
        for i, val in enumerate(cmd_args):
            if val == "--output" and i + 1 < len(cmd_args):
                output_file = cmd_args[i+1]
        
        # If not found via --output, check if we can write to standard file names
        if "grok" in executable:
            content = "Mock research details on the prompt."
            default_path = "research.md"
        elif "claude" in executable:
            content = "Mock design architecture and requirements."
            default_path = "design.md"
        elif "antigravity" in executable:
            content = "print('Hello Antigravity')"
            default_path = "app.py"
        elif "codex" in executable:
            content = "Mock review: Code quality looks good!"
            default_path = "review.md"
        else:
            content = "default output"
            default_path = None
            
        write_path = output_file or default_path
        
        # If writing to file directly (command simulation)
        if write_path and "--output" in cmd_args:
            with open(write_path, "w", encoding="utf-8") as f:
                f.write(content)
            return MagicMock(returncode=0, stdout=f"Success output for {executable}", stderr="")
        else:
            # If stdout only
            return MagicMock(returncode=0, stdout=content, stderr="")
            
    return side_effect


@patch("subprocess.run")
def test_pipeline_sequencing_and_success(mock_run, temp_workspace):
    """Verify that all 4 steps execute in sequence and generate expected files."""
    mock_run.side_effect = create_mock_run_success()
    
    run_pipeline(prompt="Build a calculator app", workspace=str(temp_workspace))
    
    # Assert sequence execution order
    assert mock_run.call_count == 4
    calls = mock_run.call_args_list
    assert "grok" in calls[0][0][0][0]
    assert "claude" in calls[1][0][0][0]
    assert "antigravity" in calls[2][0][0][0]
    assert "codex" in calls[3][0][0][0]
    
    # Assert files exist and contain real data
    research_path = temp_workspace / "research.md"
    design_path = temp_workspace / "design.md"
    app_path = temp_workspace / "app.py"
    review_path = temp_workspace / "review.md"
    
    assert research_path.exists()
    assert design_path.exists()
    assert app_path.exists()
    assert review_path.exists()
    
    assert research_path.read_text(encoding="utf-8") == "Mock research details on the prompt."
    assert design_path.read_text(encoding="utf-8") == "Mock design architecture and requirements."
    assert app_path.read_text(encoding="utf-8") == "print('Hello Antigravity')"
    assert review_path.read_text(encoding="utf-8") == "Mock review: Code quality looks good!"


@patch("subprocess.run")
def test_pipeline_stdout_redirection(mock_run, temp_workspace):
    """Verify that stdout is written to the output file if the command doesn't create it."""
    # Custom side effect for when we don't supply --output in the commands.
    # The default args have "--output", so let's override them in the arguments parameter.
    mock_run.side_effect = create_mock_run_success()
    
    run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        grok_args=["--query", "{prompt}"],
        claude_args=["--input", "{input}"],
        antigravity_args=["--design", "{input}"],
        codex_args=["--code", "{input}"]
    )
    
    # Output files should still be created by orchestrator writing stdout
    research_path = temp_workspace / "research.md"
    design_path = temp_workspace / "design.md"
    app_path = temp_workspace / "app.py"
    review_path = temp_workspace / "review.md"
    
    assert research_path.exists()
    assert design_path.exists()
    assert app_path.exists()
    assert review_path.exists()
    
    assert research_path.read_text(encoding="utf-8") == "Mock research details on the prompt."
    assert design_path.read_text(encoding="utf-8") == "Mock design architecture and requirements."
    assert app_path.read_text(encoding="utf-8") == "print('Hello Antigravity')"
    assert review_path.read_text(encoding="utf-8") == "Mock review: Code quality looks good!"


@patch("subprocess.run")
def test_pipeline_early_exit_on_error(mock_run, temp_workspace):
    """Verify that if a step fails, the pipeline halts immediately and raises PipelineError."""
    
    # Let's say Grok succeeds, but Claude fails with non-zero exit code
    def side_effect(cmd_args, *args, **kwargs):
        executable = cmd_args[0]
        if "grok" in executable:
            with open("research.md", "w", encoding="utf-8") as f:
                f.write("Grok mock research content")
            return MagicMock(returncode=0, stdout="Grok success", stderr="")
        elif "claude" in executable:
            return MagicMock(returncode=2, stdout="", stderr="Claude internal error")
        return MagicMock(returncode=0, stdout="Default", stderr="")
        
    mock_run.side_effect = side_effect
    
    with pytest.raises(PipelineError) as exc_info:
        run_pipeline(prompt="Build a calculator app", workspace=str(temp_workspace))
        
    assert "Step 'Claude' returned non-zero exit code: 2" in str(exc_info.value)
    
    # Check that only Grok and Claude were called (call_count == 2)
    assert mock_run.call_count == 2
    calls = mock_run.call_args_list
    assert "grok" in calls[0][0][0][0]
    assert "claude" in calls[1][0][0][0]
    
    # Intermediate files beyond Claude should NOT exist
    assert not (temp_workspace / "design.md").exists()
    assert not (temp_workspace / "app.py").exists()
    assert not (temp_workspace / "review.md").exists()


@patch("subprocess.run")
def test_pipeline_early_exit_on_missing_or_empty_output(mock_run, temp_workspace):
    """Verify that if a step completes but does not generate a non-empty output, the pipeline fails."""
    
    # Grok completes with exit code 0, but fails to produce research.md
    def side_effect(cmd_args, *args, **kwargs):
        return MagicMock(returncode=0, stdout="", stderr="") # Empty stdout and no file written
        
    mock_run.side_effect = side_effect
    
    with pytest.raises(PipelineError) as exc_info:
        run_pipeline(prompt="Build a calculator app", workspace=str(temp_workspace))
        
    assert "Output file for 'Grok' does not exist" in str(exc_info.value)
    assert mock_run.call_count == 1 # Aborted at first step


def test_pipeline_cleanup_old_files(temp_workspace):
    """Verify that old context files are deleted before the pipeline runs."""
    research_file = temp_workspace / "research.md"
    design_file = temp_workspace / "design.md"
    app_file = temp_workspace / "app.py"
    review_file = temp_workspace / "review.md"
    
    # Pre-create old files
    research_file.write_text("old", encoding="utf-8")
    design_file.write_text("old", encoding="utf-8")
    app_file.write_text("old", encoding="utf-8")
    review_file.write_text("old", encoding="utf-8")
    
    # Run pipeline with a failing first step to ensure files are deleted and not re-created
    with patch("subprocess.run") as mock_run:
        mock_run.returncode = 1
        mock_run.side_effect = Exception("Subprocess crashed")
        
        with pytest.raises(PipelineError):
            run_pipeline(prompt="Build a calculator app", workspace=str(temp_workspace))
            
    # All files should have been cleaned up and not re-created
    assert not research_file.exists()
    assert not design_file.exists()
    assert not app_file.exists()
    assert not review_file.exists()


@patch("subprocess.run")
def test_cli_main_success(mock_run, temp_workspace):
    """Verify that the main CLI execution succeeds with correct exit codes."""
    mock_run.side_effect = create_mock_run_success()
    
    test_args = ["orchestrator.py", "--prompt", "Build a calculator app", "--workspace", str(temp_workspace)]
    with patch("sys.argv", test_args):
        # Should not raise SystemExit
        main()
    
    assert mock_run.call_count == 4


@patch("subprocess.run")
def test_cli_main_failure(mock_run, temp_workspace):
    """Verify that the main CLI exits with status 1 on failure."""
    # Force failure
    mock_run.returncode = 1
    mock_run.side_effect = subprocess.CalledProcessError(returncode=1, cmd=["grok"])
    
    test_args = ["orchestrator.py", "--prompt", "Build a calculator app", "--workspace", str(temp_workspace)]
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
