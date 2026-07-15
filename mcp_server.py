import os
import re
import sys
import json
import time
import uuid
import hmac
import shutil
import asyncio
import logging
import traceback
from typing import Any, Dict, List
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

# Add project root to sys.path
root_dir = os.path.dirname(os.path.abspath(__file__))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from ag_core import agent_factory
from ag_core.utils.prompt_templates import CRITIC_QUALITY_CHECKLIST

# The agent classes are resolved through module globals at call time (see
# execute_agent) so tests can patch e.g. ``mcp_server.DevOpsAgent`` - flake8
# cannot see that usage, hence the noqa markers.
from ag_core.agents.researcher import ResearcherAgent  # noqa: F401

# Legacy alias for older imports of mcp_server.GrokResearcherAgent (same
# class object; execute_agent resolves the canonical "ResearcherAgent" name).
GrokResearcherAgent = ResearcherAgent
from ag_core.agents.claude_architect import ClaudeArchitectAgent  # noqa: F401
from ag_core.agents.codex_reviewer import CodexReviewerAgent  # noqa: F401
from ag_core.agents.tester import TesterAgent  # noqa: F401
from ag_core.agents.security_agent import SecurityAgent  # noqa: F401
from ag_core.agents.devops_agent import DevOpsAgent  # noqa: F401

app = FastAPI(title="Genius MCP Server")


class CallToolRequest(BaseModel):
    name: str
    arguments: Dict[str, Any]


# tool name -> (provider-factory role, agent class *name*, default-chain
# override). Provider selection (incl. explicit GENIUS_PROVIDER_<ROLE> chains)
# lives in ag_core.provider_factory. The agent class is looked up through
# module globals at call time so tests can patch e.g. ``mcp_server.DevOpsAgent``.
# The MCP ``deploy`` tool has always built its devops agent on the claude
# backend (unlike the codex-first skill server / worker paths), so its default
# chain keeps that claude-first tradition.
TOOL_AGENTS = {
    "research": ("researcher", "ResearcherAgent", None),
    "design": ("claude", "ClaudeArchitectAgent", None),
    "code": ("codex", "CodexReviewerAgent", None),
    "unit_test": ("tester", "TesterAgent", None),
    "security_audit": ("security", "SecurityAgent", None),
    "deploy": ("devops", "DevOpsAgent", ["claude", "codex", "agy"]),
}


async def execute_agent(
    agent_name: str, prompt: str, context: Dict[str, str] = None
) -> str:
    if agent_name not in TOOL_AGENTS:
        raise ValueError(f"Unknown agent: {agent_name}")

    role, agent_cls_name, default_chain = TOOL_AGENTS[agent_name]
    # Shared factory applies the stateless bundle (output_file="None",
    # use_memory=False, stateless=True): the reviewer agent must NOT fall into
    # its flake8/pytest self-healing loop, which runs the host's test suite
    # and writes model-generated code into the server's working tree (a
    # remote-code-execution surface reachable from the MCP `review`/`code`
    # tools). The class still resolves through module globals so tests can
    # patch e.g. ``mcp_server.DevOpsAgent``.
    agent = agent_factory.build_agent(
        role,
        default_chain=default_chain,
        agent_cls=globals()[agent_cls_name],
    )

    if agent_name == "code":
        prompt = f"/code {prompt}"

    return await agent.run(prompt=prompt, context_data=context)


async def _run_doctor_report() -> str:
    """Render the preflight doctor report as text (no printing, no side
    effects). Mirrors ``serve.py --doctor`` / diagnostics.run_doctor_report_async."""
    # Imported lazily so the MCP server boots fast (the module probes are
    # cheap, but keep initialize/tools-list on the fast path).
    from ag_core import diagnostics

    results = await diagnostics.run_doctor_async()
    lines, _code = diagnostics.report_lines(
        results, skill_key_ok=bool(os.getenv("SKILL_API_KEY"))
    )
    return "\n".join(lines)


# Same convention as the orchestrator's critic<->architect debate loop: the
# critic signals "no further changes needed" by including this marker verbatim.
DEBATE_APPROVAL_MARKER = "[APPROVED]"
MAX_DEBATE_ROUNDS = 3


async def _run_debate(design: str, prompt: str, rounds: int) -> str:
    """In-process critic-critiques / Claude-refines exchange over a draft design.

    Both agents are built through execute_agent (make_provider role wiring, so
    per-role fallback chains apply): the critic uses the researcher role
    (agy/Gemini by default), the refiner the claude role. Early-exits when the
    critic answers with :data:`DEBATE_APPROVAL_MARKER`. Returns a JSON payload
    with the refined design and a per-round summary.
    """
    current = design
    approved = False
    rounds_summary: List[Dict[str, Any]] = []
    for round_idx in range(1, rounds + 1):
        critic_prompt = (
            "You are CriticReviewer, a critic agent. Analyze the following "
            "draft design proposed by Claude.\n"
            "Identify potential architectural flaws, security risks, missing "
            "requirements, or execution challenges.\n"
            + CRITIC_QUALITY_CHECKLIST
            + "Provide constructive criticism and suggest concrete improvements. "
            "If the draft design is correct, complete, and needs no further "
            f"improvements, include `{DEBATE_APPROVAL_MARKER}` in your "
            "response.\n\n"
            f"Draft Design:\n{current}\n\n"
            f"Original Prompt:\n{prompt}"
        )
        # Empty context dict: the design under debate is already in the
        # prompt; do not re-scan the whole workspace on every round.
        critique = await execute_agent("research", critic_prompt, {})
        if DEBATE_APPROVAL_MARKER in critique:
            approved = True
            rounds_summary.append(
                {"round": round_idx, "approved": True, "critique": critique}
            )
            break
        refine_prompt = (
            "You are Claude, the architect agent. Refine your draft design "
            "based on the constructive criticism from CriticReviewer.\n"
            "Address the identified issues and incorporate the suggested "
            "improvements, producing a final refined design.\n\n"
            f"Previous Draft Design:\n{current}\n\n"
            f"CriticReviewer's Criticism:\n{critique}\n\n"
            f"Original Prompt:\n{prompt}"
        )
        current = await execute_agent("design", refine_prompt, {})
        rounds_summary.append(
            {"round": round_idx, "approved": False, "critique": critique}
        )
    return json.dumps(
        {"design": current, "approved": approved, "rounds": rounds_summary}
    )


async def _run_review(code: str, instructions: str) -> str:
    """Review the given code with a codex-role agent; returns the review text.

    Built like execute_agent (same provider-factory wiring + patchable class
    lookup) but without the ``/code`` generation prefix that tool applies, and
    with the submitted code as the only context (no workspace scan, no file
    writes thanks to output_file="None").
    """
    role, agent_cls_name, default_chain = TOOL_AGENTS["code"]
    # Same stateless-bundle construction as execute_agent (see the comment
    # there); the class lookup stays patchable via module globals.
    agent = agent_factory.build_agent(
        role,
        default_chain=default_chain,
        agent_cls=globals()[agent_cls_name],
    )

    prompt = (
        "Perform a thorough code review of the following code. Identify bugs, "
        "security vulnerabilities, style issues, and concrete improvements."
    )
    if instructions:
        prompt += f"\nReviewer instructions: {instructions}"
    prompt += f"\n\nCode to review:\n```\n{code}\n```"
    return await agent.run(prompt=prompt, context_data={"<submitted code>": code})


