"""Tests for the MCP ``eval`` tool (R5 eval flywheel)."""

import json

import pytest

import mcp_server

_GOOD_DESIGN = (
    "```json\n"
    '{"project_name": "demo", "description": "d", '
    '"files": [{"path": "a.py", "specification": "x"}]}\n'
    "```"
)


def _write_workspace(tmp_path):
    (tmp_path / "research.md").write_text("found X", encoding="utf-8")
    (tmp_path / "design.md").write_text(_GOOD_DESIGN, encoding="utf-8")
    (tmp_path / "review.md").write_text("ok", encoding="utf-8")
    (tmp_path / "a.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def bad(:\n    pass\n", encoding="utf-8")
    return str(tmp_path)


@pytest.mark.asyncio
async def test_eval_grade_default_metrics_offline(tmp_path):
    ws = _write_workspace(tmp_path)
    res = json.loads(
        await mcp_server.dispatch_tool("eval", {"op": "grade", "workspace": ws})
    )
    assert res["op"] == "grade"
    assert res["workspace"] == ws
    m = res["metrics"]
    assert m["artifacts_present"]["score"] == 5.0
    assert m["design_wellformed"]["score"] == 5.0
    assert m["code_syntax_valid"]["score"] == 3.0  # 1 of 2 py files parse
    # overall = mean(5, 5, 3)
    assert res["overall"] == 4.33
    # default set is deterministic -> no judge/CLI was invoked
    assert all(entry["kind"] == "code" for entry in m.values())


@pytest.mark.asyncio
async def test_eval_grade_op_is_default(tmp_path):
    ws = _write_workspace(tmp_path)
    res = json.loads(await mcp_server.dispatch_tool("eval", {"workspace": ws}))
    assert res["op"] == "grade"
    assert "artifacts_present" in res["metrics"]


@pytest.mark.asyncio
async def test_eval_list_metrics():
    res = json.loads(await mcp_server.dispatch_tool("eval", {"op": "list_metrics"}))
    names = {m["name"] for m in res["metrics"]}
    assert {"artifacts_present", "task_success", "design_quality"} <= names
    kinds = {m["name"]: m["kind"] for m in res["metrics"]}
    assert kinds["artifacts_present"] == "code"
    assert kinds["task_success"] == "llm"


@pytest.mark.asyncio
async def test_eval_compare():
    baseline = {"metrics": {"a": {"score": 4.0}, "b": {"score": 3.0}}}
    current = {"metrics": {"a": {"score": 5.0}, "b": {"score": 2.0}}}
    res = json.loads(
        await mcp_server.dispatch_tool(
            "eval", {"op": "compare", "baseline": baseline, "current": current}
        )
    )
    assert res["op"] == "compare"
    assert res["regressed"] is True
    assert res["regressions"] == ["b"]
    assert res["improvements"] == ["a"]


@pytest.mark.asyncio
async def test_eval_argument_errors(tmp_path):
    ws = _write_workspace(tmp_path)
    bad_op = json.loads(
        await mcp_server.dispatch_tool("eval", {"op": "explode", "workspace": ws})
    )
    assert "Unknown op" in bad_op["error"]

    bad_metric = json.loads(
        await mcp_server.dispatch_tool(
            "eval", {"op": "grade", "workspace": ws, "metrics": "made_up_metric"}
        )
    )
    assert "Unknown metric" in bad_metric["error"]

    bad_ws = json.loads(
        await mcp_server.dispatch_tool(
            "eval", {"op": "grade", "workspace": str(tmp_path / "nope")}
        )
    )
    assert "not found" in bad_ws["error"]

    bad_compare = json.loads(
        await mcp_server.dispatch_tool("eval", {"op": "compare", "baseline": {}})
    )
    assert "compare requires" in bad_compare["error"]


@pytest.mark.asyncio
async def test_eval_metrics_csv_string(tmp_path):
    ws = _write_workspace(tmp_path)
    res = json.loads(
        await mcp_server.dispatch_tool(
            "eval",
            {
                "op": "grade",
                "workspace": ws,
                "metrics": "artifacts_present, design_wellformed",
            },
        )
    )
    assert set(res["metrics"]) == {"artifacts_present", "design_wellformed"}


@pytest.mark.asyncio
async def test_tools_list_includes_eval():
    res = await mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    tools = {t["name"]: t for t in res["result"]["tools"]}
    assert "eval" in tools
    assert tools["eval"]["description"]
    assert tools["eval"]["input_schema"]["type"] == "object"
