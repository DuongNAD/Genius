#!/usr/bin/env python3
import argparse
import sys
import os
import asyncio
import logging
import hashlib
import json
import httpx
import re
import shutil
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, retry_if_exception

def parse_design_for_files(design_content: str) -> list:
    """
    Parses design_content for a list of files to implement.
    Returns a list of dicts, e.g., [{"path": "src/main.py", "specification": "..."}].
    """
    # 1. Check for JSON block
    json_blocks = re.findall(r'[ \t]*```json\s*(\{.*?\})\s*[ \t]*```', design_content, re.DOTALL)
    for block in json_blocks:
        try:
            data = json.loads(block)
            if isinstance(data, dict) and "files" in data:
                files = data["files"]
                if isinstance(files, list):
                    valid_files = []
                    for f in files:
                        if isinstance(f, dict) and "path" in f and "specification" in f:
                            valid_files.append(f)
                    if valid_files:
                        return valid_files
        except Exception:
            pass
            
    try:
        start = design_content.find('{')
        end = design_content.rfind('}')
        if start != -1 and end != -1 and end > start:
            data = json.loads(design_content[start:end+1])
            if isinstance(data, dict) and "files" in data:
                files = data["files"]
                if isinstance(files, list):
                    valid_files = []
                    for f in files:
                        if isinstance(f, dict) and "path" in f and "specification" in f:
                            valid_files.append(f)
                    if valid_files:
                        return valid_files
    except Exception:
        pass

    # 2. Fall back to regex that extracts markdown code blocks with filepath annotations
    code_blocks = re.findall(r'[ \t]*```[a-zA-Z0-9_-]*\s*\n(.*?)\n[ \t]*```', design_content, re.DOTALL)
    files = []
    for block in code_blocks:
        m = re.search(r'(?:#|//)\s*(?:filepath|path):\s*([^\s\n\r]+)', block)
        if m:
            filepath = m.group(1).strip()
            files.append({
                "path": filepath,
                "specification": block.strip()
            })
            
    return files

def extract_code(content: str) -> str:
    blocks = re.findall(r'```[a-zA-Z0-9_-]*\n(.*?)\n```', content, re.DOTALL)
    if blocks:
        return "\n".join(blocks).strip()
    return content.strip()


# Setup logger to output to stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("orchestrator")

DEFAULT_ANTIGRAVITY_ARGS = ["--design", "{input}", "--output", "{output}"]

ROUTING_TABLE = {
    # Grok
    "/research": ("grok", "research.md"),
    "/summarize": ("grok", "research.md"),
    "/fact-check": ("grok", "research.md"),
    # Claude
    "/plan": ("claude", "design.md"),
    "/design": ("claude", "design.md"),
    "/review-architecture": ("claude", "design.md"),
    # Codex
    "/code": ("codex", "review.md"),
    "/refactor": ("codex", "review.md"),
    # Security
    "/security": ("security", "audit.md"),
    "/audit": ("security", "audit.md"),
    "/security-audit": ("security", "audit.md"),
    # Tester
    "/unit-test": ("tester", "test_generated.py"),
    "/stress-test": ("tester", "test_generated.py"),
    # DevOps
    "/deploy": ("devops", "deploy.md"),
}


class PipelineError(Exception):
    """Custom exception raised when a pipeline stage fails or validation fails."""
    pass

class ChecksumMismatchError(Exception):
    """Custom exception raised when payload checksum validation fails."""
    pass

def resolve_grok_cmd():
    return "grok"

def resolve_claude_cmd():
    if sys.platform.startswith("win"):
        user_profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        special_path = os.path.join(user_profile, ".local", "bin", "claude.exe")
        if os.path.exists(special_path):
            return special_path
        import shutil
        resolved = shutil.which("claude.exe") or shutil.which("claude")
        return resolved or "claude"
    else:
        import shutil
        resolved = shutil.which("claude")
        return resolved or "claude"

def resolve_antigravity_cmd():
    env_path = os.environ.get("ANTIGRAVITY_BIN_PATH")
    if env_path:
        return env_path

    if sys.platform.startswith("win"):
        user_profile = os.environ.get("USERPROFILE") or os.environ.get("HOME")
        special_paths = []
        if user_profile:
            special_paths.append(os.path.join(user_profile, ".gemini", "antigravity", "bin", "antigravity.cmd"))
            special_paths.append(os.path.join(user_profile, ".gemini", "antigravity", "bin", "antigravity"))
        for path in special_paths:
            if os.path.exists(path):
                return path
        import shutil
        resolved = shutil.which("antigravity.cmd") or shutil.which("antigravity")
        return resolved or "antigravity.cmd"
    else:
        import shutil
        resolved = shutil.which("antigravity")
        return resolved or "antigravity"

def resolve_codex_cmd():
    return "codex"

def resolve_tester_cmd():
    return "tester"

def resolve_security_cmd():
    return "security"

def resolve_devops_cmd():
    return "devops"

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

from ag_core.config import load_config
from ag_core.scanner.project_scanner import ProjectScanner
from ag_core.utils.db import log_conversation


