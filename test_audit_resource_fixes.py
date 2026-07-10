"""Regression tests for the resource/robustness medium-severity audit fixes:

- rate_limiter: monotonic clock + clamps, so a backward clock step can't drive
  the token balance negative and cause sustained spurious 429s.
- db: the singleton writer thread is started under a lock, so concurrent
  first-writers can't each spawn a thread (two write connections to one file).
- cli_runner: CLIs are spawned in their own session, and communicate_with_timeout
  terminates the child on external cancellation instead of orphaning it.
- project_scanner: scan() caps per-file bytes so one huge non-binary file can't
  blow up memory.
- hub: a late /report_result for an already-terminal task does not flip a
  reassigned (now-busy) worker back to idle.
"""

import asyncio
import threading
import time

import pytest


# --- rate_limiter ------------------------------------------------------------


def test_rate_limiter_survives_backward_clock_step(monkeypatch):
    import ag_core.utils.rate_limiter as rl

    clock = {"t": 1000.0}
    monkeypatch.setattr(rl.time, "monotonic", lambda: clock["t"])

    bucket = rl.TokenBucketRateLimiter(rate=1.0, capacity=5.0)
    for _ in range(5):
        assert bucket.consume(1.0) is True
    assert bucket.consume(1.0) is False  # drained

    # Clock jumps BACKWARD: must not corrupt the balance into the negatives.
    clock["t"] = 900.0
    assert bucket.consume(1.0) is False
    assert bucket.tokens >= 0.0

    # Time advances again: normal refill resumes (5s * 1 token/s -> 5 tokens).
    clock["t"] = 905.0
    assert bucket.consume(1.0) is True
    assert bucket.tokens >= 0.0


# --- db writer thread --------------------------------------------------------


def test_writer_thread_start_is_singleton_under_concurrency():
    import ag_core.utils.db as db

    db.stop_writer_thread()  # reset to a known state
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()  # release all 20 at once to maximize the race window
        db._start_writer_thread()

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    live = [
        t
        for t in threading.enumerate()
        if t.name == "SQLiteWriterThread" and t.is_alive()
    ]
    assert len(live) == 1
    db.stop_writer_thread()  # cleanup; it re-starts lazily on the next write


# --- cli_runner --------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_cli_starts_new_session(monkeypatch):
    import ag_core.utils.cli_runner as cr

    captured = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(cr.asyncio, "create_subprocess_exec", fake_exec)
    await cr.spawn_cli(["echo", "hi"], "echo")
    assert captured.get("start_new_session") is True


@pytest.mark.asyncio
async def test_communicate_terminates_child_on_cancellation(monkeypatch):
    import ag_core.utils.cli_runner as cr

    terminated = []

    async def fake_terminate(proc):
        terminated.append(proc)

    monkeypatch.setattr(cr, "_terminate", fake_terminate)

    class FakeProc:
        pid = 999999

        async def communicate(self):
            raise asyncio.CancelledError()

    proc = FakeProc()
    with pytest.raises(asyncio.CancelledError):
        await cr.communicate_with_timeout(proc, timeout=5.0)
    assert terminated == [proc]  # child killed, not orphaned


# --- project_scanner ---------------------------------------------------------


def test_scan_skips_oversized_files(tmp_path, monkeypatch):
    import ag_core.scanner.project_scanner as ps

    monkeypatch.setattr(ps, "_MAX_SCAN_FILE_BYTES", 2048)
    (tmp_path / "small.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "big.py").write_text("# " + ("a" * 5000) + "\n", encoding="utf-8")

    scanned = ps.ProjectScanner(str(tmp_path)).scan()

    assert "small.py" in scanned
    assert "big.py" not in scanned  # over the 2048-byte cap


# --- hub late-report race ----------------------------------------------------


@pytest.mark.asyncio
async def test_late_report_keeps_reassigned_worker_busy():
    from ag_core.distributed.hub import CentralHub

    hub = CentralHub(api_key="valid-api-key")
    hub._sweeper_running = True  # don't spawn a background sweeper for this test

    now = time.time()
    hub.workers["w1"] = {
        "worker_id": "w1",
        "roles": ["grok"],
        "status": "busy",  # already reassigned to t2
        "last_heartbeat": now,
    }
    # t1 already timed out (terminal); t2 is the current running assignment.
    hub.tasks["t1"] = {
        "task_id": "t1",
        "role": "grok",
        "status": "failed",
        "worker_id": "w1",
        "result": {"error": "timed out"},
        "created_at": now,
        "started_at": now,
    }
    hub.tasks["t2"] = {
        "task_id": "t2",
        "role": "grok",
        "status": "running",
        "worker_id": "w1",
        "result": None,
        "created_at": now,
        "started_at": now,
    }

    # A late completion report for the already-terminal t1.
    payload = {
        "task_id": "t1",
        "worker_id": "w1",
        "status": "completed",
        "result": {"output": "late"},
    }
    headers = hub.create_headers(payload)
    status, _body, _hdrs = await hub.handle_request("/report_result", payload, headers)

    assert status == 200
    # The stale report must NOT re-idle a worker that is busy on t2.
    assert hub.workers["w1"]["status"] == "busy"
    assert hub.tasks["t1"]["status"] == "failed"
    assert hub.tasks["t2"]["status"] == "running"
