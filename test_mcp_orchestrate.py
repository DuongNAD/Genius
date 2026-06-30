"""Tests for the MCP initialize handshake and the orchestrate/orchestrate_status
tools (Phase 3 — Antigravity coordinator integration)."""

import asyncio
import json

import pytest
from unittest.mock import patch, AsyncMock

import mcp_server


# --- MCP JSON-RPC handshake -------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_handshake():
    res = await mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
    )
    assert res["id"] == 1
    assert res["result"]["serverInfo"]["name"] == "genius"
    assert "tools" in res["result"]["capabilities"]
    assert res["result"]["protocolVersion"] == "2024-11-05"


@pytest.mark.asyncio
async def test_initialize_defaults_protocol_version():
    res = await mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {},
        }
    )
    assert res["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION


@pytest.mark.asyncio
async def test_initialized_notification_is_silent():
    res = await mcp_server.handle_request(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert res is None


@pytest.mark.asyncio
async def test_ping_returns_empty_result():
    res = await mcp_server.handle_request({"jsonrpc": "2.0", "id": 7, "method": "ping"})
    assert res["result"] == {}


@pytest.mark.asyncio
async def test_tools_list_includes_orchestrate_and_agents():
    res = await mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
    )
    names = {t["name"] for t in res["result"]["tools"]}
    assert {
        "orchestrate",
        "orchestrate_status",
        "research",
        "design",
        "code",
        "unit_test",
        "security_audit",
        "deploy",
    } <= names


@pytest.mark.asyncio
async def test_unknown_method_with_id_returns_error():
    res = await mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 4, "method": "bogus"}
    )
    assert res["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_unknown_notification_is_silent():
    res = await mcp_server.handle_request({"jsonrpc": "2.0", "method": "bogus"})
    assert res is None


# --- orchestrate / orchestrate_status --------------------------------------


@pytest.mark.asyncio
async def test_orchestrate_registers_job_and_returns_running():
    with patch("mcp_server._run_orchestration", new=AsyncMock()) as runner:
        out = await mcp_server.dispatch_tool(
            "orchestrate", {"prompt": "build a TODO API"}
        )
        data = json.loads(out)
        assert data["status"] == "running"
        assert data["job_id"] in mcp_server.ORCHESTRATION_JOBS
        await asyncio.sleep(0)  # let create_task schedule the (mocked) runner
    runner.assert_awaited()


@pytest.mark.asyncio
async def test_orchestrate_rejects_empty_prompt():
    with pytest.raises(ValueError):
        await mcp_server.dispatch_tool("orchestrate", {"prompt": "   "})


@pytest.mark.asyncio
async def test_orchestrate_rejects_bad_pipeline():
    with pytest.raises(ValueError):
        await mcp_server.dispatch_tool(
            "orchestrate", {"prompt": "x", "pipeline": "weird"}
        )


def _register(job_id, pipeline="sequential"):
    mcp_server.ORCHESTRATION_JOBS[job_id] = {
        "job_id": job_id,
        "status": "running",
        "pipeline": pipeline,
        "prompt": "p",
        "error": None,
        "artifacts": None,
    }


@pytest.mark.asyncio
async def test_run_orchestration_collects_artifacts(tmp_path):
    (tmp_path / "research.md").write_text("R", encoding="utf-8")
    (tmp_path / "app.py").write_text("print(1)", encoding="utf-8")
    _register("j-art")
    with patch("orchestrator.run_pipeline", new=AsyncMock(return_value=None)) as rp:
        await mcp_server._run_orchestration("j-art", "p", "sequential", str(tmp_path))
    rp.assert_awaited_once()
    job = mcp_server.ORCHESTRATION_JOBS["j-art"]
    assert job["status"] == "completed"
    assert job["artifacts"]["research"] == "R"
    assert job["artifacts"]["code"] == "print(1)"


@pytest.mark.asyncio
async def test_run_orchestration_e2e_uses_e2e_pipeline(tmp_path):
    _register("j-e2e", pipeline="e2e")
    with patch("orchestrator.run_e2e_pipeline", new=AsyncMock(return_value=None)) as rp:
        await mcp_server._run_orchestration("j-e2e", "p", "e2e", str(tmp_path))
    rp.assert_awaited_once()
    assert mcp_server.ORCHESTRATION_JOBS["j-e2e"]["status"] == "completed"


@pytest.mark.asyncio
async def test_run_orchestration_records_failure(tmp_path):
    _register("j-fail")
    with patch(
        "orchestrator.run_pipeline", new=AsyncMock(side_effect=RuntimeError("boom"))
    ):
        await mcp_server._run_orchestration("j-fail", "p", "sequential", str(tmp_path))
    job = mcp_server.ORCHESTRATION_JOBS["j-fail"]
    assert job["status"] == "failed"
    assert "boom" in job["error"]


@pytest.mark.asyncio
async def test_orchestrate_status_unknown_job_raises():
    with pytest.raises(ValueError):
        await mcp_server.dispatch_tool(
            "orchestrate_status", {"job_id": "does-not-exist"}
        )


@pytest.mark.asyncio
async def test_orchestrate_status_completed_returns_artifacts():
    mcp_server.ORCHESTRATION_JOBS["j-done"] = {
        "job_id": "j-done",
        "status": "completed",
        "pipeline": "sequential",
        "prompt": "p",
        "error": None,
        "artifacts": {"code": "x"},
    }
    out = await mcp_server.dispatch_tool("orchestrate_status", {"job_id": "j-done"})
    data = json.loads(out)
    assert data["status"] == "completed"
    assert data["artifacts"]["code"] == "x"


@pytest.mark.asyncio
async def test_orchestrate_status_running_hides_artifacts_key():
    _register("j-run")
    out = await mcp_server.dispatch_tool("orchestrate_status", {"job_id": "j-run"})
    data = json.loads(out)
    assert data["status"] == "running"
    assert "artifacts" not in data