def verify_response_checksum(response) -> None:
    expected_checksum = response.headers.get("X-Payload-SHA256")
    if not expected_checksum:
        raise ChecksumMismatchError("Response is missing X-Payload-SHA256 header")
    calculated = hashlib.sha256(response.content).hexdigest()
    if calculated != expected_checksum:
        raise ChecksumMismatchError(f"Response checksum mismatch: expected {expected_checksum}, calculated {calculated}")

def is_transient_error(exception) -> bool:
    logger.info(f"DEBUG_ERR: type={type(exception)} msg={exception}")
    if isinstance(exception, ChecksumMismatchError):
        return True
    if isinstance(exception, httpx.HTTPStatusError):
        # Retry on 429 (Rate Limit) and 5xx (Server Error)
        status_code = exception.response.status_code
        return status_code == 429 or status_code >= 500
    if isinstance(exception, httpx.RequestError):
        # Retry on connection errors, timeouts, etc.
        return True
    return False

# Define a wait strategy that respects Retry-After headers or falls back to exponential backoff
def wait_strategy(retry_state):
    # Check if the last attempt raised an HTTPStatusError with Retry-After header
    exception = retry_state.outcome.exception()
    if isinstance(exception, httpx.HTTPStatusError):
        retry_after = exception.response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = float(retry_after)
                return min(delay, 60.0)
            except ValueError:
                pass
    # Fallback to standard exponential backoff: 2^attempt, min 1s, max 10s
    return wait_exponential(multiplier=1, min=1, max=10)(retry_state)

@retry(
    stop=stop_after_attempt(3),
    wait=wait_strategy,
    retry=retry_if_exception(is_transient_error),
    reraise=True
)
async def perform_post_with_retry(client, url, payload_bytes, headers):
    response = await client.post(url, content=payload_bytes, headers=headers)
    response.raise_for_status()
    verify_response_checksum(response)
    return response

@retry(
    stop=stop_after_attempt(3),
    wait=wait_strategy,
    retry=retry_if_exception(is_transient_error),
    reraise=True
)
async def perform_get_with_retry(client, url, headers):
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    verify_response_checksum(response)
    return response

_API_RESPONSE_CACHE = {}

async def call_api(url: str, api_key: str, prompt: str, context: dict = None, client: httpx.AsyncClient = None, poll_timeout: float = 60.0) -> str:
    import time
    from ag_core.utils.jwt import encode_jwt

    import os
    # Key the cache by the hash of the URL, the prompt, and the sorted JSON-serialized context dictionary.
    sorted_context = json.dumps(context or {}, sort_keys=True)
    cache_string = f"{url}\n{prompt}\n{sorted_context}"
    cache_key = hashlib.sha256(cache_string.encode("utf-8")).hexdigest()
    
    use_cache = True
    if "PYTEST_CURRENT_TEST" in os.environ and not os.environ.get("ENABLE_GENIUS_CACHE"):
        use_cache = False

    if use_cache and cache_key in _API_RESPONSE_CACHE:
        logger.info(f"Cache hit for URL: {url}")
        return _API_RESPONSE_CACHE[cache_key]

    # Generate short-lived JWT token (expiring in 5 minutes)
    payload = {
        "sub": "orchestrator",
        "exp": time.time() + 300
    }
    jwt_token = encode_jwt(payload, api_key)

    headers = {
        "X-API-Key": jwt_token,
        "Authorization": f"Bearer {jwt_token}"
    }
    req_payload = {
        "prompt": prompt,
        "context": context
    }
    
    # Calculate checksum for POST request body
    payload_bytes = json.dumps(req_payload, separators=(',', ':')).encode("utf-8")
    req_checksum = hashlib.sha256(payload_bytes).hexdigest()
    
    headers["X-Payload-SHA256"] = req_checksum
    headers["Content-Type"] = "application/json"
    
    base_url = url.rstrip('/')

    async def _execute(c):
        try:
            # 1. Start the run
            response = await perform_post_with_retry(c, f"{base_url}/run", payload_bytes, headers)
            res_data = response.json()
            task_id = res_data.get("task_id")
            if not task_id:
                raise PipelineError(f"No task_id returned from {base_url}/run")
        except Exception as e:
            logger.error(f"HTTP request to start task at {base_url}/run failed: {e}")
            raise PipelineError(f"HTTP request to start task at {base_url}/run failed: {e}")

        # GET request has empty body, so checksum is of empty bytes
        get_checksum = hashlib.sha256(b"").hexdigest()
        get_headers = {
            "X-Payload-SHA256": get_checksum,
            "X-API-Key": jwt_token,
            "Authorization": f"Bearer {jwt_token}"
        }
        
        # 2. Poll for completion
        poll_start = time.time()
        while True:
            if time.time() - poll_start > poll_timeout:
                raise PipelineError(f"Task execution timed out. Polling exceeded poll_timeout of {poll_timeout} seconds.")
            try:
                status_response = await perform_get_with_retry(c, f"{base_url}/status/{task_id}", get_headers)
                status_data = status_response.json()
                curr_status = status_data.get("status")
                
                if curr_status == "completed":
                    return status_data.get("result", "")
                elif curr_status == "failed":
                    error_msg = status_data.get("error", "Unknown error occurred on server.")
                    raise PipelineError(f"Task execution failed on server: {error_msg}")
                elif curr_status == "processing":
                    await asyncio.sleep(0.5)
                else:
                    raise PipelineError(f"Unexpected status '{curr_status}' returned for task {task_id}")
            except PipelineError:
                raise
            except Exception as e:
                logger.error(f"Failed to poll task status at {base_url}/status/{task_id}: {e}")
                raise PipelineError(f"Failed to poll task status at {base_url}/status/{task_id}: {e}")

    if client is not None:
        result = await _execute(client)
    else:
        limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
        timeout = httpx.Timeout(10.0, connect=5.0)
        async with httpx.AsyncClient(limits=limits, timeout=timeout) as local_client:
            result = await _execute(local_client)

    if use_cache:
        _API_RESPONSE_CACHE[cache_key] = result
    return result