from ag_core.mcp_tool_handlers import _run_code_graph, _run_eval  # noqa: F401
_NOTEBOOKLM_TOOLS = {"notebooklm_query", "notebooklm_list", "notebooklm_research"}


async def _run_notebooklm(name: str, arguments: Dict[str, Any]) -> str:
    """Dispatch a notebooklm_* tool to the shared ``nlm`` CLI helpers.

    Integrates NotebookLM into Genius workflows (query a curated notebook, or
    deep-research a topic into one). The ``nlm`` helpers are already async
    subprocess calls, so they never block the event loop. Failures from that
    layer (missing CLI, expired ``nlm login``, empty result) propagate as the
    tool's JSON-RPC / HTTP error through dispatch_tool's normal handling.
    ``notebooklm_research`` intentionally MUTATES: it creates a notebook and
    imports discovered sources - that is the tool's purpose, not a side effect.
    """
    from ag_core.providers import notebooklm_provider as nlm

    if name == "notebooklm_list":
        notebooks = await nlm.nlm_list_notebooks()
        return json.dumps({"count": len(notebooks), "notebooks": notebooks})

    if name == "notebooklm_query":
        notebook = (arguments.get("notebook") or "").strip()
        query = (arguments.get("query") or "").strip()
        if not notebook:
            raise ValueError("notebooklm_query requires a non-empty 'notebook'.")
        if not query:
            raise ValueError("notebooklm_query requires a non-empty 'query'.")
        data = await nlm.nlm_query(
            notebook,
            query,
            source_ids=(arguments.get("source_ids") or None),
            conversation_id=(arguments.get("conversation_id") or None),
        )
        return json.dumps(data)

    # notebooklm_research
    query = (arguments.get("query") or "").strip()
    if not query:
        raise ValueError("notebooklm_research requires a non-empty 'query'.")
    result = await nlm.nlm_research(
        query,
        mode=(arguments.get("mode") or "fast"),
        source=(arguments.get("source") or "web"),
        notebook=(arguments.get("notebook") or None),
        title=(arguments.get("title") or None),
        question=(arguments.get("question") or None),
    )
    return json.dumps(result)


from ag_core.mcp_tool_schemas import TOOLS  # noqa: E402,F401

from ag_core.mcp_resources import (  # noqa: F401
    RESOURCE_URI_PREFIX,
    _RESOURCE_ARTIFACTS,
    ResourceNotFoundError,
    _resource_catalog,
    _list_resources,
    _read_resource,
    _ARTIFACT_WORKSPACES,
    job_artifact_uri,
    register_job_workspace,
    set_job_workspace_resolver,
)
# --- Full-pipeline orchestration (the "điều phối viên" entrypoint) ---------
# The pipeline is long-running, so orchestrate launches it as a background job
# and returns a job_id; clients poll orchestrate_status for the result.
ORCHESTRATION_JOBS: Dict[str, Dict[str, Any]] = {}
# Strong refs to running pipeline tasks: asyncio only holds a weak ref, so a
# task with no strong reference can be GC'd and cancelled mid-run.
_ORCHESTRATION_TASKS: set = set()
# Cap retained finished jobs so a long-lived server doesn't accumulate their
# artifact strings without bound.
_MAX_FINISHED_JOBS = 200


def _prune_finished_jobs() -> None:
    finished = [
        (j.get("finished_at") or 0.0, jid)
        for jid, j in ORCHESTRATION_JOBS.items()
        if j.get("status") in ("completed", "failed")
    ]
    if len(finished) <= _MAX_FINISHED_JOBS:
        return
    finished.sort()  # oldest first
    for _, jid in finished[: len(finished) - _MAX_FINISHED_JOBS]:
        ORCHESTRATION_JOBS.pop(jid, None)


# Root-level artifact files produced by the pipeline, keyed by logical stage.
_ARTIFACT_FILES = {
    "research": "research.md",
    "design": "design.md",
    "code": "app.py",
    "review": "review.md",
    "tests": "test_generated.py",
    "security": "audit.md",
    "deploy": "deploy.md",
}


# Ordered (stage, artifact file) checkpoints per pipeline variant. The
# orchestrator does not expose progress callbacks, so orchestrate_status infers
# stage completion from these artifacts appearing on disk (fresh: mtime after
# the job started - stale pre-run copies are .bak-archived by the pipeline).
_PIPELINE_STAGES = {
    "sequential": [
        ("research", "research.md"),
        ("design", "design.md"),
        ("code", "review.md"),
        ("security_audit", "audit.md"),
        ("deploy", "deploy.md"),
    ],
    "e2e": [("plan", "plan.md")],
    # The custom flow adds a final-review checkpoint (appended into review.md
    # after the code stage + security audit) — mirrors its extra "review"
    # approval gate so awaiting_stage values always appear in `stages`.
    "custom": [
        ("research", "research.md"),
        ("design", "design.md"),
        ("code", "review.md"),
        ("security_audit", "audit.md"),
        ("review", "review.md"),
        ("deploy", "deploy.md"),
    ],
}

# Tolerance for coarse filesystem mtime granularity when comparing an
# artifact's mtime against the job start timestamp.
_MTIME_SLACK_SECONDS = 1.0


def _stage_progress(job: Dict[str, Any]):
    """Derive per-stage progress for a job from artifacts on disk.

    Returns (stages, artifacts_ready): the ordered stage states and the
    genius:// URIs that are already readable via resources/read.
    """
    workspace = job.get("workspace") or os.getcwd()
    started = job.get("started_at")
    stages = []
    ready = []
    checkpoints = _PIPELINE_STAGES.get(
        job.get("pipeline"), _PIPELINE_STAGES["sequential"]
    )
    for stage, fname in checkpoints:
        path = os.path.join(workspace, fname)
        done = os.path.isfile(path)
        if done and started is not None:
            try:
                done = os.path.getmtime(path) >= started - _MTIME_SLACK_SECONDS
            except OSError:
                done = False
        if done and stage == "review" and fname == "review.md":
            # Custom flow: the CODE stage writes review.md long before the
            # final review APPENDS its section — freshness alone flipped this
            # checkpoint done while the reviewer was still running (and kept
            # it done forever if the reviewer crashed). Every final-review
            # outcome (approved / non-blocking / blocking) now records a
            # "## Final review" section, so require that marker.
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    done = "## Final review" in fh.read()
            except OSError:
                done = False
        stages.append(
            {
                "stage": stage,
                "artifact": fname,
                "state": "done" if done else "pending",
            }
        )
        if done and fname in _RESOURCE_ARTIFACTS:
            # Advertise the JOB-SCOPED URI so resources/read resolves inside
            # THIS job's workspace — a bare name is ambiguous (a stale root
            # artifact or a concurrent job could shadow it). Advertising the
            # URI commits us to serving it: register the workspace mapping.
            job_id = str(job.get("job_id") or "")
            if _JOB_ID_RE.fullmatch(job_id):
                uri = job_artifact_uri(job_id, fname)
                register_job_workspace(job_id, workspace)
            else:
                # Synthetic/legacy ids (tests, hand-built views) keep the
                # bare-name form, served CWD-first as before.
                uri = RESOURCE_URI_PREFIX + fname
            if uri not in ready:
                ready.append(uri)
            # Keep the legacy bare-name fallback fresh for old clients.
            _ARTIFACT_WORKSPACES[fname] = workspace
    return stages, ready


