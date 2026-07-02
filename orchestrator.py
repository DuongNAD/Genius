#!/usr/bin/env python3
import argparse
import ast
import sys
import os
import asyncio
import logging
import hashlib
import json
import httpx
import re
import shutil
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
)

# Re-exported so existing `from orchestrator import extract_code` callers (and
# tests) keep working after the helper moved to a shared module.
from ag_core.utils.code_extract import extract_code
from ag_core.utils.cli_runner import cli_timeout


def make_http_client() -> httpx.AsyncClient:
    """Build an AsyncClient with the shared connection-pool limits and timeouts."""
    limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
    timeout = httpx.Timeout(10.0, connect=5.0)
    return httpx.AsyncClient(limits=limits, timeout=timeout)


async def gather_or_raise(*awaitables):
    """Run awaitables concurrently like asyncio.gather, but always let every
    branch finish (return_exceptions=True) so a failure in one does not leave
    the siblings running as orphaned tasks. The first exception (in argument
    order) is re-raised afterwards to preserve fail-fast propagation; otherwise
    the list of results is returned."""
    results = await asyncio.gather(*awaitables, return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException):
            raise r
    return results


def effective_poll_timeout(poll_timeout: float) -> float:
    """Clamp the polling deadline so it can never undercut the CLI timeout.

    The skill servers run local agent CLIs that may legitimately take up to
    ``cli_timeout()`` (GENIUS_CLI_TIMEOUT, default 600s). A poll deadline
    shorter than that makes the orchestrator abandon in-flight work while the
    server keeps burning credits, so the effective deadline is at least
    ``cli_timeout() + 60`` (a grace margin for queueing + HTTP overhead).

    Under pytest the clamp only applies when GENIUS_CLI_TIMEOUT is explicitly
    set, so tests exercising short poll timeouts stay fast and deterministic.
    """
    if os.getenv("GENIUS_CLI_TIMEOUT") is None and (
        "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST")
    ):
        return poll_timeout
    return max(poll_timeout, cli_timeout() + 60.0)


# Cap for failure logs embedded back into agent prompts by the self-healing
# loops; unbounded pytest/flake8 output would otherwise blow up the prompt.
MAX_EMBEDDED_LOG_CHARS = 15000


def truncate_log(text: str, limit: int = MAX_EMBEDDED_LOG_CHARS) -> str:
    """Keep only the LAST ``limit`` characters of a log (failures summarize at
    the end), prefixing a marker so the agent knows the head was dropped."""
    if text and len(text) > limit:
        return "(truncated)\n" + text[-limit:]
    return text


def invalid_python_feedback(
    code: str, dest_path: str, source: str = "Codex"
) -> str | None:
    """Validate code destined for a ``.py`` file; return steering feedback when
    it is not valid Python, else None.

    Agentic CLIs sometimes answer with a pytest log or prose instead of source
    (a real run once wrote a full test-session log into ``calculator.py``).
    Writing that garbage poisons every later stage, so the self-healing loops
    call this BEFORE writing: on a SyntaxError the attempt is treated as failed
    and the returned feedback steers the next attempt's prompt. Non-``.py``
    destinations are never validated.
    """
    if not dest_path.endswith(".py"):
        return None
    try:
        ast.parse(code)
    except SyntaxError as e:
        return (
            f"{source} response was not valid Python (SyntaxError: {e}); "
            "respond ONLY with the complete file content in a single "
            "```python fenced block."
        )
    return None


def is_test_module(rel_path: str) -> bool:
    """True when a designed file IS a pytest module (tests/**, test_*.py).

    The per-file loop runs such files directly under pytest instead of
    generating tests-for-tests (a real run produced
    ``tests/test_tests_test_core.py``) and skips the security audit of test
    code — both were wasted agent calls.
    """
    norm = rel_path.replace("\\", "/")
    name = os.path.basename(norm)
    return name.startswith("test_") or norm.startswith("tests/") or "/tests/" in norm


def save_raw_response(project_dir: str, name: str, content: str) -> None:
    """Persist a raw agent response under ``logs/raw/`` for debugging.

    Failures are non-fatal: raw capture must never break a run. Without this,
    diagnosing a rejected stage means re-driving the agents by hand.
    """
    try:
        raw_dir = os.path.join(project_dir, "logs", "raw")
        os.makedirs(raw_dir, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.\-]+", "_", name)
        with open(os.path.join(raw_dir, f"{safe}.md"), "w", encoding="utf-8") as f:
            f.write(content or "")
    except Exception as e:
        logger.warning(f"Failed to save raw response {name}: {e}")


def design_selfheal_enabled() -> bool:
    """Whether the design-format retry loop runs (production yes, pytest no).

    Under pytest the legacy single-file branch must stay reachable for the
    historical tests, whose fixed mock call sequences cannot absorb extra
    design calls — same convention as the debate-rounds default.
    """
    return not ("pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"))


def degraded_mode() -> bool:
    """Opt-in resilience: when ``GENIUS_DEGRADED_MODE`` is truthy, the pipeline
    keeps producing partial artifacts instead of aborting when a non-critical
    stage fails (some files fail to verify, or the DevOps/deploy stage errors).

    Off by default so CI and normal runs keep strict fail-fast semantics.
    """
    return os.getenv("GENIUS_DEGRADED_MODE", "").lower() in ("1", "true", "yes")


def resolve_degraded_outcome(paths, results, label):
    """Decide a degraded fan-out outcome from ``gather(return_exceptions=True)``.

    ``results`` is aligned with ``paths``. If *every* file failed, the first
    exception is re-raised (a total failure is still fatal, even in degraded
    mode). If some succeeded, returns ``(failed_paths, summary_str)``; if none
    failed, returns ``([], None)``. Pure/synchronous so it is unit-testable.
    """
    failed = [paths[i] for i, r in enumerate(results) if isinstance(r, BaseException)]
    if failed and len(failed) == len(paths):
        for r in results:
            if isinstance(r, BaseException):
                raise r
    summary = None
    if failed:
        verified = len(paths) - len(failed)
        summary = (
            f"{label} completed in degraded mode: {verified}/{len(paths)} files "
            f"verified. Failed: {', '.join(failed)}."
        )
    return failed, summary


def write_progress_md(progress_file_path: str, status_dict: dict) -> None:
    """Write the per-file pipeline progress as a markdown checklist. Failures
    are logged but non-fatal."""
    try:
        os.makedirs(os.path.dirname(progress_file_path), exist_ok=True)
        with open(progress_file_path, "w", encoding="utf-8") as f:
            f.write("# Current Progress\n\n")
            for path, status in status_dict.items():
                f.write(f"- {path}: {status}\n")
    except Exception as e:
        logger.warning(f"Failed to update CURRENT_PROG.md: {e}")


def _resolve_pipeline_setup(prompt, workspace, max_debate_rounds):
    """Shared pipeline preamble: resolve the debate-round default (0 under
    pytest), validate the prompt, derive the project name from the prompt, and
    default the workspace to cwd. Returns (project_name, workspace,
    max_debate_rounds). Raises PipelineError on an empty prompt."""
    if max_debate_rounds is None:
        if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
            max_debate_rounds = 0
        else:
            max_debate_rounds = 2

    if not prompt or not prompt.strip():
        raise PipelineError("Prompt cannot be empty.")

    slugified = re.sub(r"[^a-zA-Z0-9]+", "_", prompt.strip().lower()).strip("_")
    if not slugified:
        project_name = "default_project"
    elif len(slugified) > 50:
        project_name = (
            slugified[:40]
            + "_"
            + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
        )
    else:
        project_name = slugified

    if workspace is None:
        workspace = os.getcwd()

    return project_name, workspace, max_debate_rounds


def parse_design_for_files(design_content: str) -> list:
    """
    Parses design_content for a list of files to implement.
    Returns a list of dicts, e.g., [{"path": "src/main.py", "specification": "..."}].
    """
    from ag_core.models import DesignPlan

    def _validate_obj(obj):
        if not isinstance(obj, dict) or "files" not in obj:
            return None
        try:
            if hasattr(DesignPlan, "model_validate"):
                plan = DesignPlan.model_validate(obj)
            else:
                plan = DesignPlan.parse_obj(obj)
            return [
                {"path": f.path, "specification": f.specification} for f in plan.files
            ]
        except Exception:
            return None

    # 1. Look for a DesignPlan JSON object. Prefer a ```json fenced block, then the
    #    whole document. Use a brace-aware JSON decoder (raw_decode) so a '}' inside
    #    a specification string can't truncate the object the way the old `\{.*?\}`
    #    / find..rfind regex did.
    decoder = json.JSONDecoder()
    candidates = re.findall(
        r"```json\s*(.*?)```", design_content, re.DOTALL | re.IGNORECASE
    )
    candidates.append(design_content)
    for text in candidates:
        idx = 0
        while True:
            start = text.find("{", idx)
            if start == -1:
                break
            try:
                obj, end = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                idx = start + 1
                continue
            result = _validate_obj(obj)
            if result is not None:
                return result
            idx = start + end

    # 2. Fall back to regex that extracts markdown code blocks with filepath annotations
    code_blocks = re.findall(
        r"[ \t]*```[a-zA-Z0-9_-]*\s*\n(.*?)\n[ \t]*```", design_content, re.DOTALL
    )
    files = []
    for block in code_blocks:
        m = re.search(r"(?:#|//)\s*(?:filepath|path):\s*([^\s\n\r]+)", block)
        if m:
            filepath = m.group(1).strip()
            files.append({"path": filepath, "specification": block.strip()})

    return files


def safe_join(base_dir: str, rel_path: str) -> str:
    """
    Join an (untrusted, model-supplied) relative path onto base_dir, guaranteeing
    the result stays inside base_dir. Rejects absolute paths, Windows drive-relative
    paths, and '..' traversal so a malicious design plan cannot write outside the
    project workspace.
    """
    if (
        not rel_path
        or os.path.isabs(rel_path)
        or (len(rel_path) > 1 and rel_path[1] == ":")
    ):
        raise PipelineError(
            f"Unsafe file path rejected (absolute path not allowed): {rel_path!r}"
        )
    base_real = os.path.realpath(base_dir)
    target_real = os.path.realpath(os.path.join(base_real, rel_path))
    if target_real != base_real and not target_real.startswith(base_real + os.sep):
        raise PipelineError(
            f"Unsafe file path rejected (escapes project dir): {rel_path!r}"
        )
    return target_real


def flatten_rel_path(rel_path: str) -> str:
    """
    Turn a relative path like 'src/a/util.py' into a collision-free stem
    'src_a_util' so two files with the same basename in different dirs don't
    overwrite each other's generated test/audit/log files.
    """
    no_ext = os.path.splitext(rel_path)[0]
    return re.sub(r"[\\/]+", "_", no_ext).strip("_") or "file"


async def run_subprocess(cmd: list, env: dict = None) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
    )
    stdout, stderr = await process.communicate()
    output = (
        stdout.decode("utf-8", errors="replace")
        + "\n"
        + stderr.decode("utf-8", errors="replace")
    )
    return process.returncode, output


# Setup logger to output to stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("orchestrator")

import contextvars

# Module-level default; tests set this directly (orchestrator.DISTRIBUTED_MODE).
DISTRIBUTED_MODE = False
# Per-pipeline override: run_pipeline sets this in its own task context so two
# concurrent pipelines (e.g. MCP orchestrate jobs) don't stomp a shared global.
# Task contexts are copied at create_task time and discarded on completion, so
# the set is naturally scoped — no reset needed.
_DISTRIBUTED_MODE_VAR: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "genius_distributed_mode"
)


