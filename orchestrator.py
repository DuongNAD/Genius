#!/usr/bin/env python3
import argparse
import sys
import os
import subprocess
import logging
import shutil

# Setup logger to output to stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("orchestrator")

# Default CLI command argument structures
DEFAULT_GROK_ARGS = ["--query", "{prompt}", "--output", "{output}"]
DEFAULT_CLAUDE_ARGS = ["--input", "{input}", "--output", "{output}"]
DEFAULT_ANTIGRAVITY_ARGS = ["--design", "{input}", "--output", "{output}"]
DEFAULT_CODEX_ARGS = ["--code", "{input}", "--output", "{output}"]


class PipelineError(Exception):
    """Custom exception raised when a pipeline stage fails or validation fails."""
    pass


def resolve_claude_cmd():
    """Resolve default Claude CLI path, prioritizing Explorer 1 findings."""
    if sys.platform.startswith("win"):
        user_profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        special_path = os.path.join(user_profile, ".local", "bin", "claude.exe")
        if os.path.exists(special_path):
            return special_path
        
        resolved = shutil.which("claude.exe") or shutil.which("claude")
        return resolved or "claude"
    else:
        resolved = shutil.which("claude")
        return resolved or "claude"


def resolve_antigravity_cmd():
    """Resolve default Antigravity CLI path, prioritizing Explorer 1 findings."""
    if sys.platform.startswith("win"):
        special_paths = [
            r"E:\Antigravity\bin\antigravity.cmd",
            r"E:\Antigravity\bin\antigravity",
        ]
        for path in special_paths:
            if os.path.exists(path):
                return path
        
        resolved = shutil.which("antigravity.cmd") or shutil.which("antigravity")
        return resolved or "antigravity.cmd"
    else:
        resolved = shutil.which("antigravity")
        return resolved or "antigravity"


def clean_output_files(paths):
    """Delete context/output files if they exist to prevent stale data usage."""
    logger.info("Cleaning up old context/output files...")
    for path in paths:
        if os.path.exists(path):
            try:
                os.remove(path)
                logger.info(f"Deleted old file: {path}")
            except Exception as e:
                logger.error(f"Failed to delete {path}: {e}")
                raise PipelineError(f"Failed to delete {path}: {e}")


def validate_file(path, step_name, is_input=True):
    """Validate that a context file exists and is not empty."""
    desc = "Input" if is_input else "Output"
    if not os.path.exists(path):
        raise PipelineError(f"{desc} file for '{step_name}' does not exist: {path}")
    if os.path.getsize(path) == 0:
        raise PipelineError(f"{desc} file for '{step_name}' is empty: {path}")


def format_cmd_args(cmd_executable, args_template, prompt, input_path=None, output_path=None):
    """Format command arguments by replacing placeholders with actual values."""
    cmd = [cmd_executable]
    
    # Safely load input content if needed
    input_content = ""
    if input_path and os.path.exists(input_path):
        try:
            with open(input_path, "r", encoding="utf-8") as f:
                input_content = f.read()
        except Exception as e:
            logger.error(f"Failed to read input file {input_path} for formatting: {e}")
            raise PipelineError(f"Failed to read input file {input_path} for formatting: {e}")
            
    for arg in args_template:
        formatted = arg
        if "{prompt}" in formatted:
            formatted = formatted.replace("{prompt}", prompt)
        if "{input}" in formatted and input_path:
            formatted = formatted.replace("{input}", input_path)
        if "{input_content}" in formatted:
            formatted = formatted.replace("{input_content}", input_content)
        if "{output}" in formatted and output_path:
            formatted = formatted.replace("{output}", output_path)
        cmd.append(formatted)
        
    return cmd


def run_step(step_name, cmd_args, input_path=None, output_path=None):
    """Execute a single pipeline step via subprocess, with context validation and logging."""
    logger.info(f"--- Running Step: {step_name} ---")
    
    # 1. Pre-condition validation (input file must exist and be non-empty)
    if input_path:
        validate_file(input_path, step_name, is_input=True)
        
    logger.info(f"Command arguments: {cmd_args}")
    
    # Immediately before executing the subprocess, if output_path exists, attempt to delete it
    if output_path and os.path.exists(output_path):
        try:
            os.remove(output_path)
            logger.info(f"Deleted old output file before execution: {output_path}")
        except Exception as e:
            logger.error(f"Failed to delete existing output file {output_path} before execution: {e}")
            raise PipelineError(f"Failed to delete existing output file {output_path} before execution: {e}")

    # 2. Run the command
    try:
        result = subprocess.run(
            cmd_args,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL
        )
    except Exception as e:
        logger.error(f"Failed to execute subprocess for '{step_name}': {e}")
        raise PipelineError(f"Execution failed for '{step_name}' due to: {e}")
        
    # 3. Print captured stdout and stderr to the console
    if result.stdout:
        print(f"[{step_name} STDOUT]\n{result.stdout.strip()}")
    if result.stderr:
        print(f"[{step_name} STDERR]\n{result.stderr.strip()}")
        
    # 4. Check return code
    if result.returncode != 0:
        logger.error(f"Step '{step_name}' failed with exit code {result.returncode}")
        raise PipelineError(f"Step '{step_name}' returned non-zero exit code: {result.returncode}")
        
    # 5. Post-condition verification (output file must exist and be non-empty)
    if output_path:
        # If output file wasn't created by the command directly, write command's stdout to it
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            if result.stdout:
                logger.info(f"Writing captured stdout to output file: {output_path}")
                try:
                    with open(output_path, "w", encoding="utf-8") as f:
                        f.write(result.stdout)
                except Exception as e:
                    raise PipelineError(f"Failed to write stdout to output file {output_path}: {e}")
            else:
                logger.warning(f"No stdout captured and output file was not created for '{step_name}'")
                
        # Final validation of the output file
        validate_file(output_path, step_name, is_input=False)
        logger.info(f"Step '{step_name}' successfully completed. Output verified: {output_path}")