def _collect_artifacts(workspace: str) -> Dict[str, str]:
    """Read back the pipeline's root-level output files that exist."""
    artifacts: Dict[str, str] = {}
    for key, fname in _ARTIFACT_FILES.items():
        path = os.path.join(workspace, fname)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    artifacts[key] = f.read()
            except OSError:
                pass
    return artifacts


# Cap for the design plan inlined into orchestrate_status while a job is
# paused at the design gate. Large enough for any real plan; the full file
# stays readable via the genius://artifacts resource.
_PLAN_INLINE_MAX_CHARS = 20000


def _inline_plan(job: Dict[str, Any]):
    """The job's current design.md content (capped) for inline display while
    paused at the design approval gate, so the client can show the plan to
    the user — and iterate on it via orchestrate_revise — without a
    resources/read round trip. None when the file is unreadable."""
    design_path = os.path.join(job.get("workspace") or "", "design.md")
    try:
        with open(design_path, "r", encoding="utf-8", errors="replace") as fh:
            plan = fh.read()
    except OSError:
        return None
    if len(plan) > _PLAN_INLINE_MAX_CHARS:
        plan = plan[:_PLAN_INLINE_MAX_CHARS] + (
            "\n\n...[plan truncated - read genius://artifacts/"
            f"{job.get('job_id', '')}/design.md for the full plan]"
        )
    return plan


def _approval_timeout() -> float:
    """Max seconds an awaiting_approval job waits before failing.

    Without a bound, a client that starts a require_approval job and never
    approves/rejects (or disconnects) leaves the pipeline task parked on the
    event forever — the one job class the finished-jobs cap cannot reclaim.
    GENIUS_APPROVAL_TIMEOUT overrides; blank/junk -> 3600.
    """
    try:
        val = float(os.environ.get("GENIUS_APPROVAL_TIMEOUT") or 3600.0)
        return val if val > 0 else 3600.0
    except (TypeError, ValueError):
        return 3600.0


_SKILL_ROLES = "researcher,claude,codex,tester,security,devops"
_skill_autostart_lock = None  # created lazily on the running loop


def _skill_health_urls():
    """Health URLs for the six skill servers (from config.services, else the
    frozen default ports)."""
    defaults = {
        "researcher": "http://localhost:8001",
        "claude_architect": "http://localhost:8002",
        "codex_reviewer": "http://localhost:8003",
        "tester_agent": "http://localhost:8004",
        "security_agent": "http://localhost:8005",
        "devops_agent": "http://localhost:8006",
    }
    services = {}
    try:
        from ag_core.config import load_config

        cfg = load_config()
        if isinstance(cfg, dict):
            services = cfg.get("services") or {}
    except Exception:
        pass
    urls = []
    for key, fallback in defaults.items():
        base = str(services.get(key) or fallback).rstrip("/")
        urls.append(base + "/health")
    return urls


async def _skill_servers_healthy(timeout: float = 2.0) -> bool:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for url in _skill_health_urls():
                resp = await client.get(url)
                if resp.status_code != 200:
                    return False
    except Exception:
        return False
    return True