async def process_single_file(file_info, project_dir, config, codex_url, tester_url, security_url, api_key, client, poll_timeout, max_retries, semaphore):
    async with semaphore:
        file_path = file_info["path"]
        specification = file_info["specification"]
        
        target_file_path = os.path.join(project_dir, file_path)
        os.makedirs(os.path.dirname(target_file_path), exist_ok=True)
        
        base_name = os.path.basename(file_path)
        file_name, _ = os.path.splitext(base_name)
        
        test_file_path = os.path.join(project_dir, "tests", f"test_{file_name}.py")
        audit_log_path = os.path.join(project_dir, "logs", f"audit_{file_name}.md")
        test_log_path = os.path.join(project_dir, "logs", f"test_{file_name}.log")
        
        success = False
        test_failures_logs = ""
        security_report = ""
        
        for attempt in range(1, max_retries + 1):
            logger.info(f"Implementing {file_path} - Attempt {attempt}/{max_retries}")
            
            # 1. Call Codex API /code
            codex_req_prompt = f"/code Implement the file '{file_path}' according to this specification:\n{specification}"
            if attempt > 1:
                codex_req_prompt += f"\n\nPrevious implementation attempt failed check.\nTest Failures/Logs:\n{test_failures_logs}\n\nSecurity Report:\n{security_report}"
            
            try:
                proj_scanner = ProjectScanner(root_dir=project_dir, extra_ignores=config.scanner.exclude_patterns)
                current_context = proj_scanner.scan()
            except Exception:
                current_context = {}
            
            codex_code_raw = await call_api(codex_url, api_key, codex_req_prompt, context=current_context, client=client, poll_timeout=poll_timeout)
            code_to_write = extract_code(codex_code_raw)
            
            # 2. Write code to projects/[project_name]/[file_path]
            try:
                with open(target_file_path, "w", encoding="utf-8") as f:
                    f.write(code_to_write)
                logger.info(f"Wrote implemented code to {target_file_path}")
            except Exception as e:
                raise PipelineError(f"Failed to write code to {target_file_path}: {e}")
            
            # 3. Call Tester API /unit-test
            tester_req_prompt = f"/unit-test Generate comprehensive unit tests using pytest for the file '{file_path}' with this code:\n\n{code_to_write}"
            
            try:
                proj_scanner = ProjectScanner(root_dir=project_dir, extra_ignores=config.scanner.exclude_patterns)
                current_context = proj_scanner.scan()
            except Exception:
                current_context = {}
                
            tester_tests_raw = await call_api(tester_url, api_key, tester_req_prompt, context=current_context, client=client, poll_timeout=poll_timeout)
            test_code_to_write = extract_code(tester_tests_raw)
            
            # 4. Write test code to projects/[project_name]/tests/test_[file_name].py
            try:
                with open(test_file_path, "w", encoding="utf-8") as f:
                    f.write(test_code_to_write)
                logger.info(f"Wrote generated tests to {test_file_path}")
            except Exception as e:
                raise PipelineError(f"Failed to write test code to {test_file_path}: {e}")
                
            # 5. Call Security API /audit
            security_req_prompt = f"/audit Audit the following code for security vulnerabilities in file '{file_path}':\n\n{code_to_write}"
            
            try:
                proj_scanner = ProjectScanner(root_dir=project_dir, extra_ignores=config.scanner.exclude_patterns)
                current_context = proj_scanner.scan()
            except Exception:
                current_context = {}
                
            security_report = await call_api(security_url, api_key, security_req_prompt, context=current_context, client=client, poll_timeout=poll_timeout)
            
            # Save audit report to projects/[project_name]/logs/audit_[file_name].md
            try:
                with open(audit_log_path, "w", encoding="utf-8") as f:
                    f.write(security_report)
                logger.info(f"Wrote security audit report to {audit_log_path}")
            except Exception as e:
                raise PipelineError(f"Failed to write security audit to {audit_log_path}: {e}")
                
            # 6. Run pytest projects/[project_name]/tests/test_[file_name].py
            pytest_cmd = [sys.executable, "-m", "pytest", test_file_path]
            logger.info(f"Running pytest command: {' '.join(pytest_cmd)}")
            
            try:
                env = os.environ.copy()
                project_src_dir = os.path.join(project_dir, "src")
                env["PYTHONPATH"] = os.path.pathsep.join([
                    project_dir,
                    project_src_dir,
                    env.get("PYTHONPATH", "")
                ]).strip(os.path.pathsep)
                
                process = await asyncio.create_subprocess_exec(
                    *pytest_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env
                )
                stdout, stderr = await process.communicate()
                pytest_exit_code = process.returncode
                test_failures_logs = stdout.decode("utf-8", errors="replace") + "\n" + stderr.decode("utf-8", errors="replace")
            except Exception as e:
                pytest_exit_code = -999
                test_failures_logs = f"Failed to run pytest: {e}"
                
            # Save output to projects/[project_name]/logs/test_[file_name].log
            try:
                with open(test_log_path, "w", encoding="utf-8") as f:
                    f.write(test_failures_logs)
            except Exception as e:
                logger.warning(f"Failed to write test log to {test_log_path}: {e}")
                
            # 7. Check if tests passed (return code 0) and security audit has no vulnerabilities
            tests_passed = (pytest_exit_code == 0)
            has_vulnerabilities = False
            if ("[vulnerability detected]" in security_report.lower() or 
                "[insecure]" in security_report.lower() or 
                "HIGH" in security_report or 
                "CRITICAL" in security_report):
                has_vulnerabilities = True
                
            if tests_passed and not has_vulnerabilities:
                logger.info(f"Successfully implemented and verified {file_path}")
                success = True
                break
            else:
                fail_reasons = []
                if not tests_passed:
                    fail_reasons.append(f"pytest failed (exit code {pytest_exit_code})")
                if has_vulnerabilities:
                    fail_reasons.append("security audit detected vulnerabilities/high warnings")
                logger.warning(f"Verification failed for {file_path}: {', '.join(fail_reasons)}")
        
        if not success:
            raise PipelineError(f"Self-healing loop failed to implement and verify {file_path} after {max_retries} attempts.")
            
        return security_report


