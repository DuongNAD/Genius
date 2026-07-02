import os
import sys
import json
import time
import uuid
import asyncio
from typing import Any, Dict, List
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Add project root to sys.path
root_dir = os.path.dirname(os.path.abspath(__file__))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from ag_core.config import load_config
from ag_core.provider_factory import make_provider

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
    config = load_config()
    provider = make_provider(role, config, default_chain=default_chain)
    agent_class = globals()[agent_cls_name]
    agent = agent_class(provider=provider, config=config, output_file="None")

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
            "Provide constructive criticism and suggest concrete improvements. "
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
    config = load_config()
    provider = make_provider(role, config, default_chain=default_chain)
    agent_class = globals()[agent_cls_name]
    agent = agent_class(provider=provider, config=config, output_file="None")

    prompt = (
        "Perform a thorough code review of the following code. Identify bugs, "
        "security vulnerabilities, style issues, and concrete improvements."
    )
    if instructions:
        prompt += f"\nReviewer instructions: {instructions}"
    prompt += f"\n\nCode to review:\n```\n{code}\n```"
    return await agent.run(prompt=prompt, context_data={"<submitted code>": code})


TOOLS = [
    {
        "name": "research",
        "description": "Perform in-depth requirements research and identify technical challenges.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The research query or topic",
                },
                "context": {
                    "type": "object",
                    "description": "Optional file context as dict of filepath -> content",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "design",
        "description": "Develop high-level software architecture plans and component designs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The system design description or requirements",
                },
                "context": {"type": "object", "description": "Optional file context"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "code",
        "description": "Write or refactor high-quality code implementation based on specifications.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The coding requirements or specification",
                },
                "context": {
                    "type": "object",
                    "description": "Optional existing files context",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "unit_test",
        "description": "Generate comprehensive test cases and verify implementation behavior.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Code content or test description",
                },
                "context": {"type": "object", "description": "Optional context"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "security_audit",
        "description": "Perform security audit on the code to detect vulnerabilities and secrets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Code content or security concerns to audit",
                },
                "context": {"type": "object", "description": "Optional context"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "deploy",
        "description": "Generate CI/CD configuration, Dockerfiles, and deployment strategies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Deployment requirements"},
                "context": {"type": "object", "description": "Optional context"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "orchestrate",
        "description": (
            "Run the FULL multi-agent pipeline (research -> design -> code -> "
            "test + security + deploy) for a build request. Returns a job_id "
            "immediately; poll orchestrate_status to retrieve the artifacts. "
            "Requires the Genius skill servers to be running (python serve.py)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The build/refactor request to orchestrate",
                },
                "pipeline": {
                    "type": "string",
                    "enum": ["sequential", "e2e"],
                    "description": "Pipeline variant (default 'sequential')",
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional absolute path where artifacts are written (default: server cwd)",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "orchestrate_status",
        "description": (
            "Poll the status of a pipeline started by orchestrate. Returns status "
            "(running|completed|failed) and, when completed, the generated artifacts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job_id returned by orchestrate",
                }
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "doctor",
        "description": (
            "Preflight readiness check for Genius. Verifies the local vendor "
            "CLIs (agy/claude/codex, grok optional), SKILL_API_KEY, and the "
            "per-role provider fallback chains, and returns a text report "
            "ending in READY or NOT READY. Call this FIRST to check whether "
            "Genius is ready before calling orchestrate. Read-only, no side "
            "effects."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "debate",
        "description": (
            "Adversarially refine a draft design: a researcher-role critic "
            "(agy/Gemini by default) reviews it and a Claude architect "
            "refines it, for up to 'rounds' exchanges "
            "(early exit when the critic replies [APPROVED]). Runs in-process "
            "(no skill servers needed). Returns JSON with the refined design, "
            "an 'approved' flag, and a per-round summary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "design": {
                    "type": "string",
                    "description": "The draft design/plan to critique and refine",
                },
                "prompt": {
                    "type": "string",
                    "description": "Optional original requirements for context",
                },
                "rounds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3,
                    "description": "Max critique/refine rounds (default 1, max 3)",
                },
            },
            "required": ["design"],
        },
    },
    {
        "name": "review",
        "description": (
            "Code review of the given code by the Codex reviewer agent, "
            "in-process. Returns the review text; never writes files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The code to review"},
                "instructions": {
                    "type": "string",
                    "description": "Optional focus areas or review instructions",
                },
            },
            "required": ["code"],
        },
    },
]

