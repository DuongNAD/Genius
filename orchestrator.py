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
import time
import uuid
import email.utils
from datetime import timezone
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
)

# Re-exported so existing `from orchestrator import extract_code` callers (and
# tests) keep working after the helper moved to a shared module.
from ag_core.utils.code_extract import extract_code, fence_hint
from ag_core.utils.cli_runner import (
    cli_timeout,
    test_timeout,
    install_timeout,
    communicate_with_timeout,
    CLITimeoutError,
)
from ag_core.runtime import under_pytest
from ag_core.directives import parse_directives
from ag_core.utils.prompt_templates import (
    CRITIC_QUALITY_CHECKLIST as _CRITIC_QUALITY_CHECKLIST,
    HACKATHON_DESIGN_GUIDANCE,
    HACKATHON_DEVOPS_GUIDANCE,
    HACKATHON_PITCH_PROMPT,
    HACKATHON_RESEARCH_GUIDANCE,
)
from collections import OrderedDict


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
    if os.getenv("GENIUS_CLI_TIMEOUT") is None and under_pytest():
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


def is_pytest_infra(rel_path: str) -> bool:
    """True for pytest support files that must NOT get generated unit tests
    (``conftest.py``, ``__init__.py``).

    A real run generated a broken ``test_conftest.py`` (an ``importlib.reload``
    on a not-yet-imported module) that failed collection and dragged the
    self-heal loop through pointless retries. These files are still implemented
    and security-audited, but they never get a tests-for-infrastructure module.
    """
    return os.path.basename(rel_path.replace("\\", "/")) in (
        "conftest.py",
        "__init__.py",
    )


def save_raw_response(project_dir: str, name: str, content: str) -> None:
    """Persist a raw agent response under ``logs/raw/`` for debugging.

    Failures are non-fatal: raw capture must never break a run. Without this,
    diagnosing a rejected stage means re-driving the agents by hand.
    """
    try:
        raw_dir = os.path.join(pipeline_internal_dir(project_dir), "logs", "raw")
        os.makedirs(raw_dir, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.\-]+", "_", name)
        _write_text(os.path.join(raw_dir, f"{safe}.md"), content or "")
    except Exception as e:
        logger.warning(f"Failed to save raw response {name}: {e}")


def design_selfheal_enabled() -> bool:
    """Whether the design-format retry loop runs (production yes, pytest no).

    Under pytest the legacy single-file branch must stay reachable for the
    historical tests, whose fixed mock call sequences cannot absorb extra
    design calls — same convention as the debate-rounds default.
    """
    return not under_pytest()