async def run_pipeline(
    prompt: str,
    grok_cmd: str = 'grok',
    claude_cmd: str = 'claude',
    antigravity_cmd: str = 'antigravity',
    codex_cmd: str = 'codex',
    tester_cmd: str = 'tester',
    security_cmd: str = 'security',
    devops_cmd: str = 'devops',
    grok_args: list = None,
    claude_args: list = None,
    antigravity_args: list = None,
    codex_args: list = None,
    tester_args: list = None,
    security_args: list = None,
    devops_args: list = None,
    workspace: str = None,
    grok_url: str = None,
    claude_url: str = None,
    codex_url: str = None,
    tester_url: str = None,
    security_url: str = None,
    devops_url: str = None,
    api_key_override: str = None,
    poll_timeout: float = 60.0,
    interactive: bool = False,
    max_retries: int = 3
):
    """Execute the sequential pipeline (Grok -> Claude -> Antigravity -> Codex -> Tester -> Security -> DevOps)."""
    if not prompt or not prompt.strip():
        raise PipelineError("Prompt cannot be empty.")
        
    slugified = re.sub(r'[^a-zA-Z0-9]+', '_', prompt.strip().lower()).strip('_')
    if not slugified:
        project_name = "default_project"
    elif len(slugified) > 50:
        project_name = slugified[:40] + "_" + hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:8]
    else:
        project_name = slugified

    if workspace is None:
        workspace = os.getcwd()
    
    project_dir = os.path.join(workspace, "projects", project_name)
    os.makedirs(os.path.join(project_dir, "src"), exist_ok=True)
    os.makedirs(os.path.join(project_dir, "tests"), exist_ok=True)
    os.makedirs(os.path.join(project_dir, "logs"), exist_ok=True)
    os.makedirs(os.path.join(project_dir, "docker"), exist_ok=True)
        
    # Resolve absolute paths for context sharing files under workspace (root of the workspace directory) by default
    research_file = os.path.join(workspace, "research.md")
    design_file = os.path.join(workspace, "design.md")
    app_file = os.path.join(workspace, "app.py")
    review_file = os.path.join(workspace, "review.md")
    test_generated_file = os.path.join(workspace, "test_generated.py")
    audit_file = os.path.join(workspace, "audit.md")
    deploy_file = os.path.join(workspace, "deploy.md")
    
    # Intercept direct slash command routing before cleaning up all files
    first_word = prompt.strip().split()[0] if prompt.strip() else ""
    is_slash_cmd = first_word.startswith("/") and first_word in ROUTING_TABLE
    
    # 1. Clean up old output files
    if is_slash_cmd:
        agent_key, output_name = ROUTING_TABLE[first_word]
        target_output_file = os.path.join(workspace, output_name)
        proj_output_file = os.path.join(project_dir, output_name)
        clean_output_files([target_output_file, proj_output_file])
    else:
        all_files = [
            research_file, design_file, app_file, review_file, test_generated_file, audit_file, deploy_file,
            os.path.join(project_dir, "research.md"),
            os.path.join(project_dir, "design.md"),
            os.path.join(project_dir, "app.py"),
            os.path.join(project_dir, "review.md"),
            os.path.join(project_dir, "test_generated.py"),
            os.path.join(project_dir, "audit.md"),
            os.path.join(project_dir, "deploy.md"),
        ]
        clean_output_files(all_files)
    
    config = load_config()
    api_key = api_key_override or config.skill_api_key or os.getenv('SKILL_API_KEY', 'mock-skill-key')
    grok_url = grok_url or config.services.grok_researcher
    claude_url = claude_url or config.services.claude_architect
    codex_url = codex_url or config.services.codex_reviewer
    tester_url = tester_url or config.services.tester_agent
    security_url = security_url or config.services.security_agent
    devops_url = devops_url or config.services.devops_agent
    
    # Scan the workspace context
    try:
        scanner = ProjectScanner(root_dir=project_dir, extra_ignores=config.scanner.exclude_patterns)
        scanned_files = scanner.scan()
    except Exception as e:
        logger.warning(f"Failed to scan workspace: {e}")
        scanned_files = {}

    limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
    timeout = httpx.Timeout(10.0, connect=5.0)
    client = httpx.AsyncClient(limits=limits, timeout=timeout)
    try:
        if is_slash_cmd:
            agent_key, output_name = ROUTING_TABLE[first_word]
            logger.info(f"Smart routing active. Routing slash command '{first_word}' to '{agent_key}'...")
            
            url = None
            if agent_key == "grok":
                url = grok_url
            elif agent_key == "claude":
                url = claude_url
            elif agent_key == "codex":
                url = codex_url
            elif agent_key == "tester":
                url = tester_url
            elif agent_key == "security":
                url = security_url
            elif agent_key == "devops":
                url = devops_url
                
            if not url:
                raise PipelineError(f"Target URL for agent '{agent_key}' is not configured.")
                
            result = await call_api(url, api_key, prompt, context=scanned_files, client=client, poll_timeout=poll_timeout)
            
            output_file = os.path.join(workspace, output_name)
            try:
                with open(output_file, "w", encoding="utf-8") as f:
                    f.write(result)
                proj_output_file = os.path.join(project_dir, output_name)
                os.makedirs(project_dir, exist_ok=True)
                with open(proj_output_file, "w", encoding="utf-8") as f:
                    f.write(result)
            except Exception as e:
                raise PipelineError(f"Failed to write agent output to {output_file}: {e}")
                
            validate_file(output_file, agent_key, is_input=False)
            logger.info(f"Step '{agent_key}' completed successfully via routing. Output: {output_file}")
            log_conversation(prompt, result)
            return result

        # Step 1: Grok (Research) - Call API
        logger.info("--- Running Step: Grok ---")
        grok_content = await call_api(grok_url, api_key, prompt, context=scanned_files, client=client, poll_timeout=poll_timeout)
        try:
            with open(research_file, "w", encoding="utf-8") as f:
                f.write(grok_content)
            proj_research_file = os.path.join(project_dir, "research.md")
            with open(proj_research_file, "w", encoding="utf-8") as f:
                f.write(grok_content)
        except Exception as e:
            raise PipelineError(f"Failed to write Grok output to {research_file}: {e}")
        validate_file(research_file, "Grok", is_input=False)
        logger.info(f"Step 'Grok' successfully completed. Output verified: {research_file}")

        # Step 2: Claude (Design) - Call API
        logger.info("--- Running Step: Claude ---")
        validate_file(research_file, "Claude", is_input=True)
        try:
            with open(research_file, "r", encoding="utf-8") as f:
                claude_prompt = f.read()
        except Exception as e:
            raise PipelineError(f"Failed to read Grok output from {research_file}: {e}")
        
        scanned_files["research.md"] = claude_prompt
        
        claude_content = await call_api(claude_url, api_key, claude_prompt, context=scanned_files, client=client, poll_timeout=poll_timeout)
        try:
            with open(design_file, "w", encoding="utf-8") as f:
                f.write(claude_content)
            proj_design_file = os.path.join(project_dir, "design.md")
            with open(proj_design_file, "w", encoding="utf-8") as f:
                f.write(claude_content)
        except Exception as e:
            raise PipelineError(f"Failed to write Claude output to {design_file}: {e}")
        validate_file(design_file, "Claude", is_input=False)
        logger.info(f"Step 'Claude' successfully completed. Output verified: {design_file}")

        # Interactive loop
        if interactive:
            print(f"\n[Claude Design Output]\n{claude_content}\n")
            while True:
                feedback = input("Verify architecture. Press Enter to proceed or type modifications/comments: ").strip()
                if not feedback:
                    break
                logger.info("Re-running Claude Architect with feedback...")
                claude_prompt = f"{claude_prompt}\n\n[USER FEEDBACK]:\n{feedback}"
                scanned_files["research.md"] = claude_prompt
                claude_content = await call_api(claude_url, api_key, claude_prompt, context=scanned_files, client=client, poll_timeout=poll_timeout)
                try:
                    with open(design_file, "w", encoding="utf-8") as f:
                        f.write(claude_content)
                    proj_design_file = os.path.join(project_dir, "design.md")
                    with open(proj_design_file, "w", encoding="utf-8") as f:
                        f.write(claude_content)
                except Exception as e:
                    raise PipelineError(f"Failed to write Claude output to {design_file}: {e}")
                print(f"\n[Updated Claude Design Output]\n{claude_content}\n")

        # Parse design.md for file implementation task queue
        files_to_implement = parse_design_for_files(claude_content)
        
        if files_to_implement:
            logger.info(f"Parsed {len(files_to_implement)} files from design to implement: {[f['path'] for f in files_to_implement]}")
            # Execute self-healing loop for each file concurrently
            semaphore = asyncio.Semaphore(5)
            
            tasks = [
                process_single_file(
                    file_info, project_dir, config, codex_url, tester_url, security_url,
                    api_key, client, poll_timeout, max_retries, semaphore
                )
                for file_info in files_to_implement
            ]
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            failed_files = []
            aggregated_audits = []
            
            for i, result in enumerate(results):
                file_path = files_to_implement[i]["path"]
                if isinstance(result, Exception):
                    logger.error(f"Failed to process {file_path}: {result}")
                    failed_files.append(file_path)
                else:
                    # result is security_report
                    aggregated_audits.append(f"### Audit for {file_path}\n\n{result}")
            
            if failed_files:
                raise PipelineError(f"Self-healing loop failed to implement and verify files: {', '.join(failed_files)}")
            
            # Write review.md as implementation is verified
            review_content = "All files successfully implemented and verified through self-healing loop."
            try:
                with open(review_file, "w", encoding="utf-8") as f:
                    f.write(review_content)
                with open(os.path.join(project_dir, "review.md"), "w", encoding="utf-8") as f:
                    f.write(review_content)
            except Exception as e:
                logger.warning(f"Failed to write review.md: {e}")

            # Aggregate audit report
            consolidated_audit = "\n\n---\n\n".join(aggregated_audits) if aggregated_audits else "Consolidated project implementation and testing passed."
            try:
                with open(audit_file, "w", encoding="utf-8") as f:
                    f.write(consolidated_audit)
                with open(os.path.join(project_dir, "audit.md"), "w", encoding="utf-8") as f:
                    f.write(consolidated_audit)
            except Exception as e:
                logger.warning(f"Failed to write audit.md: {e}")
                
            # Run DevOps deployment (Step 7)
            logger.info("--- Running Step: DevOps ---")
            validate_file(audit_file, "DevOps", is_input=True)
            try:
                with open(audit_file, "r", encoding="utf-8") as f:
                    devops_prompt = f.read()
            except Exception as e:
                raise PipelineError(f"Failed to read Security output: {e}")
                
            try:
                proj_scanner = ProjectScanner(root_dir=project_dir, extra_ignores=config.scanner.exclude_patterns)
                current_context = proj_scanner.scan()
            except Exception:
                current_context = {}
            current_context["audit.md"] = devops_prompt
            
            devops_content = await call_api(devops_url, api_key, devops_prompt, context=current_context, client=client, poll_timeout=poll_timeout)
            try:
                with open(deploy_file, "w", encoding="utf-8") as f:
                    f.write(devops_content)
                with open(os.path.join(project_dir, "deploy.md"), "w", encoding="utf-8") as f:
                    f.write(devops_content)
            except Exception as e:
                raise PipelineError(f"Failed to write DevOps output to {deploy_file}: {e}")
            validate_file(deploy_file, "DevOps", is_input=False)
            logger.info(f"Step 'DevOps' successfully completed. Output verified: {deploy_file}")
            
            logger.info("Pipeline executed successfully and all files implemented, verified, and deployed.")
            log_conversation(prompt, devops_content)
            return devops_content

        # Fallback to single file pipeline (original behavior)
        logger.info("No files parsed from design.md. Running fallback single-file pipeline...")

        # Step 3: Antigravity (Programming) - Run as an async subprocess
        logger.info("--- Running Step: Antigravity ---")
        validate_file(design_file, "Antigravity", is_input=True)
        
        a_args = antigravity_args if antigravity_args is not None else DEFAULT_ANTIGRAVITY_ARGS
        antigravity_formatted_cmd = format_cmd_args(antigravity_cmd, a_args, prompt, input_path=design_file, output_path=app_file)
        
        logger.info(f"Command arguments: {antigravity_formatted_cmd}")
        
        if os.path.exists(app_file):
            try:
                os.remove(app_file)
                logger.info(f"Deleted old output file before execution: {app_file}")
            except Exception as e:
                logger.error(f"Failed to delete existing output file {app_file} before execution: {e}")
                raise PipelineError(f"Failed to delete existing output file {app_file} before execution: {e}")
                
        try:
            process = await asyncio.create_subprocess_exec(
                *antigravity_formatted_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL
            )
            stdout, stderr = await process.communicate()
        except Exception as e:
            logger.error(f"Failed to execute subprocess for 'Antigravity': {e}")
            raise PipelineError(f"Execution failed for 'Antigravity' due to: {e}")
            
        stdout_str = stdout.decode("utf-8", errors="replace")
        stderr_str = stderr.decode("utf-8", errors="replace")
        
        if stdout_str:
            print(f"[Antigravity STDOUT]\n{stdout_str.strip()}")
        if stderr_str:
            print(f"[Antigravity STDERR]\n{stderr_str.strip()}")
            
        if process.returncode != 0:
            logger.error(f"Step 'Antigravity' failed with exit code {process.returncode}")
            raise PipelineError(f"Step 'Antigravity' returned non-zero exit code: {process.returncode}")
            
        if not os.path.exists(app_file) or os.path.getsize(app_file) == 0:
            if stdout_str:
                logger.info(f"Writing captured stdout to output file: {app_file}")
                os.makedirs(project_dir, exist_ok=True)
                try:
                    with open(app_file, "w", encoding="utf-8") as f:
                        f.write(stdout_str)
                except Exception as e:
                    raise PipelineError(f"Failed to write stdout to output file {app_file}: {e}")
            else:
                logger.warning(f"No stdout captured and output file was not created for 'Antigravity'")
        
        if os.path.exists(app_file):
            try:
                shutil.copy2(app_file, os.path.join(project_dir, "app.py"))
            except Exception as e:
                logger.warning(f"Failed to copy app.py to project directory: {e}")
                
        validate_file(app_file, "Antigravity", is_input=False)
        logger.info(f"Step 'Antigravity' successfully completed. Output verified: {app_file}")

        # Step 4: Codex (Review) - Call API
        logger.info("--- Running Step: Codex ---")
        validate_file(app_file, "Codex", is_input=True)
        try:
            with open(app_file, "r", encoding="utf-8") as f:
                codex_prompt = f.read()
        except Exception as e:
            raise PipelineError(f"Failed to read Antigravity output from {app_file}: {e}")
            
        scanned_files["design.md"] = claude_content
        scanned_files["app.py"] = codex_prompt
        
        codex_content = await call_api(codex_url, api_key, codex_prompt, context=scanned_files, client=client, poll_timeout=poll_timeout)
        os.makedirs(project_dir, exist_ok=True)
        try:
            with open(review_file, "w", encoding="utf-8") as f:
                f.write(codex_content)
            proj_review_file = os.path.join(project_dir, "review.md")
            with open(proj_review_file, "w", encoding="utf-8") as f:
                f.write(codex_content)
        except Exception as e:
            raise PipelineError(f"Failed to write Codex output to {review_file}: {e}")
        validate_file(review_file, "Codex", is_input=False)
        logger.info(f"Step 'Codex' successfully completed. Output verified: {review_file}")
        
        # Step 5: Tester (Test generation) - Call API
        logger.info("--- Running Step: Tester ---")
        validate_file(review_file, "Tester", is_input=True)
        try:
            with open(review_file, "r", encoding="utf-8") as f:
                tester_prompt = f.read()
        except Exception as e:
            raise PipelineError(f"Failed to read Codex output from {review_file}: {e}")
            
        scanned_files["review.md"] = tester_prompt
        
        tester_content = await call_api(tester_url, api_key, tester_prompt, context=scanned_files, client=client, poll_timeout=poll_timeout)
        os.makedirs(project_dir, exist_ok=True)
        try:
            with open(test_generated_file, "w", encoding="utf-8") as f:
                f.write(tester_content)
            proj_test_file = os.path.join(project_dir, "test_generated.py")
            with open(proj_test_file, "w", encoding="utf-8") as f:
                f.write(tester_content)
        except Exception as e:
            raise PipelineError(f"Failed to write Tester output to {test_generated_file}: {e}")
        validate_file(test_generated_file, "Tester", is_input=False)
        logger.info(f"Step 'Tester' successfully completed. Output verified: {test_generated_file}")
        
        # Step 6: Security (Security Audit)
        logger.info("--- Running Step: Security ---")
        validate_file(test_generated_file, "Security", is_input=True)
        try:
            with open(test_generated_file, "r", encoding="utf-8") as f:
                security_prompt = f.read()
        except Exception as e:
            raise PipelineError(f"Failed to read Tester output: {e}")
        scanned_files["test_generated.py"] = security_prompt
        security_content = await call_api(security_url, api_key, security_prompt, context=scanned_files, client=client, poll_timeout=poll_timeout)
        try:
            with open(audit_file, "w", encoding="utf-8") as f:
                f.write(security_content)
            proj_audit_file = os.path.join(project_dir, "audit.md")
            with open(proj_audit_file, "w", encoding="utf-8") as f:
                f.write(security_content)
        except Exception as e:
            raise PipelineError(f"Failed to write Security output to {audit_file}: {e}")
        validate_file(audit_file, "Security", is_input=False)
        logger.info(f"Step 'Security' successfully completed. Output verified: {audit_file}")

        # Step 7: DevOps (Deployment)
        logger.info("--- Running Step: DevOps ---")
        validate_file(audit_file, "DevOps", is_input=True)
        try:
            with open(audit_file, "r", encoding="utf-8") as f:
                devops_prompt = f.read()
        except Exception as e:
            raise PipelineError(f"Failed to read Security output: {e}")
        scanned_files["audit.md"] = devops_prompt
        devops_content = await call_api(devops_url, api_key, devops_prompt, context=scanned_files, client=client, poll_timeout=poll_timeout)
        try:
            with open(deploy_file, "w", encoding="utf-8") as f:
                f.write(devops_content)
            proj_deploy_file = os.path.join(project_dir, "deploy.md")
            with open(proj_deploy_file, "w", encoding="utf-8") as f:
                f.write(devops_content)
        except Exception as e:
            raise PipelineError(f"Failed to write DevOps output to {deploy_file}: {e}")
        validate_file(deploy_file, "DevOps", is_input=False)
        logger.info(f"Step 'DevOps' successfully completed. Output verified: {deploy_file}")

        logger.info("Pipeline executed successfully and all intermediate files verified.")
        log_conversation(prompt, devops_content)
    finally:
        await client.aclose()


