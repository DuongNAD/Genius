import os
import sys
import json
import time
import uuid
import hmac
import asyncio
import traceback
from typing import Any, Dict, List
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

# Add project root to sys.path
root_dir = os.path.dirname(os.path.abspath(__file__))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from ag_core import agent_factory

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


_CODE_GRAPH_OPS = {
    "map",
    "definition",
    "references",
    "importers",
    "imports",
    "skeleton",
}
_CODE_GRAPH_MAX_REFS = 50


async def _run_code_graph(arguments: Dict[str, Any]) -> str:
    """Answer one structure query over a workspace's code graph.

    In-process and read-only (CodexGraph-style, no graph DB): scans the
    workspace, builds ag_core.scanner.graph_index.RepoIndex, and returns a
    JSON payload. The scan + index build run off the event loop so a large
    workspace cannot stall concurrent MCP requests. Argument errors come
    back as JSON {"error": ...} rather than protocol errors, so agent
    callers can self-correct.
    """
    from ag_core.config import load_config
    from ag_core.scanner.graph_index import RepoIndex
    from ag_core.scanner.project_scanner import ProjectScanner

    op = (arguments.get("op") or "map").strip().lower()
    workspace = arguments.get("workspace") or os.getcwd()
    symbol = (arguments.get("symbol") or "").strip()
    file_arg = (arguments.get("file") or "").strip()

    if op not in _CODE_GRAPH_OPS:
        return json.dumps(
            {"error": f"Unknown op: {op}. Valid ops: {sorted(_CODE_GRAPH_OPS)}"}
        )
    if not os.path.isdir(workspace):
        return json.dumps({"error": f"Workspace directory not found: {workspace}"})
    if op in ("definition", "references") and not symbol:
        return json.dumps({"error": f"op={op} requires a 'symbol' argument"})
    if op in ("importers", "imports", "skeleton") and not file_arg:
        return json.dumps({"error": f"op={op} requires a 'file' argument"})

    config = load_config()
    scanner = ProjectScanner(
        root_dir=workspace, extra_ignores=config.scanner.exclude_patterns
    )
    scanned = await asyncio.to_thread(scanner.scan)
    index = await asyncio.to_thread(RepoIndex, scanned)

    if op == "map":
        try:
            budget = int(arguments.get("budget"))
        except (TypeError, ValueError):
            budget = None
        rendered = await asyncio.to_thread(
            index.repo_map, budget=budget, task_text=arguments.get("task") or ""
        )
        return json.dumps(
            {
                "op": op,
                "workspace": workspace,
                "files_indexed": len(index.contents),
                "map": rendered,
            }
        )
    if op == "definition":
        return json.dumps(
            {"op": op, "symbol": symbol, "definitions": index.find_definition(symbol)}
        )
    if op == "references":
        refs = index.find_references(symbol)
        return json.dumps(
            {
                "op": op,
                "symbol": symbol,
                "references": refs[:_CODE_GRAPH_MAX_REFS],
                "truncated": len(refs) > _CODE_GRAPH_MAX_REFS,
            }
        )
    if op == "importers":
        return json.dumps(
            {"op": op, "file": file_arg, "importers": index.importers_of(file_arg)}
        )
    if op == "imports":
        return json.dumps(
            {"op": op, "file": file_arg, "imports": index.imports_of(file_arg)}
        )
    return json.dumps(
        {"op": op, "file": file_arg, "skeleton": index.file_skeleton(file_arg)}
    )


_EVAL_OPS = {"grade", "compare", "list_metrics"}


