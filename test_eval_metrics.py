"""Unit tests for the eval flywheel core (R5): metrics, grader, judge
parsing and before/after comparison. No provider/CLI is touched - LLM
metrics are driven by an injected fake judge.
"""

import pytest

from ag_core.eval import compare
from ag_core.eval.grader import collect_case, grade_case
from ag_core.eval.metrics import (
    BUILTIN_METRICS,
    DEFAULT_METRICS,
    LLMMetric,
    parse_verdict,
)

_GOOD_DESIGN = (
    "```json\n"
    '{"project_name": "demo", "description": "d", '
    '"files": [{"path": "a.py", "specification": "x"}]}\n'
    "```"
)


def _write_workspace(tmp_path, *, with_bad_py=True):
    (tmp_path / "research.md").write_text("found X and Y", encoding="utf-8")
    (tmp_path / "design.md").write_text(_GOOD_DESIGN, encoding="utf-8")
    (tmp_path / "review.md").write_text("looks fine", encoding="utf-8")
    (tmp_path / "a.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    if with_bad_py:
        (tmp_path / "b.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    raw = tmp_path / "logs" / "raw"
    raw.mkdir(parents=True)
    (raw / "research_attempt1.md").write_text("raw research", encoding="utf-8")
    return str(tmp_path)


# --------------------------------------------------------------------------
# Deterministic metrics
# --------------------------------------------------------------------------


def test_artifacts_present_all(tmp_path):
    ws = _write_workspace(tmp_path)
    case = collect_case(ws)
    res = BUILTIN_METRICS["artifacts_present"].evaluate(case)
    assert res["score"] == 5.0
    assert res["kind"] == "code"


def test_artifacts_present_partial(tmp_path):
    (tmp_path / "research.md").write_text("only research", encoding="utf-8")
    case = collect_case(str(tmp_path))
    res = BUILTIN_METRICS["artifacts_present"].evaluate(case)
    # 1 of 3 present -> scale(1/3) == round(1 + 4/3, 2)
    assert res["score"] == 2.33
    assert "design" in res["explanation"]


def test_code_syntax_valid_mixed(tmp_path):
    ws = _write_workspace(tmp_path, with_bad_py=True)
    case = collect_case(ws)
    res = BUILTIN_METRICS["code_syntax_valid"].evaluate(case)
    assert res["score"] == 3.0  # 1 of 2 parse -> scale(0.5)
    assert "1/2" in res["explanation"]


def test_code_syntax_valid_no_python(tmp_path):
    (tmp_path / "notes.md").write_text("no code here", encoding="utf-8")
    case = collect_case(str(tmp_path))
    res = BUILTIN_METRICS["code_syntax_valid"].evaluate(case)
    assert res["score"] == 0.0  # N/A


def test_design_wellformed_good(tmp_path):
    ws = _write_workspace(tmp_path)
    case = collect_case(ws)
    res = BUILTIN_METRICS["design_wellformed"].evaluate(case)
    assert res["score"] == 5.0


def test_design_wellformed_missing_keys(tmp_path):
    (tmp_path / "design.md").write_text('```json\n{"foo": 1}\n```', encoding="utf-8")
    case = collect_case(str(tmp_path))
    res = BUILTIN_METRICS["design_wellformed"].evaluate(case)
    assert res["score"] == 3.0
    assert "files[]" in res["explanation"]


def test_design_wellformed_unparseable(tmp_path):
    (tmp_path / "design.md").write_text("just prose, no json", encoding="utf-8")
    case = collect_case(str(tmp_path))
    res = BUILTIN_METRICS["design_wellformed"].evaluate(case)
    assert res["score"] == 1.0


# --------------------------------------------------------------------------
# LLM metric + judge parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"score": 4, "explanation": "good"}', (4.0, "good")),
        ('prefix {"score": 5, "explanation": "great"} suffix', (5.0, "great")),
        ('```json\n{"score": 2, "explanation": "meh"}\n```', (2.0, "meh")),
        ("I would say score: 3 out of 5", (3.0, None)),
        ("total nonsense", (0.0, None)),
        ("", (0.0, None)),
        ('{"score": 99}', (5.0, None)),  # clamped to 5
    ],
)
def test_parse_verdict(raw, expected):
    score, explanation = parse_verdict(raw)
    assert score == expected[0]
    if expected[1] is not None:
        assert expected[1] in explanation


@pytest.mark.asyncio
async def test_llm_metric_with_fake_judge():
    captured = {}

    async def fake_judge(prompt):
        captured["prompt"] = prompt
        return '{"score": 4, "explanation": "solid"}'

    metric = LLMMetric("task_success", "Rate this: {prompt} / {design}")
    res = await metric.evaluate({"prompt": "build API", "design": "a plan"}, fake_judge)
    assert res["score"] == 4.0
    assert res["explanation"] == "solid"
    assert res["kind"] == "llm"
    # Placeholders were substituted, literal braces preserved elsewhere.
    assert "build API" in captured["prompt"]
    assert "a plan" in captured["prompt"]


@pytest.mark.asyncio
async def test_grade_case_mixed_metrics(tmp_path):
    ws = _write_workspace(tmp_path)
    case = collect_case(ws, prompt="build a demo")

    async def fake_judge(prompt):
        return '{"score": 5, "explanation": "ok"}'

    result = await grade_case(
        case,
        ["artifacts_present", "code_syntax_valid", "task_success"],
        judge=fake_judge,
    )
    assert result["metrics"]["artifacts_present"]["score"] == 5.0
    assert result["metrics"]["code_syntax_valid"]["score"] == 3.0
    assert result["metrics"]["task_success"]["score"] == 5.0
    # overall = mean(5, 3, 5) excluding zeros
    assert result["overall"] == 4.33


@pytest.mark.asyncio
async def test_grade_case_unknown_metric_raises(tmp_path):
    case = collect_case(str(tmp_path))
    with pytest.raises(ValueError, match="Unknown metric"):
        await grade_case(case, ["does_not_exist"])


def test_default_metrics_are_all_deterministic():
    # The default grade set must run offline (no judge/token spend).
    for name in DEFAULT_METRICS:
        assert BUILTIN_METRICS[name].kind == "code"


# --------------------------------------------------------------------------
# Compare / regression gate
# --------------------------------------------------------------------------


def test_compare_flags_regression_and_improvement():
    baseline = {"metrics": {"a": {"score": 4.0}, "b": {"score": 3.0}}}
    current = {"metrics": {"a": {"score": 5.0}, "b": {"score": 2.0}}}
    diff = compare(baseline, current)
    assert diff["regressed"] is True
    assert diff["regressions"] == ["b"]
    assert diff["improvements"] == ["a"]
    assert diff["per_metric"]["b"]["delta"] == -1.0


def test_compare_no_regression_within_threshold():
    baseline = {"metrics": {"a": {"score": 4.0}}}
    current = {"metrics": {"a": {"score": 3.7}}}  # -0.3, under 0.5 threshold
    diff = compare(baseline, current)
    assert diff["regressed"] is False
    assert diff["regressions"] == []


def test_compare_accepts_plain_score_maps():
    diff = compare({"a": 5.0, "b": 4.0}, {"a": 4.0, "b": 4.0})
    assert diff["per_metric"]["a"]["delta"] == -1.0
    assert diff["regressed"] is True