def final_review_strict() -> bool:
    """Whether a BLOCKING custom final-review verdict fails the pipeline.

    Strict by default: a parsed ``{"blocking": true}`` verdict raises
    ``PipelineError`` after the Claude fix plan is recorded in review.md, so a
    job whose final review found real issues can never report ``completed``.
    ``GENIUS_FINAL_REVIEW_STRICT=0`` demotes the gate to the historical
    advisory behavior (record the fix plan, continue to deploy). Unparseable
    reviews stay advisory either way — only an explicit blocking verdict fails.
    """
    raw = (os.environ.get("GENIUS_FINAL_REVIEW_STRICT") or "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


def full_suite_gate_enabled() -> bool:
    """Whether a failing whole-project pytest run FAILS the custom pipeline.

    Strict by default: the per-file loops verified each file in isolation, so
    only this run can catch cross-file conflicts — a job whose assembled
    project cannot pass one plain ``pytest`` must not report ``completed``.
    ``GENIUS_FULL_SUITE_GATE=0`` demotes the gate to report-only (the run
    still happens and its log still lands in review.md).
    """
    raw = (os.environ.get("GENIUS_FULL_SUITE_GATE") or "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


def degraded_mode() -> bool:
    """Opt-in resilience: when ``GENIUS_DEGRADED_MODE`` is truthy, the pipeline
    keeps producing partial artifacts instead of aborting when a non-critical
    stage fails (some files fail to verify, or the DevOps/deploy stage errors).

    Off by default so CI and normal runs keep strict fail-fast semantics.
    """
    return os.getenv("GENIUS_DEGRADED_MODE", "").lower() in ("1", "true", "yes")


# Claude Code's reasoning-effort scale (mirrors _CLAUDE_EFFORT_LEVELS in
# ag_core/providers/anthropic_provider.py — the provider re-validates anyway).
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_ADAPTIVE_EFFORT_DEFAULT_THRESHOLD = 600
_ADAPTIVE_EFFORT_DEFAULT_SMALL = "high"


def _adaptive_effort(cleaned_prompt: str):
    """Small-project effort downgrade (opt-in via ``GENIUS_ADAPTIVE_EFFORT``).

    The design/plan stage runs at the Claude service's configured effort (e.g.
    ``GENIUS_CLAUDE_EFFORT_CLAUDE=max``) regardless of project size — a real
    2-file run spent ~3 minutes planning at max. When enabled and the
    @modifier-stripped pipeline prompt is shorter than
    ``GENIUS_ADAPTIVE_EFFORT_THRESHOLD`` characters (default 600), the
    pipeline-wide effort becomes ``GENIUS_ADAPTIVE_EFFORT_SMALL`` (default
    ``high``), riding the same per-task contextvar as ``@deep`` — so it
    overrides the per-role env efforts for claude/codex, while agy-backed
    stages accept-and-ignore it as usual. A prompt at/over the threshold
    returns None (byte-identical requests), and the caller lets an explicit
    ``@deep`` win over this heuristic.
    """
    if os.getenv("GENIUS_ADAPTIVE_EFFORT", "").lower() not in ("1", "true", "yes"):
        return None
    try:
        threshold = int(
            os.getenv("GENIUS_ADAPTIVE_EFFORT_THRESHOLD", "").strip()
            or _ADAPTIVE_EFFORT_DEFAULT_THRESHOLD
        )
    except (TypeError, ValueError):
        threshold = _ADAPTIVE_EFFORT_DEFAULT_THRESHOLD
    if threshold <= 0 or len(cleaned_prompt) >= threshold:
        return None
    small = (
        os.getenv("GENIUS_ADAPTIVE_EFFORT_SMALL", "").strip().lower()
        or _ADAPTIVE_EFFORT_DEFAULT_SMALL
    )
    if small not in _EFFORT_LEVELS:
        logger.warning(
            "GENIUS_ADAPTIVE_EFFORT_SMALL=%r is not one of %s; using %r",
            small,
            list(_EFFORT_LEVELS),
            _ADAPTIVE_EFFORT_DEFAULT_SMALL,
        )
        small = _ADAPTIVE_EFFORT_DEFAULT_SMALL
    logger.info(
        "Adaptive effort: prompt is %d chars (< %d); running the pipeline at "
        "effort %r instead of the per-role defaults.",
        len(cleaned_prompt),
        threshold,
        small,
    )
    return small


def eval_gate_enabled() -> bool:
    """Whether the R5 post-run eval gate runs at the end of a pipeline.

    Opt-in via ``GENIUS_EVAL_GATE`` and always OFF under pytest (like the
    debate / design-self-heal knobs): it grades the finished workspace with
    the deterministic metric set (offline, no judge) and writes a score
    under ``logs/eval/`` — behavior the fixed-mock pipeline tests must not
    see. A grade failure is swallowed at the call site; it never fails a run.
    """
    if under_pytest():
        return False
    return os.getenv("GENIUS_EVAL_GATE", "").lower() in ("1", "true", "yes")


def auto_install_enabled() -> bool:
    """Whether the pipeline auto-installs designed dependencies before verify.

    Opt-in via ``GENIUS_AUTO_INSTALL`` and always OFF under pytest (same
    convention as the eval gate). When enabled, root-level
    ``requirements*.txt`` files from the design are implemented FIRST (wave 0
    of the fan-out), then ``pip install``-ed into an isolated venv under
    ``pipeline_internal_dir()`` (built with ``--system-site-packages`` so
    pytest/flake8 stay importable), and every verification subprocess runs on
    that venv's interpreter via :func:`verification_python`. Off — the
    default — keeps today's behavior byte-identical: manifests are ordinary
    wave-1 files and verification uses the orchestrator's own interpreter.

    SECURITY: installing LLM-generated requirements executes arbitrary
    package code at install time — enable only for runs you trust enough to
    install from.
    """
    if under_pytest():
        return False
    return os.getenv("GENIUS_AUTO_INSTALL", "").lower() in ("1", "true", "yes")


def project_gate_enabled() -> bool:
    """Whether the pipeline runs the generated project's OWN quality gates.

    Opt-in via ``GENIUS_PROJECT_GATE`` and always OFF under pytest (same
    convention as auto-install: it spawns package-manager subprocesses that
    install and execute arbitrary project code). When enabled, the custom
    flow's whole-project verification additionally detects the project's
    stack (v1: ``package.json`` → npm) and runs its own gates — install,
    then the ``test``/``lint``/``build`` scripts that exist — from the
    project root; results land in review.md under "## Project gates" and a
    failing gate fails the job through the same strict full-suite gate
    (``GENIUS_FULL_SUITE_GATE=0`` demotes to report-only). Fixes the
    false-safety hole where a Next.js job read as verified while every
    per-file log said "pytest skipped: not a Python file".
    """
    if under_pytest():
        return False
    return os.getenv("GENIUS_PROJECT_GATE", "").lower() in ("1", "true", "yes")


def hackathon_mode_enabled() -> bool:
    """Whether the opt-in hackathon mode augments the CUSTOM flow.

    Opt-in via ``GENIUS_HACKATHON_MODE`` and always OFF under pytest (same
    convention as project gates / auto-install), so every fixed-mock pipeline
    test keeps byte-identical prompts and call sequences. When enabled, the
    custom flow appends rubric-oriented guidance to the research/design/devops
    request prompts (``HACKATHON_*_GUIDANCE`` in
    ``ag_core/utils/prompt_templates.py``) and, after the devops stage gate,
    best-effort emits two extra workspace-root artifacts: ``pitch.md`` (one
    extra claude-role call) and ``ai_collaboration_log.md`` (deterministic,
    ``ag_core/collab_log.py``). Off = byte-identical pipeline.
    """
    if under_pytest():
        return False
    return os.getenv("GENIUS_HACKATHON_MODE", "").lower() in ("1", "true", "yes")


async def _maybe_run_eval_gate(project_dir: str, prompt: str) -> None:
    """Grade the finished workspace and log any quality regression.

    A no-op unless :func:`eval_gate_enabled`. Fully non-fatal: a grading
    failure is logged and swallowed so it can never turn a successful run
    into a failed one.
    """
    if not eval_gate_enabled():
        return
    try:
        from ag_core.eval.gate import run_eval_gate

        # Grade the WORKSPACE root (where research/design/review.md live
        # post-separation), not the projects/<slug> deliverable — grading the
        # deliverable left the artifact metrics permanently blind. The eval
        # JSON itself is a pipeline internal, so it lands under .genius/,
        # never inside the deliverable.
        abs_project = os.path.abspath(project_dir)
        parent = os.path.dirname(abs_project)
        grade_root = (
            os.path.dirname(parent)
            if os.path.basename(parent) == "projects"
            else abs_project
        )
        result = await run_eval_gate(
            grade_root,
            prompt=prompt,
            eval_dir=os.path.join(
                pipeline_internal_dir(project_dir), "logs", "eval"
            ),
        )
        diff = result.get("compare")
        overall = result["grade"].get("overall", 0.0)
        if diff and diff.get("regressed"):
            logger.warning(
                "[eval-gate] quality regressed vs last run on %s "
                "(overall %s -> %s); score saved to %s",
                diff.get("regressions"),
                diff.get("overall_baseline"),
                diff.get("overall_current"),
                result.get("score_path"),
            )
        else:
            logger.info(
                "[eval-gate] overall score %.2f; saved to %s",
                overall,
                result.get("score_path"),
            )
    except Exception as e:  # noqa: BLE001 - the gate must never fail a run.
        logger.warning("[eval-gate] skipped (non-fatal): %s", e)


async def _emit_hackathon_artifacts(
    workspace: str,
    project_dir: str,
    prompt: str,
    design_content: str,
    review_file: str,
    review_content: str,
    audit_content: str,
    deploy_content: str,
    claude_url: str,
    api_key: str,
    client,
    poll_timeout: float,
) -> None:
    """Best-effort hackathon submission artifacts (custom flow only).

    Runs after the devops stage gate when :func:`hackathon_mode_enabled`.
    Two independent halves, each in its own try/except so neither a pitch
    failure nor a log failure can fail a completed build — and the collab
    log still exports when the pitch call died:

    1. ``pitch.md`` — one extra claude-role call over the finished artifacts
       (design, review incl. final-review sections, audit, deploy), raw-
       captured as ``pitch`` so it appears in the collaboration timeline.
    2. ``ai_collaboration_log.md`` — deterministic export from the run's own
       manifest/traces (``ag_core/collab_log.py``); runs AFTER the pitch so
       the pitch trace is part of the timeline.
    """
    try:
        if claude_url:
            # Re-read review.md from disk: _record_final_review appended the
            # "## Final review" (and fix-plan) sections after review_content
            # was captured. Fall back to the in-memory copy.
            try:
                with open(review_file, "r", encoding="utf-8") as fh:
                    review_text = fh.read()
            except OSError:
                review_text = review_content
            pitch_req = (
                HACKATHON_PITCH_PROMPT
                + f"Original request:\n{prompt}\n\n"
                + f"Design:\n{truncate_log(design_content)}\n\n"
                + f"Review:\n{truncate_log(review_text)}\n\n"
                + f"Audit:\n{truncate_log(audit_content)}\n\n"
                + f"Deploy plan:\n{truncate_log(deploy_content)}"
            )
            pitch_content = await call_api(
                claude_url,
                api_key,
                pitch_req,
                context={},
                client=client,
                poll_timeout=poll_timeout,
            )
            save_raw_response(project_dir, "pitch", pitch_content)
            _write_text(os.path.join(workspace, "pitch.md"), pitch_content)
            logger.info("[hackathon] pitch.md written to workspace root.")
    except Exception as e:  # noqa: BLE001 - best-effort by contract.
        logger.warning("[hackathon] pitch generation skipped (non-fatal): %s", e)

    try:
        from ag_core.collab_log import export_collab_log

        _write_text(
            os.path.join(workspace, "ai_collaboration_log.md"),
            export_collab_log(workspace),
        )
        logger.info("[hackathon] ai_collaboration_log.md written.")
    except Exception as e:  # noqa: BLE001 - best-effort by contract.
        logger.warning("[hackathon] collab log skipped (non-fatal): %s", e)


def _write_job_manifest(workspace: str, manifest: dict) -> None:
    """Best-effort atomic ``job.json`` write for CLI runs (MCP manifest shape).

    Gives ``ag_core/collab_log.py`` (and any other manifest reader) the same
    job metadata an MCP-driven run gets from ``mcp_server._journal_job`` —
    same tmp+``os.replace`` atomicity, same field names. Failures are logged
    and swallowed: journaling must never fail a run.
    """
    path = os.path.join(workspace, "job.json")
    tmp = path + ".tmp"
    try:
        os.makedirs(workspace, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("[cli-journal] job.json write skipped (non-fatal): %s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass


async def run_cli_journaled(pipeline_coro_factory, *, workspace, pipeline, prompt):
    """Run a pipeline coroutine with start/finish ``job.json`` journaling.

    The CLI counterpart of the MCP server's per-transition ``_journal_job``:
    a ``python orchestrator.py`` run journals ``running`` before the pipeline
    and the terminal ``completed``/``failed`` (+ ``finished_at``) after, so a
    CLI workspace carries the same job metadata an MCP job dir does — the AI
    collaboration log's Job table in particular. After the final manifest
    write, the collab log (if the run emitted one) is re-exported so the
    shipped log reflects the terminal status instead of "running". Exceptions
    from the pipeline propagate unchanged; journaling itself is best-effort.
    """
    ws = os.path.abspath(workspace or os.getcwd())
    manifest = {
        "job_id": uuid.uuid4().hex,
        "status": "running",
        "pipeline": pipeline,
        "prompt": prompt,
        "error": None,
        "workspace": ws,
        "started_at": time.time(),
        "finished_at": None,
        "require_approval": False,
        "awaiting_stage": None,
        "journaled_by": "cli",
    }
    _write_job_manifest(ws, manifest)
    try:
        result = await pipeline_coro_factory()
        manifest["status"] = "completed"
        return result
    except BaseException as e:
        manifest["status"] = "failed"
        manifest["error"] = str(e) or e.__class__.__name__
        raise
    finally:
        manifest["finished_at"] = time.time()
        _write_job_manifest(ws, manifest)
        try:
            from ag_core.collab_log import refresh_log_if_present

            if refresh_log_if_present(ws):
                logger.info(
                    "[cli-journal] ai_collaboration_log.md refreshed with the "
                    "terminal job status."
                )
        except Exception as e:  # noqa: BLE001 - best-effort by contract.
            logger.warning("[cli-journal] collab log refresh skipped: %s", e)


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
        lines = [f"- {path}: {status}\n" for path, status in status_dict.items()]
        _write_text(progress_file_path, "# Current Progress\n\n" + "".join(lines))
    except Exception as e:
        logger.warning(f"Failed to update CURRENT_PROG.md: {e}")


def _resolve_pipeline_setup(prompt, workspace, max_debate_rounds):
    """Shared pipeline preamble: resolve the debate-round default (0 under
    pytest), validate the prompt, derive the project name, and default the
    workspace to cwd.

    Also parses the leading ``@modifier`` run once (see ag_core.directives):
    returns ``cleaned`` (the prompt with any leading @modifiers stripped, used
    for slash-command DETECTION and the project slug) and ``effort`` (from
    ``@deep``, threaded on the call stack to the structured stages whose
    synthesized prompts carry no @deep token). The RAW ``prompt`` is what
    callers still SEND to the agents, which re-parse @modifiers themselves — so
    prose modifiers (@table/@critic/…) keep working via the agent, and effort
    additionally reaches codex/claude even on synthesized prompts. For a prompt
    with no @modifier, ``cleaned is prompt`` (byte-identical) and effort is None.

    Returns (project_name, workspace, max_debate_rounds, cleaned, effort).
    Raises PipelineError on an empty (or @modifier-only) prompt."""
    if max_debate_rounds is None:
        max_debate_rounds = 0 if under_pytest() else 2

    cleaned, directives = parse_directives(prompt or "")

    if not cleaned or not cleaned.strip():
        raise PipelineError("Prompt cannot be empty.")

    slugified = re.sub(r"[^a-zA-Z0-9]+", "_", cleaned.strip().lower()).strip("_")
    if not slugified:
        project_name = "default_project"
    elif len(slugified) > 50:
        project_name = (
            slugified[:40]
            + "_"
            + hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:8]
        )
    else:
        project_name = slugified

    if workspace is None:
        workspace = os.getcwd()

    # An explicit @deep always wins; otherwise the small-prompt heuristic may
    # supply a pipeline effort (None when disabled or the prompt is large —
    # byte-identical requests, same as before).
    effort = directives.effort or _adaptive_effort(cleaned)

    return project_name, workspace, max_debate_rounds, cleaned, effort


def _write_text(path: str, content: str) -> None:
    """Write text to ``path`` (UTF-8). Error policy stays with the caller:
    some stages warn-and-continue on a failed write, others abort the run."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def lint_design_plan(files_to_implement: list, design_text: str = "") -> tuple:
    """Deterministic (no-LLM) checks on a parsed DesignPlan.

    Returns ``(blocking, warnings)``. Blocking issues make the plan
    unbuildable and must stop the run BEFORE any coder tokens are spent: a
    duplicate path double-implements the same file concurrently, an empty
    specification gives the coder nothing to translate, and an
    absolute/escaping path would only explode later inside ``safe_join``.
    Warnings never stop the run — today that is an unresolved
    ``[NEEDS CLARIFICATION: ...]`` marker, which the architect contract tells
    the model to emit instead of guessing (a human should read design.md).
    """
    blocking, warnings = [], []
    seen = set()
    for entry in files_to_implement or []:
        path = str(entry.get("path") or "").strip()
        spec = str(entry.get("specification") or "").strip()
        if not path:
            blocking.append("a files[] entry has an empty path")
            continue
        norm = path.replace("\\", "/")
        # Windows drive paths (C:\x) are not isabs() on POSIX — check both.
        if os.path.isabs(path) or (len(path) > 1 and path[1] == ":"):
            blocking.append(f"absolute path not allowed: '{path}'")
        elif any(part == ".." for part in norm.split("/")):
            blocking.append(f"path escapes the project root: '{path}'")
        key = norm.lower()
        if key in seen:
            blocking.append(f"duplicate file path: '{path}'")
        seen.add(key)
        if not spec:
            blocking.append(f"empty specification for '{path}'")
    if "[NEEDS CLARIFICATION" in (design_text or ""):
        warnings.append(
            "the plan contains unresolved [NEEDS CLARIFICATION] marker(s) — "
            "review design.md before trusting the build"
        )
    return blocking, warnings


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

    # 1. Look for a DesignPlan JSON object. Prefer a ```json fenced block, then
    #    the whole document.
    candidates = re.findall(
        r"```json\s*(.*?)```", design_content, re.DOTALL | re.IGNORECASE
    )
    candidates.append(design_content)
    for text in candidates:
        for obj in _iter_json_objects(text):
            result = _validate_obj(obj)
            if result is not None:
                return result

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


def pipeline_internal_dir(project_dir: str) -> str:
    """Directory for pipeline internals kept OUT of the deliverable.

    The project directory hands over ONLY the designed files; logs, raw
    traces, the message-bus DB and tester-GENERATED test modules live here
    instead: ``<workspace>/.genius/<slug>/`` when the project sits in the
    standard ``<workspace>/projects/<slug>/`` layout, else
    ``<project_dir>/.genius/`` (a dot-dir, so pytest collection and the
    conformance walk skip it either way).
    """
    abs_dir = os.path.abspath(project_dir)
    parent = os.path.dirname(abs_dir)
    if os.path.basename(parent) == "projects":
        return os.path.join(
            os.path.dirname(parent), ".genius", os.path.basename(abs_dir)
        )
    return os.path.join(abs_dir, ".genius")


def designed_basenames_of(files) -> set:
    """Basenames of every file in a design's file list (collision lookup)."""
    return {
        os.path.basename(str(f.get("path", "")).replace("\\", "/"))
        for f in (files or [])
        if isinstance(f, dict) and f.get("path")
    }


def generated_test_path(
    project_dir: str, flat_name: str, designed_basenames: set = None
) -> str:
    """Path of the tester-GENERATED module verifying one implemented file.

    Generated modules are pipeline INTERNALS: they live under
    ``pipeline_internal_dir()/tests/``, not in the deliverable (the per-file
    loop runs them in isolation with PYTHONPATH=project_dir, and the
    whole-project gate intentionally sees only the designed files). The
    ``_gen`` suffix still dodges basename collisions with DESIGNED test files
    (pytest cannot collect two modules named ``test_foo`` in one run — a real
    job completed with that collision and only bare ``pytest`` at the project
    root exposed it).
    """
    name = f"test_{flat_name}.py"
    if designed_basenames and name in designed_basenames:
        name = f"test_{flat_name}_gen.py"
    return os.path.join(pipeline_internal_dir(project_dir), "tests", name)


def is_dependency_manifest(rel_path: str) -> bool:
    """True for a ROOT-level pip requirements file (``requirements*.txt``).

    Only these are auto-installed (see :func:`auto_install_enabled`): nested
    pin files (``deploy/requirements.txt``) and other manifest formats stay
    ordinary designed files.
    """
    norm = str(rel_path or "").replace("\\", "/").strip("/")
    if not norm or "/" in norm:
        return False
    return re.fullmatch(r"requirements[\w.-]*\.txt", norm, re.IGNORECASE) is not None


def project_venv_dir(project_dir: str) -> str:
    """Home of the auto-install venv — a pipeline INTERNAL, never handed over."""
    return os.path.join(pipeline_internal_dir(project_dir), "venv")


def venv_python(project_dir: str) -> str:
    """Interpreter path inside the auto-install venv (may not exist yet)."""
    if os.name == "nt":
        return os.path.join(project_venv_dir(project_dir), "Scripts", "python.exe")
    return os.path.join(project_venv_dir(project_dir), "bin", "python")


def verification_python(project_dir: str) -> str:
    """Interpreter for verification subprocesses (flake8/pytest).

    The auto-install venv's python when the feature is enabled AND the venv
    was actually built, else ``sys.executable``. Re-checking the flag here
    means a stale venv left in a reused workspace by an earlier opted-in run
    can never hijack verification once the feature is off again.
    """
    if auto_install_enabled():
        py = venv_python(project_dir)
        if os.path.exists(py):
            return py
    return sys.executable


def partition_fanout_waves(files_to_implement) -> tuple[list, list, list]:
    """Split a design's file list into (manifest, implementation, test) waves.

    Both fan-outs (sequential/custom and e2e) process the waves strictly in
    that order. Designed TEST MODULES always run last, once their targets are
    final; dependency manifests are peeled into wave 0 only when auto-install
    is enabled (otherwise they stay ordinary implementation files, keeping the
    default scheduling byte-identical).
    """
    impl_wave = [
        f for f in files_to_implement if not is_test_module(str(f.get("path", "")))
    ]
    test_wave = [
        f for f in files_to_implement if is_test_module(str(f.get("path", "")))
    ]
    manifest_wave = []
    if auto_install_enabled():
        manifest_wave = [
            f for f in impl_wave if is_dependency_manifest(str(f.get("path", "")))
        ]
        impl_wave = [
            f for f in impl_wave if not is_dependency_manifest(str(f.get("path", "")))
        ]
    return manifest_wave, impl_wave, test_wave


async def auto_install_requirements(project_dir: str, manifest_paths: list) -> None:
    """Build the isolated venv and ``pip install`` designed requirements into it.

    Best-effort by design: any failure is logged (and captured in the internal
    ``logs/install.log``) but never fails the pipeline — the verification
    waves that follow surface truly missing dependencies as ordinary test
    failures the self-heal loop can react to. The venv inherits the
    orchestrator's site-packages (``--system-site-packages``) so pytest and
    flake8 remain importable without reinstalling them; packages pinned by the
    manifests shadow the inherited copies.
    """
    log_sections = []
    try:
        venv_dir = project_venv_dir(project_dir)
        py = venv_python(project_dir)
        if not os.path.exists(py):
            code, out = await run_subprocess(
                [sys.executable, "-m", "venv", "--system-site-packages", venv_dir],
                timeout=install_timeout(),
            )
            log_sections.append(f"$ python -m venv {venv_dir}\n[exit {code}]\n{out}")
            if code != 0 or not os.path.exists(py):
                logger.warning(
                    "Auto-install: venv creation failed (exit %s); verification "
                    "falls back to the orchestrator interpreter. %s",
                    code,
                    truncate_log(out, 2000),
                )
                return
        for rel in sorted(str(p) for p in manifest_paths):
            manifest = os.path.join(project_dir, rel)
            if not os.path.exists(manifest):
                # The coder may have failed to produce the file; its wave-0
                # task already recorded that failure — nothing to install.
                log_sections.append(
                    f"$ pip install -r {rel}\n[skipped: file was never written]"
                )
                continue
            code, out = await run_subprocess(
                [
                    py,
                    "-m",
                    "pip",
                    "install",
                    "--no-input",
                    "--disable-pip-version-check",
                    "-r",
                    manifest,
                ],
                cwd=project_dir,
                timeout=install_timeout(),
            )
            log_sections.append(f"$ pip install -r {rel}\n[exit {code}]\n{out}")
            if code != 0:
                logger.warning(
                    "Auto-install: pip install -r %s failed (exit %s); "
                    "verification may hit ModuleNotFoundError. %s",
                    rel,
                    code,
                    truncate_log(out, 2000),
                )
            else:
                logger.info("Auto-install: installed %s into %s", rel, venv_dir)
    except Exception as e:  # noqa: BLE001 - setup assist must never kill the run
        logger.warning(f"Auto-install failed: {e}")
        log_sections.append(f"[error] {e}")
    finally:
        if log_sections:
            try:
                log_dir = os.path.join(pipeline_internal_dir(project_dir), "logs")
                os.makedirs(log_dir, exist_ok=True)
                _write_text(
                    os.path.join(log_dir, "install.log"), "\n\n".join(log_sections)
                )
            except Exception as e:
                logger.warning(f"Failed to write install.log: {e}")


async def run_subprocess(
    cmd: list, env: dict = None, cwd: str = None, timeout: float = None
) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=cwd,
    )
    cli_name = " ".join(str(c) for c in cmd[:2]) if cmd else "subprocess"
    try:
        stdout, stderr = await communicate_with_timeout(
            process,
            timeout=timeout if timeout is not None else test_timeout(),
            cli_name=cli_name,
        )
    except CLITimeoutError as e:
        # Bound the wait so a hung flake8/pytest (an LLM-generated infinite loop
        # or blocking call) can't freeze the pipeline forever; surface it as a
        # non-zero exit (124, the conventional timeout code) so callers treat it
        # as a verification failure and self-heal instead of hanging.
        return 124, str(e)
    output = (
        stdout.decode("utf-8", errors="replace")
        + "\n"
        + stderr.decode("utf-8", errors="replace")
    )
    return process.returncode, output


async def _run_project_pytest(project_dir: str) -> tuple[int, str]:
    """Run pytest ONCE over the whole generated project (cwd = project root).

    The per-file self-heal loops execute each test module in isolation, so
    they can never see cross-file conflicts — duplicate test-module basenames,
    import clashes, fixture collisions. Returns (exit_code, tail_of_output);
    exit code 5 ("no tests collected") is a valid outcome for docs-only
    projects and is treated as a pass by the caller.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.pathsep.join(
        [project_dir, os.path.join(project_dir, "src"), env.get("PYTHONPATH", "")]
    ).strip(os.path.pathsep)
    # Verification must not leave __pycache__ bytecode inside the deliverable.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        process = await asyncio.create_subprocess_exec(
            verification_python(project_dir),
            "-m",
            "pytest",
            "-q",
            cwd=project_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await communicate_with_timeout(
            process, timeout=test_timeout(), cli_name="whole-project pytest"
        )
    except CLITimeoutError as e:
        return 124, str(e)
    except Exception as e:  # noqa: BLE001 - report, don't crash the pipeline
        return -999, f"Failed to run whole-project pytest: {e}"
    output = (
        stdout.decode("utf-8", errors="replace")
        + "\n"
        + stderr.decode("utf-8", errors="replace")
    )
    return process.returncode, truncate_log(output)


def detect_project_gates(project_dir: str) -> list:
    """Detect the generated project's own quality gates (v1: npm).

    Returns ``[(gate_name, argv, timeout_seconds), ...]`` to run sequentially
    from the project root, or ``[]`` when no supported manifest is present
    (Python projects need no detection — whole-project pytest is the native
    gate). Only scripts the project actually declares are run, ``install``
    always first so ``node_modules`` exists for the rest.
    """
    pkg_path = os.path.join(project_dir, "package.json")
    if not os.path.isfile(pkg_path):
        return []
    try:
        with open(pkg_path, "r", encoding="utf-8") as fh:
            scripts = (json.load(fh) or {}).get("scripts") or {}
    except (OSError, ValueError) as e:
        logger.warning("Project gates: unreadable package.json (%s); skipping.", e)
        return []
    npm = shutil.which("npm")
    if not npm:
        logger.warning(
            "Project gates: package.json present but npm is not on PATH; "
            "the project's own test/lint/build gates cannot run."
        )
        return []

    def _cmd(args: list) -> list:
        # npm is npm.cmd on Windows; CreateProcess can't exec .cmd directly.
        base = [npm] + args
        return ["cmd.exe", "/c"] + base if os.name == "nt" else base

    gates = [
        ("npm install", _cmd(["install", "--no-audit", "--no-fund"]))
    ]
    for script in ("test", "lint", "build"):
        if script in scripts:
            gates.append((f"npm run {script}", _cmd(["run", script])))
    # npm steps (cold install, framework builds) routinely exceed the
    # pytest verification ceiling; bound them like installs instead.
    return [(name, argv, install_timeout()) for name, argv in gates]


async def _run_project_gates(project_dir: str) -> tuple[bool, str]:
    """Run the project's own detected gates; return (any_failed, md_section).

    The section body is review.md-ready (one ``###`` block per gate with the
    exit code and a bounded log tail). An ``npm install`` failure skips the
    remaining gates — they could only fail for the same missing-deps reason.
    """
    gates = detect_project_gates(project_dir)
    if not gates:
        return False, ""
    env = os.environ.copy()
    # Never watch mode / interactive wizards inside a pipeline.
    env["CI"] = "1"
    failed = False
    parts = []
    for name, argv, timeout in gates:
        code, out = await run_subprocess(
            argv, env=env, cwd=project_dir, timeout=timeout
        )
        parts.append(
            f"### {name}\nexit code: {code}\n\n"
            f"```\n{truncate_log(out, 4000).strip()}\n```"
        )
        if code != 0:
            failed = True
            logger.warning("Project gate '%s' failed (exit %s).", name, code)
            if name == "npm install":
                parts.append("(npm install failed — remaining gates skipped)")
                break
    return failed, "\n\n".join(parts)


# The pipeline no longer writes its artifacts into the project directory
# (they live at the workspace root; logs/generated tests live under
# pipeline_internal_dir()), but stale copies from pre-separation runs and
# tool caches must still never count as "extra" product files.
_PIPELINE_OWNED_BASENAMES = {
    "research.md",
    "design.md",
    "review.md",
    "audit.md",
    "deploy.md",
    "plan.md",
    "pitch.md",
    "ai_collaboration_log.md",
}
_PIPELINE_OWNED_DIRS = {"logs", "__pycache__", ".pytest_cache", ".git", ".genius"}


def sweep_runtime_caches(project_dir: str) -> None:
    """Remove regenerable Python runtime caches from the deliverable.

    The pipeline's own pytest runs write no bytecode
    (PYTHONDONTWRITEBYTECODE), but a designed conftest, a pytest plugin or
    any tool invoked during verification can still drop __pycache__ /
    .pytest_cache into the project directory — and the deliverable must hand
    over ONLY the designed files. Best-effort: never fails the run.
    """
    for root, dirs, _names in os.walk(project_dir):
        for d in list(dirs):
            if d in ("__pycache__", ".pytest_cache"):
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                dirs.remove(d)
            elif d == ".genius":
                # Pipeline internals are not part of the deliverable, and in
                # the fallback layout they include the auto-install venv —
                # thousands of directories the sweep must not crawl.
                dirs.remove(d)


def _design_conformance_report(project_dir: str, files_to_implement) -> str:
    """Compare the design's file list against what is actually on disk.

    Missing designed files and files beyond the design (excluding the
    pipeline's own artifacts and generated test modules) are listed so the
    final reviewer — and the operator — can see scope drift at a glance.
    """
    designed = {
        str(f.get("path", "")).replace("\\", "/")
        for f in (files_to_implement or [])
        if isinstance(f, dict) and f.get("path")
    }
    expected_generated = set()
    for path in designed:
        if not path.endswith(".py") or is_test_module(path) or is_pytest_infra(path):
            continue
        flat = flatten_rel_path(path)
        expected_generated.add(f"tests/test_{flat}.py")
        expected_generated.add(f"tests/test_{flat}_gen.py")
    actual = set()
    for root, dirs, names in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in _PIPELINE_OWNED_DIRS]
        for name in names:
            rel = os.path.relpath(os.path.join(root, name), project_dir)
            actual.add(rel.replace("\\", "/"))
    missing = sorted(designed - actual)
    extra = sorted(
        rel
        for rel in actual - designed - expected_generated
        if os.path.basename(rel) not in _PIPELINE_OWNED_BASENAMES
        and not rel.endswith((".bak", ".pyc"))
    )
    lines = [
        f"Designed files: {len(designed)}; present on disk: "
        f"{len(designed) - len(missing)}/{len(designed)}."
    ]
    if missing:
        lines.append("MISSING designed files: " + ", ".join(missing))
    if extra:
        lines.append(
            "Files beyond the design (excluding pipeline artifacts and "
            "generated test modules): " + ", ".join(extra)
        )
    else:
        lines.append(
            "No files beyond the design (excluding pipeline artifacts and "
            "generated test modules)."
        )
    return "\n".join(lines)


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


# Pipeline-scoped reasoning effort (from a leading @deep on the pipeline prompt),
# carried exactly like the distributed-mode flag above: set once at pipeline
# entry, read by call_api. This reaches every stage's provider on the call stack
# — including the STRUCTURED stages whose synthesized prompts carry no @deep
# token — without threading a param through ~20 call sites. Being a per-task
# contextvar copy, concurrent pipelines never stomp each other's effort, and it
# is never an environment variable.
_PIPELINE_EFFORT_VAR: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "genius_pipeline_effort"
)


def _pipeline_effort():
    """The pipeline's contextvar effort if set, else None (direct-call/test)."""
    try:
        return _PIPELINE_EFFORT_VAR.get()
    except LookupError:
        return None


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


from ag_core.orchestration_errors import (  # noqa: E402,F401
    PipelineError,
    ChecksumMismatchError,
)

from ag_core.orchestration_helpers import (  # noqa: E402,F401
    _iter_json_objects,
    resolve_grok_cmd,
    resolve_claude_cmd,
    resolve_antigravity_cmd,
    resolve_codex_cmd,
    resolve_tester_cmd,
    resolve_security_cmd,
    resolve_devops_cmd,
    clean_output_files,
    format_cmd_args,
    detect_vulnerabilities,
    parse_security_verdict,
)


def security_is_blocking(security_report: str) -> bool:
    """Decide whether the security audit should block acceptance of the code.

    Defined here (not in orchestration_helpers) so it calls
    ``parse_security_verdict`` / ``detect_vulnerabilities`` through orchestrator's
    OWN namespace — callers/tests that monkeypatch
    ``orchestrator.detect_vulnerabilities`` or ``orchestrator.parse_security_verdict``
    still take effect. A helper-module copy would resolve those names in
    orchestration_helpers and silently bypass the patch."""
    verdict = parse_security_verdict(security_report)
    if verdict is not None:
        return bool(verdict.get("blocking"))
    return detect_vulnerabilities(security_report)


def verification_mode(rel_path: str) -> str:
    """Human-readable verification mode the per-file loop applies to a file.

    Feeds the review.md "Verification coverage" section: "verified" must
    never imply "executed" — a real Next.js job shipped 28 files whose logs
    all said "pytest skipped: not a Python file" while the job read as fully
    tested.
    """
    norm = str(rel_path or "").replace("\\", "/")
    if not norm.endswith(".py"):
        return "audit-only (non-Python; NOT executed by any test runner)"
    if is_test_module(norm):
        return "executed directly under pytest (designed test module)"
    if is_pytest_infra(norm):
        return "audit-only (pytest infrastructure)"
    return "generated pytest module executed"


def file_quality_state(rel_path: str, failed: bool = False) -> str:
    """Quality-ladder badge for one designed file (generated → tested →
    security-accepted).

    Complements the lifecycle strings ("in progress"/"completed"/"failed",
    kept byte-identical for compatibility): a COMPLETED file earned exactly
    the badges its verification mode can grant, so a non-Python file can
    never read as tested — the gap behind a real mis-reported Next.js job.
    """
    if failed:
        return "generated-only (verification FAILED)"
    norm = str(rel_path or "").replace("\\", "/")
    if not norm.endswith(".py"):
        return "security-accepted (NOT tested: non-Python, no runner)"
    if is_test_module(norm):
        return "tested (executed under pytest)"
    if is_pytest_infra(norm):
        return "security-accepted (NOT tested: pytest infrastructure)"
    return "tested, security-accepted"


def design_scope_note(design_plan_content: str) -> str:
    """Suffix for security-audit prompts: the design's scope is the contract.

    A real job failed on a policy conflict no code change could resolve: the
    design EXPLICITLY excluded authentication, the auditor kept returning
    blocking "no auth" findings, and all three API/page files burned their
    whole retry budget. Capabilities the design rules out are accepted
    risks, not blocking defects.
    """
    if not design_plan_content:
        return ""
    return (
        "\n\nScope rule: the approved design below is the CONTRACT for this "
        "job. If the design explicitly excludes a capability (e.g. 'do NOT "
        "introduce authentication/pagination'), its ABSENCE is an accepted "
        "risk: report it with severity 'low', note '(accepted by design)', "
        "and it must NOT make the verdict blocking. Reserve blocking=true "
        "for defects in what the file actually implements.\n\n"
        f"Approved design (excerpt):\n{design_plan_content[:4000]}"
    )


def validate_file(path, step_name, is_input=True):
    """Validate that a context file exists and is not empty."""
    desc = "Input" if is_input else "Output"
    if not os.path.exists(path):
        raise PipelineError(f"{desc} file for '{step_name}' does not exist: {path}")
    if os.path.getsize(path) == 0:
        raise PipelineError(f"{desc} file for '{step_name}' is empty: {path}")


from ag_core.config import load_config
from ag_core.scanner.project_scanner import ProjectScanner
from ag_core.scanner.repo_graph import build_budgeted_context
from ag_core.utils.db import log_conversation_async
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


def _verify_hub_response_if_signed(response) -> None:
    """Verify a hub HTTP response's X-Payload-SHA256 when the hub signed it.

    The distributed hub-poll path consumes worker-generated results relayed by
    the hub; the local skill-server path verifies response checksums, and this
    closes the same integrity gap for cross-machine mode. Backward compatible:
    an older hub sends no signature, so verification is skipped when the header
    is absent — but a present-but-wrong signature is rejected as tampering.
    """
    # A real response header is a non-empty str; require that (a missing header
    # is None, and this also ignores non-string test doubles).
    sig = response.headers.get("X-Payload-SHA256")
    if isinstance(sig, str) and sig:
        verify_response_checksum(response)


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
def _parse_retry_after(value):
    """Parse a Retry-After header (RFC 7231): either delta-seconds or an
    HTTP-date. Returns the delay in seconds, or None when unparseable.

    The old code honored only the numeric form and silently fell through to
    exponential backoff for the (spec-legal) HTTP-date form.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() - time.time()


def wait_strategy(retry_state):
    # Check if the last attempt raised an HTTPStatusError with Retry-After header
    exception = retry_state.outcome.exception()
    if isinstance(exception, httpx.HTTPStatusError):
        retry_after = exception.response.headers.get("Retry-After")
        if retry_after:
            delay = _parse_retry_after(retry_after)
            if delay is not None:
                # Clamp: a negative delay (a past HTTP-date or a bogus value)
                # must not cause an immediate re-hit of a throttled server;
                # floor at 0 and cap at 60s.
                return max(0.0, min(delay, 60.0))
    # Fallback to standard exponential backoff: 2^attempt, min 1s, max 10s
    return wait_exponential(multiplier=1, min=1, max=10)(retry_state)


def _resolve_headers(headers):
    """Build request headers, minting a FRESH JWT per call when a factory is
    given. The skill server's anti-replay consumes a token's ``jti`` on the
    first auth check (even for a request that then 429s), so re-sending the
    same token on a tenacity retry is rejected as "Token replay detected"
    (401) — turning any transient 429/5xx into a hard failure and defeating the
    idempotency dedup. Passing a callable makes each retry attempt authenticate
    with a new jti; a plain dict is still accepted for callers that don't care.
    """
    return headers() if callable(headers) else headers


@retry(
    stop=stop_after_attempt(3),
    wait=wait_strategy,
    retry=retry_if_exception(is_transient_error),
    reraise=True,
)
async def perform_post_with_retry(client, url, payload_bytes, headers):
    response = await client.post(
        url, content=payload_bytes, headers=_resolve_headers(headers)
    )
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
    response = await client.get(url, headers=_resolve_headers(headers))
    response.raise_for_status()
    verify_response_checksum(response)
    return response


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
    effort: str | None = None,
) -> str:
    import time
    import uuid
    from ag_core.utils.jwt import encode_jwt

    import os

    # An explicit effort arg wins; otherwise inherit the pipeline-scoped effort
    # (the @deep contextvar). None on the direct-call/test path -> byte-identical.
    if effort is None:
        effort = _pipeline_effort()

    # Key the cache by the hash of the URL, the prompt, and the sorted JSON-serialized context dictionary.
    sorted_context = json.dumps(context or {}, sort_keys=True)
    # Fold effort into the cache key so calls differing only in reasoning effort
    # don't collide; a None effort leaves the key byte-identical (and the cache
    # is disabled under pytest anyway).
    cache_string = f"{url}\n{prompt}\n{sorted_context}" + (
        f"\n{effort}" if effort else ""
    )
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
        # Detect the routed role on the @modifier-stripped prompt (the URL
        # fallback below already self-heals, but keep detection consistent).
        _routing_prompt = parse_directives(prompt)[0]
        first_word = (
            _routing_prompt.strip().split()[0] if _routing_prompt.strip() else ""
        )
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
            try:
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

                    # The hub's HTTP endpoints authenticate the RAW shared secret:
                    # CentralHub.verify_auth constant-time-compares X-API-Key against
                    # SKILL_API_KEY, and the hub's own create_headers sends the raw
                    # key. A JWT never equals the raw key, so sending one (as this
                    # used to) 401'd every hub request and made distributed mode
                    # dead-on-arrival. Sending the raw key matches the hub contract
                    # and also avoids the one-time-jti replay rejection when this
                    # same header dict is re-posted in the poll loops below.
                    headers = {
                        "X-API-Key": secret,
                        "X-Payload-SHA256": workers_checksum,
                        "Content-Type": "application/json",
                    }

                    resp = await http_client.post(
                        f"{hub_url}/workers", content=payload_bytes, headers=headers
                    )
                    resp.raise_for_status()
                    _verify_hub_response_if_signed(resp)
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
                        _verify_hub_response_if_signed(resp)
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
                    if effort:
                        dispatch_payload["task_data"]["effort"] = effort
                    dispatch_checksum = calculate_checksum(dispatch_payload, secret)
                    dispatch_bytes = json.dumps(
                        dispatch_payload, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")

                    headers["X-Payload-SHA256"] = dispatch_checksum
                    resp = await http_client.post(
                        f"{hub_url}/dispatch", content=dispatch_bytes, headers=headers
                    )
                    resp.raise_for_status()
                    _verify_hub_response_if_signed(resp)
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
                        _verify_hub_response_if_signed(resp)
                        all_tasks = resp.json()

                        task_info = all_tasks.get(task_id)
                        if not task_info:
                            raise PipelineError(
                                f"Task '{task_id}' not found in tasks list."
                            )

                        status = task_info.get("status")
                        if status == "completed":
                            result = task_info.get("result")
                            # The worker wraps its output as {"output": <content>};
                            # unwrap to the content string (the in-memory WS path
                            # does the same in serve.py). Returning the raw dict made
                            # every stage write a dict to its artifact file, which
                            # failed and left research.md/design.md/... empty.
                            if isinstance(result, dict):
                                result = result.get("output", result)
                            if use_cache:
                                _cache_store(cache_key, result)
                            return result
                        elif status == "failed":
                            err = task_info.get("result", {}).get(
                                "error", "Unknown task failure"
                            )
                            raise PipelineError(f"Task failed: {err}")

                        await asyncio.sleep(0.5)
            except (PipelineError, WorkerDisconnectedError):
                raise
            except Exception as e:
                # Normalize hub transport failures (httpx timeouts / 5xx /
                # connection errors) to PipelineError so the self-heal loops'
                # `except PipelineError` retry them just like a local call.
                raise PipelineError(
                    f"Distributed hub request to {url} failed: {e}"
                ) from e
        else:
            # Dispatch to an idle worker over the in-memory WS registry, with
            # RE-DISPATCH on worker loss: if the chosen worker disconnects (or
            # its WS send fails) mid-task, pick another idle worker — or wait for
            # the same one to reconnect — and retry, instead of failing the whole
            # pipeline. Bounded by GENIUS_DISPATCH_RETRIES (default 3). A genuine
            # task timeout or an orchestrator cancel is NOT worker loss and is
            # never retried here.
            max_dispatch = max(1, int(os.environ.get("GENIUS_DISPATCH_RETRIES") or 3))
            dispatch_deadline = time.time() + poll_timeout
            last_disconnect = None

            async def _dispatch_once(worker_id):
                logger.info(
                    f"[Distributed] Selected worker '{worker_id}' for role '{role}'"
                )

                async with worker_registry.lock:
                    worker = await worker_registry.get_worker(worker_id)
                    if not worker:
                        raise WorkerDisconnectedError(
                            f"Worker '{worker_id}' disappeared from registry."
                        )
                    worker["status"] = "busy"

                task_id = f"task_{uuid.uuid4().hex[:8]}"
                task_data = {"role": role, "prompt": prompt, "context": context or {}}
                if effort:
                    task_data["effort"] = effort

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
                    async with worker_registry.lock:
                        worker["status"] = "idle"
                    raise WorkerDisconnectedError(
                        f"Worker '{worker_id}' has no active WebSocket connection."
                    )

                # HMAC (not plain SHA-256): the worker verifies the dispatch with
                # verify_checksum(task_data, checksum, api_key), HMAC-only in prod.
                from ag_core.utils.security import calculate_checksum

                secret = load_config().skill_api_key or os.getenv("SKILL_API_KEY", "")
                checksum = calculate_checksum(task_data, secret)

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
                    # A failed send == the worker's connection is gone: surface it
                    # as worker loss so the caller re-dispatches to another worker.
                    raise WorkerDisconnectedError(
                        f"Failed to send task to worker '{worker_id}' over WebSocket: {e}"
                    )

                try:
                    result = await asyncio.wait_for(fut, timeout=poll_timeout)
                    logger.info(
                        f"[Distributed] Task '{task_id}' completed successfully"
                    )
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
                    raise PipelineError(
                        f"Task timed out after {poll_timeout} seconds"
                    )
                except WorkerDisconnectedError as e:
                    logger.error(
                        f"[Distributed] Worker disconnected during task '{task_id}': {e}"
                    )
                    raise
                except asyncio.CancelledError:
                    logger.info(
                        f"[Distributed] Task '{task_id}' cancelled by orchestrator"
                    )
                    async with central_hub.lock:
                        if task_id in central_hub.tasks:
                            central_hub.tasks[task_id]["status"] = "failed"
                            central_hub.tasks[task_id]["result"] = {
                                "error": "cancelled"
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
                    raise
                except Exception as e:
                    logger.error(f"[Distributed] Task '{task_id}' failed: {e}")
                    raise PipelineError(
                        f"Task '{task_id}' failed on worker '{worker_id}': {e}"
                    )
                finally:
                    pending_tasks.pop(task_id, None)

            logger.info(f"[Distributed] Selecting idle worker for role '{role}'")
            for dispatch_attempt in range(1, max_dispatch + 1):
                worker_id = None
                while worker_id is None:
                    worker_id = await worker_registry.select_idle_worker(role)
                    if worker_id is None:
                        if time.time() > dispatch_deadline:
                            if last_disconnect is not None:
                                # We were re-dispatching after a worker loss, but
                                # no replacement worker (nor the same one
                                # reconnecting) became available in time — surface
                                # the loss as the disconnect, not a generic
                                # no-worker error.
                                raise WorkerDisconnectedError(
                                    f"Worker for role '{role}' disconnected and no "
                                    f"replacement became available within "
                                    f"{poll_timeout}s: {last_disconnect}"
                                )
                            raise PipelineError(
                                f"No idle worker available for role '{role}' within {poll_timeout} seconds."
                            )
                        await asyncio.sleep(0.5)
                try:
                    return await _dispatch_once(worker_id)
                except WorkerDisconnectedError as e:
                    last_disconnect = e
                    logger.warning(
                        f"[Distributed] worker '{worker_id}' lost on attempt "
                        f"{dispatch_attempt}/{max_dispatch} for role '{role}'; "
                        f"re-dispatching to another worker"
                    )
                    continue
            raise WorkerDisconnectedError(
                f"All {max_dispatch} dispatch attempts for role '{role}' failed; "
                f"last error: {last_disconnect}"
            )

    req_payload = {"prompt": prompt, "context": context}
    # Conditional so a None effort leaves the signed body byte-identical.
    if effort:
        req_payload["effort"] = effort

    # Calculate checksum for POST request body
    from ag_core.utils.security import calculate_checksum

    config = load_config()
    secret = config.skill_api_key or os.getenv("SKILL_API_KEY", "")
    req_checksum = calculate_checksum(req_payload, secret)
    # GET /status has an empty body, so its checksum is over empty bytes.
    get_checksum = calculate_checksum(b"", secret)
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

    def _make_post_headers():
        # Fresh jti per attempt (see _resolve_headers); the idempotency key is
        # deliberately STABLE across retries so a re-sent /run after a lost
        # response is deduplicated server-side instead of running twice.
        post_token = encode_jwt(
            {"sub": "orchestrator", "exp": time.time() + 300}, api_key
        )
        return {
            "X-API-Key": post_token,
            "Authorization": f"Bearer {post_token}",
            "X-Payload-SHA256": req_checksum,
            "Content-Type": "application/json",
            "X-Idempotency-Key": idempotency_key,
        }

    def _make_get_headers():
        poll_token = encode_jwt(
            {"sub": "orchestrator", "exp": time.time() + 300}, api_key
        )
        return {
            "X-Payload-SHA256": get_checksum,
            "X-API-Key": poll_token,
            "Authorization": f"Bearer {poll_token}",
        }

    async def _execute(c):
        try:
            # 1. Start the run
            response = await perform_post_with_retry(
                c, f"{base_url}/run", payload_bytes, _make_post_headers
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
                status_response = await perform_get_with_retry(
                    c, f"{base_url}/status/{task_id}", _make_get_headers
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
    flow: str = "sequential",
    claude_url: str = None,
    designed_basenames: set = None,
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
            test_file_path = generated_test_path(
                project_dir, flat_name, designed_basenames
            )
            os.makedirs(os.path.dirname(test_file_path), exist_ok=True)
        _internal_logs = os.path.join(pipeline_internal_dir(project_dir), "logs")
        os.makedirs(_internal_logs, exist_ok=True)
        audit_log_path = os.path.join(_internal_logs, f"audit_{flat_name}.md")
        test_log_path = os.path.join(_internal_logs, f"test_{flat_name}.log")

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
            if not file_is_python:
                # Non-Python target: override the coder contract's default
                # ```python fence with the file type's own fence so extraction
                # can recover the FULL content — a README.md answered inside a
                # ```python fence once truncated at its first nested fence.
                codex_req_prompt += (
                    "\n\nThis target file is NOT a Python module. Respond with "
                    f"ONLY the complete contents of '{file_path}' in a single "
                    f"{fence_hint(file_path)}; this overrides any instruction "
                    "to use a ```python block."
                )
            if attempt > 1:
                raw_feedback = (
                    f"\n\nPrevious implementation attempt failed check.\n"
                    f"Test Failures/Logs:\n{truncate_log(test_failures_logs)}\n\n"
                    f"Security Report:\n{truncate_log(security_report)}"
                )
                diagnosed = False
                if flow == "custom" and claude_url:
                    # Custom self-heal: Claude(Max) diagnoses the ROOT CAUSE from
                    # the logs and prescribes concrete fixes for the gemini coder,
                    # instead of dumping raw logs at it (user's flow: "Claude Max
                    # finds the cause and directs gemini to fix"). Falls back to
                    # the raw logs if the diagnosis call fails.
                    diagnose_prompt = (
                        "You are the architect. A generated implementation of "
                        f"'{file_path}' failed its checks. Diagnose the ROOT "
                        "CAUSE and give concrete, numbered fix instructions for "
                        "the coder. Do NOT write the code yourself.\n\n"
                        f"Specification:\n{specification}"
                        + raw_feedback
                    )
                    try:
                        diagnosis = await call_api(
                            claude_url,
                            api_key,
                            diagnose_prompt,
                            context={},
                            client=client,
                            poll_timeout=poll_timeout,
                        )
                        codex_req_prompt += (
                            "\n\nThe previous attempt failed. The architect "
                            "diagnosed the cause and prescribed these fixes:\n"
                            f"{diagnosis}"
                        )
                        diagnosed = True
                    except Exception as e:
                        logger.warning(
                            "Custom self-heal diagnosis failed (%s); using raw "
                            "logs instead.",
                            e,
                        )
                if not diagnosed:
                    codex_req_prompt += raw_feedback
                codex_req_prompt += (
                    "\n\nDo NOT run tests, commands, or tools. Output ONLY the "
                    f"complete file content in a single {fence_hint(file_path)}."
                )

            try:
                proj_scanner = ProjectScanner(
                    root_dir=project_dir, extra_ignores=config.scanner.exclude_patterns
                )
                current_context = await asyncio.to_thread(proj_scanner.scan)
                # Budget by graph relevance, seeded by the file being
                # implemented and any paths its specification names; the
                # design.md / current-file overlays below stay full.
                current_context = await asyncio.to_thread(
                    build_budgeted_context, current_context, [file_path], specification
                )
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
            code_to_write = extract_code(codex_code_raw, filename=file_path)

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
            codex_art_id = await message_bus.publish_async(codex_art)

            # 2. Write code to projects/[project_name]/[file_path]
            try:
                _write_text(target_file_path, code_to_write)
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
            elif not file_is_python or is_pytest_infra(file_path):
                _skip_reason = (
                    "is pytest infrastructure (conftest.py/__init__.py)"
                    if file_is_python
                    else "is not a Python module"
                )
                logger.info(
                    f"{file_path} {_skip_reason}: skipping test generation; "
                    "running the security audit only."
                )
                security_req_prompt = (
                    f"/audit Audit the following file for security issues (secrets, unsafe configuration, injection vectors) in '{file_path}':\n\n{code_to_write}"
                    + design_scope_note(design_plan_content)
                )
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
                    _write_text(audit_log_path, security_report)
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
                    "LOCATION CONTRACT: the test module is stored OUTSIDE the "
                    "project directory and executed with cwd = the project "
                    "root (PYTHONPATH includes it). To reference project "
                    "files (README, configs, the module's source), use the "
                    "imported module's location (e.g. "
                    f"Path({module_path}.__file__).parent) or plain relative "
                    "paths from cwd — NEVER the test file's own __file__.\n\n"
                    f"Code:\n\n{code_to_write}"
                )
                # Feed the previous attempt's pytest failures to the TESTER
                # too (the e2e loop already does): the coder gets this
                # feedback but cannot fix a WRONG generated test. A real run
                # failed 3/3 attempts because the tester kept regenerating an
                # over-mocked test asserting per-chunk I/O internals against
                # correct code — without seeing its own failure it repeats
                # the same systematic mistake every attempt.
                if attempt > 1 and test_failures_logs:
                    tester_req_prompt += (
                        "\n\nA previously generated test suite for this file "
                        "FAILED verification with the log below. If a failure "
                        "shows the TEST itself was wrong (over-mocking, "
                        "asserting internal call patterns or per-chunk I/O, "
                        "wrong expected values), write corrected tests that "
                        "verify the documented PUBLIC behavior (return "
                        "values, stdout/stderr, exit codes) instead of "
                        "repeating it.\nPrevious failures:\n"
                        f"{truncate_log(test_failures_logs)}"
                    )
                security_req_prompt = (
                    f"/audit Audit the following code for security vulnerabilities in file '{file_path}':\n\n{code_to_write}"
                    + design_scope_note(design_plan_content)
                )

                # Reuse this attempt's pre-Codex scan instead of a second
                # full-workspace walk: the only content that changed since is
                # the file just written, and that entry is overridden right
                # here. (Sibling files being written concurrently were never
                # a reliable part of this context — the scan raced them.)
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
                test_code_to_write = extract_code(
                    tester_tests_raw, filename=test_file_path
                )

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
                await message_bus.publish_async(tester_art)

                security_art = Artifact(
                    name=f"audit_{file_path}",
                    content=security_report,
                    created_by="security",
                    parent_id=codex_art_id,
                )
                await message_bus.publish_async(security_art)

                # 4. Write debug/log files to disk
                try:
                    _write_text(test_file_path, test_code_to_write)
                    logger.info(f"Wrote generated tests to {test_file_path}")
                except Exception as e:
                    raise PipelineError(
                        f"Failed to write test code to {test_file_path}: {e}"
                    )

                try:
                    _write_text(audit_log_path, security_report)
                    logger.info(f"Wrote security audit report to {audit_log_path}")
                except Exception as e:
                    raise PipelineError(
                        f"Failed to write security audit to {audit_log_path}: {e}"
                    )

            # 6. Run pytest projects/[project_name]/tests/test_[file_name].py
            # (nothing to execute for non-Python files — their verification is
            # the security audit alone — nor for pytest infrastructure outside
            # tests/ (root conftest.py, package __init__.py): no test module
            # was generated for them, so running pytest on that never-written
            # path would exit 4 and doom the self-heal loop).
            if not file_is_python or (not file_is_test and is_pytest_infra(file_path)):
                pytest_exit_code = 0
                test_failures_logs = (
                    "(pytest skipped: not a Python file)"
                    if not file_is_python
                    else "(pytest skipped: pytest infrastructure file)"
                )
            else:
                pytest_cmd = [
                    verification_python(project_dir),
                    "-m",
                    "pytest",
                    test_file_path,
                ]
                logger.info(f"Running pytest command: {' '.join(pytest_cmd)}")

                try:
                    env = os.environ.copy()
                    project_src_dir = os.path.join(project_dir, "src")
                    env["PYTHONPATH"] = os.path.pathsep.join(
                        [project_dir, project_src_dir, env.get("PYTHONPATH", "")]
                    ).strip(os.path.pathsep)
                    env["PYTHONDONTWRITEBYTECODE"] = "1"

                    process = await asyncio.create_subprocess_exec(
                        *pytest_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                        # cwd = the project root: generated tests live OUTSIDE
                        # the deliverable (pipeline_internal_dir), so cwd- or
                        # __file__-relative lookups of product files (a real
                        # test hunted README.md via its own parents) only work
                        # from the project directory.
                        cwd=project_dir,
                    )
                    # Bounded: a generated test with an infinite loop or a
                    # blocking call would otherwise hang this file's task (and
                    # the whole asyncio.gather fan-out) forever. On timeout the
                    # process tree is killed and CLITimeoutError is caught below.
                    stdout, stderr = await communicate_with_timeout(
                        process, timeout=test_timeout(), cli_name="pytest"
                    )
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
                _write_text(test_log_path, test_failures_logs)
            except Exception as e:
                logger.warning(f"Failed to write test log to {test_log_path}: {e}")

            # 7. Check if tests passed (return code 0) and security audit has no vulnerabilities.
            # An empty/whitespace security report means the audit stage produced no
            # output (crash, truncation, stripped result) — fail closed rather than
            # treating an absent audit as "clean". Test modules are exempt:
            # their audit is skipped by design.
            # Exit 5 = "no tests were collected". For a directly-executed test
            # module that is a support file (tests/__init__.py, conftest.py) or
            # simply defines no test functions, that is NOT a failure — pytest
            # can never make such a file exit 0, so treating 5 as failure loops
            # the self-heal until it gives up and fails the whole pipeline (a
            # DesignPlan listing tests/__init__.py used to guarantee that). Only
            # relaxed for designer-provided test modules run directly; a
            # generated test_<file>.py returning 5 still means the Tester failed.
            PYTEST_NO_TESTS_COLLECTED = 5
            tests_passed = pytest_exit_code == 0 or (
                file_is_test and pytest_exit_code == PYTEST_NO_TESTS_COLLECTED
            )
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
    flow: str = "sequential",
    review_url: str = None,
):
    """Execute the sequential pipeline (Research -> Claude -> Codex -> Tester -> Security -> DevOps).

    ``stage_gate`` is an optional ``async def gate(stage: str)`` awaited after
    the research, design and code stages complete; it may pause (human
    approval) or raise to abort. None (the default) runs straight through.
    The custom flow adds two more gates — "review" (after the final review)
    and "devops" (after deploy) — so its full order is research -> design ->
    code -> review -> devops.

    ``flow`` selects the pipeline variant: ``"sequential"`` (default, the path
    above) or ``"custom"`` — the opt-in user-tailored flow (plan-first, codex
    debate, Claude-diagnosed self-heal, final review, per-stage gates). Every
    custom-only behaviour lives behind ``if flow == "custom":`` so the default
    branch is byte-identical to before.
    """
    _DISTRIBUTED_MODE_VAR.set(distributed)

    project_name, workspace, max_debate_rounds, cleaned_prompt, effort = (
        _resolve_pipeline_setup(prompt, workspace, max_debate_rounds)
    )
    # Carry the @deep effort to every stage's provider via a per-task contextvar
    # (read by call_api). Set AFTER the unpack that defines `effort`.
    _PIPELINE_EFFORT_VAR.set(effort)
    if effort:
        logger.info(
            "Pipeline reasoning effort '%s' (from @deep) applies on the call "
            "stack to the codex/claude stages; the researcher primary provider "
            "agy has no effort flag, so it is a no-op there unless agy fails "
            "over to claude/codex.",
            effort,
        )

    project_dir = os.path.join(workspace, "projects", project_name)
    # The project directory IS the deliverable: only designed files land in
    # it (their parent dirs are created per-write). Pipeline internals —
    # logs, raw traces, the message-bus DB, generated test modules — live
    # under pipeline_internal_dir(); artifact copies stay at the workspace
    # root only. Empty src/tests/logs/docker dirs used to pollute handover.
    os.makedirs(project_dir, exist_ok=True)
    os.makedirs(
        os.path.join(pipeline_internal_dir(project_dir), "logs"), exist_ok=True
    )

    # Resolve absolute paths for context sharing files under workspace (root of the workspace directory) by default
    research_file = os.path.join(workspace, "research.md")
    design_file = os.path.join(workspace, "design.md")
    app_file = os.path.join(workspace, "app.py")
    review_file = os.path.join(workspace, "review.md")
    test_generated_file = os.path.join(workspace, "test_generated.py")
    audit_file = os.path.join(workspace, "audit.md")
    deploy_file = os.path.join(workspace, "deploy.md")

    # Intercept direct slash command routing before cleaning up all files.
    # Detect on the @modifier-stripped prompt so `@deep /code ...` still routes
    # to the single /code stage; the RAW `prompt` is still what agents receive
    # (they re-parse @modifiers). `effort` (from @deep) is threaded on the call
    # stack to every stage below.
    first_word = (
        cleaned_prompt.strip().split()[0] if cleaned_prompt.strip() else ""
    )
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
            # Hackathon-mode artifacts (workspace root only — never written
            # into the project dir, so no project-dir twins to clean).
            os.path.join(workspace, "pitch.md"),
            os.path.join(workspace, "ai_collaboration_log.md"),
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
        scanned_files = await asyncio.to_thread(scanner.scan)
        # Graph-aware budgeting (aider-repo-map style): rank files by the
        # intra-repo import/reference graph, seeded by paths the prompt
        # mentions, and trim to GENIUS_CONTEXT_TOKEN_BUDGET. Under the
        # budget this is an identity passthrough.
        scanned_files = await asyncio.to_thread(
            build_budgeted_context, scanned_files, None, prompt
        )
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
                _write_text(output_file, result)
                proj_output_file = os.path.join(project_dir, output_name)
                os.makedirs(project_dir, exist_ok=True)
                _write_text(proj_output_file, result)
            except Exception as e:
                raise PipelineError(
                    f"Failed to write agent output to {output_file}: {e}"
                )

            validate_file(output_file, agent_key, is_input=False)
            logger.info(
                f"Step '{agent_key}' completed successfully via routing. Output: {output_file}"
            )
            await log_conversation_async(prompt, result)
            return result

        # Step 1: Research (agy/claude/codex fallback chain) - Call API
        logger.info("--- Running Step: Research ---")
        from ag_core.utils.message_bus import MessageBus, Artifact

        message_bus = MessageBus(
            db_path=os.path.join(
                pipeline_internal_dir(project_dir), "logs", "message_bus.db"
            )
        )

        # CUSTOM plan-first: Claude drafts the plan from the RAW prompt BEFORE
        # research, so research is informed by the plan (user's flow: plan ->
        # research -> debate). DEFAULT: research first, then Claude designs from
        # it below. claude_content stays None in the default path until then.
        plan_first = flow == "custom"
        claude_content = None
        if plan_first:
            logger.info("--- Custom flow: Claude drafts the plan first ---")
            # HACKATHON (opt-in): shape the DesignPlan CONTENT (AI-native
            # architecture / UX / safety + deploy files) without touching the
            # single-```json``` output contract. Local bind; `prompt` itself
            # stays untouched.
            plan_prompt = prompt
            if hackathon_mode_enabled():
                plan_prompt = prompt + HACKATHON_DESIGN_GUIDANCE
            claude_content = await call_api(
                claude_url,
                api_key,
                plan_prompt,
                context=scanned_files,
                client=client,
                poll_timeout=poll_timeout,
            )

        # Default: research context is scanned_files verbatim (byte-identical).
        # Custom: add the draft plan so research is informed by it.
        research_ctx = scanned_files
        if plan_first and claude_content is not None:
            research_ctx = dict(scanned_files)
            research_ctx["plan.md"] = claude_content
        # HACKATHON (opt-in, custom only): steer the research brief toward the
        # submission-critical sections (users/pain, viability, differentiation,
        # AI-native opportunity). Local bind — the RAW `prompt` every other
        # stage receives stays untouched.
        research_prompt = prompt
        if flow == "custom" and hackathon_mode_enabled():
            research_prompt = prompt + HACKATHON_RESEARCH_GUIDANCE
        try:
            research_content = await call_api(
                researcher_url,
                api_key,
                research_prompt,
                context=research_ctx,
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
        research_art_id = await message_bus.publish_async(
            Artifact(
                name="research_data", content=research_content, created_by="researcher"
            )
        )

        try:
            # Workspace-root copy only: the project directory is the
            # deliverable and must not carry pipeline artifacts.
            _write_text(research_file, research_content)
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

        # DEFAULT: Claude designs FROM the research. CUSTOM: claude_content was
        # already drafted plan-first, so the debate below (now with research
        # available) refines it instead of a second full design call.
        if not plan_first:
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
                _write_text(design_file, content)
            except Exception as e:
                logger.warning(f"Failed to write Claude debug output: {e}")

        # Write the initial design BEFORE the debate: if a debate round fails,
        # the already-produced (and paid-for) design is safe on disk.
        _write_design_files(claude_content)

        # Multi-Agent Debate Refinement. DEFAULT critic = researcher role;
        # CUSTOM critic = codex (user's flow: "codex debates the plan"). The
        # refiner stays Claude ("route back to Claude to revise") in both.
        #
        # CUSTOM debate critic + final review default to the codex-role service
        # (codex_url). When the codex role is repurposed (e.g. as the gemini
        # coder), route them to a codex-gpt5.6-sol service instead: pass
        # review_url, or set GENIUS_REVIEW_ROLE to a role name whose service
        # runs that model (e.g. "security"). Unset => codex_url, so the default
        # custom flow stays byte-identical.
        _review_url = review_url
        if _review_url is None:
            _review_url = {
                "codex": codex_url,
                "security": security_url,
                "tester": tester_url,
                "devops": devops_url,
                "researcher": researcher_url,
                "claude": claude_url,
            }.get(os.getenv("GENIUS_REVIEW_ROLE", "").strip().lower())
        review_target_url = _review_url or codex_url
        critic_url = review_target_url if flow == "custom" else researcher_url
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
                    + _CRITIC_QUALITY_CHECKLIST
                    + "Provide constructive criticism and suggest concrete improvements. If the draft architecture plan is correct, complete, and needs no further improvements, include `[APPROVED]` in your response.\n\n"
                    f"Draft Architecture Plan:\n{claude_content}\n\n"
                    f"Original Research and Context:\n{claude_prompt}"
                )
                # A debate-round failure must not lose the design Claude
                # already produced: design.md is written before the debate, so
                # in strict mode the error propagates with the design safely
                # on disk, and in degraded mode the debate simply stops here.
                try:
                    # Empty context: the draft plan and original research are
                    # already inlined in the prompt; re-sending the whole
                    # scanned workspace every round is pure token burn (the
                    # MCP debate has always passed {} for the same reason).
                    critic_content = await call_api(
                        critic_url,
                        api_key,
                        critic_prompt,
                        context={},
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
                # HACKATHON (opt-in, custom only): keep the design guidance in
                # view during refinement so a debate round cannot launder the
                # AI-native/UX/safety content out of the plan.
                if flow == "custom" and hackathon_mode_enabled():
                    claude_refine_prompt += HACKATHON_DESIGN_GUIDANCE
                try:
                    claude_content = await call_api(
                        claude_url,
                        api_key,
                        claude_refine_prompt,
                        context={},
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
        claude_art_id = await message_bus.publish_async(
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
                claude_art_id = await message_bus.publish_async(
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

        # Deterministic design lint (production only, same convention as the
        # format self-heal): catch plan defects no LLM check reliably does —
        # duplicate paths, empty specifications, escaping paths — BEFORE any
        # coder tokens are spent. One corrective re-prompt, then fail loudly.
        if files_to_implement and design_selfheal_enabled():
            lint_blocking, lint_warnings = lint_design_plan(
                files_to_implement, claude_content
            )
            for warning in lint_warnings:
                logger.warning(f"Design lint: {warning}")
            if lint_blocking:
                logger.warning(
                    "Design lint found blocking issues; re-prompting the "
                    f"architect once: {'; '.join(lint_blocking)}"
                )
                lint_retry_prompt = (
                    "Your design parsed as a DesignPlan but failed these "
                    "deterministic checks:\n- "
                    + "\n- ".join(lint_blocking)
                    + "\n\nFix ONLY these issues, keeping everything else "
                    "unchanged. Respond with EXACTLY ONE ```json fenced block "
                    "conforming to the DesignPlan schema and NOTHING else.\n\n"
                    f"Original design request:\n{claude_prompt}\n\n"
                    f"Your previous response:\n{truncate_log(claude_content)}"
                )
                retry_content = await call_api(
                    claude_url,
                    api_key,
                    lint_retry_prompt,
                    context=scanned_files,
                    client=client,
                    poll_timeout=poll_timeout,
                )
                save_raw_response(project_dir, "design_lint_retry1", retry_content)
                retry_files = parse_design_for_files(retry_content)
                if retry_files:
                    lint_blocking, retry_warnings = lint_design_plan(
                        retry_files, retry_content
                    )
                    for warning in retry_warnings:
                        logger.warning(f"Design lint: {warning}")
                    if not lint_blocking:
                        claude_content = retry_content
                        files_to_implement = retry_files
                        _write_design_files(claude_content)
                if lint_blocking:
                    raise PipelineError(
                        "Design failed deterministic lint after a corrective "
                        f"retry: {'; '.join(lint_blocking)}. See design.md and "
                        "logs/raw/ for the raw architect output."
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
            # Basenames of every designed file, so generated test modules can
            # dodge collisions with designed ones (generated_test_path).
            fanout_designed_basenames = designed_basenames_of(files_to_implement)

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
                        flow=flow,
                        claude_url=claude_url,
                        designed_basenames=fanout_designed_basenames,
                    )
                    status_dict[file_path] = "completed"
                    update_progress_md(status_dict)
                    return file_path, result, None
                except Exception as e:
                    logger.error(f"Failed to process {file_path}: {e}")
                    status_dict[file_path] = "failed"
                    update_progress_md(status_dict)
                    return file_path, None, e

            # Two-phase fan-out: DESIGNED TEST MODULES run in a second wave,
            # only after every implementation file is FINAL. They execute
            # directly against the real modules, so racing a mid-rewrite
            # target burns their whole retry budget on stale code — a real
            # run failed exactly this way (the designed test exhausted 3
            # attempts against in-flight wordfreq.py versions; the final
            # implementation was correct and would have passed).
            # With GENIUS_AUTO_INSTALL there is an extra wave 0 up front:
            # root requirements*.txt manifests are implemented first, then
            # pip-installed into the isolated venv, so the implementation
            # wave's pytest runs can already import the declared deps.
            manifest_wave, impl_wave, test_wave = partition_fanout_waves(
                files_to_implement
            )
            results = []
            if manifest_wave:
                results.extend(
                    await asyncio.gather(*[handle_file(f) for f in manifest_wave])
                )
                await auto_install_requirements(
                    project_dir, [str(f.get("path")) for f in manifest_wave]
                )
            results.extend(
                await asyncio.gather(*[handle_file(f) for f in impl_wave])
            )
            impl_wave_failed = [fp for fp, _, err in results if err is not None]
            skipped_test_paths = set()
            if test_wave and impl_wave_failed and not degraded_mode():
                # The job is already doomed (strict mode fails on any file):
                # designed tests exercise those very implementations, so
                # running them would only burn their whole retry budget —
                # worst case 3 x 300s timeouts each — against known-broken
                # code. A live run spent 15 extra minutes exactly this way.
                logger.warning(
                    "Skipping the designed-test wave (%s): the implementation "
                    "wave already failed for %s.",
                    ", ".join(str(f.get("path")) for f in test_wave),
                    ", ".join(impl_wave_failed),
                )
                for f in test_wave:
                    status_dict[f["path"]] = "failed"
                    skipped_test_paths.add(f["path"])
                    results.append(
                        (
                            f["path"],
                            None,
                            PipelineError(
                                "skipped: implementation wave failed"
                            ),
                        )
                    )
                update_progress_md(status_dict)
            else:
                results.extend(
                    await asyncio.gather(*[handle_file(f) for f in test_wave])
                )

            for file_path, result, err in results:
                if err is not None:
                    failed_files.append(file_path)
                else:
                    aggregated_audits.append(f"### Audit for {file_path}\n\n{result}")

            all_failed = len(failed_files) == len(files_to_implement)
            if failed_files and not (degraded_mode() and not all_failed):
                hint = ""
                # Only a designed test that actually RAN and failed implicates
                # its implementation; wave-skipped tests are collateral of an
                # implementation failure that is already listed.
                if any(
                    is_test_module(f)
                    for f in failed_files
                    if f not in skipped_test_paths
                ):
                    hint = (
                        " A DESIGNED test module that fails after the "
                        "implementation wave usually means an implementation "
                        "it exercises violates the spec (and its own "
                        "generated tests were too weak to catch it) — see "
                        "the internal logs/test_<module>.log."
                    )
                raise PipelineError(
                    f"Self-healing loop failed to implement and verify files: "
                    f"{', '.join(failed_files)}.{hint}"
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

            # HONEST COVERAGE: "verified" above means each file passed ITS
            # verification mode — for non-Python files that is the security
            # audit alone (this pipeline has no JS/TS test runner). Spell the
            # modes out so "completed" can never be read as "executed".
            _mode_groups = {}
            for _f in files_to_implement:
                _rel = str(_f.get("path", "") or "")
                if not _rel:
                    continue
                _mode_groups.setdefault(verification_mode(_rel), []).append(_rel)
            if _mode_groups:
                review_content += "\n\n## Verification coverage\n" + "\n".join(
                    f"- {_mode}: {len(_paths)} file(s) — {', '.join(sorted(_paths))}"
                    for _mode, _paths in sorted(_mode_groups.items())
                )
                if any(
                    _mode.startswith("audit-only (non-Python")
                    for _mode in _mode_groups
                ):
                    review_content += (
                        "\nWARNING: non-Python files were NOT executed by any "
                        "test runner — their verification is the security "
                        "audit alone."
                    )

            # Quality ladder per file (proposal #2): completed ≠ shippable.
            _failed_set = set(failed_files)
            _state_lines = [
                f"- {str(_f.get('path'))}: "
                f"{file_quality_state(str(_f.get('path')), str(_f.get('path')) in _failed_set)}"
                for _f in files_to_implement
                if _f.get("path")
            ]
            if _state_lines:
                review_content += "\n\n## File quality states\n" + "\n".join(
                    sorted(_state_lines)
                )

            # CUSTOM: whole-project verification. The per-file loops execute
            # each test module in isolation, so they can never see cross-file
            # conflicts (a real job completed while bare `pytest` at its root
            # failed collection on duplicate test basenames). Run pytest ONCE
            # from the project root + report design-vs-disk conformance; both
            # sections land in review.md and feed the final reviewer. A
            # failing suite FAILS the job below (full_suite_gate_enabled,
            # default strict).
            full_suite_failed = False
            full_suite_rc = None
            if flow == "custom":
                conformance = _design_conformance_report(
                    project_dir, files_to_implement
                )
                review_content += (
                    "\n\n## File conformance (design vs disk)\n" + conformance
                )
                if not failed_files:
                    full_suite_rc, full_suite_log = await _run_project_pytest(
                        project_dir
                    )
                    # Exit 5 = "no tests collected": valid for docs-only
                    # projects, not a failure.
                    full_suite_failed = full_suite_rc not in (0, 5)
                    logger.info(
                        "Whole-project pytest exit code: %s", full_suite_rc
                    )
                    review_content += (
                        "\n\n## Whole-project pytest\n"
                        f"exit code: {full_suite_rc}"
                        f"\n\n```\n{full_suite_log.strip()}\n```"
                    )
                    if full_suite_rc == 5:
                        review_content += (
                            "\n(exit 5 = pytest collected NO tests: for a "
                            "non-Python project this gate verifies nothing — "
                            "the product's own test suite was not run)"
                        )
                    # Stack-aware project gates (opt-in GENIUS_PROJECT_GATE):
                    # run the product's OWN install/test/lint/build from the
                    # project root and fail the job through the same strict
                    # gate — pytest alone proves nothing about a JS/TS stack.
                    if project_gate_enabled():
                        gates_failed, gates_section = await _run_project_gates(
                            project_dir
                        )
                        if gates_section:
                            review_content += (
                                "\n\n## Project gates (stack-aware)\n"
                                + gates_section
                            )
                        if gates_failed:
                            full_suite_failed = True
                            if full_suite_rc in (0, 5):
                                full_suite_rc = "project-gates"

            # Job-level release verdict (proposal #2): "completed" alone must
            # not read as shippable. Strict gates make a surviving job
            # release-ready by construction; degraded / report-only runs get
            # an honest NO. On the custom flow the final review can still
            # veto AFTER this write — a blocking verdict fails the job, so a
            # surviving YES stays truthful.
            if failed_files or full_suite_failed:
                _why = []
                if failed_files:
                    _why.append(f"{len(failed_files)} file(s) failed verification")
                if full_suite_failed:
                    _why.append("whole-project gates failed")
                review_content += (
                    "\n\n## Release readiness\nrelease-ready: NO — "
                    + "; ".join(_why)
                )
            else:
                _caveat = (
                    " (final review still pending below)"
                    if flow == "custom" and review_target_url
                    else ""
                )
                review_content += (
                    "\n\n## Release readiness\nrelease-ready: YES — all "
                    "designed files passed their verification modes and the "
                    f"whole-project gates raised no failures{_caveat}"
                )

            review_art_id = await message_bus.publish_async(
                Artifact(
                    name="review_data",
                    content=review_content,
                    created_by="codex",
                    parent_id=claude_art_id,
                )
            )
            try:
                _write_text(review_file, review_content)
            except Exception as e:
                logger.warning(f"Failed to write review.md: {e}")

            # Aggregate audit report — written BEFORE the full-suite gate can
            # raise: a real gate failure used to leave the job with review.md
            # but NO audit.md at all, silently discarding per-file audits
            # that had all passed (they survived only in internal logs).
            consolidated_audit = (
                "\n\n---\n\n".join(aggregated_audits)
                if aggregated_audits
                else "Consolidated project implementation and testing passed."
            )
            consolidated_art_id = await message_bus.publish_async(
                Artifact(
                    name="consolidated_audit",
                    content=consolidated_audit,
                    created_by="security",
                    parent_id=review_art_id,
                )
            )
            try:
                _write_text(audit_file, consolidated_audit)
            except Exception as e:
                logger.warning(f"Failed to write audit.md: {e}")

            if full_suite_failed and full_suite_gate_enabled():
                _gate_desc = (
                    "The project's own gates (npm install/test/lint/build) failed"
                    if full_suite_rc == "project-gates"
                    else f"Whole-project pytest failed (exit {full_suite_rc})"
                )
                raise PipelineError(
                    f"{_gate_desc} after "
                    "per-file verification — a cross-file conflict or "
                    "integration failure the per-file loops cannot see; the "
                    "log is recorded in review.md. Set GENIUS_FULL_SUITE_GATE=0 "
                    "to demote this gate to report-only."
                )

            if stage_gate is not None:
                await stage_gate("code")

            # CUSTOM: a final reviewer (codex/gpt-5.6 per config) audits the
            # implemented project against its design + audit, AFTER the code
            # gate; if it does not emit [APPROVED], Claude analyses the review
            # and prescribes fixes, recorded in review.md (user's flow:
            # "review -> route issues back to Claude"). An explicitly BLOCKING
            # verdict then FAILS the pipeline (final_review_strict, default
            # strict) so a job with known-bad output can never report
            # completed; the automatic re-code loop is still deferred — the
            # fix plan is the input for the operator/next run.
            if flow == "custom" and review_target_url:
                # Gather the ACTUAL implemented files so the reviewer audits real
                # code, not just design+audit. Without this the reviewer never
                # sees the sources and falsely reports files "missing"/"empty".
                _code_sections = []
                for _f in files_to_implement:
                    _rel = _f.get("path")
                    if not _rel:
                        continue
                    try:
                        with open(
                            os.path.join(project_dir, _rel),
                            "r",
                            encoding="utf-8",
                        ) as _fh:
                            _src = _fh.read()
                    except OSError:
                        _code_sections.append(
                            f"### {_rel}\n(file not found on disk)"
                        )
                        continue
                    if len(_src) > 8000:
                        _src = _src[:8000] + "\n... (truncated)"
                    _code_sections.append(f"### {_rel}\n```\n{_src}\n```")
                implemented_code = (
                    "\n\n".join(_code_sections)
                    if _code_sections
                    else "(no files were implemented)"
                )
                review_blocking = False

                def _record_final_review(suffix: str) -> None:
                    # The workspace-root review.md is the single canonical
                    # copy (what the genius:// artifact serves); the project
                    # directory is the deliverable and carries no artifacts.
                    try:
                        _write_text(review_file, review_content + suffix)
                    except Exception:
                        pass

                try:
                    final_review = await call_api(
                        review_target_url,
                        api_key,
                        "/security Review the implemented project against its "
                        "design and audit. If it is correct and complete, "
                        "include [APPROVED]. Otherwise list concrete issues.\n\n"
                        f"Design:\n{claude_content}\n\n"
                        f"Implemented code:\n{implemented_code}\n\n"
                        "Verification summary (per-file self-heal, file "
                        "conformance, whole-project pytest):\n"
                        f"{review_content}\n\n"
                        f"Consolidated audit:\n{consolidated_audit}",
                        context={},
                        client=client,
                        poll_timeout=poll_timeout,
                    )
                    save_raw_response(project_dir, "final_review", final_review)
                    _verdict = parse_security_verdict(final_review)
                    if "[APPROVED]" in final_review:
                        # Record the approval too: review.md must be able to
                        # distinguish "final review approved" from "final
                        # review never ran / crashed".
                        _record_final_review(
                            "\n\n## Final review (approved)\n" + final_review
                        )
                        logger.info("Custom final review approved the implementation.")
                    elif _verdict is not None and not _verdict.get("blocking", True):
                        # A non-blocking verdict (e.g. codex-gpt5.6-sol via the
                        # security service) = approved with optional hardening
                        # notes. Record them but spend NO Claude fix plan (user's
                        # flow: route to Claude only on real, blocking issues).
                        _record_final_review(
                            "\n\n## Final review (non-blocking)\n" + final_review
                        )
                        logger.info(
                            "Custom final review returned a non-blocking verdict "
                            "(%d suggestion(s)); no Claude fix plan.",
                            len(_verdict.get("findings") or []),
                        )
                    elif claude_url:
                        # Only a PARSED blocking verdict arms the strict gate;
                        # unparseable non-approved reviews stay advisory.
                        review_blocking = _verdict is not None and bool(
                            _verdict.get("blocking")
                        )
                        # Record the review BEFORE the fix-plan call: if that
                        # call dies, the strict gate below still fails the job
                        # claiming the evidence "is recorded in review.md" — a
                        # crash here used to lose the entire final review.
                        _record_final_review("\n\n## Final review\n" + final_review)
                        fix_plan = await call_api(
                            claude_url,
                            api_key,
                            "A reviewer raised issues with the implementation. "
                            "Analyse the root cause and prescribe concrete "
                            f"fixes.\n\nReview:\n{final_review}",
                            context={},
                            client=client,
                            poll_timeout=poll_timeout,
                        )
                        save_raw_response(project_dir, "final_review_fix_plan", fix_plan)
                        _record_final_review(
                            "\n\n## Final review\n"
                            + final_review
                            + "\n\n## Claude fix plan\n"
                            + fix_plan
                        )
                        logger.info(
                            "Custom final review raised issues; Claude fix plan "
                            "recorded in review.md."
                        )
                    else:
                        # No Claude service to draft a fix plan: still record
                        # the review and arm the gate on a blocking verdict.
                        review_blocking = _verdict is not None and bool(
                            _verdict.get("blocking")
                        )
                        _record_final_review(
                            "\n\n## Final review\n" + final_review
                        )
                except Exception as e:
                    logger.warning("Custom final review failed (%s); continuing.", e)
                if review_blocking and final_review_strict():
                    raise PipelineError(
                        "Custom final review returned a BLOCKING verdict; the "
                        "Claude fix plan (when available) is recorded in "
                        "review.md. Set GENIUS_FINAL_REVIEW_STRICT=0 to demote "
                        "this gate to advisory."
                    )
                if stage_gate is not None:
                    await stage_gate("review")

            # Run DevOps deployment (Step 7)
            logger.info("--- Running Step: DevOps ---")
            validate_file(audit_file, "DevOps", is_input=True)

            # Retrieve from MessageBus
            audit_art = message_bus.retrieve(consolidated_art_id)
            devops_prompt = audit_art["content"] if audit_art else consolidated_audit
            # The design's file set is a HARD budget the deploy guidance must
            # respect: a live deploy.md proposed a fifth file
            # (requirements-ci.txt) for an "exactly four files" request —
            # anyone applying it would break the user's constraint.
            _designed_list = ", ".join(
                sorted(
                    str(f.get("path"))
                    for f in files_to_implement
                    if f.get("path")
                )
            )
            if _designed_list:
                devops_prompt += (
                    "\n\nFile budget (FIXED by the approved design): the "
                    f"project consists of EXACTLY these files — {_designed_list}. "
                    "Do NOT propose creating, renaming or adding any other "
                    "file; express deployment/CI improvements INSIDE the "
                    "existing files (e.g. pin versions directly in the "
                    "workflow command: pip install pytest==<version>)."
                )
            # HACKATHON (opt-in, custom only): the deploy plan must take a
            # judge to a LIVE public URL. Appended before the context copy
            # below so the devops agent sees one consistent view.
            if flow == "custom" and hackathon_mode_enabled():
                devops_prompt += HACKATHON_DEVOPS_GUIDANCE

            try:
                proj_scanner = ProjectScanner(
                    root_dir=project_dir, extra_ignores=config.scanner.exclude_patterns
                )
                current_context = await asyncio.to_thread(proj_scanner.scan)
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
            await message_bus.publish_async(
                Artifact(
                    name="devops_deploy",
                    content=devops_content,
                    created_by="devops",
                    parent_id=consolidated_art_id,
                )
            )
            try:
                _write_text(deploy_file, devops_content)
            except Exception as e:
                raise PipelineError(
                    f"Failed to write DevOps output to {deploy_file}: {e}"
                )
            validate_file(deploy_file, "DevOps", is_input=False)
            logger.info(
                f"Step 'DevOps' successfully completed. Output verified: {deploy_file}"
            )

            # CUSTOM: report the final deploy step for approval too, so the
            # custom flow gates after EVERY stage (research/design/code/review/
            # devops), not only the first three.
            if flow == "custom" and stage_gate is not None:
                await stage_gate("devops")

            # HACKATHON (opt-in, custom only): submission artifacts, both
            # best-effort — a failure logs and never fails a completed build.
            if flow == "custom" and hackathon_mode_enabled():
                await _emit_hackathon_artifacts(
                    workspace,
                    project_dir,
                    prompt,
                    claude_content,
                    review_file,
                    review_content,
                    consolidated_audit,
                    devops_content,
                    claude_url,
                    api_key,
                    client,
                    poll_timeout,
                )

            # Hand over ONLY the designed files: strip any runtime caches the
            # verification steps (or a designed conftest/plugin) left behind.
            sweep_runtime_caches(project_dir)

            logger.info(
                "Pipeline executed successfully and all files implemented, verified, and deployed."
            )
            await log_conversation_async(prompt, devops_content)
            await _maybe_run_eval_gate(project_dir, prompt)
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
                    _write_text(app_file, stdout_str)
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
        app_art_id = await message_bus.publish_async(
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
        codex_art_id = await message_bus.publish_async(
            Artifact(
                name="review_data",
                content=codex_content,
                created_by="codex",
                parent_id=app_art_id,
            )
        )

        os.makedirs(project_dir, exist_ok=True)
        try:
            _write_text(review_file, codex_content)
            proj_review_file = os.path.join(project_dir, "review.md")
            _write_text(proj_review_file, codex_content)
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
        await message_bus.publish_async(
            Artifact(
                name="test_code",
                content=tester_content,
                created_by="tester",
                parent_id=codex_art_id,
            )
        )
        await message_bus.publish_async(
            Artifact(
                name="security_audit",
                content=security_content,
                created_by="security",
                parent_id=codex_art_id,
            )
        )
        await message_bus.publish_async(
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
            _write_text(test_generated_file, tester_content)
            proj_test_file = os.path.join(project_dir, "test_generated.py")
            _write_text(proj_test_file, tester_content)
        except Exception as e:
            raise PipelineError(
                f"Failed to write Tester output to {test_generated_file}: {e}"
            )

        # Write Security outputs
        try:
            _write_text(audit_file, security_content)
            proj_audit_file = os.path.join(project_dir, "audit.md")
            _write_text(proj_audit_file, security_content)
        except Exception as e:
            raise PipelineError(f"Failed to write Security output to {audit_file}: {e}")

        # Write DevOps outputs
        try:
            _write_text(deploy_file, devops_content)
            proj_deploy_file = os.path.join(project_dir, "deploy.md")
            _write_text(proj_deploy_file, devops_content)
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
        await log_conversation_async(prompt, devops_content)
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

    project_name, workspace, max_debate_rounds, cleaned_prompt, effort = (
        _resolve_pipeline_setup(prompt, workspace, max_debate_rounds)
    )
    # Carry the @deep effort to every stage's provider via a per-task contextvar
    # (read by call_api). Set AFTER the unpack that defines `effort`.
    _PIPELINE_EFFORT_VAR.set(effort)
    if effort:
        logger.info(
            "Pipeline reasoning effort '%s' (from @deep) applies on the call "
            "stack to the codex/claude stages; the researcher primary provider "
            "agy has no effort flag, so it is a no-op there unless agy fails "
            "over to claude/codex.",
            effort,
        )

    project_dir = os.path.join(workspace, "projects", project_name)
    # Same deliverable rule as run_pipeline: only designed files in the
    # project dir; internals under pipeline_internal_dir().
    os.makedirs(project_dir, exist_ok=True)
    os.makedirs(
        os.path.join(pipeline_internal_dir(project_dir), "logs"), exist_ok=True
    )

    # Paths for context sharing files
    plan_file = os.path.join(workspace, "plan.md")
    # Stale project-dir copies from pre-separation runs are still cleaned.
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
        scanned_files = await asyncio.to_thread(scanner.scan)
        # Graph-aware budgeting (aider-repo-map style): rank files by the
        # intra-repo import/reference graph, seeded by paths the prompt
        # mentions, and trim to GENIUS_CONTEXT_TOKEN_BUDGET. Under the
        # budget this is an identity passthrough.
        scanned_files = await asyncio.to_thread(
            build_budgeted_context, scanned_files, None, prompt
        )
    except Exception as e:
        logger.warning(f"Failed to scan workspace: {e}")
        scanned_files = {}

    client = make_http_client()

    try:
        # Step 1: Claude (Architect) - Call API
        logger.info("--- Running E2E Step: Claude (Planning) ---")
        # Detect /plan on the @modifier-stripped prompt, but forward the RAW
        # prompt so the architect re-parses @deep etc. itself.
        claude_prompt = (
            prompt
            if cleaned_prompt.lstrip().startswith("/plan")
            else f"/plan {prompt}"
        )
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
                _write_text(plan_file, content)
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
                    + _CRITIC_QUALITY_CHECKLIST
                    + "Provide constructive criticism and suggest concrete improvements. If the draft plan is correct, complete, and needs no further improvements, include `[APPROVED]` in your response.\n\n"
                    f"Draft Plan:\n{claude_content}\n\n"
                    f"Original Prompt:\n{prompt}"
                )
                # Mirror of the sequential pipeline: a debate failure must not
                # lose the plan already written to disk before the debate.
                try:
                    # Empty context: the draft plan and original research are
                    # already inlined in the prompt; re-sending the whole
                    # scanned workspace every round is pure token burn (the
                    # MCP debate has always passed {} for the same reason).
                    critic_content = await call_api(
                        researcher_url,
                        api_key,
                        critic_prompt,
                        context={},
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
                        context={},
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
            # A plan with no parseable files means nothing gets implemented.
            # Returning the plan text as a "successful" result silently exits 0
            # having built nothing (a prose answer from the architect did exactly
            # that in a real run). Fail loudly in production; keep the legacy
            # return under pytest, where design self-heal is off and fixtures
            # rely on it (same convention as run_pipeline).
            logger.warning("No files parsed from plan.md. Nothing to implement.")
            if design_selfheal_enabled():
                raise PipelineError(
                    "E2E design produced no implementable files — the architect "
                    "response was not a parseable plan. Refine the prompt or "
                    "re-run."
                )
            return claude_content

        # Deterministic plan lint (production only). The e2e variant has no
        # format-retry loop, so blocking lint findings fail fast the same way
        # an unparseable plan does — before any coder tokens are spent.
        if design_selfheal_enabled():
            lint_blocking, lint_warnings = lint_design_plan(
                files_to_implement, claude_content
            )
            for warning in lint_warnings:
                logger.warning(f"Design lint: {warning}")
            if lint_blocking:
                raise PipelineError(
                    "E2E plan failed deterministic lint: "
                    f"{'; '.join(lint_blocking)}. See plan.md for the raw "
                    "architect output."
                )

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

                test_file_path = generated_test_path(
                    project_dir,
                    flat_name,
                    designed_basenames_of(files_to_implement),
                )
                os.makedirs(os.path.dirname(test_file_path), exist_ok=True)

                file_is_python = target_file_path.endswith(".py")

                # --- Codex Implementation & Self-healing loop ---
                codex_success = False
                codex_error_log = ""

                # One workspace scan per FILE, not per attempt: between
                # attempts of the same file nothing else changes on disk
                # (the attempt's own output is inlined in the retry prompt).
                try:
                    proj_scanner = ProjectScanner(
                        root_dir=project_dir,
                        extra_ignores=config.scanner.exclude_patterns,
                    )
                    current_context = await asyncio.to_thread(proj_scanner.scan)
                    current_context = await asyncio.to_thread(
                        build_budgeted_context,
                        current_context,
                        [file_path],
                        specification,
                    )
                except Exception:
                    current_context = {}

                for attempt in range(1, max_retries + 1):
                    logger.info(
                        f"Codex implementing {file_path} - Attempt {attempt}/{max_retries}"
                    )

                    codex_prompt = f"/code Implement the file '{file_path}' according to this specification:\n{specification}"
                    if not file_path.endswith(".py"):
                        codex_prompt += (
                            "\n\nThis target file is NOT a Python module. "
                            "Respond with ONLY the complete contents of "
                            f"'{file_path}' in a single {fence_hint(file_path)}; "
                            "this overrides any instruction to use a "
                            "```python block."
                        )
                    if attempt > 1:
                        codex_prompt += f"\n\nPrevious implementation attempt failed verification.\nErrors/Logs:\n{truncate_log(codex_error_log)}"
                        codex_prompt += (
                            "\n\nDo NOT run tests, commands, or tools. Output "
                            "ONLY the complete file content in a single "
                            f"{fence_hint(file_path)}."
                        )

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
                    code_content = extract_code(codex_raw, filename=file_path)

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
                        _write_text(target_file_path, code_content)
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
                    env["PYTHONDONTWRITEBYTECODE"] = "1"

                    # Check lint with flake8 — Python files only. A non-.py
                    # target (README.md, Dockerfile, JSON, ...) linted as Python
                    # yields a spurious E999 SyntaxError that fails every attempt
                    # and dooms the whole e2e run; its content is written as-is.
                    if file_is_python:
                        flake8_cmd = [
                            verification_python(project_dir),
                            "-m",
                            "flake8",
                            target_file_path,
                        ]
                        flake8_code, flake8_out = await run_subprocess(
                            flake8_cmd, env=env
                        )
                    else:
                        flake8_code, flake8_out = 0, ""
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
                    if file_is_python and os.path.exists(test_file_path):
                        pytest_cmd = [
                            verification_python(project_dir),
                            "-m",
                            "pytest",
                            test_file_path,
                        ]
                        pytest_code, pytest_out = await run_subprocess(
                            pytest_cmd, env=env, cwd=project_dir
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
                # Same guards as the sequential pipeline: no generated tests
                # for non-Python files (a pytest module "importing" README.md
                # can never pass, so its 3 doomed self-heal attempts fail the
                # whole run), for files that ARE pytest modules (no
                # tests-for-tests), or for pytest infrastructure
                # (conftest.py/__init__.py). Their implementation was already
                # verified above (flake8 + AST hygiene for Python targets).
                if (
                    not file_is_python
                    or is_test_module(file_path)
                    or is_pytest_infra(file_path)
                ):
                    logger.info(
                        f"{file_path}: skipping E2E test generation (non-Python "
                        "file, pytest module, or pytest infrastructure)."
                    )
                    status_dict[file_path] = "completed"
                    update_progress_md(status_dict)
                    return

                tester_success = False
                tester_error_log = ""

                # Once per file (see the Codex loop): the freshly implemented
                # file is the only delta and it is inlined in tester_prompt.
                try:
                    proj_scanner = ProjectScanner(
                        root_dir=project_dir,
                        extra_ignores=config.scanner.exclude_patterns,
                    )
                    current_context = await asyncio.to_thread(proj_scanner.scan)
                    current_context = await asyncio.to_thread(
                        build_budgeted_context, current_context, [file_path], ""
                    )
                except Exception:
                    current_context = {}

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
                    test_code_content = extract_code(
                        tester_raw, filename=test_file_path
                    )

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
                        _write_text(test_file_path, test_code_content)
                    except Exception as e:
                        raise PipelineError(
                            f"Failed to write test code to {test_file_path}: {e}"
                        )

                    # Run pytest on the generated test file (cwd = project
                    # root: the generated module lives outside the deliverable)
                    pytest_cmd = [
                        verification_python(project_dir),
                        "-m",
                        "pytest",
                        test_file_path,
                    ]
                    pytest_code, pytest_out = await run_subprocess(
                        pytest_cmd, env=env, cwd=project_dir
                    )

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

        # Same wave ordering as the sequential fan-out: designed test modules
        # only run once the implementations they exercise are final, and (with
        # GENIUS_AUTO_INSTALL) root requirements*.txt manifests run first so
        # the venv is provisioned before any implementation is verified.
        e2e_manifest_wave, e2e_impl_wave, e2e_test_wave = partition_fanout_waves(
            files_to_implement
        )
        if degraded_mode():
            results = []
            if e2e_manifest_wave:
                results.extend(
                    await asyncio.gather(
                        *[process_e2e_file(f) for f in e2e_manifest_wave],
                        return_exceptions=True,
                    )
                )
                await auto_install_requirements(
                    project_dir, [str(f.get("path")) for f in e2e_manifest_wave]
                )
            results.extend(
                await asyncio.gather(
                    *[process_e2e_file(f) for f in e2e_impl_wave],
                    return_exceptions=True,
                )
            )
            results.extend(
                await asyncio.gather(
                    *[process_e2e_file(f) for f in e2e_test_wave],
                    return_exceptions=True,
                )
            )
            paths = [
                f["path"] for f in e2e_manifest_wave + e2e_impl_wave + e2e_test_wave
            ]
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
            if e2e_manifest_wave:
                await gather_or_raise(
                    *[process_e2e_file(f) for f in e2e_manifest_wave]
                )
                await auto_install_requirements(
                    project_dir, [str(f.get("path")) for f in e2e_manifest_wave]
                )
            await gather_or_raise(*[process_e2e_file(f) for f in e2e_impl_wave])
            await gather_or_raise(*[process_e2e_file(f) for f in e2e_test_wave])

        # Same deliverable rule as run_pipeline: no runtime caches hand over.
        sweep_runtime_caches(project_dir)

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
        choices=["sequential", "e2e", "custom"],
        default="sequential",
        help="Pipeline type: sequential (default), e2e, or custom (opt-in "
        "user-tailored flow: plan-first, codex debate, Claude-diagnosed "
        "self-heal, final review, per-stage gates)",
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
    default_debate_rounds = 0 if under_pytest() else 2
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
                run_cli_journaled(
                    lambda: run_e2e_pipeline(
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
                    ),
                    workspace=args.workspace,
                    pipeline=args.pipeline,
                    prompt=args.prompt,
                )
            )
        else:
            asyncio.run(
                run_cli_journaled(
                    lambda: run_pipeline(
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
                        flow=(
                            "custom" if args.pipeline == "custom" else "sequential"
                        ),
                    ),
                    workspace=args.workspace,
                    pipeline=args.pipeline,
                    prompt=args.prompt,
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
    # mac branch: faster event loop for the CLI pipeline run (see serve.py).
    try:
        import uvloop

        uvloop.install()
    except ImportError:
        pass
    main()
