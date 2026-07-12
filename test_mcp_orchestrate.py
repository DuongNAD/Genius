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
from ag_core import mcp_resources


@pytest.fixture(autouse=True)
def _isolated_jobs_dir(tmp_path, monkeypatch):
    """Point GENIUS_JOBS_DIR at a per-test tmp dir: orchestrate journals a
    job.json into its workspace at submit time, which would otherwise write
    real dirs under the repo's .genius_jobs during the suite."""
    monkeypatch.setenv("GENIUS_JOBS_DIR", str(tmp_path / "jobs"))


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


@pytest.mark.asyncio
async def test_run_orchestration_custom_passes_flow(tmp_path):
    """pipeline='custom' runs run_pipeline with flow='custom' (plan-first /
    codex-debate / codex-gpt5.6-sol final-review variant)."""
    _register("j-custom", pipeline="custom")
    with patch("orchestrator.run_pipeline", new=AsyncMock(return_value=None)) as rp:
        await mcp_server._run_orchestration("j-custom", "p", "custom", str(tmp_path))
    rp.assert_awaited_once()
    _, kwargs = rp.call_args
    assert kwargs.get("flow") == "custom"
    assert mcp_server.ORCHESTRATION_JOBS["j-custom"]["status"] == "completed"


@pytest.mark.asyncio
async def test_run_orchestration_sequential_keeps_default_flow(tmp_path):
    """pipeline='sequential' keeps flow='sequential' (byte-identical default)."""
    _register("j-seq")
    with patch("orchestrator.run_pipeline", new=AsyncMock(return_value=None)) as rp:
        await mcp_server._run_orchestration("j-seq", "p", "sequential", str(tmp_path))
    rp.assert_awaited_once()
    _, kwargs = rp.call_args
    assert kwargs.get("flow") == "sequential"


@pytest.mark.asyncio
async def test_orchestrate_accepts_custom_pipeline():
    """dispatch_tool registers a 'custom' job instead of rejecting it."""
    with patch("mcp_server._run_orchestration", new=AsyncMock()):
        out = await mcp_server.dispatch_tool(
            "orchestrate", {"prompt": "build x", "pipeline": "custom"}
        )
        await asyncio.sleep(0)
    data = json.loads(out)
    assert data["status"] == "running"
    assert mcp_server.ORCHESTRATION_JOBS[data["job_id"]]["pipeline"] == "custom"


@pytest.mark.asyncio
async def test_orchestrate_ignores_unusable_relative_workspace():
    """A relative workspace (resolved against the MCP server's cwd, often '/')
    is ignored in favour of the guaranteed-writable jobs dir, so artifact
    writes cannot fail silently."""
    with patch("mcp_server._run_orchestration", new=AsyncMock()):
        out = await mcp_server.dispatch_tool(
            "orchestrate", {"prompt": "build x", "workspace": "test"}
        )
        await asyncio.sleep(0)
    data = json.loads(out)
    ws = mcp_server.ORCHESTRATION_JOBS[data["job_id"]]["workspace"]
    assert ws != "test"
    assert ws.startswith(mcp_server._jobs_root())


@pytest.mark.asyncio
async def test_orchestrate_keeps_usable_absolute_workspace(tmp_path):
    """An absolute workspace with a writable parent is honoured as-is."""
    ws_in = str(tmp_path / "myws")
    with patch("mcp_server._run_orchestration", new=AsyncMock()):
        out = await mcp_server.dispatch_tool(
            "orchestrate", {"prompt": "build x", "workspace": ws_in}
        )
        await asyncio.sleep(0)
    data = json.loads(out)
    assert mcp_server.ORCHESTRATION_JOBS[data["job_id"]]["workspace"] == ws_in