# --- MCP resources: pipeline artifacts as genius:// URIs -------------------
# Whitelist of root-level markdown artifacts the pipeline produces, keyed by
# file name. Only these names (plus their .bak archives) are ever listed or
# served - never glob arbitrary workspace files, so user files cannot leak.
RESOURCE_URI_PREFIX = "genius://artifacts/"

_RESOURCE_ARTIFACTS = {
    "research.md": (
        "Requirements research produced by the research stage (researcher role)."
    ),
    "design.md": "Architecture design produced by the design stage (Claude).",
    "review.md": "Code review + lint/test logs produced by the code stage (Codex).",
    "audit.md": "Security audit produced by the security stage.",
    "deploy.md": "Deployment plan produced by the deploy stage (DevOps).",
    "plan.md": "End-to-end plan produced by the e2e pipeline (Claude).",
}


class ResourceNotFoundError(Exception):
    """Maps to JSON-RPC error -32002 (MCP: resource not found)."""


def _resource_catalog() -> Dict[str, str]:
    """name -> description for every servable artifact (incl. .bak archives)."""
    catalog: Dict[str, str] = {}
    for name, desc in _RESOURCE_ARTIFACTS.items():
        catalog[name] = desc
        catalog[name + ".bak"] = (
            f"Archived previous-run copy of {name} (renamed on pipeline start)."
        )
    return catalog


def _list_resources(workspace: str = None) -> List[Dict[str, str]]:
    """Enumerate the whitelisted artifacts that exist in the workspace."""
    root = workspace or os.getcwd()
    resources = []
    for name, desc in _resource_catalog().items():
        if os.path.isfile(os.path.join(root, name)):
            resources.append(
                {
                    "uri": RESOURCE_URI_PREFIX + name,
                    "name": name,
                    "description": desc,
                    "mimeType": "text/markdown",
                }
            )
    return resources


