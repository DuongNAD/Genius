"""Tests for the MCP initialize handshake and the orchestrate/orchestrate_status
tools (Phase 3 — Antigravity coordinator integration), plus the artifact
resources, stage-progress derivation, and the doctor/debate/review tools."""

import asyncio
import json
import os
import time

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

import mcp_server


# --- MCP JSON-RPC handshake -------------------------------------------------


def test_stdio_logging_never_leaks_to_stdout():
    """The ag_core logger binds a StreamHandler to sys.stdout at import time;
    in stdio mode that would corrupt the JSON-RPC channel. Verify the redirect
    helper retargets it (and every other StreamHandler) to the stderr stand-in,
    so an ag_core log line lands on stderr, never on the real stdout."""
    import io
    import logging
    from ag_core.utils.logger import logger as ag_logger

    stderr_stand_in = io.StringIO()
    saved = [
        (h, h.stream)
        for lg in [logging.getLogger()]
        + [logging.getLogger(n) for n in list(logging.Logger.manager.loggerDict)]
        for h in getattr(lg, "handlers", [])
        if isinstance(h, logging.StreamHandler)
    ]
    try:
        mcp_server._redirect_all_logging_to_stderr(stderr_stand_in)
        ag_logger.info("LEAK_CHECK_TOKEN")
        for h in ag_logger.handlers:
            if isinstance(h, logging.StreamHandler):
                h.flush()
        assert "LEAK_CHECK_TOKEN" in stderr_stand_in.getvalue()
        assert all(
            isinstance(h, logging.StreamHandler) is False or h.stream is stderr_stand_in
            for h in ag_logger.handlers
        )
    finally:
        for handler, stream in saved:
            handler.setStream(stream)


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
async def test_initialize_advertises_resources_capability():
    res = await mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert res["result"]["capabilities"]["resources"] == {"listChanged": False}


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
        "doctor",
        "debate",
        "review",
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


# --- approval gates ---------------------------------------------------------


async def _wait_job_state(job_id, state, timeout=5.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        job = mcp_server.ORCHESTRATION_JOBS[job_id]
        if job["status"] == state:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"job {job_id} never reached {state!r} "
        f"(last: {mcp_server.ORCHESTRATION_JOBS[job_id]['status']!r})"
    )


@pytest.mark.asyncio
async def test_orchestrate_approval_gates_pause_and_resume(tmp_path):
    stages_run = []

    async def fake_pipeline(prompt, workspace=None, stage_gate=None):
        # Mirrors the real run_pipeline gate protocol.
        for stage in ("research", "design", "code"):
            stages_run.append(stage)
            if stage_gate is not None:
                await stage_gate(stage)

    with patch("orchestrator.run_pipeline", new=AsyncMock(side_effect=fake_pipeline)):
        out = await mcp_server.dispatch_tool(
            "orchestrate",
            {
                "prompt": "gated build",
                "workspace": str(tmp_path),
                "require_approval": True,
            },
        )
        job_id = json.loads(out)["job_id"]

        for expected_stage in ("research", "design", "code"):
            job = await _wait_job_state(job_id, "awaiting_approval")
            assert job["awaiting_stage"] == expected_stage
            status = json.loads(
                await mcp_server.dispatch_tool("orchestrate_status", {"job_id": job_id})
            )
            assert status["awaiting_stage"] == expected_stage
            # Artifacts are exposed while paused so the stage can be reviewed.
            assert "artifacts" in status
            approved = json.loads(
                await mcp_server.dispatch_tool(
                    "orchestrate_approve", {"job_id": job_id}
                )
            )
            assert approved["action"] == "approved"
            assert approved["stage"] == expected_stage

        await _wait_job_state(job_id, "completed")
    assert stages_run == ["research", "design", "code"]


