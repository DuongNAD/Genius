"""Before/after score comparison for the eval flywheel (R5).

The regression gate: given a baseline grade and a current grade, report
which metrics moved and whether any regressed past ``threshold``. Mirrors
google/agents-cli's ``eval compare`` - the step that turns grading into a
gate instead of a vanity number.

Accepts either a full grade result (``{"metrics": {name: {"score": ...}}}``)
or a plain ``{name: score}`` map on both sides, so it composes with both
``grade_case`` output and hand-built score dicts.
"""

import statistics
from typing import Dict, Optional

# Default: a drop of half a point (on the 1-5 scale) or more counts as a
# regression. Tunable per call / via config in the orchestrator gate.
DEFAULT_THRESHOLD = 0.5


def _score_map(grade_or_scores: dict) -> Dict[str, float]:
    """Normalize either grade shape into ``{metric_name: score}``."""
    if not isinstance(grade_or_scores, dict):
        return {}
    metrics = grade_or_scores.get("metrics")
    if isinstance(metrics, dict):
        out: Dict[str, float] = {}
        for name, entry in metrics.items():
            if isinstance(entry, dict) and "score" in entry:
                out[name] = float(entry["score"])
            elif isinstance(entry, (int, float)):
                out[name] = float(entry)
        return out
    # Plain {name: score} map.
    return {
        name: float(val)
        for name, val in grade_or_scores.items()
        if isinstance(val, (int, float))
    }


def _mean(scores) -> Optional[float]:
    vals = [s for s in scores if s > 0]
    return round(statistics.fmean(vals), 2) if vals else None


def compare(
    baseline: dict, current: dict, *, threshold: float = DEFAULT_THRESHOLD
) -> dict:
    """Diff two grades. Returns per-metric deltas + regression/improvement
    lists + a top-level ``regressed`` flag for a gate to act on.
    """
    base = _score_map(baseline)
    cur = _score_map(current)

    per_metric: Dict[str, dict] = {}
    regressions, improvements = [], []
    for name in sorted(set(base) | set(cur)):
        b = base.get(name)
        c = cur.get(name)
        delta = round(c - b, 2) if (b is not None and c is not None) else None
        per_metric[name] = {"baseline": b, "current": c, "delta": delta}
        if delta is None:
            continue
        if delta <= -threshold:
            regressions.append(name)
        elif delta >= threshold:
            improvements.append(name)

    overall_b = _mean(base.values())
    overall_c = _mean(cur.values())
    overall_delta = (
        round(overall_c - overall_b, 2)
        if (overall_b is not None and overall_c is not None)
        else None
    )
    return {
        "per_metric": per_metric,
        "regressions": regressions,
        "improvements": improvements,
        "overall_baseline": overall_b,
        "overall_current": overall_c,
        "overall_delta": overall_delta,
        "threshold": threshold,
        "regressed": bool(regressions),
    }