def _read_resource(uri: str, workspace: str = None) -> List[Dict[str, str]]:
    """Return the MCP `contents` blocks for a genius://artifacts/ URI.

    The artifact name must match the whitelist exactly - same traversal
    posture as orchestrator.safe_join: no separators, no '..', no absolute
    paths can ever reach the filesystem join below.
    """
    catalog = _resource_catalog()
    name = None
    if isinstance(uri, str) and uri.startswith(RESOURCE_URI_PREFIX):
        name = uri[len(RESOURCE_URI_PREFIX) :]
    if not name or name not in catalog:
        raise ResourceNotFoundError(
            f"Unknown resource URI: {uri!r}. Valid URIs are "
            f"{RESOURCE_URI_PREFIX}<name> where <name> is one of the pipeline "
            "artifacts reported by resources/list."
        )
    path = os.path.join(workspace or os.getcwd(), name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        raise ResourceNotFoundError(
            f"Artifact '{name}' does not exist yet - the pipeline stage that "
            "produces it has not completed. Poll orchestrate_status."
        )
    return [{"uri": uri, "mimeType": "text/markdown", "text": text}]


# --- Full-pipeline orchestration (the "điều phối viên" entrypoint) ---------
# The pipeline is long-running, so orchestrate launches it as a background job
# and returns a job_id; clients poll orchestrate_status for the result.
ORCHESTRATION_JOBS: Dict[str, Dict[str, Any]] = {}

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
        stages.append(
            {
                "stage": stage,
                "artifact": fname,
                "state": "done" if done else "pending",
            }
        )
        if done and fname in _RESOURCE_ARTIFACTS:
            ready.append(RESOURCE_URI_PREFIX + fname)
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


async def _run_orchestration(
    job_id: str, prompt: str, pipeline: str, workspace: str = None
) -> None:
    job = ORCHESTRATION_JOBS[job_id]
    try:
        # Imported lazily so the MCP server boots fast (initialize/tools-list
        # must respond before Antigravity's connect timeout).
        from orchestrator import run_pipeline, run_e2e_pipeline

        if pipeline == "e2e":
            await run_e2e_pipeline(prompt, workspace=workspace)
        else:
            await run_pipeline(prompt, workspace=workspace)
        job["artifacts"] = _collect_artifacts(workspace or os.getcwd())
        job["status"] = "completed"
    except Exception as e:  # noqa: BLE001 - surface any pipeline failure to the client
        job["status"] = "failed"
        job["error"] = str(e)
    finally:
        job["finished_at"] = time.time()


async def dispatch_tool(name: str, arguments: Dict[str, Any]) -> str:
    """Route a tool call to either the full pipeline or a single agent.

    Returns the text payload for the MCP `content` block.
    """
    if name == "orchestrate":
        prompt = (arguments.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("orchestrate requires a non-empty 'prompt'.")
        pipeline = arguments.get("pipeline", "sequential")
        if pipeline not in ("sequential", "e2e"):
            raise ValueError("pipeline must be 'sequential' or 'e2e'.")
        workspace = arguments.get("workspace")
        job_id = uuid.uuid4().hex
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
        }
        asyncio.create_task(_run_orchestration(job_id, prompt, pipeline, workspace))
        return json.dumps(
            {
                "job_id": job_id,
                "status": "running",
                "message": "Pipeline started. Poll orchestrate_status with this job_id.",
            }
        )

    if name == "orchestrate_status":
        job_id = arguments.get("job_id", "")
        job = ORCHESTRATION_JOBS.get(job_id)
        if job is None:
            raise ValueError(f"Unknown job_id: {job_id}")
        view = {k: job[k] for k in ("job_id", "status", "pipeline", "error")}
        started = job.get("started_at")
        if started is not None:
            end = job.get("finished_at") or time.time()
            view["elapsed_seconds"] = round(end - started, 1)
        stages, ready = _stage_progress(job)
        view["stages"] = stages
        view["artifacts_ready"] = ready
        if job["status"] == "completed":
            view["artifacts"] = job["artifacts"]
        return json.dumps(view)

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

    prompt = arguments.get("prompt", "")
    context = arguments.get("context")
    return await execute_agent(name, prompt, context)


@app.get("/tools")
async def list_tools():
    return {"tools": TOOLS}


@app.post("/tools/call")
async def call_tool(req: CallToolRequest):
    valid_tool_names = {t["name"] for t in TOOLS}
    if req.name not in valid_tool_names:
        raise HTTPException(status_code=400, detail=f"Tool {req.name} not found")

    try:
        result = await dispatch_tool(req.name, req.arguments)
        return {"content": [{"type": "text", "text": result}]}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
                if hasattr(handler, "setStream"):
                    handler.setStream(target)
                else:  # pragma: no cover - Python < 3.7
                    handler.stream = target


async def run_stdio_mcp():
    import logging

    real_stdout = sys.stdout
    real_stderr = sys.stderr

    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    logging.basicConfig(stream=real_stderr, level=logging.INFO)
    # Retarget non-root handlers (e.g. ag_core's stdout handler) to stderr so
    # they can never contaminate the JSON-RPC stream on stdout.
    _redirect_all_logging_to_stderr(real_stderr)

    sys.stdout = sys.stderr

    if sys.platform == "win32":
        # The win32 branch reads text-mode sys.stdin, which decodes with the
        # locale codepage (e.g. cp1252) for pipes. A client's UTF-8 BOM then
        # arrives as 'ï»¿' (and any non-ASCII JSON as mojibake), so the BOM
        # strip below never matches and the request fails to parse. Force
        # UTF-8 to match the JSON-RPC wire format.
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    loop = asyncio.get_running_loop()

    if sys.platform != "win32":
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        if sys.platform == "win32":
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line_str = line
        else:
            line_bytes = await reader.readline()
            if not line_bytes:
                break
            line_str = line_bytes.decode("utf-8")
        # Strip a leading UTF-8 BOM (some clients/Windows pipes prepend one to
        # the first line) and surrounding whitespace before parsing.
        line_str = line_str.lstrip("﻿").strip()
        if not line_str:
            continue
        try:
            req = json.loads(line_str)
            res = await handle_request(req)
            if res is not None:
                real_stdout.write(json.dumps(res) + "\n")
                real_stdout.flush()
        except Exception as e:
            sys.stderr.write(f"Error handling request: {e}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        asyncio.run(run_stdio_mcp())
    else:
        import uvicorn

        uvicorn.run("mcp_server:app", host="0.0.0.0", port=8000)
