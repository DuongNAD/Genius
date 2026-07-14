"""Quality-reporting fixes driven by a real failed Next.js job (student
management, job b8f1df13) + the 2026-07-14 quality-system audit:

- string-typed ``"blocking"`` verdicts must not invert the gates
- the eval gate must grade the WORKSPACE root and keep its JSON out of the
  deliverable; the grader must skip ``.genius`` and read the real trace path
- ``design_wellformed`` must agree with the pipeline's own DesignPlan parser
- review.md must carry an honest "Verification coverage" breakdown
- the MCP custom ``review`` checkpoint must key on the final-review marker
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orchestrator  # noqa: E402
from ag_core.eval.gate import run_eval_gate  # noqa: E402
from ag_core.eval.grader import collect_case  # noqa: E402
from ag_core.eval.metrics import BUILTIN_METRICS  # noqa: E402
from ag_core.orchestration_helpers import parse_security_verdict  # noqa: E402

import mcp_server  # noqa: E402


# ------------------------------------------------------- verdict coercion


def test_blocking_string_false_is_not_blocking():
    v = parse_security_verdict('{"blocking": "false", "findings": []}')
    assert v["blocking"] is False
    assert orchestrator.security_is_blocking('{"blocking": "false"}') is False


def test_blocking_string_true_is_blocking():
    v = parse_security_verdict('```json\n{"blocking": "TRUE"}\n```')
    assert v["blocking"] is True
    assert orchestrator.security_is_blocking('{"blocking": "yes"}') is True


def test_blocking_real_booleans_unchanged():
    assert parse_security_verdict('{"blocking": true}')["blocking"] is True
    assert parse_security_verdict('{"blocking": false}')["blocking"] is False


# ------------------------------------------------------- design scope note


def test_design_scope_note_marks_exclusions_accepted():
    note = orchestrator.design_scope_note("Do NOT introduce authentication.")
    assert "accepted" in note and "blocking" in note
    assert "Do NOT introduce authentication." in note
    assert orchestrator.design_scope_note("") == ""


# ------------------------------------------------------- verification modes


def test_verification_mode_classification():
    assert "non-Python" in orchestrator.verification_mode("src/app/route.ts")
    assert "designed test module" in orchestrator.verification_mode(
        "tests/test_app.py"
    )
    assert "pytest infrastructure" in orchestrator.verification_mode("conftest.py")
    assert (
        orchestrator.verification_mode("src/lib.py") == "generated pytest module executed"
    )


# ------------------------------------------------------- eval: layered layout


def _layered_workspace(tmp_path):
    """The REAL post-separation layout: root artifacts + deliverable + .genius."""
    ws = tmp_path / "ws"
    (ws / "projects" / "demo").mkdir(parents=True)
    (ws / ".genius" / "demo" / "tests").mkdir(parents=True)
    (ws / ".genius" / "demo" / "logs" / "raw").mkdir(parents=True)
    (ws / "research.md").write_text("findings", encoding="utf-8")
    (ws / "design.md").write_text(
        '```json\n{"project_name": "demo", "files": '
        '[{"path": "app.py", "specification": "spec"}]}\n```',
        encoding="utf-8",
    )
    (ws / "review.md").write_text("All files verified.", encoding="utf-8")
    (ws / "projects" / "demo" / "app.py").write_text("x = 1\n", encoding="utf-8")
    # Pipeline internals that must NOT leak into the grade:
    (ws / ".genius" / "demo" / "tests" / "test_app_gen.py").write_text(
        "def broken(:\n", encoding="utf-8"
    )
    (ws / ".genius" / "demo" / "logs" / "raw" / "codex_app_attempt1.md").write_text(
        "raw trace body", encoding="utf-8"
    )
    return str(ws)


def test_collect_case_reads_layered_layout(tmp_path):
    case = collect_case(_layered_workspace(tmp_path), prompt="p")
    assert case["design"].startswith("```json")
    assert case["research"] and case["review"]
    assert list(case["code_files"]) == ["projects/demo/app.py"]  # .genius pruned
    assert "raw trace body" in case["trace"]  # new trace location found


def test_deterministic_metrics_score_layered_layout(tmp_path):
    case = collect_case(_layered_workspace(tmp_path))
    for name in ("artifacts_present", "code_syntax_valid", "design_wellformed"):
        res = BUILTIN_METRICS[name].evaluate(case)
        assert res["score"] == 5.0, (name, res["explanation"])


def test_eval_gate_writes_outside_deliverable(tmp_path):
    ws = _layered_workspace(tmp_path)
    eval_dir = os.path.join(ws, ".genius", "demo", "logs", "eval")
    result = asyncio.run(run_eval_gate(ws, now=1000.0, eval_dir=eval_dir))
    assert result["score_path"].startswith(eval_dir)
    assert os.path.exists(os.path.join(eval_dir, "baseline.json"))
    assert not os.path.exists(os.path.join(ws, "logs"))  # default path unused


def test_maybe_run_eval_gate_targets_workspace_root(tmp_path, monkeypatch):
    ws = _layered_workspace(tmp_path)
    project_dir = os.path.join(ws, "projects", "demo")
    seen = {}

    async def fake_gate(root, prompt="", eval_dir=None, **kw):
        seen["root"], seen["eval_dir"] = root, eval_dir
        return {"grade": {"overall": 5.0}, "compare": None, "score_path": "s"}

    import ag_core.eval.gate as gate_mod

    monkeypatch.setattr(gate_mod, "run_eval_gate", fake_gate)
    monkeypatch.setattr(orchestrator, "eval_gate_enabled", lambda: True)
    asyncio.run(orchestrator._maybe_run_eval_gate(project_dir, "p"))
    assert seen["root"] == ws
    assert seen["eval_dir"] == os.path.join(
        orchestrator.pipeline_internal_dir(project_dir), "logs", "eval"
    )


# ------------------------------------------------------- design_wellformed


def _score_design(text):
    res = BUILTIN_METRICS["design_wellformed"].evaluate({"design": text})
    return res["score"], res["explanation"]


def test_design_example_block_before_real_plan_is_wellformed():
    text = (
        "Example format:\n```json\n{\"note\": \"just an example\"}\n```\n"
        "The plan:\n```json\n{\"files\": "
        "[{\"path\": \"a.py\", \"specification\": \"s\"}]}\n```"
    )
    score, detail = _score_design(text)
    assert score == 5.0, detail  # pipeline accepts this; the metric must too


def test_design_missing_project_name_is_wellformed():
    # DesignPlan.project_name is Optional — the pipeline accepts this design.
    score, detail = _score_design(
        '{"files": [{"path": "a.py", "specification": "s"}]}'
    )
    assert score == 5.0, detail


def test_design_files_as_strings_is_rejected_like_pipeline():
    # pydantic rejects files-as-strings; the old metric scored it 5.0.
    score, detail = _score_design('{"project_name": "x", "files": ["a.py"]}')
    assert score == 3.0, detail


def test_design_no_json_still_low():
    assert _score_design("prose only")[0] == 1.0
    assert _score_design("")[0] == 0.0


# ------------------------------------------------------- MCP review marker


def _custom_job(ws, job_id="a" * 32):
    return {
        "job_id": job_id,
        "workspace": str(ws),
        "pipeline": "custom",
        "started_at": 0,  # every artifact mtime is fresh vs 0
        "status": "running",
    }


def test_custom_review_stage_pending_without_final_review_marker(tmp_path):
    (tmp_path / "review.md").write_text("code stage summary", encoding="utf-8")
    stages, _ = mcp_server._stage_progress(_custom_job(tmp_path))
    by_name = {s["stage"]: s["state"] for s in stages}
    assert by_name["code"] == "done"
    assert by_name["review"] == "pending"


def test_custom_review_stage_done_with_marker(tmp_path):
    (tmp_path / "review.md").write_text(
        "summary\n\n## Final review (approved)\nok", encoding="utf-8"
    )
    stages, _ = mcp_server._stage_progress(_custom_job(tmp_path))
    by_name = {s["stage"]: s["state"] for s in stages}
    assert by_name["review"] == "done"


# ------------------------------------------------------- quality ladder (#2)


def test_file_quality_state_ladder():
    assert orchestrator.file_quality_state("src/lib.py") == (
        "tested, security-accepted"
    )
    assert "NOT tested: non-Python" in orchestrator.file_quality_state(
        "src/app/route.ts"
    )
    assert orchestrator.file_quality_state("tests/test_app.py").startswith("tested")
    assert "pytest infrastructure" in orchestrator.file_quality_state("conftest.py")
    assert orchestrator.file_quality_state("a.py", failed=True) == (
        "generated-only (verification FAILED)"
    )


def test_release_ready_flag_read_from_review_md(tmp_path):
    (tmp_path / "review.md").write_text(
        "summary\n\n## Release readiness\nrelease-ready: YES — all good",
        encoding="utf-8",
    )
    job = {
        "job_id": "b" * 32,
        "status": "completed",
        "workspace": str(tmp_path),
        "pipeline": "custom",
        "started_at": 0,
        "artifacts": {},
        "error": None,
    }
    import json as _json

    mcp_server.ORCHESTRATION_JOBS[job["job_id"]] = job
    try:
        raw = asyncio.run(
            mcp_server.dispatch_tool(
                "orchestrate_status", {"job_id": job["job_id"]}
            )
        )
    finally:
        mcp_server.ORCHESTRATION_JOBS.pop(job["job_id"], None)
    view = _json.loads(raw)
    assert view.get("release_ready") is True
