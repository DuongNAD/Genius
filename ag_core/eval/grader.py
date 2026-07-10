"""Collect a workspace's artifacts/traces and grade them (R5).

``collect_case`` turns a finished pipeline workspace into a flat ``case``
dict (the same shape the metrics consume); ``grade_case`` scores that case
against a list of metric names; ``grade`` is the convenience wrapper that
does the (blocking) file read off the event loop.

This reads what the pipeline already writes - root artifacts
(``research.md``/``design.md``/``review.md``), generated ``*.py`` files,
and the raw per-stage traces under ``logs/raw/`` - so no new capture step
is needed.
"""

import os
import statistics
from typing import Dict, List, Optional

from ag_core.eval.metrics import BUILTIN_METRICS, DEFAULT_METRICS, Judge

# Artifact stem -> case field. Matches the pipeline's fixed root filenames.
_ARTIFACT_FILES = {
    "research": "research.md",
    "design": "design.md",
    "review": "review.md",
}

# Directories never walked for generated code (VCS, caches, our own logs,
# agent scratch, deps). Keeps a grade over a large repo bounded.
_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".chroma",
    "logs",
    ".agents",
    "node_modules",
    "venv",
    ".venv",
    "build",
    "dist",
    ".mypy_cache",
}

_MAX_CODE_FILES = 100
_MAX_FILE_BYTES = 200_000
_MAX_TRACE_CHARS = 8000


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _collect_code_files(workspace: str) -> Dict[str, str]:
    """Repo-relative path -> content for generated Python files (bounded)."""
    files: Dict[str, str] = {}
    for root, dirs, names in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            abs_path = os.path.join(root, name)
            try:
                if os.path.getsize(abs_path) > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            rel = os.path.relpath(abs_path, workspace).replace("\\", "/")
            files[rel] = _read_text(abs_path)
            if len(files) >= _MAX_CODE_FILES:
                return files
    return files


def _collect_trace(workspace: str) -> str:
    """A trimmed digest of the raw per-stage traces, for judge context."""
    raw_dir = os.path.join(workspace, "logs", "raw")
    if not os.path.isdir(raw_dir):
        return ""
    parts: List[str] = []
    for name in sorted(os.listdir(raw_dir)):
        if not name.endswith(".md"):
            continue
        parts.append(f"### {name}\n{_read_text(os.path.join(raw_dir, name))}")
        if sum(len(p) for p in parts) >= _MAX_TRACE_CHARS:
            break
    return "\n\n".join(parts)[:_MAX_TRACE_CHARS]


def collect_case(workspace: str, prompt: str = "") -> dict:
    """Build a ``case`` dict from a finished pipeline ``workspace``.

    ``prompt`` is the original user request (the grader cannot recover it
    from disk reliably, so callers pass it through); defaults to "".
    """
    case: dict = {"prompt": prompt or "", "workspace": workspace}
    for field, filename in _ARTIFACT_FILES.items():
        case[field] = _read_text(os.path.join(workspace, filename))
    case["code_files"] = _collect_code_files(workspace)
    case["code"] = "\n\n".join(
        f"# --- {path} ---\n{content}" for path, content in case["code_files"].items()
    )
    case["trace"] = _collect_trace(workspace)
    return case


def _overall(results: Dict[str, dict]) -> float:
    """Mean over metrics that actually applied (score > 0)."""
    scores = [r["score"] for r in results.values() if r.get("score", 0) > 0]
    return round(statistics.fmean(scores), 2) if scores else 0.0


async def grade_case(
    case: dict,
    metrics_to_run: List[str],
    *,
    judge: Optional[Judge] = None,
    config=None,
) -> dict:
    """Score a ``case`` against ``metrics_to_run``.

    Deterministic (``code``) metrics run inline; LLM metrics await the
    ``judge`` callable (lazily built via ``default_judge`` if any LLM metric
    is requested and no judge was supplied). Unknown metric names raise
    ``ValueError`` - callers validate first for a friendlier message.
    """
    results: Dict[str, dict] = {}
    for name in metrics_to_run:
        metric = BUILTIN_METRICS.get(name)
        if metric is None:
            raise ValueError(f"Unknown metric: {name}")
        if metric.kind == "llm":
            if judge is None:
                from ag_core.eval.judge import default_judge

                judge = default_judge(config)
            results[name] = await metric.evaluate(case, judge)
        else:
            results[name] = metric.evaluate(case)
    return {
        "metrics": results,
        "overall": _overall(results),
        "metrics_run": list(metrics_to_run),
    }


async def grade(
    workspace: str,
    metrics_to_run: Optional[List[str]] = None,
    *,
    judge: Optional[Judge] = None,
    prompt: str = "",
    config=None,
) -> dict:
    """Read ``workspace`` off the event loop and grade it.

    Convenience wrapper over ``collect_case`` + ``grade_case`` for the
    orchestrator eval-gate (Wave 4); the MCP tool calls the two steps
    directly so it can validate metric names before touching disk.
    """
    import asyncio

    metrics_to_run = list(metrics_to_run or DEFAULT_METRICS)
    case = await asyncio.to_thread(collect_case, workspace, prompt)
    result = await grade_case(case, metrics_to_run, judge=judge, config=config)
    result["workspace"] = workspace
    return result