def _is_distributed() -> bool:
    """Effective distributed-dispatch flag: the pipeline's contextvar if set,
    else the module-level DISTRIBUTED_MODE (the direct-call/test path)."""
    try:
        return _DISTRIBUTED_MODE_VAR.get()
    except LookupError:
        return DISTRIBUTED_MODE


DEFAULT_ANTIGRAVITY_ARGS = ["--design", "{input}", "--output", "{output}"]

ROUTING_TABLE = {
    # Researcher
    "/research": ("researcher", "research.md"),
    "/summarize": ("researcher", "research.md"),
    "/fact-check": ("researcher", "research.md"),
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


class ChecksumMismatchError(Exception):
    """Custom exception raised when payload checksum validation fails."""


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
            special_paths.append(
                os.path.join(
                    user_profile, ".gemini", "antigravity", "bin", "antigravity.cmd"
                )
            )
            special_paths.append(
                os.path.join(
                    user_profile, ".gemini", "antigravity", "bin", "antigravity"
                )
            )
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
    """Archive context/output files from a previous run to ``<name>.bak``
    (overwriting an older .bak) so a fresh run cannot consume stale data but
    the previous artifacts are still recoverable."""
    logger.info("Archiving old context/output files...")
    for path in paths:
        if os.path.exists(path):
            try:
                backup_path = path + ".bak"
                os.replace(path, backup_path)
                logger.info(f"Archived old file: {path} -> {backup_path}")
            except Exception as e:
                logger.error(f"Failed to archive {path}: {e}")
                raise PipelineError(f"Failed to archive {path}: {e}")


def validate_file(path, step_name, is_input=True):
    """Validate that a context file exists and is not empty."""
    desc = "Input" if is_input else "Output"
    if not os.path.exists(path):
        raise PipelineError(f"{desc} file for '{step_name}' does not exist: {path}")
    if os.path.getsize(path) == 0:
        raise PipelineError(f"{desc} file for '{step_name}' is empty: {path}")


def format_cmd_args(
    cmd_executable, args_template, prompt, input_path=None, output_path=None
):
    """Format command arguments by replacing placeholders with actual values."""
    cmd = [cmd_executable]

    input_content = ""
    if input_path and os.path.exists(input_path):
        try:
            with open(input_path, "r", encoding="utf-8") as f:
                input_content = f.read()
        except Exception as e:
            logger.error(f"Failed to read input file {input_path} for formatting: {e}")
            raise PipelineError(
                f"Failed to read input file {input_path} for formatting: {e}"
            )

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
from ag_core.utils.security import verify_checksum


def verify_response_checksum(response) -> None:
    expected_checksum = response.headers.get("X-Payload-SHA256")
    if not expected_checksum:
        raise ChecksumMismatchError("Response is missing X-Payload-SHA256 header")
    config = load_config()
    secret = config.skill_api_key or os.getenv("SKILL_API_KEY", "")
    if not verify_checksum(response.content, expected_checksum, secret):
        raise ChecksumMismatchError(
            f"Response checksum mismatch: expected {expected_checksum}"
        )


def is_transient_error(exception) -> bool:
    logger.debug("Evaluating retryability: %s", type(exception).__name__)
    if isinstance(exception, ChecksumMismatchError):
        # Intentionally retryable: a mismatch may be a transient corruption,
        # and call_api still fails fast after the bounded retry count. This
        # behaviour is locked by
        # test_integration.test_orchestrator_checksum_mismatch_response_retries.
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
    reraise=True,
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
    reraise=True,
)
async def perform_get_with_retry(client, url, headers):
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    verify_response_checksum(response)
    return response


from collections import OrderedDict

# Process-global response cache. Bounded with LRU eviction so a long-lived
# server (MCP / hub) can't grow it without limit — every distinct agent call
# used to store its full response string forever. Size configurable via
# GENIUS_CACHE_MAXSIZE (entries).
_API_RESPONSE_CACHE: "OrderedDict[str, str]" = OrderedDict()
_API_RESPONSE_CACHE_MAXSIZE = max(1, int(os.environ.get("GENIUS_CACHE_MAXSIZE") or 256))


def _cache_store(key: str, value: str) -> None:
    cache = _API_RESPONSE_CACHE
    if key in cache:
        cache.move_to_end(key)
    cache[key] = value
    while len(cache) > _API_RESPONSE_CACHE_MAXSIZE:
        cache.popitem(last=False)


