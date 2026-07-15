"""Read-only MCP tool handlers extracted from ``mcp_server.py``.

``_run_code_graph`` (CodexGraph-style structure queries) and ``_run_eval`` (the
R5 eval flywheel) are self-contained — they only touch ``ag_core`` and never the
server's agent-class globals — so they live here to keep ``mcp_server`` focused
on dispatch/transport. ``mcp_server`` re-imports them; ``dispatch_tool`` calls
them through its own namespace, so patching still targets ``mcp_server``.
"""

import asyncio
import json
import os
from typing import Any, Dict

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
    # Default through resolve_workspace_root(), not bare os.getcwd(): an MCP
    # server launched with cwd="/" would otherwise hand grader.collect_case a
    # filesystem root to walk (an unguarded parallel of the agent scan path —
    # bounded to 100 files, but it leaked out-of-project sources to the LLM
    # judge). The env pin (GENIUS_MCP_WORKSPACE) rescues it the same way, and
    # collect_case's own scan-root guard refuses a dangerous default.
    from ag_core.scanner.project_scanner import resolve_workspace_root

    workspace = arguments.get("workspace") or resolve_workspace_root()
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