@pytest.mark.asyncio
async def test_orchestrate_reject_fails_job(tmp_path):
    async def fake_pipeline(prompt, workspace=None, stage_gate=None):
        await stage_gate("research")

    with patch("orchestrator.run_pipeline", new=AsyncMock(side_effect=fake_pipeline)):
        out = await mcp_server.dispatch_tool(
            "orchestrate",
            {
                "prompt": "gated build",
                "workspace": str(tmp_path),
                "require_approval": True,
            },
        )
        job_id = json.loads(out)["job_id"]
        await _wait_job_state(job_id, "awaiting_approval")
        await mcp_server.dispatch_tool(
            "orchestrate_reject", {"job_id": job_id, "reason": "wrong direction"}
        )
        job = await _wait_job_state(job_id, "failed")
    assert "rejected" in job["error"]
    assert "wrong direction" in job["error"]


@pytest.mark.asyncio
async def test_orchestrate_approve_requires_awaiting_state():
    _register("j-not-waiting")
    with pytest.raises(ValueError):
        await mcp_server.dispatch_tool(
            "orchestrate_approve", {"job_id": "j-not-waiting"}
        )


@pytest.mark.asyncio
async def test_orchestrate_require_approval_rejects_e2e():
    with pytest.raises(ValueError):
        await mcp_server.dispatch_tool(
            "orchestrate",
            {"prompt": "x", "pipeline": "e2e", "require_approval": True},
        )


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


# --- MCP resources (genius://artifacts/...) ---------------------------------


@pytest.fixture(autouse=True)
def _isolate_artifact_workspace_map():
    # resources/read|list consult a module-global artifact->workspace
    # fallback map (populated by orchestrate_status polls) so URIs advertised
    # for isolated job workspaces stay readable. Earlier orchestrate tests in
    # this file populate it as a side effect; the resource tests below assert
    # cwd-scoped behavior, so keep the map empty around every test.
    mcp_server._ARTIFACT_WORKSPACES.clear()
    yield
    mcp_server._ARTIFACT_WORKSPACES.clear()


async def _rpc(method, params=None, req_id=1):
    return await mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    )


@pytest.mark.asyncio
async def test_resources_list_empty_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = await _rpc("resources/list")
    assert res["result"]["resources"] == []


@pytest.mark.asyncio
async def test_resources_list_only_whitelisted_artifacts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "research.md").write_text("R", encoding="utf-8")
    (tmp_path / "design.md.bak").write_text("old D", encoding="utf-8")
    # Non-whitelisted files must never leak into the resource list.
    (tmp_path / "secrets.txt").write_text("s3cret", encoding="utf-8")
    (tmp_path / "notes.md").write_text("private", encoding="utf-8")

    res = await _rpc("resources/list")
    resources = res["result"]["resources"]
    by_name = {r["name"]: r for r in resources}
    assert set(by_name) == {"research.md", "design.md.bak"}
    r = by_name["research.md"]
    assert r["uri"] == "genius://artifacts/research.md"
    assert r["mimeType"] == "text/markdown"
    assert r["description"]
    assert "research" in r["description"].lower()


