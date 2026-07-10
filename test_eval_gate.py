"""Tests for the R5 post-run eval gate (Wave 4)."""

import json
import os

import pytest

import orchestrator
from ag_core.eval.gate import run_eval_gate

_GOOD_DESIGN = (
    "```json\n"
    '{"project_name": "demo", "description": "d", '
    '"files": [{"path": "a.py", "specification": "x"}]}\n'
    "```"
)


def _workspace(tmp_path):
    (tmp_path / "research.md").write_text("found X", encoding="utf-8")
    (tmp_path / "design.md").write_text(_GOOD_DESIGN, encoding="utf-8")
    (tmp_path / "review.md").write_text("ok", encoding="utf-8")
    (tmp_path / "a.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    return str(tmp_path)


def _eval_file(ws, name):
    return os.path.join(ws, "logs", "eval", name)


@pytest.mark.asyncio
async def test_gate_first_run_writes_score_and_baseline(tmp_path):
    ws = _workspace(tmp_path)
    res = await run_eval_gate(ws, now=1000)

    assert res["compare"] is None  # no prior baseline
    assert res["grade"]["overall"] > 0
    assert os.path.exists(_eval_file(ws, "score-1000.json"))

    with open(_eval_file(ws, "baseline.json"), encoding="utf-8") as f:
        baseline = json.load(f)
    assert baseline["metrics"]["design_wellformed"]["score"] == 5.0


@pytest.mark.asyncio
async def test_gate_detects_regression_and_preserves_baseline(tmp_path):
    ws = _workspace(tmp_path)
    await run_eval_gate(ws, now=1000)  # good baseline

    # Degrade the design so the next grade regresses.
    (tmp_path / "design.md").write_text("just prose, no json", encoding="utf-8")
    res = await run_eval_gate(ws, now=2000)

    diff = res["compare"]
    assert diff is not None
    assert diff["regressed"] is True
    assert "design_wellformed" in diff["regressions"]

    # Baseline must NOT be overwritten by the regressed run.
    with open(_eval_file(ws, "baseline.json"), encoding="utf-8") as f:
        baseline = json.load(f)
    assert baseline["metrics"]["design_wellformed"]["score"] == 5.0
    # Both score snapshots are kept for history.
    assert os.path.exists(_eval_file(ws, "score-1000.json"))
    assert os.path.exists(_eval_file(ws, "score-2000.json"))


def test_eval_gate_off_under_pytest(monkeypatch):
    # Even with the opt-in env set, the gate stays OFF under pytest so the
    # fixed-mock pipeline tests never see it.
    monkeypatch.setenv("GENIUS_EVAL_GATE", "1")
    assert orchestrator.eval_gate_enabled() is False


@pytest.mark.asyncio
async def test_maybe_run_eval_gate_is_noop_when_disabled(tmp_path):
    # _maybe_run_eval_gate must be a safe no-op when the gate is disabled
    # (the default): no logs/eval directory is created.
    ws = _workspace(tmp_path)
    await orchestrator._maybe_run_eval_gate(ws, "build a demo")
    assert not os.path.isdir(os.path.join(ws, "logs", "eval"))
