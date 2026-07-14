"""Post-run eval gate (R5 Wave 4).

Grade a finished pipeline workspace, persist the score under
``logs/eval/``, and compare against the previous baseline to flag quality
regressions between runs.

Opt-in and OFF under pytest (the orchestrator only calls this when
``GENIUS_EVAL_GATE`` is set and not under pytest). It defaults to the
deterministic metric set, so the gate runs OFFLINE - no judge, no token
spend, no CLI - and writes only inside the job's own workspace. The
baseline is preserved on a regressed run (not overwritten), so a
regression keeps showing until it is fixed.
"""

import json
import logging
import os
import time
from typing import List, Optional

from ag_core.eval.compare import compare
from ag_core.eval.grader import grade

_EVAL_SUBDIR = ("logs", "eval")
_BASELINE = "baseline.json"


def _eval_dir(workspace: str) -> str:
    return os.path.join(workspace, *_EVAL_SUBDIR)


def _write_json(path: str, obj: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


async def run_eval_gate(
    workspace: str,
    *,
    prompt: str = "",
    metrics: Optional[List[str]] = None,
    judge=None,
    now: Optional[float] = None,
    config=None,
    eval_dir: Optional[str] = None,
) -> dict:
    """Grade ``workspace``, persist the score, diff vs the last baseline.

    Returns ``{"grade", "compare", "score_path", "baseline_path"}``.
    ``compare`` is ``None`` when there is no prior baseline. ``now`` is
    injectable so the persisted ``score-<ts>.json`` filename is
    deterministic in tests. ``eval_dir`` overrides where the score/baseline
    JSON is persisted (the orchestrator passes the pipeline-internal
    ``.genius/<slug>/logs/eval`` so eval state never lands in a
    deliverable); default stays ``<workspace>/logs/eval``.
    """
    result = await grade(workspace, metrics, prompt=prompt, judge=judge, config=config)

    eval_dir = eval_dir or _eval_dir(workspace)
    os.makedirs(eval_dir, exist_ok=True)
    ts = int(now if now is not None else time.time())
    score_path = os.path.join(eval_dir, f"score-{ts}.json")
    _write_json(score_path, result)

    baseline_path = os.path.join(eval_dir, _BASELINE)
    diff = None
    baseline = _read_json(baseline_path)
    # A baseline file that exists but won't parse is NOT the same as "no
    # baseline yet": overwriting it would silently reset the quality bar and
    # hide a regression. Detect that case and preserve the file instead.
    baseline_corrupt = baseline is None and os.path.exists(baseline_path)
    if baseline is not None:
        diff = compare(baseline, result)

    if baseline_corrupt:
        logging.getLogger(__name__).warning(
            "Eval baseline %s is unreadable/corrupt; skipping the regression "
            "comparison and preserving it (not resetting the quality bar).",
            baseline_path,
        )
    elif diff is None or not diff.get("regressed"):
        # No baseline yet, or a non-regressed run: (re)write the baseline. Keep
        # the previous baseline when this run regressed, so the regression stays
        # visible on the next comparison until it is actually fixed.
        _write_json(baseline_path, result)

    return {
        "grade": result,
        "compare": diff,
        "score_path": score_path,
        "baseline_path": baseline_path,
    }