async def _ensure_skill_servers(wait_seconds: float = 45.0) -> None:
    """Ensure the six skill servers are up — ``genius_orchestrate`` routes to
    them over HTTP. If any is down, spawn ``serve.py`` detached and wait for
    health. No-op under pytest and when GENIUS_ORCHESTRATE_AUTOSTART=0 (then you
    manage the servers). Raises with an actionable message if they never boot.
    """
    import subprocess

    from ag_core.runtime import under_pytest

    if under_pytest():
        return
    if os.environ.get("GENIUS_ORCHESTRATE_AUTOSTART", "1").strip().lower() in (
        "0",
        "false",
        "no",
    ):
        return
    if await _skill_servers_healthy():
        return

    global _skill_autostart_lock
    if _skill_autostart_lock is None:
        _skill_autostart_lock = asyncio.Lock()
    async with _skill_autostart_lock:
        # Another concurrent job may have started them while we waited.
        if await _skill_servers_healthy():
            return
        root = os.path.dirname(os.path.abspath(__file__))
        serve_py = os.path.join(root, "serve.py")
        log_path = os.path.join(_jobs_root(), "serve_autostart.log")
        try:
            os.makedirs(_jobs_root(), exist_ok=True)
            log_fh = open(log_path, "ab")
        except Exception:
            log_fh = subprocess.DEVNULL
        popen_kwargs = {}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
            )
        else:
            popen_kwargs["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, serve_py, "--roles", _SKILL_ROLES],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            env=os.environ.copy(),
            **popen_kwargs,
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        while loop.time() < deadline:
            await asyncio.sleep(2)
            if await _skill_servers_healthy():
                return
        raise RuntimeError(
            f"genius_orchestrate: skill servers did not come up within "
            f"{wait_seconds:.0f}s (see {log_path}). Start them manually with "
            f"`python serve.py --roles {_SKILL_ROLES}`, or set "
            f"GENIUS_ORCHESTRATE_AUTOSTART=0 to manage them yourself."
        )


# --- MCP progress notifications (proposal #3) --------------------------------
#
# Stage progress used to be pull-only (clients poll orchestrate_status every
# 15-20s). When the stdio transport is live, a per-job watcher additionally
# PUSHES each stage completion / status change as a spec-standard
# ``notifications/message`` log notification (logger "genius.orchestrate"),
# so a client that renders MCP log messages shows progress in real time.
# Pull polling still works unchanged — the watcher is purely additive.

# The live stdio ServerSession, captured on each SDK tool call (there is no
# public hook for "session started" on the lowlevel Server). None under HTTP
# mode and in tests -> every notify is a silent no-op.
_MCP_LOG_SESSION = None
# RFC-5424 order used by MCP logging setLevel.
_LOG_LEVELS = (
    "debug",
    "info",
    "notice",
    "warning",
    "error",
    "critical",
    "alert",
    "emergency",
)
_MCP_MIN_LOG_LEVEL = "info"


def _progress_poll_seconds() -> float:
    """How often the job watcher checks artifacts for stage completions.
    GENIUS_PROGRESS_POLL_SECONDS overrides; blank/junk -> 5s."""
    try:
        val = float(os.environ.get("GENIUS_PROGRESS_POLL_SECONDS") or 5.0)
        return val if val > 0 else 5.0
    except (TypeError, ValueError):
        return 5.0


async def _notify_progress(data: Dict[str, Any], level: str = "info") -> None:
    """Best-effort MCP log notification to the connected stdio client.

    No session (HTTP mode, tests) or a client-requested level above ours ->
    no-op; a transport error is swallowed — progress reporting must never
    break the pipeline."""
    session = _MCP_LOG_SESSION
    if session is None:
        return
    try:
        if _LOG_LEVELS.index(level) < _LOG_LEVELS.index(_MCP_MIN_LOG_LEVEL):
            return
    except ValueError:
        pass
    try:
        await session.send_log_message(level, data, logger="genius.orchestrate")
    except Exception:  # noqa: BLE001 - notification loss is acceptable
        pass


async def _watch_job_progress(job: Dict[str, Any]) -> None:
    """Push a notification for every stage completion and status change while
    the job is alive. Runs alongside ``_run_orchestration``; cancelled in its
    ``finally`` (which also emits the terminal status)."""
    seen_done = set()
    last_status = job.get("status")
    while job.get("status") in ("running", "awaiting_approval"):
        stages, _ready = _stage_progress(job)
        for s in stages:
            if s["state"] == "done" and s["stage"] not in seen_done:
                seen_done.add(s["stage"])
                await _notify_progress(
                    {
                        "event": "stage_done",
                        "job_id": job["job_id"],
                        "stage": s["stage"],
                        "artifact": s["artifact"],
                        "stages_done": sorted(seen_done),
                    }
                )
        status = job.get("status")
        if status != last_status:
            last_status = status
            payload = {
                "event": "status",
                "job_id": job["job_id"],
                "status": status,
            }
            if job.get("awaiting_stage"):
                payload["awaiting_stage"] = job["awaiting_stage"]
                if job["awaiting_stage"] == "design":
                    payload["hint"] = (
                        "Plan ready for review: call orchestrate_status to "
                        "read the 'plan' field and show it to the user, then "
                        "orchestrate_revise (feedback) to improve it or "
                        "orchestrate_approve to start coding."
                    )
            await _notify_progress(payload)
        await asyncio.sleep(_progress_poll_seconds())


async def _run_orchestration(
    job_id: str,
    prompt: str,
    pipeline: str,
    workspace: str = None,
    require_approval: bool = False,
    approval_stages=None,
) -> None:
    job = ORCHESTRATION_JOBS[job_id]
    watcher = asyncio.create_task(_watch_job_progress(job))
    try:
        # Imported lazily so the MCP server boots fast (initialize/tools-list
        # must respond before Antigravity's connect timeout).
        from orchestrator import run_pipeline, run_e2e_pipeline

        # The pipeline routes through the six FastAPI skill servers; bring them
        # up if the caller (e.g. Antigravity) hasn't started serve.py.
        await _ensure_skill_servers()

        stage_gate = None
        if require_approval:

            async def _approval_gate(stage: str):
                # Pause at a stage boundary until orchestrate_approve /
                # orchestrate_reject / orchestrate_revise flips the event.
                # Artifacts are collected first so the reviewer can read the
                # stage output while the job is paused.
                if approval_stages and stage not in approval_stages:
                    # The caller gated a subset (e.g. only "design" for the
                    # plan-review flow): other stages pass straight through.
                    return None
                job["awaiting_stage"] = stage
                job["status"] = "awaiting_approval"
                job["artifacts"] = _collect_artifacts(workspace or os.getcwd())
                job["approval_event"].clear()
                _journal_job(job)
                try:
                    await asyncio.wait_for(
                        job["approval_event"].wait(), timeout=_approval_timeout()
                    )
                except asyncio.TimeoutError:
                    job["awaiting_stage"] = None
                    raise RuntimeError(
                        f"Pipeline stopped: stage '{stage}' approval timed "
                        f"out after {_approval_timeout():.0f}s"
                    )
                # The approve/reject handler already flipped status and
                # cleared awaiting_stage (synchronously, so pollers never see
                # a stale pause state); only the rejection outcome is ours.
                if job.get("rejected"):
                    reason = job.get("reject_reason")
                    raise RuntimeError(
                        f"Pipeline stopped: stage '{stage}' was rejected"
                        + (f" ({reason})" if reason else "")
                    )
                # orchestrate_revise stashed reviewer feedback: hand it to the
                # pipeline, which revises the stage output and re-enters this
                # gate. An approve leaves no feedback -> None -> the pipeline
                # proceeds (the legacy contract).
                return job.pop("revision_feedback", None)

            stage_gate = _approval_gate

        if pipeline == "e2e":
            await run_e2e_pipeline(prompt, workspace=workspace)
        else:
            # "sequential" or "custom"; flow="custom" selects the plan-first /
            # codex-debate / codex-gpt5.6-sol final-review variant.
            flow = "custom" if pipeline == "custom" else "sequential"
            await run_pipeline(
                prompt, workspace=workspace, stage_gate=stage_gate, flow=flow
            )
        job["artifacts"] = _collect_artifacts(workspace or os.getcwd())
        job["status"] = "completed"
    except Exception as e:  # noqa: BLE001 - surface any pipeline failure to the client
        job["status"] = "failed"
        job["error"] = str(e)
    finally:
        # Invariant: a job with finished_at set must be TERMINAL. A
        # CancelledError (client disconnect / server shutdown mid-run) is a
        # BaseException that bypasses the except above — a live manifest
        # ended up journaled as status "running" WITH a finished_at. Repair
        # the state BEFORE journaling (both writes below are synchronous, so
        # they complete even while this task is being cancelled).
        if job.get("status") not in ("completed", "failed"):
            job["status"] = "failed"
            job["error"] = job.get("error") or (
                "The orchestration task was cancelled before the pipeline "
                "finished (client disconnect or server shutdown)."
            )
        job["finished_at"] = time.time()
        _journal_job(job)
        # Hackathon-mode runs export ai_collaboration_log.md BEFORE the job
        # reaches its terminal state, so the shipped log would say "running"
        # forever. Now that the terminal manifest is journaled, re-export the
        # (deterministic) log if this run emitted one. Best-effort: never
        # breaks job completion. No-op for every non-hackathon job.
        try:
            from ag_core.collab_log import refresh_log_if_present

            refresh_log_if_present(workspace or os.getcwd())
        except Exception:  # noqa: BLE001 - the refresh must never break a job
            pass
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        terminal = {
            "event": "status",
            "job_id": job_id,
            "status": job["status"],
        }
        if job.get("error"):
            terminal["error"] = job["error"]
        await _notify_progress(terminal)


def _jobs_root() -> str:
    """Root directory for per-job workspaces (override via GENIUS_JOBS_DIR)."""
    return os.environ.get("GENIUS_JOBS_DIR") or os.path.join(
        os.getcwd(), ".genius_jobs"
    )


# Orchestration job state lives in ORCHESTRATION_JOBS (this process's RAM), so
# a stdio client disconnect or server restart used to lose every job_id — a
# poller got "Unknown job_id" for a job whose artifacts sit on disk. Each job
# therefore journals a small manifest into its workspace on every state
# transition; orchestrate_status falls back to that manifest for ids it no
# longer holds in memory.
_JOB_MANIFEST = "job.json"
# uuid4().hex — anything else is never looked up on disk (also blocks path
# traversal via a crafted job_id).
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _journal_job(job: Dict[str, Any]) -> None:
    """Best-effort persist of the job manifest; never fails the pipeline."""
    workspace = job.get("workspace")
    if not workspace:
        return
    manifest = {
        k: job.get(k)
        for k in (
            "job_id",
            "status",
            "pipeline",
            "prompt",
            "error",
            "workspace",
            "started_at",
            "finished_at",
            "require_approval",
            "approval_stages",
            "awaiting_stage",
            "revision_round",
        )
    }
    try:
        os.makedirs(workspace, exist_ok=True)
        tmp = os.path.join(workspace, _JOB_MANIFEST + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        # Atomic swap so a status poll never reads a torn manifest.
        os.replace(tmp, os.path.join(workspace, _JOB_MANIFEST))
    except OSError as e:
        print(
            f"[MCP] job journal write failed for {job.get('job_id')}: {e}",
            file=sys.stderr,
        )


def _load_journaled_job(job_id: str):
    """Rebuild an orchestrate_status view from a job's on-disk manifest.

    Only jobs under the DEFAULT jobs dir are discoverable by id (a
    caller-supplied workspace holds its manifest too, but there is no index
    from id to that path). A manifest that says running/awaiting_approval
    describes a pipeline that died with the previous server process, so it is
    reported as ``interrupted`` — the artifacts of every completed stage are
    still on disk and listed via ``stages``/``artifacts_ready``.
    """
    if not _JOB_ID_RE.fullmatch(job_id or ""):
        return None
    path = os.path.join(_jobs_root(), job_id, _JOB_MANIFEST)
    try:
        with open(path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict) or manifest.get("job_id") != job_id:
        return None
    status = manifest.get("status")
    interrupted = status in ("running", "awaiting_approval")
    view = {
        "job_id": job_id,
        "status": "interrupted" if interrupted else status,
        "pipeline": manifest.get("pipeline"),
        "error": manifest.get("error"),
        "workspace": manifest.get("workspace"),
        "recovered_from_journal": True,
    }
    if interrupted:
        view["error"] = (
            "The MCP server restarted while this job was in flight; the "
            "pipeline is no longer running. Artifacts of the stages that "
            "finished are still in the workspace — re-submit orchestrate "
            "to build again."
        )
        # Read-repair the journal too: a manifest stuck on "running" (or the
        # pre-invariant bug: "running" WITH a finished_at) would stay
        # self-inconsistent on disk forever. Persist the normalized terminal
        # state so later reads — and humans inspecting job.json — see the
        # truth. Best-effort: a failed write never breaks the status poll.
        try:
            manifest["status"] = "interrupted"
            manifest["error"] = view["error"]
            if manifest.get("finished_at") is None:
                manifest["finished_at"] = time.time()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(manifest, f)
            os.replace(tmp, path)
        except OSError:
            pass
    started = manifest.get("started_at")
    finished = manifest.get("finished_at")
    if started is not None and finished is not None:
        view["elapsed_seconds"] = round(finished - started, 1)
    stages, ready = _stage_progress(manifest)
    view["stages"] = stages
    view["artifacts_ready"] = ready
    if status == "completed":
        view["artifacts"] = _collect_artifacts(manifest.get("workspace") or "")
    return view


def _job_workspace_from_journal(job_id: str):
    """job_id -> workspace fallback for job-scoped resource URIs.

    Lets a resources/read on genius://artifacts/<job_id>/<name> survive a
    server restart without an orchestrate_status call first: the mapping is
    rebuilt from the job's on-disk manifest (default jobs dir only, same
    discoverability rule as _load_journaled_job).
    """
    view = _load_journaled_job(job_id)
    return view.get("workspace") if view else None


set_job_workspace_resolver(_job_workspace_from_journal)


def _jobs_retention():
    """(max_jobs, max_age_days) for pruning old per-job workspaces.

    ``GENIUS_JOBS_MAX_JOBS`` keeps only the newest N job dirs (default 50,
    ``0`` disables). ``GENIUS_JOBS_RETENTION_DAYS`` additionally drops job
    dirs older than N days (default 0 = off). Blank/junk -> default, matching
    the DB retention knobs.
    """

    def _ival(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            return max(int(raw), 0)
        except (TypeError, ValueError):
            return default

    return (
        _ival("GENIUS_JOBS_MAX_JOBS", 50),
        _ival("GENIUS_JOBS_RETENTION_DAYS", 0),
    )


def _prune_jobs_root() -> None:
    """Bound ``.genius_jobs`` growth: every orchestrate job adds a workspace
    dir that was never reclaimed. Runs on each orchestrate call.

    Never touches: entries whose name is not a 32-hex job id (user files),
    and jobs currently unfinished in ORCHESTRATION_JOBS. Deletion is
    best-effort (``ignore_errors``) — retention must never fail a new job.
    """
    max_jobs, max_age_days = _jobs_retention()
    if max_jobs <= 0 and max_age_days <= 0:
        return
    entries = []
    try:
        with os.scandir(_jobs_root()) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if not _JOB_ID_RE.fullmatch(entry.name):
                        continue
                    live = ORCHESTRATION_JOBS.get(entry.name)
                    if live and live.get("status") not in ("completed", "failed"):
                        continue
                    entries.append(
                        (entry.stat(follow_symlinks=False).st_mtime, entry.path)
                    )
                except OSError:
                    continue
    except OSError:
        return
    doomed = set()
    if max_age_days > 0:
        cutoff = time.time() - max_age_days * 86400
        doomed.update(p for m, p in entries if m < cutoff)
    if max_jobs > 0:
        entries.sort(reverse=True)  # newest first by mtime
        doomed.update(p for _m, p in entries[max_jobs:])
    for path in doomed:
        shutil.rmtree(path, ignore_errors=True)


def _workspace_is_usable(workspace: str) -> bool:
    """A caller-supplied workspace is safe only when it is an ABSOLUTE path
    that either already exists as a writable directory, or can be created
    (``os.makedirs``-style) under a writable existing ancestor.

    A GUI launcher (e.g. Antigravity) starts the MCP server with cwd ``/``, so a
    relative workspace like ``"test"`` resolves to the read-only ``/test`` and a
    non-writable parent makes every ``_write_text`` fail silently — the pipeline
    then "completes" with zero artifacts and the job looks stuck forever. In
    that case orchestrate falls back to the guaranteed-writable jobs dir.
    """
    if not workspace or not os.path.isabs(workspace):
        return False
    path = os.path.abspath(workspace)
    if os.path.isdir(path):
        # Existing directory: its own writability is what matters (the parent
        # may legitimately be read-only, e.g. /opt/<user-owned-dir>).
        return os.access(path, os.W_OK)
    if os.path.exists(path):
        # Exists but is not a directory (a regular file): unusable.
        return False
    # Not created yet: run_pipeline will os.makedirs() it, which only needs the
    # DEEPEST EXISTING ancestor to be a writable directory.
    parent = os.path.dirname(path) or os.sep
    while not os.path.exists(parent):
        nxt = os.path.dirname(parent)
        if nxt == parent:  # filesystem root
            break
        parent = nxt
    return os.path.isdir(parent) and os.access(parent, os.W_OK)


async def dispatch_tool(name: str, arguments: Dict[str, Any]) -> str:
    """Route a tool call to either the full pipeline or a single agent.

    Returns the text payload for the MCP `content` block.
    """
    if name == "orchestrate":
        prompt = (arguments.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("orchestrate requires a non-empty 'prompt'.")
        pipeline = arguments.get("pipeline", "sequential")
        if pipeline not in ("sequential", "e2e", "custom"):
            raise ValueError("pipeline must be 'sequential', 'e2e', or 'custom'.")
        workspace = arguments.get("workspace")
        require_approval = bool(arguments.get("require_approval", False))
        # Optional subset of gates to actually pause at (e.g. ["design"] for
        # the plan-review-then-code flow). Passing it implies require_approval.
        approval_stages = arguments.get("approval_stages")
        if approval_stages is not None:
            if not isinstance(approval_stages, (list, tuple)) or not approval_stages:
                raise ValueError(
                    "approval_stages must be a non-empty list of stage names."
                )
            valid_stages = {"research", "design", "code", "review", "devops"}
            approval_stages = [str(s).strip().lower() for s in approval_stages]
            unknown = sorted(set(approval_stages) - valid_stages)
            if unknown:
                raise ValueError(
                    f"Unknown approval_stages {unknown}; valid stages: "
                    f"{sorted(valid_stages)}."
                )
            require_approval = True
        if require_approval and pipeline == "e2e":
            raise ValueError(
                "require_approval is not supported for the 'e2e' pipeline "
                "(it has no stage gates); use 'sequential' or 'custom'."
            )
        job_id = uuid.uuid4().hex
        if workspace and not _workspace_is_usable(workspace):
            # A relative or non-writable workspace (the MCP server's cwd is
            # often "/" under a GUI launch) would make every artifact write
            # fail silently and the job look stuck forever. Ignore it and use
            # the guaranteed-writable jobs dir instead.
            logging.getLogger(__name__).warning(
                "orchestrate: ignoring unusable workspace %r (relative path or "
                "non-writable parent); using the jobs dir instead.",
                workspace,
            )
            workspace = None
        if not workspace:
            # Isolate each job in its own directory. Concurrent jobs sharing the
            # CWD clobber each other's fixed-name artifacts (design.md, app.py,
            # ...) and one job's clean_output_files archives another's live
            # files mid-run.
            workspace = os.path.join(_jobs_root(), job_id)
        ORCHESTRATION_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "pipeline": pipeline,
            "prompt": prompt,
            "error": None,
            "artifacts": None,
            "workspace": workspace,
            "started_at": time.time(),
            "finished_at": None,
            "require_approval": require_approval,
            "approval_stages": approval_stages,
            "awaiting_stage": None,
            "approval_event": asyncio.Event(),
            "rejected": False,
            "reject_reason": None,
        }
        _journal_job(ORCHESTRATION_JOBS[job_id])
        _prune_finished_jobs()
        _prune_jobs_root()
        task = asyncio.create_task(
            _run_orchestration(
                job_id,
                prompt,
                pipeline,
                workspace,
                require_approval,
                approval_stages=approval_stages,
            )
        )
        # Hold a strong ref until the task finishes so it isn't GC-cancelled.
        _ORCHESTRATION_TASKS.add(task)
        task.add_done_callback(_ORCHESTRATION_TASKS.discard)
        message = "Pipeline started. Poll orchestrate_status with this job_id."
        if require_approval:
            gate_names = (
                "research, design, code, review, devops"
                if pipeline == "custom"
                else "research, design, code"
            )
            if approval_stages:
                gate_names = ", ".join(approval_stages)
            message = (
                "Pipeline started WITH approval gates: after each gated stage "
                f"({gate_names}) the job pauses as "
                "'awaiting_approval' — review the artifacts from "
                "orchestrate_status, then call orchestrate_approve (or "
                "orchestrate_reject; at the design gate orchestrate_revise "
                "iterates on the plan) with this job_id to continue."
            )
        return json.dumps({"job_id": job_id, "status": "running", "message": message})

    if name == "orchestrate_status":
        job_id = arguments.get("job_id", "")
        job = ORCHESTRATION_JOBS.get(job_id)
        if job is None:
            # Not in this process's memory (server restarted / job evicted):
            # fall back to the on-disk manifest the job journaled.
            recovered = _load_journaled_job(job_id)
            if recovered is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            return json.dumps(recovered)
        view = {k: job[k] for k in ("job_id", "status", "pipeline", "error")}
        view["workspace"] = job.get("workspace")
        started = job.get("started_at")
        if started is not None:
            end = job.get("finished_at") or time.time()
            view["elapsed_seconds"] = round(end - started, 1)
        stages, ready = _stage_progress(job)
        view["stages"] = stages
        view["artifacts_ready"] = ready
        if job["status"] == "running":
            # The pipeline works the checkpoints in order, so the first
            # not-yet-done stage is what it is working on now.
            current = next(
                (s["stage"] for s in stages if s["state"] != "done"), None
            )
            if current:
                view["current_stage"] = current
        if job.get("awaiting_stage"):
            view["awaiting_stage"] = job["awaiting_stage"]
            if job.get("revision_round"):
                view["revision_round"] = job["revision_round"]
            # Inline the current plan while paused at the design gate so the
            # client can render it for the user's review (and iterate via
            # orchestrate_revise) without a resources/read round trip.
            if job["awaiting_stage"] == "design":
                plan = _inline_plan(job)
                if plan is not None:
                    view["plan"] = plan
        # Expose artifacts while paused too, so the reviewer can read the
        # stage output before approving/rejecting.
        if job["status"] in ("completed", "awaiting_approval"):
            view["artifacts"] = job["artifacts"]
        if job["status"] == "completed":
            # Quality ladder (proposal from a real mis-reported Next.js job):
            # "completed" alone must not read as shippable. review.md is the
            # canonical report; it carries an explicit release-readiness
            # verdict since the same change. Absent marker (old workspaces,
            # e2e flow) => None, not a claim either way.
            review_md = os.path.join(job.get("workspace") or "", "review.md")
            try:
                with open(review_md, "r", encoding="utf-8", errors="replace") as fh:
                    _rv = fh.read()
                view["release_ready"] = (
                    True
                    if "release-ready: YES" in _rv
                    else False if "release-ready: NO" in _rv else None
                )
            except OSError:
                view["release_ready"] = None
        return json.dumps(view)

    if name in ("orchestrate_approve", "orchestrate_reject"):
        job_id = arguments.get("job_id", "")
        job = ORCHESTRATION_JOBS.get(job_id)
        if job is None:
            raise ValueError(f"Unknown job_id: {job_id}")
        if job.get("status") != "awaiting_approval":
            raise ValueError(
                f"Job {job_id} is not awaiting approval "
                f"(status: {job.get('status')})."
            )
        stage = job.get("awaiting_stage")
        if name == "orchestrate_reject":
            job["rejected"] = True
            job["reject_reason"] = (arguments.get("reason") or "").strip() or None
        # Flip the pause state synchronously so a status poll right after
        # this call never sees a stale 'awaiting_approval'; the gate coroutine
        # then resumes and (on rejection) fails the job.
        job["awaiting_stage"] = None
        job["status"] = "running"
        job["approval_event"].set()
        _journal_job(job)
        return json.dumps(
            {
                "job_id": job_id,
                "stage": stage,
                "action": ("rejected" if name == "orchestrate_reject" else "approved"),
            }
        )

    if name == "orchestrate_revise":
        job_id = arguments.get("job_id", "")
        job = ORCHESTRATION_JOBS.get(job_id)
        if job is None:
            raise ValueError(f"Unknown job_id: {job_id}")
        if job.get("status") != "awaiting_approval":
            raise ValueError(
                f"Job {job_id} is not awaiting approval "
                f"(status: {job.get('status')})."
            )
        stage = job.get("awaiting_stage")
        if stage != "design":
            raise ValueError(
                "orchestrate_revise currently supports only the 'design' "
                f"approval gate (job is awaiting '{stage}'). Use "
                "orchestrate_approve or orchestrate_reject at this gate."
            )
        feedback = (arguments.get("feedback") or "").strip()
        if not feedback:
            raise ValueError("orchestrate_revise requires non-empty 'feedback'.")
        job["revision_feedback"] = feedback
        job["revision_round"] = int(job.get("revision_round") or 0) + 1
        # Flip the pause state synchronously (same contract as approve/reject)
        # so a status poll right after this call never sees a stale pause.
        job["awaiting_stage"] = None
        job["status"] = "running"
        job["approval_event"].set()
        _journal_job(job)
        return json.dumps(
            {
                "job_id": job_id,
                "stage": stage,
                "action": "revision_requested",
                "revision_round": job["revision_round"],
                "message": (
                    "The architect is revising the plan with this feedback. "
                    "Poll orchestrate_status: the job will pause at the "
                    "design gate again, with the revised plan inlined in the "
                    "'plan' field. Repeat orchestrate_revise until the plan "
                    "is right, then orchestrate_approve to start coding."
                ),
            }
        )

    if name == "doctor":
        return await _run_doctor_report()

    if name == "debate":
        design = (arguments.get("design") or "").strip()
        if not design:
            raise ValueError("debate requires a non-empty 'design'.")
        try:
            rounds = int(arguments.get("rounds") or 1)
        except (TypeError, ValueError):
            raise ValueError("rounds must be an integer between 1 and 3.")
        rounds = max(1, min(rounds, MAX_DEBATE_ROUNDS))
        return await _run_debate(design, arguments.get("prompt") or "", rounds)

    if name == "review":
        code = arguments.get("code") or ""
        if not code.strip():
            raise ValueError("review requires non-empty 'code'.")
        return await _run_review(code, arguments.get("instructions") or "")

    if name == "code_graph":
        return await _run_code_graph(arguments)

    if name == "eval":
        return await _run_eval(arguments)

    if name in _NOTEBOOKLM_TOOLS:
        return await _run_notebooklm(name, arguments)

    prompt = arguments.get("prompt", "")
    context = arguments.get("context")
    return await execute_agent(name, prompt, context)


@app.get("/tools")
async def list_tools():
    return {"tools": TOOLS}


def _require_http_auth(authorization: str) -> None:
    """Guard the HTTP tool endpoint. ``/tools/call`` drives local vendor CLIs,
    the full pipeline, and filesystem writes, so an exposed (non-localhost)
    server must not be open. When GENIUS_MCP_TOKEN is set, require a matching
    bearer token; the default localhost bind keeps the token optional for the
    trusted single-user case."""
    expected = (os.environ.get("GENIUS_MCP_TOKEN") or "").strip()
    if not expected:
        return
    header = (authorization or "").strip()
    provided = header[7:].strip() if header.lower().startswith("bearer ") else header
    if not (provided and hmac.compare_digest(provided, expected)):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _enforce_public_bind_token(host: str) -> None:
    """Fail closed at startup (same policy as dashboard.py): ``/tools/call``
    drives local vendor CLIs, the full pipeline, and filesystem writes, so a
    non-loopback bind without GENIUS_MCP_TOKEN would expose an unauthenticated
    remote-execution endpoint to the network. ``_require_http_auth`` only
    enforces the token when one is set, so the missing-token-on-public-bind
    case must be refused here."""
    loopback_hosts = {"127.0.0.1", "::1", "localhost", ""}
    if host.strip() in loopback_hosts:
        return
    if not (os.environ.get("GENIUS_MCP_TOKEN") or "").strip():
        sys.exit(
            f"Refusing to start: GENIUS_MCP_HOST={host!r} exposes /tools/call "
            "beyond loopback, but GENIUS_MCP_TOKEN is not set. /tools/call "
            "runs local CLIs and writes files. Set GENIUS_MCP_TOKEN=<secret> "
            "to require a bearer token, or bind 127.0.0.1."
        )


@app.post("/tools/call")
async def call_tool(req: CallToolRequest, authorization: str = Header(default="")):
    _require_http_auth(authorization)
    valid_tool_names = {t["name"] for t in TOOLS}
    if req.name not in valid_tool_names:
        raise HTTPException(status_code=400, detail=f"Tool {req.name} not found")

    try:
        result = await dispatch_tool(req.name, req.arguments)
        return {"content": [{"type": "text", "text": result}]}
    except ValueError as e:
        # Client input errors are safe to echo (they describe the bad request).
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        # Don't leak internal detail (paths, upstream response bodies, auth
        # diagnostics) to the caller; log it server-side instead.
        print(
            f"[MCP] tool '{req.name}' failed:\n{traceback.format_exc()}",
            file=sys.stderr,
        )
        raise HTTPException(status_code=500, detail="Internal server error")


PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "genius", "version": "1.0.0"}


async def handle_request(req: Dict[str, Any]):
    """Handle one JSON-RPC request. Returns a response dict, or None for
    notifications (which must not be answered)."""
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params", {}) or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {
                    "tools": {},
                    "resources": {"listChanged": False},
                },
                "serverInfo": SERVER_INFO,
            },
        }
    if method in ("notifications/initialized", "initialized"):
        return None  # notification: no response
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "resources/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"resources": _list_resources()},
        }
    if method == "resources/read":
        uri = params.get("uri") or ""
        try:
            contents = _read_resource(uri)
        except ResourceNotFoundError as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32002, "message": str(e)},
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"contents": contents},
        }
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {}) or {}
        try:
            content = await dispatch_tool(name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": content}]},
            }
        except Exception as e:  # noqa: BLE001 - report as JSON-RPC error
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(e)},
            }

    if req_id is None:
        return None  # unknown notification: stay silent
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": "Method not found"},
    }