def run_pipeline(
    prompt: str,
    grok_cmd: str = "grok",
    claude_cmd: str = "claude",
    antigravity_cmd: str = "antigravity",
    codex_cmd: str = "codex",
    grok_args: list = None,
    claude_args: list = None,
    antigravity_args: list = None,
    codex_args: list = None,
    workspace: str = None
):
    """Execute the sequential 4-AI pipeline (Grok -> Claude -> Antigravity -> Codex)."""
    if not prompt or not prompt.strip():
        raise PipelineError("Prompt cannot be empty.")
        
    if workspace is None:
        workspace = os.getcwd()
        
    # Resolve absolute paths for context sharing files
    research_file = os.path.join(workspace, "research.md")
    design_file = os.path.join(workspace, "design.md")
    app_file = os.path.join(workspace, "app.py")
    review_file = os.path.join(workspace, "review.md")
    
    # 1. Clean up old output files
    all_files = [research_file, design_file, app_file, review_file]
    clean_output_files(all_files)
    
    # Use default args if not overridden
    g_args = grok_args if grok_args is not None else DEFAULT_GROK_ARGS
    c_args = claude_args if claude_args is not None else DEFAULT_CLAUDE_ARGS
    a_args = antigravity_args if antigravity_args is not None else DEFAULT_ANTIGRAVITY_ARGS
    cx_args = codex_args if codex_args is not None else DEFAULT_CODEX_ARGS
    
    # Step 1: Grok (Research)
    grok_formatted_cmd = format_cmd_args(grok_cmd, g_args, prompt, input_path=None, output_path=research_file)
    run_step("Grok", grok_formatted_cmd, input_path=None, output_path=research_file)
    
    # Step 2: Claude (Design)
    claude_formatted_cmd = format_cmd_args(claude_cmd, c_args, prompt, input_path=research_file, output_path=design_file)
    run_step("Claude", claude_formatted_cmd, input_path=research_file, output_path=design_file)
    
    # Step 3: Antigravity (Programming)
    antigravity_formatted_cmd = format_cmd_args(antigravity_cmd, a_args, prompt, input_path=design_file, output_path=app_file)
    run_step("Antigravity", antigravity_formatted_cmd, input_path=design_file, output_path=app_file)
    
    # Step 4: Codex (Review)
    codex_formatted_cmd = format_cmd_args(codex_cmd, cx_args, prompt, input_path=app_file, output_path=review_file)
    run_step("Codex", codex_formatted_cmd, input_path=app_file, output_path=review_file)
    
    logger.info("Pipeline executed successfully and all intermediate files verified.")


def main():
    parser = argparse.ArgumentParser(
        description="4-AI CLI Orchestrator pipeline executing Grok -> Claude -> Antigravity -> Codex."
    )
    parser.add_argument("--prompt", required=True, help="Initial research/query prompt for the pipeline")
    parser.add_argument("--workspace", default=None, help="Workspace directory for context files (defaults to current dir)")
    
    # Custom commands/paths
    parser.add_argument("--grok-cmd", default="grok", help="Command/path to Grok CLI")
    parser.add_argument("--claude-cmd", default=resolve_claude_cmd(), help="Command/path to Claude CLI")
    parser.add_argument("--antigravity-cmd", default=resolve_antigravity_cmd(), help="Command/path to Antigravity CLI")
    parser.add_argument("--codex-cmd", default="codex", help="Command/path to Codex CLI")
    
    # Custom arguments
    parser.add_argument("--grok-args", nargs="*", default=None, help="Custom arguments for Grok step")
    parser.add_argument("--claude-args", nargs="*", default=None, help="Custom arguments for Claude step")
    parser.add_argument("--antigravity-args", nargs="*", default=None, help="Custom arguments for Antigravity step")
    parser.add_argument("--codex-args", nargs="*", default=None, help="Custom arguments for Codex step")
    
    args = parser.parse_args()
    
    try:
        run_pipeline(
            prompt=args.prompt,
            grok_cmd=args.grok_cmd,
            claude_cmd=args.claude_cmd,
            antigravity_cmd=args.antigravity_cmd,
            codex_cmd=args.codex_cmd,
            grok_args=args.grok_args,
            claude_args=args.claude_args,
            antigravity_args=args.antigravity_args,
            codex_args=args.codex_args,
            workspace=args.workspace
        )
    except PipelineError as e:
        logger.error(f"Pipeline Execution Failed: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected Pipeline Failure: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