def test_workspace_existing_writable_dir_ignores_readonly_parent(tmp_path):
    """An EXISTING writable workspace is usable even when its parent is
    read-only (e.g. /opt/<user-owned-dir>): only the dir's own writability
    matters, run_pipeline never needs to create it."""
    parent = tmp_path / "locked"
    ws = parent / "ws"
    ws.mkdir(parents=True)
    parent.chmod(0o555)
    try:
        assert mcp_server._workspace_is_usable(str(ws))
    finally:
        parent.chmod(0o755)


def test_workspace_deep_uncreated_path_is_usable(tmp_path):
    """A not-yet-existing path several levels deep is usable: run_pipeline
    creates it with os.makedirs, which only needs the deepest EXISTING
    ancestor to be writable."""
    assert mcp_server._workspace_is_usable(str(tmp_path / "a" / "b" / "ws"))


def test_workspace_existing_regular_file_is_not_usable(tmp_path):
    """A path that exists but is a regular file can never hold artifacts."""
    f = tmp_path / "occupied.txt"
    f.write_text("x")
    assert not mcp_server._workspace_is_usable(str(f))


# --- status view: workspace + current_stage ----------------------------------


@pytest.mark.asyncio
async def test_orchestrate_status_reports_workspace_and_current_stage():
    """Antigravity needs to know WHERE files land and WHAT the pipeline is
    doing right now — before this, the first minutes of a job showed only an
    empty stages list."""
    with patch("mcp_server._run_orchestration", new=AsyncMock()):
        out = await mcp_server.dispatch_tool("orchestrate", {"prompt": "build x"})
        await asyncio.sleep(0)
    job_id = json.loads(out)["job_id"]
    st = json.loads(
        await mcp_server.dispatch_tool("orchestrate_status", {"job_id": job_id})
    )
    assert st["workspace"] == mcp_server.ORCHESTRATION_JOBS[job_id]["workspace"]
    # No artifact exists yet, so the current stage is the first checkpoint.
    assert st["current_stage"] == mcp_server._PIPELINE_STAGES["sequential"][0][0]


# --- job journal + recovery ---------------------------------------------------


@pytest.mark.asyncio
async def test_job_manifest_journaled_and_recovered_as_interrupted(tmp_path):
    """A running job's manifest is journaled at submit; after a server restart
    (simulated by dropping the in-memory job) orchestrate_status recovers it
    from disk and reports it as interrupted instead of 'Unknown job_id'."""
    with patch("mcp_server._run_orchestration", new=AsyncMock()):
        out = await mcp_server.dispatch_tool("orchestrate", {"prompt": "build x"})
        await asyncio.sleep(0)
    job_id = json.loads(out)["job_id"]
    ws = mcp_server.ORCHESTRATION_JOBS[job_id]["workspace"]
    assert os.path.isfile(os.path.join(ws, "job.json"))

    saved = mcp_server.ORCHESTRATION_JOBS.pop(job_id)
    try:
        st = json.loads(
            await mcp_server.dispatch_tool("orchestrate_status", {"job_id": job_id})
        )
    finally:
        mcp_server.ORCHESTRATION_JOBS[job_id] = saved
    assert st["recovered_from_journal"] is True
    assert st["status"] == "interrupted"  # the manifest said "running"
    assert st["workspace"] == ws
    assert "no longer running" in st["error"]


@pytest.mark.asyncio
async def test_completed_job_recovered_from_journal_with_artifacts(
    tmp_path, monkeypatch
):
    """A finished job remains queryable across restarts: status, elapsed time,
    stages and artifacts all come back from the journal + the workspace."""
    jobs_root = tmp_path / "jobs"
    monkeypatch.setenv("GENIUS_JOBS_DIR", str(jobs_root))
    job_id = "a" * 32
    ws = jobs_root / job_id
    ws.mkdir(parents=True)
    (ws / "design.md").write_text("the design", encoding="utf-8")
    manifest = {
        "job_id": job_id,
        "status": "completed",
        "pipeline": "sequential",
        "prompt": "x",
        "error": None,
        "workspace": str(ws),
        "started_at": 1.0,
        "finished_at": 5.0,
        "require_approval": False,
        "awaiting_stage": None,
    }
    (ws / "job.json").write_text(json.dumps(manifest), encoding="utf-8")
    st = json.loads(
        await mcp_server.dispatch_tool("orchestrate_status", {"job_id": job_id})
    )
    assert st["status"] == "completed"
    assert st["recovered_from_journal"] is True
    assert st["elapsed_seconds"] == 4.0
    assert st["artifacts"]["design"] == "the design"
    assert any(
        s["stage"] == "design" and s["state"] == "done" for s in st["stages"]
    )