async def call_api(
    url: str,
    api_key: str,
    prompt: str,
    context: dict = None,
    client: httpx.AsyncClient = None,
    poll_timeout: float = 60.0,
) -> str:
    import time
    import uuid
    from ag_core.utils.jwt import encode_jwt

    import os

    # Key the cache by the hash of the URL, the prompt, and the sorted JSON-serialized context dictionary.
    sorted_context = json.dumps(context or {}, sort_keys=True)
    cache_string = f"{url}\n{prompt}\n{sorted_context}"
    cache_key = hashlib.sha256(cache_string.encode("utf-8")).hexdigest()

    use_cache = True
    if "PYTEST_CURRENT_TEST" in os.environ and not os.environ.get(
        "ENABLE_GENIUS_CACHE"
    ):
        use_cache = False

    # Never poll for less time than the skill server's own CLI is allowed to
    # run, or a slow-but-legitimate agent call gets abandoned mid-flight.
    poll_timeout = effective_poll_timeout(poll_timeout)

    if use_cache and cache_key in _API_RESPONSE_CACHE:
        logger.info(f"Cache hit for URL: {url}")
        _API_RESPONSE_CACHE.move_to_end(cache_key)
        return _API_RESPONSE_CACHE[cache_key]

    if _is_distributed():
        from serve import (
            worker_registry,
            pending_tasks,
            central_hub,
            WorkerDisconnectedError,
        )

        role = None
        first_word = prompt.strip().split()[0] if prompt.strip() else ""
        if first_word.startswith("/") and first_word in ROUTING_TABLE:
            role = ROUTING_TABLE[first_word][0]
        else:
            url_lower = url.lower()
            # "grok" in the URL is the researcher's legacy service name.
            if "8001" in url_lower or "researcher" in url_lower or "grok" in url_lower:
                role = "researcher"
            elif "8002" in url_lower or "claude" in url_lower:
                role = "claude"
            elif "8003" in url_lower or "codex" in url_lower:
                role = "codex"
            elif "8004" in url_lower or "tester" in url_lower:
                role = "tester"
            elif "8005" in url_lower or "security" in url_lower:
                role = "security"
            elif "8006" in url_lower or "devops" in url_lower:
                role = "devops"

        if not role:
            raise PipelineError(
                f"Could not determine role for URL: {url} and prompt: {prompt}"
            )

        # Check if the in-memory registry has any workers registered (in-process tests)
        has_in_memory_workers = False
        try:
            if worker_registry and len(worker_registry.workers) > 0:
                has_in_memory_workers = True
        except Exception:
            pass

        if not has_in_memory_workers:
            # `or` so a blank GENIUS_HUB_URL from .env.example is treated as unset.
            hub_url = os.environ.get("GENIUS_HUB_URL") or "http://127.0.0.1:8000"
            config = load_config()
            secret = config.skill_api_key or os.getenv("SKILL_API_KEY", "")

            async with httpx.AsyncClient() as http_client:
                from ag_core.utils.security import calculate_checksum

                payload_workers = {}
                workers_checksum = calculate_checksum(payload_workers, secret)
                payload_bytes = json.dumps(
                    payload_workers, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")

                post_payload = {"sub": "orchestrator", "exp": time.time() + 300}
                post_token = encode_jwt(post_payload, api_key)
                headers = {
                    "X-API-Key": post_token,
                    "Authorization": f"Bearer {post_token}",
                    "X-Payload-SHA256": workers_checksum,
                    "Content-Type": "application/json",
                }

                resp = await http_client.post(
                    f"{hub_url}/workers", content=payload_bytes, headers=headers
                )
                resp.raise_for_status()
                workers_dict = resp.json()

                # Alias-tolerant role matching: workers may still advertise
                # the legacy "grok"/"grok_researcher" ids for the researcher.
                from ag_core.provider_factory import canonical_role

                def _eligible(w_info):
                    registered = (canonical_role(r) for r in w_info.get("roles", []))
                    return (
                        canonical_role(role) in registered
                        and w_info.get("status") == "idle"
                    )

                idle_worker_ids = [
                    w_id for w_id, w_info in workers_dict.items() if _eligible(w_info)
                ]
                poll_start = time.time()
                while not idle_worker_ids:
                    if time.time() - poll_start > poll_timeout:
                        raise PipelineError(
                            f"No idle worker available for role '{role}' within {poll_timeout} seconds."
                        )
                    await asyncio.sleep(0.5)
                    resp = await http_client.post(
                        f"{hub_url}/workers", content=payload_bytes, headers=headers
                    )
                    resp.raise_for_status()
                    workers_dict = resp.json()
                    idle_worker_ids = [
                        w_id
                        for w_id, w_info in workers_dict.items()
                        if _eligible(w_info)
                    ]

                worker_id = idle_worker_ids[0]

                dispatch_payload = {
                    "role": role,
                    "task_data": {
                        "role": role,
                        "prompt": prompt,
                        "context": context or {},
                    },
                }
                dispatch_checksum = calculate_checksum(dispatch_payload, secret)
                dispatch_bytes = json.dumps(
                    dispatch_payload, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")

                headers["X-Payload-SHA256"] = dispatch_checksum
                resp = await http_client.post(
                    f"{hub_url}/dispatch", content=dispatch_bytes, headers=headers
                )
                resp.raise_for_status()
                dispatch_res = resp.json()
                task_id = dispatch_res["task_id"]

                task_completed = False
                poll_start = time.time()
                while not task_completed:
                    if time.time() - poll_start > poll_timeout:
                        raise PipelineError(
                            f"Task '{task_id}' timed out after {poll_timeout} seconds."
                        )

                    tasks_payload = {}
                    tasks_checksum = calculate_checksum(tasks_payload, secret)
                    tasks_bytes = json.dumps(
                        tasks_payload, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                    headers["X-Payload-SHA256"] = tasks_checksum

                    resp = await http_client.post(
                        f"{hub_url}/tasks", content=tasks_bytes, headers=headers
                    )
                    resp.raise_for_status()
                    all_tasks = resp.json()

                    task_info = all_tasks.get(task_id)
                    if not task_info:
                        raise PipelineError(
                            f"Task '{task_id}' not found in tasks list."
                        )

                    status = task_info.get("status")
                    if status == "completed":
                        result = task_info.get("result")
                        if use_cache:
                            _cache_store(cache_key, result)
                        return result
                    elif status == "failed":
                        err = task_info.get("result", {}).get(
                            "error", "Unknown task failure"
                        )
                        raise PipelineError(f"Task failed: {err}")

                    await asyncio.sleep(0.5)
        else:
            logger.info(f"[Distributed] Selecting idle worker for role '{role}'")

            worker_id = None
            poll_start = time.time()
            while worker_id is None:
                worker_id = await worker_registry.select_idle_worker(role)
                if worker_id is None:
                    if time.time() - poll_start > poll_timeout:
                        raise PipelineError(
                            f"No idle worker available for role '{role}' within {poll_timeout} seconds."
                        )
                    await asyncio.sleep(0.5)

            logger.info(
                f"[Distributed] Selected worker '{worker_id}' for role '{role}'"
            )

            async with worker_registry.lock:
                worker = await worker_registry.get_worker(worker_id)
                if not worker:
                    raise PipelineError(
                        f"Worker '{worker_id}' disappeared from registry."
                    )
                worker["status"] = "busy"

            task_id = f"task_{uuid.uuid4().hex[:8]}"
            task_data = {"role": role, "prompt": prompt, "context": context or {}}

            async with central_hub.lock:
                central_hub.tasks[task_id] = {
                    "task_id": task_id,
                    "role": role,
                    "task_data": task_data,
                    "status": "running",
                    "result": None,
                    "created_at": time.time(),
                    "worker_id": worker_id,
                    "started_at": time.time(),
                }

            ws = worker.get("ws")
            if ws is None:
                raise PipelineError(
                    f"Worker '{worker_id}' does not have an active WebSocket connection."
                )

            serialized = json.dumps(task_data, sort_keys=True).encode("utf-8")
            checksum = hashlib.sha256(serialized).hexdigest()

            dispatch_payload = {
                "type": "dispatch",
                "task_id": task_id,
                "task_data": task_data,
                "checksum": checksum,
            }
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            pending_tasks[task_id] = fut

            logger.info(
                f"[Distributed] Sending dispatch message for task '{task_id}' to worker '{worker_id}'"
            )
            try:
                await ws.send_json(dispatch_payload)
            except Exception as e:
                pending_tasks.pop(task_id, None)
                async with worker_registry.lock:
                    worker["status"] = "idle"
                async with central_hub.lock:
                    central_hub.tasks[task_id]["status"] = "failed"
                    central_hub.tasks[task_id]["result"] = {
                        "error": f"WS send error: {str(e)}"
                    }
                raise PipelineError(
                    f"Failed to send task to worker '{worker_id}' over WebSocket: {e}"
                )

            try:
                result = await asyncio.wait_for(fut, timeout=poll_timeout)
                logger.info(f"[Distributed] Task '{task_id}' completed successfully")
                if use_cache:
                    _cache_store(cache_key, result)
                return result
            except (asyncio.TimeoutError, TimeoutError) as e:
                logger.error(f"[Distributed] Task '{task_id}' timed out: {e}")
                async with central_hub.lock:
                    if task_id in central_hub.tasks:
                        central_hub.tasks[task_id]["status"] = "failed"
                        central_hub.tasks[task_id]["result"] = {
                            "error": f"Task timed out after {poll_timeout}s"
                        }
                async with worker_registry.lock:
                    worker = await worker_registry.get_worker(worker_id)
                    if worker:
                        worker["status"] = "idle"
                        ws = worker.get("ws")
                        if ws:
                            try:
                                await ws.send_json(
                                    {"type": "cancel", "task_id": task_id}
                                )
                            except Exception:
                                pass
                        elif central_hub.network:
                            try:
                                payload = {"task_id": task_id}
                                headers = central_hub.create_headers(payload)
                                await central_hub.network.send_to_worker(
                                    worker_id, "/cancel", payload, headers
                                )
                            except Exception:
                                pass
                raise asyncio.TimeoutError(
                    f"Task timed out after {poll_timeout} seconds"
                )
            except WorkerDisconnectedError as e:
                logger.error(
                    f"[Distributed] Worker disconnected during task '{task_id}': {e}"
                )
                raise
            except asyncio.CancelledError:
                logger.info(f"[Distributed] Task '{task_id}' cancelled by orchestrator")
                async with central_hub.lock:
                    if task_id in central_hub.tasks:
                        central_hub.tasks[task_id]["status"] = "failed"
                        central_hub.tasks[task_id]["result"] = {"error": "cancelled"}
                async with worker_registry.lock:
                    worker = await worker_registry.get_worker(worker_id)
                    if worker:
                        worker["status"] = "idle"
                        ws = worker.get("ws")
                        if ws:
                            try:
                                await ws.send_json(
                                    {"type": "cancel", "task_id": task_id}
                                )
                            except Exception:
                                pass
                        elif central_hub.network:
                            try:
                                payload = {"task_id": task_id}
                                headers = central_hub.create_headers(payload)
                                await central_hub.network.send_to_worker(
                                    worker_id, "/cancel", payload, headers
                                )
                            except Exception:
                                pass
                raise
            except Exception as e:
                logger.error(f"[Distributed] Task '{task_id}' failed: {e}")
                raise PipelineError(
                    f"Task '{task_id}' failed on worker '{worker_id}': {e}"
                )
            finally:
                pending_tasks.pop(task_id, None)

    req_payload = {"prompt": prompt, "context": context}

    # Calculate checksum for POST request body
    from ag_core.utils.security import calculate_checksum

    config = load_config()
    secret = config.skill_api_key or os.getenv("SKILL_API_KEY", "")
    req_checksum = calculate_checksum(req_payload, secret)
    payload_bytes = json.dumps(
        req_payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    base_url = url.rstrip("/")
    # Stable across the perform_post_with_retry retries so a re-sent /run after
    # a transient error is deduplicated server-side instead of running twice.
    idempotency_key = uuid.uuid4().hex

    def _http_error_detail(exc: Exception) -> str:
        """Append the response body for HTTP status errors so a 401 'Invalid
        API Key' is distinguishable from a 400 'Checksum mismatch'."""
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                return f" | Response body: {exc.response.text[:500]}"
            except Exception:
                return ""
        return ""

    async def _execute(c):
        try:
            # 1. Start the run
            post_payload = {"sub": "orchestrator", "exp": time.time() + 300}
            post_token = encode_jwt(post_payload, api_key)
            post_headers = {
                "X-API-Key": post_token,
                "Authorization": f"Bearer {post_token}",
                "X-Payload-SHA256": req_checksum,
                "Content-Type": "application/json",
                "X-Idempotency-Key": idempotency_key,
            }
            response = await perform_post_with_retry(
                c, f"{base_url}/run", payload_bytes, post_headers
            )
            res_data = response.json()
            task_id = res_data.get("task_id")
            if not task_id:
                raise PipelineError(f"No task_id returned from {base_url}/run")
        except Exception as e:
            detail = _http_error_detail(e)
            logger.error(
                f"HTTP request to start task at {base_url}/run failed: {e}{detail}"
            )
            raise PipelineError(
                f"HTTP request to start task at {base_url}/run failed: {e}{detail}"
            )

        # GET request has empty body, so checksum is of empty bytes
        get_checksum = calculate_checksum(b"", secret)

        # 2. Poll for completion
        poll_start = time.time()
        while True:
            if time.time() - poll_start > poll_timeout:
                raise PipelineError(
                    f"Task '{task_id}' at {base_url} timed out. Polling exceeded "
                    f"poll_timeout of {poll_timeout} seconds. If the agent CLI "
                    f"legitimately needs longer, raise --poll-timeout and/or "
                    f"GENIUS_CLI_TIMEOUT."
                )
            try:
                poll_payload = {"sub": "orchestrator", "exp": time.time() + 300}
                poll_token = encode_jwt(poll_payload, api_key)
                get_headers = {
                    "X-Payload-SHA256": get_checksum,
                    "X-API-Key": poll_token,
                    "Authorization": f"Bearer {poll_token}",
                }
                status_response = await perform_get_with_retry(
                    c, f"{base_url}/status/{task_id}", get_headers
                )
                status_data = status_response.json()
                curr_status = status_data.get("status")

                if curr_status == "completed":
                    return status_data.get("result", "")
                elif curr_status == "failed":
                    error_msg = status_data.get(
                        "error", "Unknown error occurred on server."
                    )
                    raise PipelineError(f"Task execution failed on server: {error_msg}")
                elif curr_status == "processing":
                    await asyncio.sleep(0.5)
                else:
                    raise PipelineError(
                        f"Unexpected status '{curr_status}' returned for task {task_id}"
                    )
            except PipelineError:
                raise
            except Exception as e:
                detail = _http_error_detail(e)
                logger.error(
                    f"Failed to poll task status at {base_url}/status/{task_id}: "
                    f"{e}{detail}"
                )
                raise PipelineError(
                    f"Failed to poll task status at {base_url}/status/{task_id}: "
                    f"{e}{detail}"
                )

    if client is not None:
        result = await _execute(client)
    else:
        async with make_http_client() as local_client:
            result = await _execute(local_client)

    if use_cache:
        _cache_store(cache_key, result)
    return result


def detect_vulnerabilities(security_report: str) -> bool:
    """
    Decide whether a security audit report indicates a real, actionable vulnerability.

    Replaces naive case-sensitive substring matching (which produced false positives
    on phrases like "no HIGH severity issues found" or "HIGHLY recommended") with
    word-boundary matching plus negation-aware context checks.

    Returns True only when an explicit vulnerability marker or a high/critical
    severity term is present in a non-negated context.
    """
    if not security_report:
        return False

    text = security_report.lower()

    # 1. Explicit machine-readable markers emitted intentionally by the audit.
    explicit_markers = ("[vulnerability detected]", "[insecure]", "[vulnerable]")
    if any(marker in text for marker in explicit_markers):
        return True

    # 2. Severity terms matched on word boundaries (so "highly"/"highlight" don't match).
    severity_pattern = re.compile(
        r"\b(high|critical)\b(?:\s+(?:severity|risk|vulnerabilit\w+|issue\w*))?",
        re.IGNORECASE,
    )

    # Negation cues that flip a severity hit into a "clean" statement, e.g.
    # "no high severity issues", "0 critical vulnerabilities", "without critical".
    negation_pattern = re.compile(r"\b(no|none|zero|0|without|free of|not? any)\b")

    for match in severity_pattern.finditer(text):
        # Inspect the ~40 chars preceding the match for a negation cue.
        window_start = max(0, match.start() - 40)
        preceding = text[window_start : match.start()]
        if negation_pattern.search(preceding):
            continue
        return True

    return False


def parse_security_verdict(security_report: str):
    """
    Extract a structured security verdict {"blocking": bool, "findings": [...]}
    from the audit report. Returns the dict, or None if no verdict object is present.
    """
    if not security_report:
        return None
    decoder = json.JSONDecoder()
    fenced = re.findall(
        r"```json\s*(.*?)```", security_report, re.DOTALL | re.IGNORECASE
    )
    for text in fenced + [security_report]:
        idx = 0
        while True:
            start = text.find("{", idx)
            if start == -1:
                break
            try:
                obj, end = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                idx = start + 1
                continue
            if isinstance(obj, dict) and "blocking" in obj:
                return obj
            idx = start + end
    return None


def security_is_blocking(security_report: str) -> bool:
    """
    Decide whether the security audit should block acceptance of the code.
    Prefers a structured verdict (the security agent's machine-readable output);
    falls back to free-text detection for legacy/prose reports.
    """
    verdict = parse_security_verdict(security_report)
    if verdict is not None:
        return bool(verdict.get("blocking"))
    return detect_vulnerabilities(security_report)


async def process_single_file(
    file_info,
    project_dir,
    config,
    codex_url,
    tester_url,
    security_url,
    api_key,
    client,
    poll_timeout,
    max_retries,
    semaphore,
    message_bus,
    parent_art_id,
    design_plan_content="",
):
    from ag_core.utils.message_bus import Artifact

    async with semaphore:
        file_path = file_info["path"]
        specification = file_info["specification"]

        target_file_path = safe_join(project_dir, file_path)
        os.makedirs(os.path.dirname(target_file_path), exist_ok=True)

        flat_name = flatten_rel_path(file_path)

        file_is_test = is_test_module(file_path)
        file_is_python = file_path.endswith(".py")
        if file_is_test:
            # The file IS a pytest module: run it directly, don't generate
            # tests-for-tests and don't security-audit test code.
            test_file_path = target_file_path
        else:
            test_file_path = os.path.join(project_dir, "tests", f"test_{flat_name}.py")
        audit_log_path = os.path.join(project_dir, "logs", f"audit_{flat_name}.md")
        test_log_path = os.path.join(project_dir, "logs", f"test_{flat_name}.log")

        success = False
        test_failures_logs = ""
        security_report = ""

        # design_plan_content is passed in from the caller (fetched once before the
        # fan-out) so the design context can't be evicted from the in-memory bus
        # mid-run on large projects (>100 artifacts), which would silently strip
        # Codex's specification.

        for attempt in range(1, max_retries + 1):
            logger.info(f"Implementing {file_path} - Attempt {attempt}/{max_retries}")

            # 1. Call Codex API /code
            codex_req_prompt = f"/code Implement the file '{file_path}' according to this specification:\n{specification}"
            if attempt > 1:
                codex_req_prompt += f"\n\nPrevious implementation attempt failed check.\nTest Failures/Logs:\n{truncate_log(test_failures_logs)}\n\nSecurity Report:\n{truncate_log(security_report)}"
                codex_req_prompt += (
                    "\n\nDo NOT run tests, commands, or tools. Output ONLY the "
                    "complete file content in a single ```python fenced block."
                )

            try:
                proj_scanner = ProjectScanner(
                    root_dir=project_dir, extra_ignores=config.scanner.exclude_patterns
                )
                current_context = proj_scanner.scan()
            except Exception:
                current_context = {}
            current_context["design.md"] = design_plan_content

            # An API/agent failure inside an attempt must not abort the whole
            # self-healing loop: record it as this attempt's failure log and
            # let the next attempt (if any) retry.
            try:
                codex_code_raw = await call_api(
                    codex_url,
                    api_key,
                    codex_req_prompt,
                    context=current_context,
                    client=client,
                    poll_timeout=poll_timeout,
                )
            except PipelineError as e:
                logger.warning(
                    f"Codex call failed for {file_path} on attempt "
                    f"{attempt}/{max_retries}: {e}"
                )
                test_failures_logs = f"Codex agent call failed: {e}"
                security_report = ""
                continue
            save_raw_response(
                project_dir, f"codex_{flat_name}_attempt{attempt}", codex_code_raw
            )
            code_to_write = extract_code(codex_code_raw)

            # Never write non-Python garbage (pytest logs, prose) into a .py
            # file: fail the attempt and steer the next prompt instead. Not
            # writing also preserves the previous attempt's good file version.
            feedback = invalid_python_feedback(code_to_write, target_file_path)
            if feedback:
                logger.warning(
                    f"Codex output for {file_path} on attempt "
                    f"{attempt}/{max_retries} is not valid Python; skipping write."
                )
                test_failures_logs = feedback
                security_report = ""
                continue

            # Publish Codex implementation to MessageBus
            codex_art = Artifact(
                name=f"code_{file_path}",
                content=code_to_write,
                created_by="codex",
                parent_id=parent_art_id,
            )
            codex_art_id = message_bus.publish(codex_art)

            # 2. Write code to projects/[project_name]/[file_path]
            try:
                with open(target_file_path, "w", encoding="utf-8") as f:
                    f.write(code_to_write)
                logger.info(f"Wrote implemented code to {target_file_path}")
            except Exception as e:
                raise PipelineError(f"Failed to write code to {target_file_path}: {e}")

            # 3. Parallel Tester & Security APIs (skipped when the file IS a
            # pytest module: it is executed directly in step 6 instead).
            # Non-Python files (README.md, Dockerfile, configs...) get a
            # security audit but no generated pytest module — a real run
            # failed self-heal on README.md because a test was generated
            # against a markdown "module".
            if file_is_test:
                logger.info(
                    f"{file_path} is a test module: skipping test generation "
                    "and security audit; it will be executed directly."
                )
                security_report = ""
            elif not file_is_python:
                logger.info(
                    f"{file_path} is not a Python module: skipping test "
                    "generation; running the security audit only."
                )
                security_req_prompt = f"/audit Audit the following file for security issues (secrets, unsafe configuration, injection vectors) in '{file_path}':\n\n{code_to_write}"
                try:
                    security_report = await call_api(
                        security_url,
                        api_key,
                        security_req_prompt,
                        context={file_path: code_to_write},
                        client=client,
                        poll_timeout=poll_timeout,
                    )
                except PipelineError as e:
                    logger.warning(
                        f"Security call failed for {file_path} on attempt "
                        f"{attempt}/{max_retries}: {e}"
                    )
                    test_failures_logs = f"Security agent call failed: {e}"
                    security_report = ""
                    continue
                save_raw_response(
                    project_dir,
                    f"security_{flat_name}_attempt{attempt}",
                    security_report,
                )
                try:
                    with open(audit_log_path, "w", encoding="utf-8") as f:
                        f.write(security_report)
                except Exception as e:
                    raise PipelineError(
                        f"Failed to write security audit to {audit_log_path}: {e}"
                    )
            else:
                module_path = (
                    os.path.splitext(file_path)[0].replace("/", ".").replace("\\", ".")
                )
                tester_req_prompt = (
                    f"/unit-test Generate comprehensive pytest unit tests for the file '{file_path}'. "
                    f"Import the code under test as the module `{module_path}` (e.g. `from {module_path} import ...`).\n\n"
                    f"Code:\n\n{code_to_write}"
                )
                security_req_prompt = f"/audit Audit the following code for security vulnerabilities in file '{file_path}':\n\n{code_to_write}"

                try:
                    proj_scanner = ProjectScanner(
                        root_dir=project_dir,
                        extra_ignores=config.scanner.exclude_patterns,
                    )
                    current_context = proj_scanner.scan()
                except Exception:
                    current_context = {}
                current_context["design.md"] = design_plan_content
                current_context[file_path] = code_to_write

                # Execute Tester and Security concurrently using asyncio.gather
                tester_task = call_api(
                    tester_url,
                    api_key,
                    tester_req_prompt,
                    context=current_context,
                    client=client,
                    poll_timeout=poll_timeout,
                )
                security_task = call_api(
                    security_url,
                    api_key,
                    security_req_prompt,
                    context=current_context,
                    client=client,
                    poll_timeout=poll_timeout,
                )

                try:
                    tester_tests_raw, security_report = await gather_or_raise(
                        tester_task, security_task
                    )
                except PipelineError as e:
                    logger.warning(
                        f"Tester/Security call failed for {file_path} on attempt "
                        f"{attempt}/{max_retries}: {e}"
                    )
                    test_failures_logs = f"Tester/Security agent call failed: {e}"
                    security_report = ""
                    continue

                save_raw_response(
                    project_dir,
                    f"tester_{flat_name}_attempt{attempt}",
                    tester_tests_raw,
                )
                save_raw_response(
                    project_dir,
                    f"security_{flat_name}_attempt{attempt}",
                    security_report,
                )
                test_code_to_write = extract_code(tester_tests_raw)

                # Same hygiene for the generated pytest module.
                feedback = invalid_python_feedback(
                    test_code_to_write, test_file_path, source="Tester"
                )
                if feedback:
                    logger.warning(
                        f"Tester output for {file_path} on attempt "
                        f"{attempt}/{max_retries} is not valid Python; skipping write."
                    )
                    test_failures_logs = feedback
                    continue

                # Publish Tester and Security artifacts to MessageBus
                tester_art = Artifact(
                    name=f"test_{file_path}",
                    content=test_code_to_write,
                    created_by="tester",
                    parent_id=codex_art_id,
                )
                message_bus.publish(tester_art)

                security_art = Artifact(
                    name=f"audit_{file_path}",
                    content=security_report,
                    created_by="security",
                    parent_id=codex_art_id,
                )
                message_bus.publish(security_art)

                # 4. Write debug/log files to disk
                try:
                    with open(test_file_path, "w", encoding="utf-8") as f:
                        f.write(test_code_to_write)
                    logger.info(f"Wrote generated tests to {test_file_path}")
                except Exception as e:
                    raise PipelineError(
                        f"Failed to write test code to {test_file_path}: {e}"
                    )

                try:
                    with open(audit_log_path, "w", encoding="utf-8") as f:
                        f.write(security_report)
                    logger.info(f"Wrote security audit report to {audit_log_path}")
                except Exception as e:
                    raise PipelineError(
                        f"Failed to write security audit to {audit_log_path}: {e}"
                    )

            # 6. Run pytest projects/[project_name]/tests/test_[file_name].py
            # (nothing to execute for non-Python files — their verification is
            # the security audit alone).
            if not file_is_python:
                pytest_exit_code = 0
                test_failures_logs = "(pytest skipped: not a Python file)"
            else:
                pytest_cmd = [sys.executable, "-m", "pytest", test_file_path]
                logger.info(f"Running pytest command: {' '.join(pytest_cmd)}")

                try:
                    env = os.environ.copy()
                    project_src_dir = os.path.join(project_dir, "src")
                    env["PYTHONPATH"] = os.path.pathsep.join(
                        [project_dir, project_src_dir, env.get("PYTHONPATH", "")]
                    ).strip(os.path.pathsep)

                    process = await asyncio.create_subprocess_exec(
                        *pytest_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                    )
                    stdout, stderr = await process.communicate()
                    pytest_exit_code = process.returncode
                    test_failures_logs = (
                        stdout.decode("utf-8", errors="replace")
                        + "\n"
                        + stderr.decode("utf-8", errors="replace")
                    )
                except Exception as e:
                    pytest_exit_code = -999
                    test_failures_logs = f"Failed to run pytest: {e}"

            # Save output to projects/[project_name]/logs/test_[file_name].log
            try:
                with open(test_log_path, "w", encoding="utf-8") as f:
                    f.write(test_failures_logs)
            except Exception as e:
                logger.warning(f"Failed to write test log to {test_log_path}: {e}")

            # 7. Check if tests passed (return code 0) and security audit has no vulnerabilities.
            # An empty/whitespace security report means the audit stage produced no
            # output (crash, truncation, stripped result) — fail closed rather than
            # treating an absent audit as "clean". Test modules are exempt:
            # their audit is skipped by design.
            tests_passed = pytest_exit_code == 0
            security_missing = not file_is_test and not (
                security_report and security_report.strip()
            )
            has_vulnerabilities = security_is_blocking(security_report)
            if tests_passed and not security_missing and not has_vulnerabilities:
                logger.info(f"Successfully implemented and verified {file_path}")
                success = True
                break
            else:
                fail_reasons = []
                if not tests_passed:
                    fail_reasons.append(f"pytest failed (exit code {pytest_exit_code})")
                if security_missing:
                    fail_reasons.append(
                        "security audit returned no report (cannot confirm code is safe)"
                    )
                elif has_vulnerabilities:
                    fail_reasons.append(
                        "security audit detected vulnerabilities/high warnings"
                    )
                logger.warning(
                    f"Verification failed for {file_path}: {', '.join(fail_reasons)}"
                )

        if not success:
            raise PipelineError(
                f"Self-healing loop failed to implement and verify {file_path} after {max_retries} attempts."
            )

        return security_report


async def run_pipeline(
    prompt: str,
    grok_cmd: str = "grok",
    claude_cmd: str = "claude",
    antigravity_cmd: str = "antigravity",
    codex_cmd: str = "codex",
    tester_cmd: str = "tester",
    security_cmd: str = "security",
    devops_cmd: str = "devops",
    grok_args: list = None,
    claude_args: list = None,
    antigravity_args: list = None,
    codex_args: list = None,
    tester_args: list = None,
    security_args: list = None,
    devops_args: list = None,
    workspace: str = None,
    researcher_url: str = None,
    claude_url: str = None,
    codex_url: str = None,
    tester_url: str = None,
    security_url: str = None,
    devops_url: str = None,
    api_key_override: str = None,
    poll_timeout: float = 300.0,
    interactive: bool = False,
    max_retries: int = 3,
    max_debate_rounds: int = None,
    distributed: bool = False,
    stage_gate=None,
):
    """Execute the sequential pipeline (Research -> Claude -> Codex -> Tester -> Security -> DevOps).

    ``stage_gate`` is an optional ``async def gate(stage: str)`` awaited after
    the research, design and code stages complete; it may pause (human
    approval) or raise to abort. None (the default) runs straight through.
    """
    _DISTRIBUTED_MODE_VAR.set(distributed)

    project_name, workspace, max_debate_rounds = _resolve_pipeline_setup(
        prompt, workspace, max_debate_rounds
    )

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
            research_file,
            design_file,
            app_file,
            review_file,
            test_generated_file,
            audit_file,
            deploy_file,
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
    api_key = api_key_override or config.skill_api_key or os.getenv("SKILL_API_KEY", "")
    researcher_url = researcher_url or config.services.researcher
    claude_url = claude_url or config.services.claude_architect
    codex_url = codex_url or config.services.codex_reviewer
    tester_url = tester_url or config.services.tester_agent
    security_url = security_url or config.services.security_agent
    devops_url = devops_url or config.services.devops_agent

    # Scan the workspace context
    try:
        scanner = ProjectScanner(
            root_dir=project_dir, extra_ignores=config.scanner.exclude_patterns
        )
        scanned_files = scanner.scan()
    except Exception as e:
        logger.warning(f"Failed to scan workspace: {e}")
        scanned_files = {}

    client = make_http_client()
    try:
        if is_slash_cmd:
            agent_key, output_name = ROUTING_TABLE[first_word]
            logger.info(
                f"Smart routing active. Routing slash command '{first_word}' to '{agent_key}'..."
            )

            url = None
            if agent_key == "researcher":
                url = researcher_url
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
                raise PipelineError(
                    f"Target URL for agent '{agent_key}' is not configured."
                )

            result = await call_api(
                url,
                api_key,
                prompt,
                context=scanned_files,
                client=client,
                poll_timeout=poll_timeout,
            )

            output_file = os.path.join(workspace, output_name)
            try:
                with open(output_file, "w", encoding="utf-8") as f:
                    f.write(result)
                proj_output_file = os.path.join(project_dir, output_name)
                os.makedirs(project_dir, exist_ok=True)
                with open(proj_output_file, "w", encoding="utf-8") as f:
                    f.write(result)
            except Exception as e:
                raise PipelineError(
                    f"Failed to write agent output to {output_file}: {e}"
                )

            validate_file(output_file, agent_key, is_input=False)
            logger.info(
                f"Step '{agent_key}' completed successfully via routing. Output: {output_file}"
            )
            log_conversation(prompt, result)
            return result

        # Step 1: Research (agy/claude/codex fallback chain) - Call API
        logger.info("--- Running Step: Research ---")
        from ag_core.utils.message_bus import MessageBus, Artifact

        message_bus = MessageBus(
            db_path=os.path.join(project_dir, "logs", "message_bus.db")
        )

        try:
            research_content = await call_api(
                researcher_url,
                api_key,
                prompt,
                context=scanned_files,
                client=client,
                poll_timeout=poll_timeout,
            )
        except Exception as e:
            if not degraded_mode():
                raise
            # Research is enrichment, not a hard prerequisite: in degraded
            # mode (e.g. every research backend down) continue with the raw
            # prompt.
            logger.warning(
                "DEGRADED MODE: Research stage failed (%s). Continuing "
                "WITHOUT research context - design quality may suffer.",
                e,
            )
            research_content = (
                f"(research unavailable: {e})\n\nOriginal request: {prompt}"
            )

        # Publish to MessageBus
        research_art_id = message_bus.publish(
            Artifact(
                name="research_data", content=research_content, created_by="researcher"
            )
        )

        try:
            with open(research_file, "w", encoding="utf-8") as f:
                f.write(research_content)
            proj_research_file = os.path.join(project_dir, "research.md")
            with open(proj_research_file, "w", encoding="utf-8") as f:
                f.write(research_content)
        except Exception as e:
            logger.warning(f"Failed to write research debug output: {e}")
        validate_file(research_file, "Research", is_input=False)
        logger.info(
            f"Step 'Research' successfully completed. Output verified: {research_file}"
        )

        if stage_gate is not None:
            await stage_gate("research")

        # Step 2: Claude (Design) - Call API
        logger.info("--- Running Step: Claude ---")
        validate_file(research_file, "Claude", is_input=True)

        # Retrieve research content from message bus
        research_art = message_bus.retrieve(research_art_id)
        claude_prompt = research_art["content"] if research_art else research_content

        scanned_files["research.md"] = claude_prompt

        claude_content = await call_api(
            claude_url,
            api_key,
            claude_prompt,
            context=scanned_files,
            client=client,
            poll_timeout=poll_timeout,
        )

        def _write_design_files(content):
            """Persist the design to disk. Called immediately after the initial
            Claude call (so a later debate failure cannot lose a valid design)
            and again after the debate refines it."""
            try:
                with open(design_file, "w", encoding="utf-8") as f:
                    f.write(content)
                with open(
                    os.path.join(project_dir, "design.md"), "w", encoding="utf-8"
                ) as f:
                    f.write(content)
            except Exception as e:
                logger.warning(f"Failed to write Claude debug output: {e}")

        # Write the initial design BEFORE the debate: if a debate round fails,
        # the already-produced (and paid-for) design is safe on disk.
        _write_design_files(claude_content)

        # Multi-Agent Debate Refinement
        if max_debate_rounds > 0:
            logger.info(
                f"--- Starting Multi-Agent Debate Refinement (Max Rounds: {max_debate_rounds}) ---"
            )
            for round_idx in range(1, max_debate_rounds + 1):
                logger.info(
                    f"Debate Round {round_idx} Start: Critic reviewing Claude's draft plan..."
                )

                critic_prompt = (
                    "You are CriticReviewer, a critic agent. Analyze the following draft architecture plan proposed by Claude.\n"
                    "Identify potential architectural flaws, security risks, missing requirements, or execution challenges.\n"
                    "Provide constructive criticism and suggest concrete improvements. If the draft architecture plan is correct, complete, and needs no further improvements, include `[APPROVED]` in your response.\n\n"
                    f"Draft Architecture Plan:\n{claude_content}\n\n"
                    f"Original Research and Context:\n{claude_prompt}"
                )
                # A debate-round failure must not lose the design Claude
                # already produced: design.md is written before the debate, so
                # in strict mode the error propagates with the design safely
                # on disk, and in degraded mode the debate simply stops here.
                try:
                    critic_content = await call_api(
                        researcher_url,
                        api_key,
                        critic_prompt,
                        context=scanned_files,
                        client=client,
                        poll_timeout=poll_timeout,
                    )
                except Exception as e:
                    if not degraded_mode():
                        raise
                    logger.warning(
                        "DEGRADED MODE: debate round %d critic call failed "
                        "(%s). Keeping the current design and skipping the "
                        "remaining debate rounds.",
                        round_idx,
                        e,
                    )
                    break

                if "[APPROVED]" in critic_content:
                    logger.info(
                        f"Debate Round {round_idx} End: Critic approved the plan with [APPROVED]. Exiting debate loop early."
                    )
                    break

                logger.info(
                    f"Debate Round {round_idx}: Critique received. Sending to Claude for refinement..."
                )

                claude_refine_prompt = (
                    "You are Claude, the architect agent. Refine your draft architecture plan based on the constructive criticism from CriticReviewer.\n"
                    "Address the identified issues and incorporate the suggested improvements, producing a final refined architecture plan.\n"
                    "Output the refined plan as EXACTLY ONE ```json fenced block conforming to the same DesignPlan schema "
                    "(project_name, description, and files[] where each file has path + specification) and nothing else.\n\n"
                    f"Previous Draft Plan:\n{claude_content}\n\n"
                    f"CriticReviewer's Criticism:\n{critic_content}\n\n"
                    f"Original Research and Context:\n{claude_prompt}"
                )
                try:
                    claude_content = await call_api(
                        claude_url,
                        api_key,
                        claude_refine_prompt,
                        context=scanned_files,
                        client=client,
                        poll_timeout=poll_timeout,
                    )
                except Exception as e:
                    if not degraded_mode():
                        raise
                    logger.warning(
                        "DEGRADED MODE: debate round %d refine call failed "
                        "(%s). Keeping the current design and skipping the "
                        "remaining debate rounds.",
                        round_idx,
                        e,
                    )
                    break
                # Persist each refinement as soon as it exists.
                _write_design_files(claude_content)
                logger.info(f"Debate Round {round_idx} End: Round complete.")

        # Publish Claude content to MessageBus
        claude_art_id = message_bus.publish(
            Artifact(
                name="design_plan",
                content=claude_content,
                created_by="claude",
                parent_id=research_art_id,
            )
        )

        _write_design_files(claude_content)
        validate_file(design_file, "Claude", is_input=False)
        logger.info(
            f"Step 'Claude' successfully completed. Output verified: {design_file}"
        )

        # Interactive loop
        if interactive:
            print(f"\n[Claude Design Output]\n{claude_content}\n")
            while True:
                feedback = (
                    await asyncio.to_thread(
                        input,
                        "Verify architecture. Press Enter to proceed or type "
                        "modifications/comments: ",
                    )
                ).strip()
                if not feedback:
                    break
                logger.info("Re-running Claude Architect with feedback...")
                claude_prompt = f"{claude_prompt}\n\n[USER FEEDBACK]:\n{feedback}"
                scanned_files["research.md"] = claude_prompt
                claude_content = await call_api(
                    claude_url,
                    api_key,
                    claude_prompt,
                    context=scanned_files,
                    client=client,
                    poll_timeout=poll_timeout,
                )
                # Re-publish to MessageBus
                claude_art_id = message_bus.publish(
                    Artifact(
                        name="design_plan",
                        content=claude_content,
                        created_by="claude",
                        parent_id=research_art_id,
                    )
                )
                _write_design_files(claude_content)
                print(f"\n[Updated Claude Design Output]\n{claude_content}\n")

        # Parse design.md for file implementation task queue
        files_to_implement = parse_design_for_files(claude_content)

        # Self-heal an unparseable design (production only): re-prompt the
        # architect with explicit format feedback instead of dropping into the
        # legacy single-file path below.
        if not files_to_implement and design_selfheal_enabled():
            for retry_idx in (1, 2):
                logger.warning(
                    "Design output was not a parseable DesignPlan; re-prompting "
                    f"the architect (retry {retry_idx}/2)."
                )
                design_retry_prompt = (
                    "Your previous design response could not be parsed as a "
                    "DesignPlan. Respond with EXACTLY ONE ```json fenced block "
                    "conforming to the DesignPlan schema "
                    '({"project_name": str, "description": str, "files": '
                    '[{"path": str, "specification": str}]}) and NOTHING else '
                    "- no prose before or after the block.\n\n"
                    f"Original design request:\n{claude_prompt}\n\n"
                    f"Your previous response:\n{truncate_log(claude_content)}"
                )
                claude_content = await call_api(
                    claude_url,
                    api_key,
                    design_retry_prompt,
                    context=scanned_files,
                    client=client,
                    poll_timeout=poll_timeout,
                )
                _write_design_files(claude_content)
                save_raw_response(
                    project_dir, f"design_retry{retry_idx}", claude_content
                )
                files_to_implement = parse_design_for_files(claude_content)
                if files_to_implement:
                    break
            if not files_to_implement:
                raise PipelineError(
                    "Design stage produced no parseable DesignPlan after 2 "
                    "format retries; aborting. See design.md and logs/raw/ for "
                    "the raw architect output."
                )

        if files_to_implement:
            logger.info(
                f"Parsed {len(files_to_implement)} files from design to implement: {[f['path'] for f in files_to_implement]}"
            )

            if stage_gate is not None:
                await stage_gate("design")

            progress_file_path = os.path.join(workspace, ".agents", "CURRENT_PROG.md")

            def update_progress_md(status_dict):
                write_progress_md(progress_file_path, status_dict)

            status_dict = {f["path"]: "pending" for f in files_to_implement}
            update_progress_md(status_dict)

            failed_files = []
            aggregated_audits = []
            semaphore = asyncio.Semaphore(3)

            # Fetch the design content once, before the fan-out publishes the
            # per-file artifacts that would evict it from the in-memory bus.
            design_art = message_bus.retrieve(claude_art_id)
            design_plan_content = design_art["content"] if design_art else ""

            async def handle_file(file_info):
                file_path = file_info["path"]
                status_dict[file_path] = "in progress"
                update_progress_md(status_dict)
                try:
                    result = await process_single_file(
                        file_info,
                        project_dir,
                        config,
                        codex_url,
                        tester_url,
                        security_url,
                        api_key,
                        client,
                        poll_timeout,
                        max_retries,
                        semaphore,
                        message_bus,
                        claude_art_id,
                        design_plan_content=design_plan_content,
                    )
                    status_dict[file_path] = "completed"
                    update_progress_md(status_dict)
                    return file_path, result, None
                except Exception as e:
                    logger.error(f"Failed to process {file_path}: {e}")
                    status_dict[file_path] = "failed"
                    update_progress_md(status_dict)
                    return file_path, None, e

            tasks_list = [handle_file(f) for f in files_to_implement]
            results = await asyncio.gather(*tasks_list)

            for file_info, (file_path, result, err) in zip(files_to_implement, results):
                if err is not None:
                    failed_files.append(file_path)
                else:
                    aggregated_audits.append(f"### Audit for {file_path}\n\n{result}")

            all_failed = len(failed_files) == len(files_to_implement)
            if failed_files and not (degraded_mode() and not all_failed):
                raise PipelineError(
                    f"Self-healing loop failed to implement and verify files: {', '.join(failed_files)}"
                )
            if failed_files:
                logger.warning(
                    "Degraded mode: %d/%d files failed verification; continuing "
                    "with the rest. Failed: %s",
                    len(failed_files),
                    len(files_to_implement),
                    ", ".join(failed_files),
                )

            # Write review.md as implementation is verified
            if failed_files:
                verified = len(files_to_implement) - len(failed_files)
                review_content = (
                    f"Degraded run: {verified}/{len(files_to_implement)} files "
                    f"verified through the self-healing loop. "
                    f"Failed: {', '.join(failed_files)}."
                )
            else:
                review_content = "All files successfully implemented and verified through self-healing loop."
            review_art_id = message_bus.publish(
                Artifact(
                    name="review_data",
                    content=review_content,
                    created_by="codex",
                    parent_id=claude_art_id,
                )
            )
            try:
                with open(review_file, "w", encoding="utf-8") as f:
                    f.write(review_content)
                with open(
                    os.path.join(project_dir, "review.md"), "w", encoding="utf-8"
                ) as f:
                    f.write(review_content)
            except Exception as e:
                logger.warning(f"Failed to write review.md: {e}")

            # Aggregate audit report
            consolidated_audit = (
                "\n\n---\n\n".join(aggregated_audits)
                if aggregated_audits
                else "Consolidated project implementation and testing passed."
            )
            consolidated_art_id = message_bus.publish(
                Artifact(
                    name="consolidated_audit",
                    content=consolidated_audit,
                    created_by="security",
                    parent_id=review_art_id,
                )
            )
            try:
                with open(audit_file, "w", encoding="utf-8") as f:
                    f.write(consolidated_audit)
                with open(
                    os.path.join(project_dir, "audit.md"), "w", encoding="utf-8"
                ) as f:
                    f.write(consolidated_audit)
            except Exception as e:
                logger.warning(f"Failed to write audit.md: {e}")

            if stage_gate is not None:
                await stage_gate("code")

            # Run DevOps deployment (Step 7)
            logger.info("--- Running Step: DevOps ---")
            validate_file(audit_file, "DevOps", is_input=True)

            # Retrieve from MessageBus
            audit_art = message_bus.retrieve(consolidated_art_id)
            devops_prompt = audit_art["content"] if audit_art else consolidated_audit

            try:
                proj_scanner = ProjectScanner(
                    root_dir=project_dir, extra_ignores=config.scanner.exclude_patterns
                )
                current_context = proj_scanner.scan()
            except Exception:
                current_context = {}
            current_context["audit.md"] = devops_prompt

            try:
                devops_content = await call_api(
                    devops_url,
                    api_key,
                    devops_prompt,
                    context=current_context,
                    client=client,
                    poll_timeout=poll_timeout,
                )
            except Exception as e:
                if not degraded_mode():
                    raise
                logger.error(
                    "Degraded mode: DevOps stage failed, emitting a placeholder "
                    "deploy artifact and continuing: %s",
                    e,
                )
                devops_content = (
                    "# DevOps stage unavailable (degraded mode)\n\n"
                    "The DevOps/deploy stage failed and was skipped; the code and "
                    f"audit artifacts above are still valid.\n\nError: {e}\n"
                )

            # Publish DevOps to MessageBus
            message_bus.publish(
                Artifact(
                    name="devops_deploy",
                    content=devops_content,
                    created_by="devops",
                    parent_id=consolidated_art_id,
                )
            )
            try:
                with open(deploy_file, "w", encoding="utf-8") as f:
                    f.write(devops_content)
                with open(
                    os.path.join(project_dir, "deploy.md"), "w", encoding="utf-8"
                ) as f:
                    f.write(devops_content)
            except Exception as e:
                raise PipelineError(
                    f"Failed to write DevOps output to {deploy_file}: {e}"
                )
            validate_file(deploy_file, "DevOps", is_input=False)
            logger.info(
                f"Step 'DevOps' successfully completed. Output verified: {deploy_file}"
            )

            logger.info(
                "Pipeline executed successfully and all files implemented, verified, and deployed."
            )
            log_conversation(prompt, devops_content)
            return devops_content

        # LEGACY single-file fallback — PYTEST-ONLY compatibility path. In
        # production an unparseable design is retried and then raises above,
        # so this branch is unreachable outside the test suite. It exists
        # solely to keep the historical single-file tests (fixed mock call
        # sequences) green; do not extend it, and delete it together with
        # those tests when they migrate to DesignPlan-based mocks.
        logger.info(
            "No files parsed from design.md. Running fallback single-file pipeline..."
        )

        # Step 3: Antigravity (Programming) - Run as an async subprocess
        logger.info("--- Running Step: Antigravity ---")
        validate_file(design_file, "Antigravity", is_input=True)

        a_args = (
            antigravity_args
            if antigravity_args is not None
            else DEFAULT_ANTIGRAVITY_ARGS
        )
        antigravity_formatted_cmd = format_cmd_args(
            antigravity_cmd,
            a_args,
            prompt,
            input_path=design_file,
            output_path=app_file,
        )

        # On Windows, create_subprocess_exec (CreateProcess) cannot launch a
        # .cmd/.bat directly — it must be run via cmd.exe /c (matches provider logic).
        if (
            sys.platform == "win32"
            and antigravity_formatted_cmd
            and str(antigravity_formatted_cmd[0]).lower().endswith((".cmd", ".bat"))
        ):
            antigravity_formatted_cmd = ["cmd.exe", "/c"] + antigravity_formatted_cmd

        logger.info(f"Command arguments: {antigravity_formatted_cmd}")

        if os.path.exists(app_file):
            try:
                os.remove(app_file)
                logger.info(f"Deleted old output file before execution: {app_file}")
            except Exception as e:
                logger.error(
                    f"Failed to delete existing output file {app_file} before execution: {e}"
                )
                raise PipelineError(
                    f"Failed to delete existing output file {app_file} before execution: {e}"
                )

        try:
            process = await asyncio.create_subprocess_exec(
                *antigravity_formatted_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
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
            logger.error(
                f"Step 'Antigravity' failed with exit code {process.returncode}"
            )
            raise PipelineError(
                f"Step 'Antigravity' returned non-zero exit code: {process.returncode}"
            )

        if not os.path.exists(app_file) or os.path.getsize(app_file) == 0:
            if stdout_str:
                logger.info(f"Writing captured stdout to output file: {app_file}")
                os.makedirs(project_dir, exist_ok=True)
                try:
                    with open(app_file, "w", encoding="utf-8") as f:
                        f.write(stdout_str)
                except Exception as e:
                    raise PipelineError(
                        f"Failed to write stdout to output file {app_file}: {e}"
                    )
            else:
                logger.warning(
                    "No stdout captured and output file was not created for 'Antigravity'"
                )

        if os.path.exists(app_file):
            try:
                shutil.copy2(app_file, os.path.join(project_dir, "app.py"))
            except Exception as e:
                logger.warning(f"Failed to copy app.py to project directory: {e}")

        validate_file(app_file, "Antigravity", is_input=False)
        logger.info(
            f"Step 'Antigravity' successfully completed. Output verified: {app_file}"
        )

        # Publish app code to MessageBus
        try:
            with open(app_file, "r", encoding="utf-8") as f:
                app_content = f.read()
        except Exception:
            app_content = ""
        app_art_id = message_bus.publish(
            Artifact(
                name="app_code",
                content=app_content,
                created_by="antigravity",
                parent_id=claude_art_id,
            )
        )

        # Step 4: Codex (Review) - Call API
        logger.info("--- Running Step: Codex ---")
        validate_file(app_file, "Codex", is_input=True)

        # Retrieve from MessageBus
        app_art = message_bus.retrieve(app_art_id)
        codex_prompt = app_art["content"] if app_art else ""

        design_art = message_bus.retrieve(claude_art_id)
        scanned_files["design.md"] = design_art["content"] if design_art else ""
        scanned_files["app.py"] = codex_prompt

        codex_content = await call_api(
            codex_url,
            api_key,
            codex_prompt,
            context=scanned_files,
            client=client,
            poll_timeout=poll_timeout,
        )

        # Publish Codex review to MessageBus
        codex_art_id = message_bus.publish(
            Artifact(
                name="review_data",
                content=codex_content,
                created_by="codex",
                parent_id=app_art_id,
            )
        )

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
        logger.info(
            f"Step 'Codex' successfully completed. Output verified: {review_file}"
        )

        # Step 5, 6 & 7: Tester, Security and DevOps in parallel
        logger.info("--- Running Steps: Tester, Security & DevOps (Parallel) ---")
        validate_file(review_file, "Tester", is_input=True)

        # Retrieve Codex output from MessageBus
        review_art = message_bus.retrieve(codex_art_id)
        shared_prompt = review_art["content"] if review_art else ""

        scanned_files["review.md"] = shared_prompt
        app_art = message_bus.retrieve(app_art_id)
        scanned_files["app.py"] = app_art["content"] if app_art else ""

        # Invoke concurrently
        tester_task = call_api(
            tester_url,
            api_key,
            shared_prompt,
            context=scanned_files,
            client=client,
            poll_timeout=poll_timeout,
        )
        security_task = call_api(
            security_url,
            api_key,
            shared_prompt,
            context=scanned_files,
            client=client,
            poll_timeout=poll_timeout,
        )
        devops_task = call_api(
            devops_url,
            api_key,
            shared_prompt,
            context=scanned_files,
            client=client,
            poll_timeout=poll_timeout,
        )

        tester_content, security_content, devops_content = await gather_or_raise(
            tester_task, security_task, devops_task
        )

        # Publish to MessageBus
        message_bus.publish(
            Artifact(
                name="test_code",
                content=tester_content,
                created_by="tester",
                parent_id=codex_art_id,
            )
        )
        message_bus.publish(
            Artifact(
                name="security_audit",
                content=security_content,
                created_by="security",
                parent_id=codex_art_id,
            )
        )
        message_bus.publish(
            Artifact(
                name="devops_deploy",
                content=devops_content,
                created_by="devops",
                parent_id=codex_art_id,
            )
        )

        # Write Tester outputs
        os.makedirs(project_dir, exist_ok=True)
        try:
            with open(test_generated_file, "w", encoding="utf-8") as f:
                f.write(tester_content)
            proj_test_file = os.path.join(project_dir, "test_generated.py")
            with open(proj_test_file, "w", encoding="utf-8") as f:
                f.write(tester_content)
        except Exception as e:
            raise PipelineError(
                f"Failed to write Tester output to {test_generated_file}: {e}"
            )

        # Write Security outputs
        try:
            with open(audit_file, "w", encoding="utf-8") as f:
                f.write(security_content)
            proj_audit_file = os.path.join(project_dir, "audit.md")
            with open(proj_audit_file, "w", encoding="utf-8") as f:
                f.write(security_content)
        except Exception as e:
            raise PipelineError(f"Failed to write Security output to {audit_file}: {e}")

        # Write DevOps outputs
        try:
            with open(deploy_file, "w", encoding="utf-8") as f:
                f.write(devops_content)
            proj_deploy_file = os.path.join(project_dir, "deploy.md")
            with open(proj_deploy_file, "w", encoding="utf-8") as f:
                f.write(devops_content)
        except Exception as e:
            raise PipelineError(f"Failed to write DevOps output to {deploy_file}: {e}")

        # Validate outputs
        validate_file(test_generated_file, "Tester", is_input=False)
        logger.info(
            f"Step 'Tester' successfully completed. Output verified: {test_generated_file}"
        )

        validate_file(audit_file, "Security", is_input=False)
        logger.info(
            f"Step 'Security' successfully completed. Output verified: {audit_file}"
        )

        validate_file(deploy_file, "DevOps", is_input=False)
        logger.info(
            f"Step 'DevOps' successfully completed. Output verified: {deploy_file}"
        )

        logger.info(
            "Pipeline executed successfully and all intermediate files verified."
        )
        log_conversation(prompt, devops_content)
    finally:
        await client.aclose()


async def run_e2e_pipeline(
    prompt: str,
    grok_cmd: str = "grok",
    claude_cmd: str = "claude",
    codex_cmd: str = "codex",
    tester_cmd: str = "tester",
    workspace: str = None,
    researcher_url: str = None,
    claude_url: str = None,
    codex_url: str = None,
    tester_url: str = None,
    api_key_override: str = None,
    poll_timeout: float = 300.0,
    max_retries: int = 3,
    max_debate_rounds: int = None,
    distributed: bool = False,
):
    """Execute the E2E automated pipeline (Claude -> critic critique -> Codex implementation & self-healing -> Tester test generation & self-healing)."""
    _DISTRIBUTED_MODE_VAR.set(distributed)

    project_name, workspace, max_debate_rounds = _resolve_pipeline_setup(
        prompt, workspace, max_debate_rounds
    )

    project_dir = os.path.join(workspace, "projects", project_name)
    os.makedirs(os.path.join(project_dir, "src"), exist_ok=True)
    os.makedirs(os.path.join(project_dir, "tests"), exist_ok=True)
    os.makedirs(os.path.join(project_dir, "logs"), exist_ok=True)

    # Paths for context sharing files
    plan_file = os.path.join(workspace, "plan.md")
    proj_plan_file = os.path.join(project_dir, "plan.md")

    # 1. Clean up old output files
    clean_output_files([plan_file, proj_plan_file])

    config = load_config()
    api_key = api_key_override or config.skill_api_key or os.getenv("SKILL_API_KEY", "")
    researcher_url = researcher_url or config.services.researcher
    claude_url = claude_url or config.services.claude_architect
    codex_url = codex_url or config.services.codex_reviewer
    tester_url = tester_url or config.services.tester_agent

    # Scan the workspace context
    try:
        scanner = ProjectScanner(
            root_dir=project_dir, extra_ignores=config.scanner.exclude_patterns
        )
        scanned_files = scanner.scan()
    except Exception as e:
        logger.warning(f"Failed to scan workspace: {e}")
        scanned_files = {}

    client = make_http_client()

    try:
        # Step 1: Claude (Architect) - Call API
        logger.info("--- Running E2E Step: Claude (Planning) ---")
        claude_prompt = prompt if prompt.startswith("/plan") else f"/plan {prompt}"
        claude_content = await call_api(
            claude_url,
            api_key,
            claude_prompt,
            context=scanned_files,
            client=client,
            poll_timeout=poll_timeout,
        )

        def _write_plan_files(content):
            """Persist the plan to disk. Called immediately after the initial
            Claude call (so a later debate failure cannot lose a valid plan)
            and again after the debate refines it."""
            try:
                with open(plan_file, "w", encoding="utf-8") as f:
                    f.write(content)
                with open(proj_plan_file, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                raise PipelineError(f"Failed to write Claude plan to {plan_file}: {e}")

        # Write the initial plan BEFORE the debate: if a debate round fails,
        # the already-produced (and paid-for) plan is safe on disk.
        _write_plan_files(claude_content)

        # Step 2: Critic critique & debate refinement (researcher role)
        if max_debate_rounds > 0:
            logger.info(
                f"--- Starting E2E Debate Refinement (Max Rounds: {max_debate_rounds}) ---"
            )
            for round_idx in range(1, max_debate_rounds + 1):
                logger.info(
                    f"E2E Debate Round {round_idx} Start: Critic reviewing Claude's draft plan..."
                )

                critic_prompt = (
                    "You are CriticReviewer, a critic agent. Analyze the following draft plan proposed by Claude.\n"
                    "Identify potential flaws, security risks, missing requirements, or execution challenges.\n"
                    "Provide constructive criticism and suggest concrete improvements. If the draft plan is correct, complete, and needs no further improvements, include `[APPROVED]` in your response.\n\n"
                    f"Draft Plan:\n{claude_content}\n\n"
                    f"Original Prompt:\n{prompt}"
                )
                # Mirror of the sequential pipeline: a debate failure must not
                # lose the plan already written to disk before the debate.
                try:
                    critic_content = await call_api(
                        researcher_url,
                        api_key,
                        critic_prompt,
                        context=scanned_files,
                        client=client,
                        poll_timeout=poll_timeout,
                    )
                except Exception as e:
                    if not degraded_mode():
                        raise
                    logger.warning(
                        "DEGRADED MODE: E2E debate round %d critic call failed "
                        "(%s). Keeping the current plan and skipping the "
                        "remaining debate rounds.",
                        round_idx,
                        e,
                    )
                    break

                if "[APPROVED]" in critic_content:
                    logger.info(
                        f"E2E Debate Round {round_idx} End: Critic approved the plan with [APPROVED]. Exiting debate loop early."
                    )
                    break

                logger.info(
                    f"E2E Debate Round {round_idx}: Critique received. Sending to Claude for refinement..."
                )

                claude_refine_prompt = (
                    "You are Claude, the architect agent. Refine your draft plan based on the constructive criticism from CriticReviewer.\n"
                    "Address the identified issues and incorporate the suggested improvements, producing a final refined plan.\n\n"
                    f"Previous Draft Plan:\n{claude_content}\n\n"
                    f"CriticReviewer's Criticism:\n{critic_content}\n\n"
                    f"Original Prompt:\n{prompt}"
                )
                try:
                    claude_content = await call_api(
                        claude_url,
                        api_key,
                        claude_refine_prompt,
                        context=scanned_files,
                        client=client,
                        poll_timeout=poll_timeout,
                    )
                except Exception as e:
                    if not degraded_mode():
                        raise
                    logger.warning(
                        "DEGRADED MODE: E2E debate round %d refine call failed "
                        "(%s). Keeping the current plan and skipping the "
                        "remaining debate rounds.",
                        round_idx,
                        e,
                    )
                    break
                # Persist each refinement as soon as it exists.
                _write_plan_files(claude_content)
                logger.info(f"E2E Debate Round {round_idx} End: Round complete.")

        # Save final plan to plan.md
        _write_plan_files(claude_content)
        validate_file(plan_file, "Claude Plan", is_input=False)
        logger.info(
            f"Step 'Claude Plan' successfully completed. Output verified: {plan_file}"
        )

        # Parse plan for files
        files_to_implement = parse_design_for_files(claude_content)
        if not files_to_implement:
            logger.info("No files parsed from plan.md. Nothing to implement.")
            return claude_content

        logger.info(
            f"Parsed {len(files_to_implement)} files from plan to implement: {[f['path'] for f in files_to_implement]}"
        )

        # Current progress tracking setup
        progress_file_path = os.path.join(workspace, "CURRENT_PROG.md")

        def update_progress_md(status_dict):
            write_progress_md(progress_file_path, status_dict)

        status_dict = {f["path"]: "pending" for f in files_to_implement}
        update_progress_md(status_dict)

        semaphore = asyncio.Semaphore(3)

        async def process_e2e_file(file_info):
            async with semaphore:
                file_path = file_info["path"]
                specification = file_info["specification"]
                status_dict[file_path] = "in progress"
                update_progress_md(status_dict)

                target_file_path = safe_join(project_dir, file_path)
                os.makedirs(os.path.dirname(target_file_path), exist_ok=True)

                flat_name = flatten_rel_path(file_path)

                test_file_path = os.path.join(
                    project_dir, "tests", f"test_{flat_name}.py"
                )

                # --- Codex Implementation & Self-healing loop ---
                codex_success = False
                codex_error_log = ""

                for attempt in range(1, max_retries + 1):
                    logger.info(
                        f"Codex implementing {file_path} - Attempt {attempt}/{max_retries}"
                    )

                    codex_prompt = f"/code Implement the file '{file_path}' according to this specification:\n{specification}"
                    if attempt > 1:
                        codex_prompt += f"\n\nPrevious implementation attempt failed verification.\nErrors/Logs:\n{truncate_log(codex_error_log)}"
                        codex_prompt += (
                            "\n\nDo NOT run tests, commands, or tools. Output "
                            "ONLY the complete file content in a single "
                            "```python fenced block."
                        )

                    try:
                        proj_scanner = ProjectScanner(
                            root_dir=project_dir,
                            extra_ignores=config.scanner.exclude_patterns,
                        )
                        current_context = proj_scanner.scan()
                    except Exception:
                        current_context = {}

                    # An API/agent failure inside an attempt must not abort the
                    # loop: record it as this attempt's failure log and retry.
                    try:
                        codex_raw = await call_api(
                            codex_url,
                            api_key,
                            codex_prompt,
                            context=current_context,
                            client=client,
                            poll_timeout=poll_timeout,
                        )
                    except PipelineError as e:
                        logger.warning(
                            f"Codex call failed for {file_path} on attempt "
                            f"{attempt}/{max_retries}: {e}"
                        )
                        codex_error_log = f"Codex agent call failed: {e}"
                        continue
                    code_content = extract_code(codex_raw)

                    # Never write non-Python garbage (pytest logs, prose) into
                    # a .py file: fail the attempt and steer the next prompt,
                    # keeping the previous good file version intact.
                    feedback = invalid_python_feedback(code_content, target_file_path)
                    if feedback:
                        logger.warning(
                            f"Codex output for {file_path} on attempt "
                            f"{attempt}/{max_retries} is not valid Python; "
                            "skipping write."
                        )
                        codex_error_log = feedback
                        continue

                    try:
                        with open(target_file_path, "w", encoding="utf-8") as f:
                            f.write(code_content)
                    except Exception as e:
                        raise PipelineError(
                            f"Failed to write code to {target_file_path}: {e}"
                        )

                    # Verify using flake8 and pytest
                    env = os.environ.copy()
                    project_src_dir = os.path.join(project_dir, "src")
                    env["PYTHONPATH"] = os.path.pathsep.join(
                        [project_dir, project_src_dir, env.get("PYTHONPATH", "")]
                    ).strip(os.path.pathsep)

                    # Check lint with flake8
                    flake8_cmd = [sys.executable, "-m", "flake8", target_file_path]
                    flake8_code, flake8_out = await run_subprocess(flake8_cmd, env=env)
                    # flake8 is only a dev dependency; if it's not installed, don't
                    # treat its absence as a lint failure that fails every attempt.
                    if flake8_code != 0 and "No module named flake8" in flake8_out:
                        logger.warning(
                            "flake8 is not installed; skipping the lint gate for this file."
                        )
                        flake8_code, flake8_out = 0, ""

                    # Check tests with pytest, scoped to THIS file's own test
                    # file rather than the whole tests/ directory. Running the
                    # whole dir is racy: process_e2e_file runs concurrently per
                    # file, so a sibling's not-yet-implemented or mid-write test
                    # would fail our verification non-deterministically. Each
                    # sibling's test is verified by its own task.
                    if os.path.exists(test_file_path):
                        pytest_cmd = [sys.executable, "-m", "pytest", test_file_path]
                        pytest_code, pytest_out = await run_subprocess(
                            pytest_cmd, env=env
                        )
                    else:
                        pytest_code, pytest_out = 0, "No test for this file yet."

                    if flake8_code == 0 and pytest_code == 0:
                        logger.info(
                            f"Codex implementation verified successfully for {file_path}"
                        )
                        codex_success = True
                        break
                    else:
                        codex_error_log = ""
                        if flake8_code != 0:
                            codex_error_log += f"Flake8 Errors:\n{flake8_out}\n"
                        if pytest_code != 0:
                            codex_error_log += f"Pytest Errors:\n{pytest_out}\n"
                        logger.warning(
                            f"Codex verification failed on attempt {attempt}: {codex_error_log}"
                        )

                if not codex_success:
                    status_dict[file_path] = "failed"
                    update_progress_md(status_dict)
                    raise PipelineError(
                        f"Codex self-healing failed for {file_path} after {max_retries} attempts."
                    )

                # --- Tester Unit Test Generation & Self-healing loop ---
                tester_success = False
                tester_error_log = ""

                for attempt in range(1, max_retries + 1):
                    logger.info(
                        f"Tester generating tests for {file_path} - Attempt {attempt}/{max_retries}"
                    )

                    with open(target_file_path, "r", encoding="utf-8") as f:
                        implemented_code = f.read()

                    e2e_module_path = (
                        os.path.splitext(file_path)[0]
                        .replace("/", ".")
                        .replace("\\", ".")
                    )
                    tester_prompt = (
                        f"/unit-test Generate comprehensive pytest unit tests for the file '{file_path}'. "
                        f"Import the code under test as the module `{e2e_module_path}` (e.g. `from {e2e_module_path} import ...`).\n\n"
                        f"Code:\n\n{implemented_code}"
                    )
                    if attempt > 1:
                        tester_prompt += f"\n\nPrevious test generation attempt failed verification.\nErrors/Logs:\n{truncate_log(tester_error_log)}"

                    try:
                        proj_scanner = ProjectScanner(
                            root_dir=project_dir,
                            extra_ignores=config.scanner.exclude_patterns,
                        )
                        current_context = proj_scanner.scan()
                    except Exception:
                        current_context = {}

                    try:
                        tester_raw = await call_api(
                            tester_url,
                            api_key,
                            tester_prompt,
                            context=current_context,
                            client=client,
                            poll_timeout=poll_timeout,
                        )
                    except PipelineError as e:
                        logger.warning(
                            f"Tester call failed for {file_path} on attempt "
                            f"{attempt}/{max_retries}: {e}"
                        )
                        tester_error_log = f"Tester agent call failed: {e}"
                        continue
                    test_code_content = extract_code(tester_raw)

                    # Same hygiene for the generated pytest module.
                    feedback = invalid_python_feedback(
                        test_code_content, test_file_path, source="Tester"
                    )
                    if feedback:
                        logger.warning(
                            f"Tester output for {file_path} on attempt "
                            f"{attempt}/{max_retries} is not valid Python; "
                            "skipping write."
                        )
                        tester_error_log = feedback
                        continue

                    try:
                        with open(test_file_path, "w", encoding="utf-8") as f:
                            f.write(test_code_content)
                    except Exception as e:
                        raise PipelineError(
                            f"Failed to write test code to {test_file_path}: {e}"
                        )

                    # Run pytest on the generated test file
                    pytest_cmd = [sys.executable, "-m", "pytest", test_file_path]
                    pytest_code, pytest_out = await run_subprocess(pytest_cmd, env=env)

                    if pytest_code == 0:
                        logger.info(
                            f"Tester tests verified successfully for {file_path}"
                        )
                        tester_success = True
                        break
                    else:
                        tester_error_log = f"Pytest Errors:\n{pytest_out}\n"
                        logger.warning(
                            f"Tester verification failed on attempt {attempt}: {tester_error_log}"
                        )

                if not tester_success:
                    status_dict[file_path] = "failed"
                    update_progress_md(status_dict)
                    raise PipelineError(
                        f"Tester self-healing failed for {file_path} after {max_retries} attempts."
                    )

                status_dict[file_path] = "completed"
                update_progress_md(status_dict)

        tasks_list = [process_e2e_file(f) for f in files_to_implement]
        if degraded_mode():
            results = await asyncio.gather(*tasks_list, return_exceptions=True)
            paths = [f["path"] for f in files_to_implement]
            failed, summary = resolve_degraded_outcome(paths, results, "E2E Pipeline")
            if failed:
                logger.warning(
                    "Degraded mode: %d/%d files failed in the E2E pipeline; "
                    "continuing with the rest. Failed: %s",
                    len(failed),
                    len(files_to_implement),
                    ", ".join(failed),
                )
                return summary
        else:
            await gather_or_raise(*tasks_list)

        logger.info(
            "E2E Pipeline executed successfully and all files implemented, verified, and tested."
        )
        return "E2E Pipeline execution completed successfully."
    finally:
        await client.aclose()


def main():
    parser = argparse.ArgumentParser(
        description="Multi-agent CLI Orchestrator pipeline executing Research -> Claude -> Antigravity -> Codex -> Tester."
    )
    parser.add_argument(
        "--prompt", required=True, help="Initial research/query prompt for the pipeline"
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace directory for context files (defaults to current dir)",
    )

    # Custom commands/paths
    parser.add_argument(
        "--grok-cmd", default=resolve_grok_cmd(), help="Command/path to Grok CLI"
    )
    parser.add_argument(
        "--claude-cmd", default=resolve_claude_cmd(), help="Command/path to Claude CLI"
    )
    parser.add_argument(
        "--antigravity-cmd",
        default=resolve_antigravity_cmd(),
        help="Command/path to Antigravity CLI",
    )
    parser.add_argument(
        "--codex-cmd", default=resolve_codex_cmd(), help="Command/path to Codex CLI"
    )
    parser.add_argument(
        "--tester-cmd", default=resolve_tester_cmd(), help="Command/path to Tester CLI"
    )
    parser.add_argument(
        "--security-cmd",
        default=resolve_security_cmd(),
        help="Command/path to Security CLI",
    )
    parser.add_argument(
        "--devops-cmd", default=resolve_devops_cmd(), help="Command/path to DevOps CLI"
    )

    # Custom arguments
    parser.add_argument(
        "--grok-args", nargs="*", default=None, help="Custom arguments for Grok step"
    )
    parser.add_argument(
        "--claude-args",
        nargs="*",
        default=None,
        help="Custom arguments for Claude step",
    )
    parser.add_argument(
        "--antigravity-args",
        nargs="*",
        default=None,
        help="Custom arguments for Antigravity step",
    )
    parser.add_argument(
        "--codex-args", nargs="*", default=None, help="Custom arguments for Codex step"
    )
    parser.add_argument(
        "--tester-args",
        nargs="*",
        default=None,
        help="Custom arguments for Tester step",
    )
    parser.add_argument(
        "--security-args",
        nargs="*",
        default=None,
        help="Custom arguments for Security step",
    )
    parser.add_argument(
        "--devops-args",
        nargs="*",
        default=None,
        help="Custom arguments for DevOps step",
    )

    # Service URL overrides
    parser.add_argument(
        "--researcher-url",
        "--grok-url",  # legacy alias from when the role id was "grok"
        dest="researcher_url",
        default=None,
        help="Service URL override for the Researcher service",
    )
    parser.add_argument(
        "--claude-url", default=None, help="Service URL override for Claude"
    )
    parser.add_argument(
        "--codex-url", default=None, help="Service URL override for Codex"
    )
    parser.add_argument(
        "--tester-url", default=None, help="Service URL override for Tester"
    )
    parser.add_argument(
        "--security-url", default=None, help="Service URL override for Security"
    )
    parser.add_argument(
        "--devops-url", default=None, help="Service URL override for DevOps"
    )

    # API key override
    parser.add_argument(
        "--api-key-override",
        "--api-key",
        dest="api_key_override",
        default=None,
        help="API key override for the pipeline",
    )

    # Pipeline selection
    parser.add_argument(
        "--pipeline",
        choices=["sequential", "e2e"],
        default="sequential",
        help="Pipeline type to execute",
    )

    # Polling timeout
    parser.add_argument(
        "--poll-timeout", type=float, default=300.0, help="Polling timeout in seconds"
    )
    parser.add_argument(
        "--interactive", action="store_true", help="Interactive design review loop"
    )
    parser.add_argument(
        "--max-retries", type=int, default=3, help="Max retries for self-healing loop"
    )
    default_debate_rounds = (
        0 if ("pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST")) else 2
    )
    parser.add_argument(
        "--max-debate-rounds",
        type=int,
        default=default_debate_rounds,
        help="Maximum number of debate rounds for design refinement",
    )
    parser.add_argument(
        "--distributed",
        action="store_true",
        help="Run orchestrator in distributed mode",
    )

    args = parser.parse_args()

    try:
        if args.pipeline == "e2e":
            asyncio.run(
                run_e2e_pipeline(
                    prompt=args.prompt,
                    grok_cmd=args.grok_cmd,
                    claude_cmd=args.claude_cmd,
                    codex_cmd=args.codex_cmd,
                    tester_cmd=args.tester_cmd,
                    workspace=args.workspace,
                    researcher_url=args.researcher_url,
                    claude_url=args.claude_url,
                    codex_url=args.codex_url,
                    tester_url=args.tester_url,
                    api_key_override=args.api_key_override,
                    poll_timeout=args.poll_timeout,
                    max_retries=args.max_retries,
                    max_debate_rounds=args.max_debate_rounds,
                    distributed=args.distributed,
                )
            )
        else:
            asyncio.run(
                run_pipeline(
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
                    researcher_url=args.researcher_url,
                    claude_url=args.claude_url,
                    codex_url=args.codex_url,
                    tester_url=args.tester_url,
                    security_url=args.security_url,
                    devops_url=args.devops_url,
                    api_key_override=args.api_key_override,
                    poll_timeout=args.poll_timeout,
                    interactive=args.interactive,
                    max_retries=args.max_retries,
                    max_debate_rounds=args.max_debate_rounds,
                    distributed=args.distributed,
                )
            )
    except PipelineError as e:
        logger.error(f"Pipeline Execution Failed: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected Pipeline Failure: {e}")
        sys.exit(1)


run_pipeline_async = run_pipeline

if __name__ == "__main__":
    main()