def _redirect_all_logging_to_stderr(stream=None):
    """Point every logging StreamHandler at ``stream`` (default: real stderr).

    In stdio mode stdout is the JSON-RPC channel. Handlers bound to the real
    stdout at import time - notably ``ag_core``'s logger
    (``StreamHandler(sys.stdout)`` in ag_core/utils/logger.py) - would otherwise
    interleave log lines with responses and corrupt the protocol for strict
    MCP clients. Retarget all of them, across every logger, not just root.
    """
    import logging

    target = stream if stream is not None else sys.stderr
    loggers = [logging.getLogger()] + [
        logging.getLogger(name) for name in list(logging.Logger.manager.loggerDict)
    ]
    seen = set()
    for lg in loggers:
        for handler in list(getattr(lg, "handlers", [])):
            if isinstance(handler, logging.StreamHandler) and id(handler) not in seen:
                seen.add(id(handler))
                try:
                    if hasattr(handler, "setStream"):
                        handler.setStream(target)
                    else:  # pragma: no cover - Python < 3.7
                        handler.stream = target
                except AttributeError:
                    # Handlers with a read-only ``stream`` property — e.g.
                    # logging's lastResort _StderrHandler when some lib/test
                    # attached it to a logger — resolve their stream at emit
                    # time (stderr) and can never contaminate stdout. Skip
                    # them instead of crashing stdio boot.
                    continue