@pytest.mark.asyncio
async def test_status_invalid_job_id_never_recovers_from_disk():
    """A non-uuid4-hex job_id (e.g. a path-traversal attempt) is rejected
    without touching the filesystem."""
    with pytest.raises(ValueError, match="Unknown job_id"):
        await mcp_server.dispatch_tool(
            "orchestrate_status", {"job_id": "../../etc/passwd"}
        )


# --- MCP progress notifications ------------------------------------------------


class _FakeSession:
    """Records send_log_message calls like the SDK ServerSession would."""

    def __init__(self):
        self.events = []

    async def send_log_message(self, level, data, logger=None, related_request_id=None):
        self.events.append((level, data, logger))


@pytest.mark.asyncio
async def test_notify_without_session_is_noop(monkeypatch):
    monkeypatch.setattr(mcp_server, "_MCP_LOG_SESSION", None)
    await mcp_server._notify_progress({"event": "x"})  # must not raise


@pytest.mark.asyncio
async def test_notify_respects_client_log_level(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(mcp_server, "_MCP_LOG_SESSION", session)
    monkeypatch.setattr(mcp_server, "_MCP_MIN_LOG_LEVEL", "warning")
    await mcp_server._notify_progress({"event": "suppressed"})  # info < warning
    assert session.events == []
    await mcp_server._notify_progress({"event": "kept"}, level="error")
    assert len(session.events) == 1


@pytest.mark.asyncio
async def test_watcher_pushes_stage_done_events(tmp_path, monkeypatch):
    """When an artifact lands on disk the watcher pushes a stage_done
    notification (logger genius.orchestrate) without any client poll."""
    session = _FakeSession()
    monkeypatch.setattr(mcp_server, "_MCP_LOG_SESSION", session)
    monkeypatch.setenv("GENIUS_PROGRESS_POLL_SECONDS", "0.01")
    ws = tmp_path / "ws"
    ws.mkdir()
    job = {
        "job_id": "d" * 32,
        "status": "running",
        "pipeline": "sequential",
        "workspace": str(ws),
        "started_at": time.time() - 10,
        "awaiting_stage": None,
    }
    watcher = asyncio.create_task(mcp_server._watch_job_progress(job))
    (ws / "research.md").write_text("r", encoding="utf-8")
    await asyncio.sleep(0.08)
    job["status"] = "completed"  # ends the watcher loop
    await asyncio.wait_for(watcher, timeout=2)
    stage_events = [d for _l, d, _lg in session.events if d["event"] == "stage_done"]
    assert any(e["stage"] == "research" for e in stage_events)
    assert all(lg == "genius.orchestrate" for _l, _d, lg in session.events)


@pytest.mark.asyncio
async def test_run_orchestration_emits_terminal_status(tmp_path, monkeypatch):
    """The job's finally block pushes the terminal status notification."""
    session = _FakeSession()
    monkeypatch.setattr(mcp_server, "_MCP_LOG_SESSION", session)
    monkeypatch.setenv("GENIUS_PROGRESS_POLL_SECONDS", "0.01")
    job_id = "e" * 32
    ws = str(tmp_path / "ws")
    mcp_server.ORCHESTRATION_JOBS[job_id] = {
        "job_id": job_id,
        "status": "running",
        "pipeline": "sequential",
        "prompt": "x",
        "error": None,
        "artifacts": None,
        "workspace": ws,
        "started_at": time.time(),
        "finished_at": None,
        "require_approval": False,
        "awaiting_stage": None,
        "approval_event": asyncio.Event(),
        "rejected": False,
        "reject_reason": None,
    }
    try:
        with (
            patch("mcp_server._ensure_skill_servers", new=AsyncMock()),
            patch("orchestrator.run_pipeline", new=AsyncMock()),
        ):
            await mcp_server._run_orchestration(job_id, "x", "sequential", ws, False)
    finally:
        mcp_server.ORCHESTRATION_JOBS.pop(job_id, None)
    assert session.events, "expected at least the terminal status notification"
    _level, data, _logger = session.events[-1]
    assert data["event"] == "status"
    assert data["status"] == "completed"


# --- jobs-root retention -------------------------------------------------------


@pytest.mark.asyncio
async def test_jobs_root_pruned_to_max_jobs(tmp_path, monkeypatch):
    """Old finished job dirs beyond GENIUS_JOBS_MAX_JOBS are removed on the
    next orchestrate; live jobs and non-job entries are never touched."""
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    monkeypatch.setenv("GENIUS_JOBS_DIR", str(jobs_root))
    monkeypatch.setenv("GENIUS_JOBS_MAX_JOBS", "2")

    old = time.time() - 3600
    stale_ids = [f"{i:032x}" for i in range(3)]
    for i, jid in enumerate(stale_ids):
        d = jobs_root / jid
        d.mkdir()
        os.utime(d, (old + i, old + i))

    (jobs_root / "user-notes").mkdir()  # non-job entry: untouchable

    live_id = "f" * 32  # oldest of all, but unfinished in memory: untouchable
    live_dir = jobs_root / live_id
    live_dir.mkdir()
    os.utime(live_dir, (old - 9999, old - 9999))
    mcp_server.ORCHESTRATION_JOBS[live_id] = {"job_id": live_id, "status": "running"}

    try:
        with patch("mcp_server._run_orchestration", new=AsyncMock()):
            out = await mcp_server.dispatch_tool("orchestrate", {"prompt": "build x"})
            await asyncio.sleep(0)
    finally:
        mcp_server.ORCHESTRATION_JOBS.pop(live_id, None)

    new_id = json.loads(out)["job_id"]
    survivors = {p.name for p in jobs_root.iterdir()}
    assert "user-notes" in survivors
    assert live_id in survivors
    assert new_id in survivors
    # Cap 2 keeps the two newest prunable dirs; the oldest stale one is gone.
    assert stale_ids[0] not in survivors
    assert stale_ids[1] in survivors and stale_ids[2] in survivors


@pytest.mark.asyncio
async def test_jobs_root_pruned_by_age(tmp_path, monkeypatch):
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    monkeypatch.setenv("GENIUS_JOBS_DIR", str(jobs_root))
    monkeypatch.setenv("GENIUS_JOBS_MAX_JOBS", "0")  # cap off
    monkeypatch.setenv("GENIUS_JOBS_RETENTION_DAYS", "1")

    ancient = jobs_root / ("b" * 32)
    ancient.mkdir()
    two_days = time.time() - 2 * 86400
    os.utime(ancient, (two_days, two_days))
    fresh = jobs_root / ("c" * 32)
    fresh.mkdir()

    with patch("mcp_server._run_orchestration", new=AsyncMock()):
        await mcp_server.dispatch_tool("orchestrate", {"prompt": "build x"})
        await asyncio.sleep(0)

    survivors = {p.name for p in jobs_root.iterdir()}
    assert ancient.name not in survivors
    assert fresh.name in survivors


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

    async def fake_pipeline(prompt, workspace=None, stage_gate=None, flow="sequential"):
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
async def test_cancelled_job_journals_terminal_state(tmp_path):
    """Invariant: finished_at set => status is terminal. CancelledError is a
    BaseException that bypasses `except Exception`, and a live manifest ended
    up journaled as status "running" WITH a finished_at; the finally block
    now repairs the state before journaling."""
    started = asyncio.Event()

    async def hanging_pipeline(prompt, workspace=None, stage_gate=None, flow="sequential"):
        started.set()
        await asyncio.Event().wait()  # hangs until cancelled

    before = set(mcp_server._ORCHESTRATION_TASKS)
    with patch("orchestrator.run_pipeline", new=AsyncMock(side_effect=hanging_pipeline)):
        out = await mcp_server.dispatch_tool(
            "orchestrate", {"prompt": "hang", "workspace": str(tmp_path)}
        )
        job_id = json.loads(out)["job_id"]
        await asyncio.wait_for(started.wait(), timeout=5.0)
        (task,) = set(mcp_server._ORCHESTRATION_TASKS) - before
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    job = mcp_server.ORCHESTRATION_JOBS[job_id]
    assert job["status"] == "failed"
    assert job["finished_at"] is not None
    assert "cancelled" in job["error"]
    manifest = json.loads((tmp_path / "job.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["finished_at"] is not None


@pytest.mark.asyncio
async def test_orchestrate_reject_fails_job(tmp_path):
    async def fake_pipeline(prompt, workspace=None, stage_gate=None, flow="sequential"):
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
    # cwd-scoped behavior, so keep the map empty around every test. The
    # job-scoped registry gets the same isolation.
    mcp_server._ARTIFACT_WORKSPACES.clear()
    mcp_resources._JOB_WORKSPACES.clear()
    yield
    mcp_server._ARTIFACT_WORKSPACES.clear()
    mcp_resources._JOB_WORKSPACES.clear()


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


# --- job-scoped resource URIs (genius://artifacts/<job_id>/<name>) ----------

_JOB_A = "a" * 32
_JOB_B = "b" * 32


@pytest.mark.asyncio
async def test_job_scoped_read_prefers_job_workspace_over_cwd(tmp_path, monkeypatch):
    """Regression: a stale root-workspace design.md ('Second response') used
    to shadow the job's own artifact behind the very URI orchestrate_status
    had just advertised."""
    cwd = tmp_path / "cwd"
    ws = tmp_path / "job_ws"
    cwd.mkdir()
    ws.mkdir()
    monkeypatch.chdir(cwd)
    (cwd / "design.md").write_text("Second response", encoding="utf-8")
    (ws / "design.md").write_text("# real job design", encoding="utf-8")
    mcp_resources.register_job_workspace(_JOB_A, str(ws))

    res = await _rpc(
        "resources/read", {"uri": f"genius://artifacts/{_JOB_A}/design.md"}
    )
    assert res["result"]["contents"][0]["text"] == "# real job design"
    # The legacy bare-name URI still serves the CWD copy (compat behavior).
    res = await _rpc("resources/read", {"uri": "genius://artifacts/design.md"})
    assert res["result"]["contents"][0]["text"] == "Second response"


@pytest.mark.asyncio
async def test_job_scoped_read_isolates_concurrent_jobs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for jid, text in ((_JOB_A, "job A research"), (_JOB_B, "job B research")):
        ws = tmp_path / jid
        ws.mkdir()
        (ws / "research.md").write_text(text, encoding="utf-8")
        mcp_resources.register_job_workspace(jid, str(ws))

    a = await _rpc(
        "resources/read", {"uri": f"genius://artifacts/{_JOB_A}/research.md"}
    )
    b = await _rpc(
        "resources/read", {"uri": f"genius://artifacts/{_JOB_B}/research.md"}
    )
    assert a["result"]["contents"][0]["text"] == "job A research"
    assert b["result"]["contents"][0]["text"] == "job B research"


@pytest.mark.asyncio
async def test_job_scoped_read_unknown_job_is_not_found(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = await _rpc(
        "resources/read", {"uri": f"genius://artifacts/{'f' * 32}/design.md"}
    )
    assert res["error"]["code"] == -32002
    assert "orchestrate_status" in res["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uri",
    [
        f"genius://artifacts/{'A' * 32}/design.md",  # uppercase: not a job id
        f"genius://artifacts/{'a' * 31}/design.md",  # wrong length
        f"genius://artifacts/{'a' * 32}/secrets.txt",  # non-whitelisted name
        f"genius://artifacts/{'a' * 32}/",  # empty name
        f"genius://artifacts/{'a' * 32}/../design.md",  # traversal in name
    ],
)
async def test_job_scoped_read_rejects_bad_job_uris(tmp_path, monkeypatch, uri):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "secrets.txt").write_text("s3cret", encoding="utf-8")
    mcp_resources.register_job_workspace("a" * 32, str(tmp_path))
    res = await _rpc("resources/read", {"uri": uri})
    assert "result" not in res
    assert res["error"]["code"] == -32002


@pytest.mark.asyncio
async def test_job_scoped_read_survives_registry_loss_via_journal(
    tmp_path, monkeypatch
):
    """After a server restart the in-memory registry is empty; the journal
    resolver rebuilds job_id -> workspace from <jobs dir>/<id>/job.json."""
    monkeypatch.chdir(tmp_path)
    jid = "c" * 32
    ws = tmp_path / "jobs" / jid  # the autouse GENIUS_JOBS_DIR
    ws.mkdir(parents=True)
    (ws / "deploy.md").write_text("deployed", encoding="utf-8")
    manifest = {
        "job_id": jid,
        "status": "completed",
        "pipeline": "sequential",
        "prompt": "p",
        "error": None,
        "workspace": str(ws),
        "started_at": None,
        "finished_at": None,
    }
    (ws / "job.json").write_text(json.dumps(manifest), encoding="utf-8")

    res = await _rpc(
        "resources/read", {"uri": f"genius://artifacts/{jid}/deploy.md"}
    )
    assert res["result"]["contents"][0]["text"] == "deployed"


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
async def test_orchestrate_status_real_job_id_advertises_job_scoped_uris(tmp_path):
    """A real (32-hex) job id gets job-scoped artifact URIs, and the
    advertised URI is immediately readable from THAT job's workspace."""
    jid = "d" * 32
    _register_progress_job(jid, tmp_path)
    (tmp_path / "research.md").write_text("R", encoding="utf-8")

    out = await mcp_server.dispatch_tool("orchestrate_status", {"job_id": jid})
    data = json.loads(out)
    assert data["artifacts_ready"] == [f"genius://artifacts/{jid}/research.md"]

    res = await _rpc("resources/read", {"uri": data["artifacts_ready"][0]})
    assert res["result"]["contents"][0]["text"] == "R"


@pytest.mark.asyncio
async def test_orchestrate_status_custom_pipeline_lists_review_stage(tmp_path):
    """The custom pipeline's stage list includes the final-review checkpoint,
    so every awaiting_stage value maps to a listed stage; the shared review.md
    artifact is advertised only once."""
    _register_progress_job("j-custom", tmp_path, pipeline="custom")
    (tmp_path / "review.md").write_text("reviewed", encoding="utf-8")

    out = await mcp_server.dispatch_tool("orchestrate_status", {"job_id": "j-custom"})
    data = json.loads(out)
    assert [s["stage"] for s in data["stages"]] == [
        "research",
        "design",
        "code",
        "security_audit",
        "review",
        "deploy",
    ]
    assert data["artifacts_ready"] == ["genius://artifacts/review.md"]


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
async def test_debate_critic_prompt_carries_quality_checklist():
    """The MCP debate tool's critic prompt includes the same design-quality
    checklist as the pipeline debates."""
    calls = []

    async def fake_execute(agent_name, prompt, context=None):
        calls.append((agent_name, prompt))
        return "[APPROVED]"

    with patch("mcp_server.execute_agent", new=AsyncMock(side_effect=fake_execute)):
        await mcp_server.dispatch_tool("debate", {"design": "draft", "rounds": 1})
    assert calls
    critic_prompt = calls[0][1]
    assert "Contract-algorithm consistency" in critic_prompt
    assert "Test-locked claims" in critic_prompt


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
