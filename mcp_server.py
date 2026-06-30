import os
import sys
import json
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
from ag_core.providers.grok_provider import GrokProvider
from ag_core.providers.anthropic_provider import AnthropicProvider
from ag_core.providers.openai_provider import OpenAIProvider
from ag_core.agents.grok_researcher import GrokResearcherAgent
from ag_core.agents.claude_architect import ClaudeArchitectAgent
from ag_core.agents.codex_reviewer import CodexReviewerAgent
from ag_core.agents.tester import TesterAgent
from ag_core.agents.security_agent import SecurityAgent
from ag_core.agents.devops_agent import DevOpsAgent

app = FastAPI(title="Genius MCP Server")


class CallToolRequest(BaseModel):
    name: str
    arguments: Dict[str, Any]


async def execute_agent(
    agent_name: str, prompt: str, context: Dict[str, str] = None
) -> str:
    config = load_config()
    if agent_name == "research":
        provider = GrokProvider(
            api_key=config.grok_api_key, model_name=config.models.grok
        )
        agent = GrokResearcherAgent(
            provider=provider, config=config, output_file="None"
        )
    elif agent_name == "design":
        provider = AnthropicProvider(
            api_key=config.anthropic_api_key, model_name=config.models.anthropic
        )
        agent = ClaudeArchitectAgent(
            provider=provider, config=config, output_file="None"
        )
    elif agent_name == "code":
        provider = OpenAIProvider(
            api_key=config.openai_api_key, model_name=config.models.openai
        )
        agent = CodexReviewerAgent(provider=provider, config=config, output_file="None")
        prompt = f"/code {prompt}"
    elif agent_name == "unit_test":
        provider = OpenAIProvider(
            api_key=config.openai_api_key, model_name=config.models.openai
        )
        agent = TesterAgent(provider=provider, config=config, output_file="None")
    elif agent_name == "security_audit":
        provider = OpenAIProvider(
            api_key=config.openai_api_key, model_name=config.models.openai
        )
        agent = SecurityAgent(provider=provider, config=config, output_file="None")
    elif agent_name == "deploy":
        provider = AnthropicProvider(
            api_key=config.anthropic_api_key, model_name=config.models.anthropic
        )
        agent = DevOpsAgent(provider=provider, config=config, output_file="None")
    else:
        raise ValueError(f"Unknown agent: {agent_name}")

    return await agent.run(prompt=prompt, context_data=context)


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
]

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
        if job["status"] == "completed":
            view["artifacts"] = job["artifacts"]
        return json.dumps(view)

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
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method in ("notifications/initialized", "initialized"):
        return None  # notification: no response
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
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


async def run_stdio_mcp():
    import logging

    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)

    real_stdout = sys.stdout
    sys.stdout = sys.stderr

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