# ---------------------------------------------------------------------------
# Official MCP SDK stdio transport — the wire that Antigravity consumes.
#
# ``handle_request`` above stays as the in-process engine the test-suite drives
# directly. This layer wraps the SAME ``dispatch_tool`` engine + ``TOOLS`` +
# resource whitelist behind the official ``mcp`` SDK so the wire is fully
# spec-compliant: camelCase ``inputSchema`` (without it a strict client like
# Antigravity/Gemini sees every tool as having NO parameters), automatic
# protocolVersion negotiation, tool failures surfaced as ``isError`` results,
# and proper parse/invalid-request error codes.
#
# Tools are namespaced ``genius_<name>`` on the wire so they never collide with
# other MCP servers sharing the same Antigravity config; the short engine names
# stay internal (``dispatch_tool("review", ...)``).
# ---------------------------------------------------------------------------

# Wire-name prefix, overridable so a SECOND registration of this same server
# (e.g. a `genius-debug` entry pointing at different provider models) can expose
# non-colliding tool names like ``gdbg_code``. Defaults to ``genius_``.
WIRE_PREFIX = os.environ.get("GENIUS_MCP_WIRE_PREFIX") or "genius_"

# Advisory-only annotation hints (they help Antigravity display and safely
# auto-approve tools; they do not change behavior). Read-only tools never touch
# the workspace; the destructive ones launch/steer a pipeline job or write
# artifacts / notebooks.
_READ_ONLY_TOOLS = {
    "research", "design", "code", "unit_test", "security_audit", "deploy",
    "doctor", "debate", "review", "code_graph", "eval",
    "orchestrate_status", "notebooklm_list", "notebooklm_query",
}
_DESTRUCTIVE_TOOLS = {
    "orchestrate", "orchestrate_approve", "orchestrate_reject",
    "orchestrate_revise", "notebooklm_research",
}