async def _run_eval(arguments: Dict[str, Any]) -> str:
    """Grade a finished pipeline workspace against eval metrics (R5).

    Read-only, in-process, JSON out (like ``code_graph``/``review``): it
    never writes files, so a grade cannot mutate the workspace it scores.

    Ops:
    * ``grade`` - collect a workspace's artifacts/traces and score them.
      Defaults to the deterministic metrics only (offline, no judge/token
      spend); LLM-judge metrics are opt-in via ``metrics``. The blocking
      file read runs off the event loop.
    * ``compare`` - diff two grade results (``baseline`` + ``current``) and
      flag regressions - the gate primitive.
    * ``list_metrics`` - the built-in metric catalog (name/kind/description).

    Argument errors come back as JSON ``{"error": ...}`` so agent callers
    can self-correct, matching ``_run_code_graph``.
    """
    from ag_core.eval import grader
    from ag_core.eval.compare import compare as compare_grades
    from ag_core.eval.metrics import BUILTIN_METRICS, DEFAULT_METRICS

    op = (arguments.get("op") or "grade").strip().lower()
    if op not in _EVAL_OPS:
        return json.dumps(
            {"error": f"Unknown op: {op}. Valid ops: {sorted(_EVAL_OPS)}"}
        )

    if op == "list_metrics":
        return json.dumps(
            {
                "op": op,
                "metrics": [
                    {"name": m.name, "kind": m.kind, "description": m.description}
                    for m in BUILTIN_METRICS.values()
                ],
            }
        )

    if op == "compare":
        baseline = arguments.get("baseline")
        current = arguments.get("current")
        if not isinstance(baseline, dict) or not isinstance(current, dict):
            return json.dumps(
                {
                    "error": (
                        "compare requires 'baseline' and 'current' grade "
                        "objects (from a prior eval grade)."
                    )
                }
            )
        return json.dumps({"op": op, **compare_grades(baseline, current)})

    # op == "grade"
    workspace = arguments.get("workspace") or os.getcwd()
    if not os.path.isdir(workspace):
        return json.dumps({"error": f"Workspace directory not found: {workspace}"})

    metrics = arguments.get("metrics") or list(DEFAULT_METRICS)
    if isinstance(metrics, str):
        metrics = [m.strip() for m in metrics.split(",") if m.strip()]
    unknown = [m for m in metrics if m not in BUILTIN_METRICS]
    if unknown:
        return json.dumps(
            {
                "error": (
                    f"Unknown metric(s): {unknown}. "
                    f"Valid metrics: {sorted(BUILTIN_METRICS)}"
                )
            }
        )

    prompt = arguments.get("prompt") or ""
    case = await asyncio.to_thread(grader.collect_case, workspace, prompt)
    needs_judge = any(BUILTIN_METRICS[m].kind == "llm" for m in metrics)
    judge = None
    if needs_judge:
        from ag_core.eval.judge import default_judge

        judge = default_judge()
    result = await grader.grade_case(case, metrics, judge=judge)
    result["op"] = op
    result["workspace"] = workspace
    return json.dumps(result)


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
                "require_approval": {
                    "type": "boolean",
                    "description": (
                        "Pause after each stage (research, design, code) as "
                        "'awaiting_approval' until orchestrate_approve / "
                        "orchestrate_reject is called (sequential pipeline only)"
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "orchestrate_approve",
        "description": (
            "Approve the stage a paused orchestrate job is waiting on "
            "(status 'awaiting_approval') so the pipeline continues to the "
            "next stage."
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
        "name": "orchestrate_reject",
        "description": (
            "Reject the stage a paused orchestrate job is waiting on: the "
            "pipeline stops and the job is marked failed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job_id returned by orchestrate",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason recorded in the job error",
                },
            },
            "required": ["job_id"],
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
    {
        "name": "code_graph",
        "description": (
            "Query the workspace's code graph in-process (CodexGraph-style, "
            "read-only, no graph DB): where a symbol is defined or "
            "referenced, what a file imports / what imports it, a file's "
            "signature skeleton, or an aider-style ranked repo map under a "
            "token budget. Python is parsed with stdlib ast; JS/TS/Go via "
            "tree-sitter when installed. Returns JSON."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": [
                        "map",
                        "definition",
                        "references",
                        "importers",
                        "imports",
                        "skeleton",
                    ],
                    "description": "Query type (default: map)",
                },
                "workspace": {
                    "type": "string",
                    "description": "Workspace directory to index (default: server cwd)",
                },
                "symbol": {
                    "type": "string",
                    "description": "Symbol name (required for definition/references)",
                },
                "file": {
                    "type": "string",
                    "description": (
                        "Repo-relative file path (required for "
                        "importers/imports/skeleton)"
                    ),
                },
                "task": {
                    "type": "string",
                    "description": "Optional task text to personalize op=map ranking",
                },
                "budget": {
                    "type": "integer",
                    "description": "Token budget for op=map (default 32000)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "eval",
        "description": (
            "Grade a finished pipeline workspace against eval metrics "
            "(R5 eval flywheel), in-process and read-only (JSON out, never "
            "writes files). op=grade scores a workspace's artifacts/traces; "
            "op=compare diffs two grades and flags regressions; "
            "op=list_metrics lists the built-in metrics. grade defaults to "
            "the deterministic metrics (artifacts_present, design_wellformed, "
            "code_syntax_valid) which run offline; LLM-as-judge metrics "
            "(task_success, grounding, design_quality, final_response_quality) "
            "are opt-in via 'metrics' and call a provider."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": ["grade", "compare", "list_metrics"],
                    "description": "Operation (default: grade)",
                },
                "workspace": {
                    "type": "string",
                    "description": (
                        "Workspace to grade for op=grade (default: server cwd)"
                    ),
                },
                "metrics": {
                    "type": "string",
                    "description": (
                        "op=grade: comma-separated metric names "
                        "(default: the deterministic set). Use op=list_metrics "
                        "to see all names."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "op=grade: the original user request, used by the "
                        "task_success/grounding judge metrics"
                    ),
                },
                "baseline": {
                    "type": "object",
                    "description": "op=compare: the baseline grade result",
                },
                "current": {
                    "type": "object",
                    "description": "op=compare: the current grade result",
                },
            },
            "required": [],
        },
    },
    {
        "name": "notebooklm_list",
        "description": (
            "List the NotebookLM notebooks on the authenticated account "
            "(id + title + source_count), via the local `nlm` CLI. Read-only. "
            "Use it to discover a notebook id to pass to notebooklm_query. "
            "Requires a one-time `nlm login` and GENIUS_NLM_PATH set."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "notebooklm_query",
        "description": (
            "Ask a question against an EXISTING NotebookLM notebook and get a "
            "grounded, cited answer (the model answers only from that "
            "notebook's sources). Returns JSON with 'answer', 'citations' and "
            "'references'. Read-only; needs `nlm login`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "notebook": {
                    "type": "string",
                    "description": "Notebook id or alias (see notebooklm_list)",
                },
                "query": {
                    "type": "string",
                    "description": "The question to ask the notebook's sources",
                },
                "source_ids": {
                    "type": "string",
                    "description": "Optional comma-separated source ids to restrict to",
                },
                "conversation_id": {
                    "type": "string",
                    "description": "Optional conversation id for follow-up questions",
                },
            },
            "required": ["notebook", "query"],
        },
    },
    {
        "name": "notebooklm_research",
        "description": (
            "Deep-research a topic with NotebookLM and answer from the sources "
            "it finds. MUTATES: discovers web/Drive sources, imports them into "
            "a notebook (a new one unless 'notebook' is given), then queries "
            "it. Returns JSON with 'notebook_id' and a cited 'answer'. Runs "
            "synchronously - mode 'fast' ~30s, 'deep' ~5min (raise "
            "GENIUS_NLM_RESEARCH_TIMEOUT for deep). Needs `nlm login`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The research topic to search for",
                },
                "mode": {
                    "type": "string",
                    "enum": ["fast", "deep"],
                    "description": "fast (~30s, ~10 sources) or deep (~5min, ~40, web only); default fast",
                },
                "source": {
                    "type": "string",
                    "enum": ["web", "drive"],
                    "description": "Where to search for new sources (default web)",
                },
                "notebook": {
                    "type": "string",
                    "description": "Optional existing notebook id to enrich (default: create a new one)",
                },
                "title": {
                    "type": "string",
                    "description": "Optional title for the new notebook (when none is given)",
                },
                "question": {
                    "type": "string",
                    "description": "Optional final question to ask (defaults to the research query)",
                },
            },
            "required": ["query"],
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