def main():
    parser = argparse.ArgumentParser(
        description="5-AI CLI Orchestrator pipeline executing Grok -> Claude -> Antigravity -> Codex -> Tester."
    )
    parser.add_argument("--prompt", required=True, help="Initial research/query prompt for the pipeline")
    parser.add_argument("--workspace", default=None, help="Workspace directory for context files (defaults to current dir)")
    
    # Custom commands/paths
    parser.add_argument("--grok-cmd", default=resolve_grok_cmd(), help="Command/path to Grok CLI")
    parser.add_argument("--claude-cmd", default=resolve_claude_cmd(), help="Command/path to Claude CLI")
    parser.add_argument("--antigravity-cmd", default=resolve_antigravity_cmd(), help="Command/path to Antigravity CLI")
    parser.add_argument("--codex-cmd", default=resolve_codex_cmd(), help="Command/path to Codex CLI")
    parser.add_argument("--tester-cmd", default=resolve_tester_cmd(), help="Command/path to Tester CLI")
    parser.add_argument("--security-cmd", default=resolve_security_cmd(), help="Command/path to Security CLI")
    parser.add_argument("--devops-cmd", default=resolve_devops_cmd(), help="Command/path to DevOps CLI")
    
    # Custom arguments
    parser.add_argument("--grok-args", nargs="*", default=None, help="Custom arguments for Grok step")
    parser.add_argument("--claude-args", nargs="*", default=None, help="Custom arguments for Claude step")
    parser.add_argument("--antigravity-args", nargs="*", default=None, help="Custom arguments for Antigravity step")
    parser.add_argument("--codex-args", nargs="*", default=None, help="Custom arguments for Codex step")
    parser.add_argument("--tester-args", nargs="*", default=None, help="Custom arguments for Tester step")
    parser.add_argument("--security-args", nargs="*", default=None, help="Custom arguments for Security step")
    parser.add_argument("--devops-args", nargs="*", default=None, help="Custom arguments for DevOps step")
    
    # Service URL overrides
    parser.add_argument("--grok-url", default=None, help="Service URL override for Grok")
    parser.add_argument("--claude-url", default=None, help="Service URL override for Claude")
    parser.add_argument("--codex-url", default=None, help="Service URL override for Codex")
    parser.add_argument("--tester-url", default=None, help="Service URL override for Tester")
    parser.add_argument("--security-url", default=None, help="Service URL override for Security")
    parser.add_argument("--devops-url", default=None, help="Service URL override for DevOps")

    # API key override
    parser.add_argument("--api-key-override", "--api-key", dest="api_key_override", default=None, help="API key override for the pipeline")

    # Polling timeout
    parser.add_argument("--poll-timeout", type=float, default=60.0, help="Polling timeout in seconds")
    parser.add_argument("--interactive", action="store_true", help="Interactive design review loop")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retries for self-healing loop")
    
    args = parser.parse_args()
    
    try:
        asyncio.run(run_pipeline(
            prompt=args.prompt,
            grok_cmd=args.grok_cmd,
            claude_cmd=args.claude_cmd,
            antigravity_cmd=args.antigravity_cmd,
            codex_cmd=args.codex_cmd,
            tester_cmd=args.tester_cmd,
            security_cmd=args.security_cmd,
            devops_cmd=args.devops_cmd,
            grok_args=args.grok_args,
            claude_args=args.claude_args,
            antigravity_args=args.antigravity_args,
            codex_args=args.codex_args,
            tester_args=args.tester_args,
            security_args=args.security_args,
            devops_args=args.devops_args,
            workspace=args.workspace,
            grok_url=args.grok_url,
            claude_url=args.claude_url,
            codex_url=args.codex_url,
            tester_url=args.tester_url,
            security_url=args.security_url,
            devops_url=args.devops_url,
            api_key_override=args.api_key_override,
            poll_timeout=args.poll_timeout,
            interactive=args.interactive,
            max_retries=args.max_retries
        ))
    except PipelineError as e:
        logger.error(f"Pipeline Execution Failed: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected Pipeline Failure: {e}")
        sys.exit(1)

run_pipeline_async = run_pipeline

if __name__ == "__main__":
    main()