def _selected_tool_names():
    """Engine tool names to expose. Defaults to every tool in ``TOOLS``; an
    operator can expose a lean subset via ``GENIUS_MCP_TOOLS=comma,list`` to
    stay under Antigravity's cross-server tool budget (~100 tools total) when
    several MCP servers are active. Both bare and ``genius_``-prefixed spellings
    are accepted; unknown names are ignored, and an empty/all-unknown value
    falls back to the full set."""
    known = {t["name"] for t in TOOLS}
    raw = (os.environ.get("GENIUS_MCP_TOOLS") or "").strip()
    if not raw:
        return known
    resolved = set()
    for token in raw.split(","):
        name = token.strip()
        if not name:
            continue
        engine = name[len(WIRE_PREFIX):] if name.startswith(WIRE_PREFIX) else name
        if engine in known:
            resolved.add(engine)
    return resolved or known


def _build_sdk_server():
    """Construct the official-SDK MCP ``Server`` bound to the existing engine.

    Built lazily (only for the ``stdio`` entrypoint) so importing this module
    for the HTTP path or the test-suite never pulls in the SDK.
    """
    from mcp.server import Server
    from mcp.server.lowlevel.helper_types import ReadResourceContents
    from mcp.shared.exceptions import McpError
    import mcp.types as types

    # Server name overridable (GENIUS_MCP_SERVER_NAME) so a second registration
    # identifies itself distinctly (e.g. "genius-debug"); defaults to "genius".
    server = Server(
        os.environ.get("GENIUS_MCP_SERVER_NAME") or "genius",
        version=SERVER_INFO["version"],
    )

    @server.list_tools()
    async def _sdk_list_tools():
        selected = _selected_tool_names()
        tools = []
        for spec in TOOLS:
            name = spec["name"]
            if name not in selected:
                continue
            tools.append(
                types.Tool(
                    name=WIRE_PREFIX + name,
                    description=spec["description"],
                    inputSchema=spec["input_schema"],
                    annotations=types.ToolAnnotations(
                        readOnlyHint=name in _READ_ONLY_TOOLS,
                        destructiveHint=name in _DESTRUCTIVE_TOOLS,
                    ),
                )
            )
        return tools

    # validate_input=False: the engine (dispatch_tool) is deliberately lenient
    # (e.g. it coerces debate.rounds from a string, accepts str-or-list args),
    # so let its friendly ValueErrors surface as isError results instead of the
    # SDK's strict jsonschema pre-rejection. The client still SEES each schema
    # from list_tools — which is the whole point.
    @server.call_tool(validate_input=False)
    async def _sdk_call_tool(name, arguments):
        # Capture the live session so background jobs can push progress
        # notifications (the lowlevel Server has no session-started hook;
        # any tool call — orchestrate itself at the latest — sets it before
        # the job's watcher needs it).
        global _MCP_LOG_SESSION
        try:
            _MCP_LOG_SESSION = server.request_context.session
        except LookupError:
            pass
        engine = name[len(WIRE_PREFIX):] if name.startswith(WIRE_PREFIX) else name
        text = await dispatch_tool(engine, arguments or {})
        return [types.TextContent(type="text", text=text)]

    @server.set_logging_level()
    async def _sdk_set_logging_level(level):
        # Declares the ``logging`` capability (capabilities derive from the
        # registered handlers) and lets the client raise the floor for the
        # genius.orchestrate progress notifications.
        global _MCP_MIN_LOG_LEVEL
        _MCP_MIN_LOG_LEVEL = str(level)

    @server.list_resources()
    async def _sdk_list_resources():
        return [
            types.Resource(
                uri=r["uri"],
                name=r["name"],
                description=r.get("description"),
                mimeType=r.get("mimeType"),
            )
            for r in _list_resources()
        ]

    @server.read_resource()
    async def _sdk_read_resource(uri):
        try:
            blocks = _read_resource(str(uri))
        except ResourceNotFoundError as e:
            raise McpError(types.ErrorData(code=-32002, message=str(e)))
        return [
            ReadResourceContents(content=b["text"], mime_type=b["mimeType"])
            for b in blocks
        ]

    return server