# artifact file name -> workspace of the most recent job observed (by
# orchestrate_status/_stage_progress) to have produced it. resources/read
# and resources/list serve the CWD first (the long-standing behavior), then
# fall back here: orchestrate jobs default to isolated .genius_jobs/<id>
# workspaces, so without this fallback every artifacts_ready URI advertised
# by orchestrate_status would 404 (-32002) on a follow-up resources/read.
_ARTIFACT_WORKSPACES: Dict[str, str] = {}


def _list_resources(workspace: str = None) -> List[Dict[str, str]]:
    """Enumerate the whitelisted artifacts that exist in the workspace
    (or, failing that, in the last job workspace observed to hold them)."""
    root = workspace or os.getcwd()
    resources = []
    for name, desc in _resource_catalog().items():
        present = os.path.isfile(os.path.join(root, name))
        if not present:
            alt = _ARTIFACT_WORKSPACES.get(name)
            present = bool(
                alt and alt != root and os.path.isfile(os.path.join(alt, name))
            )
        if present:
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
    root = workspace or os.getcwd()
    candidates = [os.path.join(root, name)]
    alt = _ARTIFACT_WORKSPACES.get(name)
    if alt and alt != root:
        candidates.append(os.path.join(alt, name))
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        return [{"uri": uri, "mimeType": "text/markdown", "text": text}]
    raise ResourceNotFoundError(
        f"Artifact '{name}' does not exist yet - the pipeline stage that "
        "produces it has not completed. Poll orchestrate_status."
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
            # Advertising this URI commits us to serving it: remember which
            # workspace holds the artifact so resources/read can find it.
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


async def _run_orchestration(
    job_id: str,
    prompt: str,
    pipeline: str,
    workspace: str = None,
    require_approval: bool = False,
) -> None:
    job = ORCHESTRATION_JOBS[job_id]
    try:
        # Imported lazily so the MCP server boots fast (initialize/tools-list
        # must respond before Antigravity's connect timeout).
        from orchestrator import run_pipeline, run_e2e_pipeline

        stage_gate = None
        if require_approval:

            async def _approval_gate(stage: str) -> None:
                # Pause at a stage boundary until orchestrate_approve /
                # orchestrate_reject flips the event. Artifacts are collected
                # first so the reviewer can read the stage output while the
                # job is paused.
                job["awaiting_stage"] = stage
                job["status"] = "awaiting_approval"
                job["artifacts"] = _collect_artifacts(workspace or os.getcwd())
                job["approval_event"].clear()
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

            stage_gate = _approval_gate

        if pipeline == "e2e":
            await run_e2e_pipeline(prompt, workspace=workspace)
        else:
            await run_pipeline(prompt, workspace=workspace, stage_gate=stage_gate)
        job["artifacts"] = _collect_artifacts(workspace or os.getcwd())
        job["status"] = "completed"
    except Exception as e:  # noqa: BLE001 - surface any pipeline failure to the client
        job["status"] = "failed"
        job["error"] = str(e)
    finally:
        job["finished_at"] = time.time()


def _jobs_root() -> str:
    """Root directory for per-job workspaces (override via GENIUS_JOBS_DIR)."""
    return os.environ.get("GENIUS_JOBS_DIR") or os.path.join(
        os.getcwd(), ".genius_jobs"
    )


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
        require_approval = bool(arguments.get("require_approval", False))
        if require_approval and pipeline == "e2e":
            raise ValueError(
                "require_approval is only supported for the 'sequential' "
                "pipeline (the e2e variant has no stage gates)."
            )
        job_id = uuid.uuid4().hex
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
            "awaiting_stage": None,
            "approval_event": asyncio.Event(),
            "rejected": False,
            "reject_reason": None,
        }
        _prune_finished_jobs()
        task = asyncio.create_task(
            _run_orchestration(job_id, prompt, pipeline, workspace, require_approval)
        )
        # Hold a strong ref until the task finishes so it isn't GC-cancelled.
        _ORCHESTRATION_TASKS.add(task)
        task.add_done_callback(_ORCHESTRATION_TASKS.discard)
        message = "Pipeline started. Poll orchestrate_status with this job_id."
        if require_approval:
            message = (
                "Pipeline started WITH approval gates: after each stage "
                "(research, design, code) the job pauses as "
                "'awaiting_approval' — review the artifacts from "
                "orchestrate_status, then call orchestrate_approve (or "
                "orchestrate_reject) with this job_id to continue."
            )
        return json.dumps({"job_id": job_id, "status": "running", "message": message})

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
        if job.get("awaiting_stage"):
            view["awaiting_stage"] = job["awaiting_stage"]
        # Expose artifacts while paused too, so the reviewer can read the
        # stage output before approving/rejecting.
        if job["status"] in ("completed", "awaiting_approval"):
            view["artifacts"] = job["artifacts"]
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
        return json.dumps(
            {
                "job_id": job_id,
                "stage": stage,
                "action": ("rejected" if name == "orchestrate_reject" else "approved"),
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
        uvicorn.run("mcp_server:app", host=host, port=port)
