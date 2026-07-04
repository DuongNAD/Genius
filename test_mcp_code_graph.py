"""Tests for the MCP ``code_graph`` tool (R4 CodexGraph-lite)."""

import json

import pytest

import mcp_server


def _write_workspace(tmp_path):
    (tmp_path / "util.py").write_text(
        "def helper():\n    return 42\n", encoding="utf-8"
    )
    (tmp_path / "main.py").write_text(
        "import util\n\n\ndef main():\n    return util.helper()\n",
        encoding="utf-8",
    )
    return str(tmp_path)


@pytest.mark.asyncio
async def test_code_graph_definition(tmp_path):
    ws = _write_workspace(tmp_path)
    res = json.loads(
        await mcp_server.dispatch_tool(
            "code_graph", {"op": "definition", "workspace": ws, "symbol": "helper"}
        )
    )
    assert res["definitions"] == [
        {
            "path": "util.py",
            "kind": "function",
            "line": 1,
            "signature": "def helper():",
        }
    ]


@pytest.mark.asyncio
async def test_code_graph_map_default_op(tmp_path):
    ws = _write_workspace(tmp_path)
    res = json.loads(
        await mcp_server.dispatch_tool(
            "code_graph", {"workspace": ws, "task": "fix helper"}
        )
    )
    assert res["op"] == "map"
    assert res["files_indexed"] == 2
    assert "--- util.py ---" in res["map"]
    assert "def helper(): ..." in res["map"]


@pytest.mark.asyncio
async def test_code_graph_imports_importers_skeleton(tmp_path):
    ws = _write_workspace(tmp_path)
    imports = json.loads(
        await mcp_server.dispatch_tool(
            "code_graph", {"op": "imports", "workspace": ws, "file": "main.py"}
        )
    )
    assert imports["imports"] == ["util.py"]
    importers = json.loads(
        await mcp_server.dispatch_tool(
            "code_graph", {"op": "importers", "workspace": ws, "file": "util.py"}
        )
    )
    assert importers["importers"] == ["main.py"]
    skel = json.loads(
        await mcp_server.dispatch_tool(
            "code_graph", {"op": "skeleton", "workspace": ws, "file": "util.py"}
        )
    )
    assert "def helper(): ..." in skel["skeleton"]


@pytest.mark.asyncio
async def test_code_graph_references_word_bound(tmp_path):
    ws = _write_workspace(tmp_path)
    res = json.loads(
        await mcp_server.dispatch_tool(
            "code_graph", {"op": "references", "workspace": ws, "symbol": "helper"}
        )
    )
    assert {r["path"] for r in res["references"]} == {"main.py", "util.py"}
    assert res["truncated"] is False


@pytest.mark.asyncio
async def test_code_graph_argument_errors(tmp_path):
    ws = _write_workspace(tmp_path)
    bad_op = json.loads(
        await mcp_server.dispatch_tool("code_graph", {"op": "explode", "workspace": ws})
    )
    assert "Unknown op" in bad_op["error"]
    missing_symbol = json.loads(
        await mcp_server.dispatch_tool(
            "code_graph", {"op": "definition", "workspace": ws}
        )
    )
    assert "requires a 'symbol'" in missing_symbol["error"]
    missing_file = json.loads(
        await mcp_server.dispatch_tool(
            "code_graph", {"op": "skeleton", "workspace": ws}
        )
    )
    assert "requires a 'file'" in missing_file["error"]
    bad_ws = json.loads(
        await mcp_server.dispatch_tool(
            "code_graph",
            {"op": "map", "workspace": str(tmp_path / "does-not-exist")},
        )
    )
    assert "not found" in bad_ws["error"]


@pytest.mark.asyncio
async def test_tools_list_includes_code_graph():
    res = await mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    tools = {t["name"]: t for t in res["result"]["tools"]}
    assert "code_graph" in tools
    assert tools["code_graph"]["description"]
    assert tools["code_graph"]["input_schema"]["type"] == "object"