async def _run_sdk_stdio():
    """Serve MCP over stdio via the official SDK.

    stdout is the JSON-RPC channel, so (1) all logging is retargeted to stderr,
    and (2) the REAL stdout is captured BEFORE ``sys.stdout`` is aliased to
    stderr (which catches stray ``print()`` from lazily-imported providers).
    ``stdio_server`` binds ``sys.stdout.buffer`` at entry, so the captured
    stream must be handed to it explicitly or responses would go to stderr.
    """
    import io
    import logging

    import anyio
    from mcp.server.stdio import stdio_server

    real_stderr = sys.stderr
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    logging.basicConfig(stream=real_stderr, level=logging.INFO)
    _redirect_all_logging_to_stderr(real_stderr)

    # Take ownership of the binary stdout buffer via detach(): once we swap
    # sys.stdout to stderr below, the original text wrapper loses its last
    # reference and its finalizer would close(buffer) — killing the SDK writer
    # the moment GC runs (only visible with a client that keeps the pipe open).
    # detach() leaves the old wrapper defunct so it can't close our buffer; we
    # re-wrap as UTF-8 for cross-platform (Windows) correctness.
    stdout_buffer = sys.stdout.detach()
    real_stdout = anyio.wrap_file(io.TextIOWrapper(stdout_buffer, encoding="utf-8"))
    # Route stray print()/logging from lazily-imported providers to stderr; only
    # the SDK writer (real_stdout) may touch the JSON-RPC channel on stdout.
    sys.stdout = sys.stderr

    server = _build_sdk_server()
    async with stdio_server(stdout=real_stdout) as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        # Must be the asyncio backend: the engine uses asyncio.create_task /
        # Event / wait_for / to_thread directly (uvloop is asyncio-compatible).
        import anyio

        anyio.run(_run_sdk_stdio, backend="asyncio")
    else:
        import uvicorn

        # Bind localhost by default so the unauthenticated tool endpoint isn't
        # reachable off-box. To expose it (GENIUS_MCP_HOST=0.0.0.0), set
        # GENIUS_MCP_TOKEN so /tools/call requires a bearer token.
        # `or` (not a get() default): a blank GENIUS_MCP_HOST shipped in
        # .env.example and loaded as "" by python-dotenv would otherwise become
        # the empty host, which uvicorn/socket binds as 0.0.0.0 — exposing the
        # unauthenticated tool endpoint to the whole network. Blank == loopback.
        # The MCP-specific GENIUS_MCP_HOST wins over the generic
        # GENIUS_BIND_HOST shared with the other locally-consumed servers.
        host = (
            os.environ.get("GENIUS_MCP_HOST")
            or os.environ.get("GENIUS_BIND_HOST")
            or "127.0.0.1"
        )
        port = int(os.environ.get("GENIUS_MCP_PORT") or 8000)
        _enforce_public_bind_token(host)
        uvicorn.run("mcp_server:app", host=host, port=port)