@pytest.mark.asyncio
async def test_resources_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "design.md").write_text("# Thiết kế", encoding="utf-8")
    res = await _rpc(
        "resources/read", {"uri": "genius://artifacts/design.md"}, req_id=9
    )
    assert res["id"] == 9
    contents = res["result"]["contents"]
    assert contents == [
        {
            "uri": "genius://artifacts/design.md",
            "mimeType": "text/markdown",
            "text": "# Thiết kế",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uri",
    [
        "genius://artifacts/../conftest.py",
        "genius://artifacts/..\\conftest.py",
        "genius://artifacts/sub/research.md",
        "genius://artifacts/",
        "file:///etc/passwd",
        "genius://artifacts/secrets.txt",
    ],
)
async def test_resources_read_rejects_traversal_and_unknown_names(
    tmp_path, monkeypatch, uri
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "secrets.txt").write_text("s3cret", encoding="utf-8")
    res = await _rpc("resources/read", {"uri": uri})
    assert "result" not in res
    assert res["error"]["code"] == -32002


@pytest.mark.asyncio
async def test_resources_read_missing_artifact_is_not_found(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = await _rpc("resources/read", {"uri": "genius://artifacts/audit.md"})
    assert res["error"]["code"] == -32002
    assert "audit.md" in res["error"]["message"]


# --- orchestrate_status stage derivation ------------------------------------


def _register_progress_job(job_id, tmp_path, pipeline="sequential", started_ago=100.0):
    mcp_server.ORCHESTRATION_JOBS[job_id] = {
        "job_id": job_id,
        "status": "running",
        "pipeline": pipeline,
        "prompt": "p",
        "error": None,
        "artifacts": None,
        "workspace": str(tmp_path),
        "started_at": time.time() - started_ago,
        "finished_at": None,
    }


@pytest.mark.asyncio
async def test_orchestrate_status_derives_stages_from_artifacts(tmp_path):
    _register_progress_job("j-stages", tmp_path)
    (tmp_path / "research.md").write_text("R", encoding="utf-8")
    (tmp_path / "design.md").write_text("D", encoding="utf-8")

    out = await mcp_server.dispatch_tool("orchestrate_status", {"job_id": "j-stages"})
    data = json.loads(out)

    assert data["elapsed_seconds"] >= 99
    assert [s["stage"] for s in data["stages"]] == [
        "research",
        "design",
        "code",
        "security_audit",
        "deploy",
    ]
    states = {s["stage"]: s["state"] for s in data["stages"]}
    assert states["research"] == "done"
    assert states["design"] == "done"
    assert states["code"] == "pending"
    assert states["deploy"] == "pending"
    assert data["artifacts_ready"] == [
        "genius://artifacts/research.md",
        "genius://artifacts/design.md",
    ]


@pytest.mark.asyncio
async def test_orchestrate_status_ignores_stale_pre_run_artifacts(tmp_path):
    _register_progress_job("j-stale", tmp_path, started_ago=0.0)
    stale = tmp_path / "research.md"
    stale.write_text("old", encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))  # written long before the job started

    out = await mcp_server.dispatch_tool("orchestrate_status", {"job_id": "j-stale"})
    data = json.loads(out)
    assert data["stages"][0] == {
        "stage": "research",
        "artifact": "research.md",
        "state": "pending",
    }
    assert data["artifacts_ready"] == []


@pytest.mark.asyncio
async def test_orchestrate_status_e2e_tracks_plan_artifact(tmp_path):
    _register_progress_job("j-e2e-stage", tmp_path, pipeline="e2e")
    (tmp_path / "plan.md").write_text("P", encoding="utf-8")
    out = await mcp_server.dispatch_tool(
        "orchestrate_status", {"job_id": "j-e2e-stage"}
    )
    data = json.loads(out)
    assert data["stages"] == [{"stage": "plan", "artifact": "plan.md", "state": "done"}]
    assert data["artifacts_ready"] == ["genius://artifacts/plan.md"]


@pytest.mark.asyncio
async def test_orchestrate_job_records_start_metadata(tmp_path):
    with patch("mcp_server._run_orchestration", new=AsyncMock()):
        out = await mcp_server.dispatch_tool(
            "orchestrate", {"prompt": "x", "workspace": str(tmp_path)}
        )
        await asyncio.sleep(0)
    job = mcp_server.ORCHESTRATION_JOBS[json.loads(out)["job_id"]]
    assert job["workspace"] == str(tmp_path)
    assert job["started_at"] <= time.time()
    assert job["finished_at"] is None


# --- doctor tool -------------------------------------------------------------


def _doctor_result(cli, status):
    return {
        "cli": cli,
        "dependents": ["X"],
        "path": f"/bin/{cli}",
        "status": status,
        "detail": f"{cli} detail",
    }


@pytest.mark.asyncio
async def test_doctor_tool_returns_report_text(monkeypatch):
    monkeypatch.setenv("SKILL_API_KEY", "k")
    fake = [_doctor_result(c, "OK") for c in ("grok", "claude", "codex")]
    # The CLI probes are mocked out: the doctor tool must not spawn real CLIs.
    with patch(
        "ag_core.diagnostics.run_doctor_async", new=AsyncMock(return_value=fake)
    ) as probes:
        out = await mcp_server.dispatch_tool("doctor", {})
    probes.assert_awaited_once()
    assert "Genius preflight doctor" in out
    assert "READY" in out
    assert "grok" in out and "codex" in out


@pytest.mark.asyncio
async def test_doctor_tool_reports_not_ready_on_missing_cli(monkeypatch):
    # codex is a required backend (grok is optional/opt-in and would only
    # degrade the report).
    monkeypatch.setenv("SKILL_API_KEY", "k")
    fake = [_doctor_result("codex", "MISSING")]
    with patch(
        "ag_core.diagnostics.run_doctor_async", new=AsyncMock(return_value=fake)
    ):
        out = await mcp_server.dispatch_tool("doctor", {})
    assert "NOT READY" in out


# --- debate tool -------------------------------------------------------------


@pytest.mark.asyncio
async def test_debate_runs_critique_refine_rounds():
    calls = []

    async def fake_execute(agent_name, prompt, context=None):
        calls.append((agent_name, prompt))
        if agent_name == "research":
            return f"critique {len(calls)}"
        return f"refined {len(calls)}"

    with patch("mcp_server.execute_agent", new=AsyncMock(side_effect=fake_execute)):
        out = await mcp_server.dispatch_tool(
            "debate", {"design": "draft v0", "prompt": "build X", "rounds": 2}
        )
    data = json.loads(out)
    # 2 rounds x (critic + refiner), no approval
    assert [name for name, _ in calls] == ["research", "design"] * 2
    assert data["approved"] is False
    assert len(data["rounds"]) == 2
    assert data["design"] == "refined 4"
    # the critic sees the current draft; the refiner sees the critique
    assert "draft v0" in calls[0][1]
    assert "critique 1" in calls[1][1]


@pytest.mark.asyncio
async def test_debate_early_exits_on_approved_marker():
    runner = AsyncMock(return_value="Looks great. [APPROVED]")
    with patch("mcp_server.execute_agent", new=runner):
        out = await mcp_server.dispatch_tool(
            "debate", {"design": "draft v0", "rounds": 3}
        )
    data = json.loads(out)
    runner.assert_awaited_once()  # only the critic ran, refiner skipped
    assert data["approved"] is True
    assert data["design"] == "draft v0"  # unchanged
    assert data["rounds"] == [
        {"round": 1, "approved": True, "critique": "Looks great. [APPROVED]"}
    ]


@pytest.mark.asyncio
async def test_debate_clamps_rounds_to_max():
    runner = AsyncMock(return_value="never approves")
    with patch("mcp_server.execute_agent", new=runner):
        out = await mcp_server.dispatch_tool("debate", {"design": "d", "rounds": 99})
    data = json.loads(out)
    assert len(data["rounds"]) == mcp_server.MAX_DEBATE_ROUNDS
    assert runner.await_count == mcp_server.MAX_DEBATE_ROUNDS * 2


@pytest.mark.asyncio
async def test_debate_rejects_empty_design():
    with pytest.raises(ValueError):
        await mcp_server.dispatch_tool("debate", {"design": "  "})


# --- review tool -------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_tool_runs_codex_agent_without_writing_files():
    with patch("mcp_server.CodexReviewerAgent") as agent_cls:
        instance = MagicMock()
        instance.run = AsyncMock(return_value="review: LGTM")
        agent_cls.return_value = instance

        out = await mcp_server.dispatch_tool(
            "review", {"code": "print(1)", "instructions": "focus on security"}
        )

    assert out == "review: LGTM"
    _, ctor_kwargs = agent_cls.call_args
    assert ctor_kwargs["output_file"] == "None"  # no file writes
    _, run_kwargs = instance.run.call_args
    assert "print(1)" in run_kwargs["prompt"]
    assert "focus on security" in run_kwargs["prompt"]
    assert not run_kwargs["prompt"].startswith("/code")


@pytest.mark.asyncio
async def test_review_rejects_empty_code():
    with pytest.raises(ValueError):
        await mcp_server.dispatch_tool("review", {"code": ""})
