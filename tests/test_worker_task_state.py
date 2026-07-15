"""Worker task-state cleanup.

Two behaviors pinned here:

1. ``execute_task``'s finally is PER-TASK: one task settling (or a stale
   cancelled task from before a reconnect) must not wipe the liveness state of
   other in-flight tasks. The old unconditional ``status = "idle"`` went
   through a setter that cleared ``active_tasks`` wholesale.

2. ``run_production_loop``'s disconnect cleanup awaits the cancelled tasks'
   finallies BEFORE the loop can reconnect and accept new work, so a stale
   finally can never run after new tasks were accepted.
"""

import asyncio
import json

import pytest

from ag_core.distributed.worker import ClientWorker

original_sleep = asyncio.sleep


class _AckNetwork:
    async def send_to_hub(self, endpoint, payload, headers):
        return 200, {"status": "result_acknowledged"}


def _worker():
    w = ClientWorker("w1", ["codex"], api_key="unit-key")
    w.network = _AckNetwork()
    return w


@pytest.mark.asyncio
async def test_finished_task_does_not_clear_other_active_tasks():
    w = _worker()
    w.active_tasks.add("task_other")
    await w.execute_task("task_1", "sleep:0")
    assert "task_other" in w.active_tasks
    assert w.status == "busy"


@pytest.mark.asyncio
async def test_last_task_settling_resets_to_idle():
    w = _worker()
    await w.execute_task("task_1", "sleep:0")
    assert w.status == "idle"
    assert w.current_task is None
    assert w.active_tasks == set()


@pytest.mark.asyncio
async def test_stale_cancelled_task_does_not_wipe_post_reconnect_task():
    w = _worker()
    t = asyncio.create_task(w.execute_task("task_old", {"sleep": 30}))
    w.running_tasks["task_old"] = t
    await original_sleep(0.01)
    # A fresh task accepted (conceptually after a reconnect) while the old one
    # is being cancelled.
    w.active_tasks.add("task_new")
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    assert "task_new" in w.active_tasks
    assert "task_old" not in w.active_tasks
    assert w.status == "busy"


@pytest.mark.asyncio
async def test_http_cancel_handler_is_per_task():
    """/cancel must clean up ONLY the cancelled task — the old blanket
    ``status = "idle"`` cleared active_tasks wholesale, so cancelling a stale
    task wiped the liveness of every other in-flight task."""
    w = _worker()
    t = asyncio.create_task(w.execute_task("task_old", {"sleep": 30}))
    w.running_tasks["task_old"] = t
    await original_sleep(0.01)
    w.active_tasks.add("task_new")

    payload = {"task_id": "task_old"}
    status, body, _ = await w.handle_request(
        "/cancel", payload, w.create_headers(payload)
    )
    assert status == 200
    await asyncio.gather(t, return_exceptions=True)

    assert "task_new" in w.active_tasks
    assert "task_old" not in w.active_tasks
    assert w.status == "busy"


@pytest.mark.asyncio
async def test_http_cancel_last_task_returns_to_idle():
    w = _worker()
    t = asyncio.create_task(w.execute_task("task_only", {"sleep": 30}))
    w.running_tasks["task_only"] = t
    await original_sleep(0.01)

    payload = {"task_id": "task_only"}
    status, _body, _ = await w.handle_request(
        "/cancel", payload, w.create_headers(payload)
    )
    assert status == 200
    await asyncio.gather(t, return_exceptions=True)
    assert w.status == "idle"
    assert w.active_tasks == set()


@pytest.mark.asyncio
async def test_ws_cancel_frame_is_per_task(monkeypatch):
    """The WS 'cancel' frame handler mirrors /cancel: cancelling one task must
    not wipe a task accepted right after it."""
    from ag_core.utils.security import calculate_checksum

    class _Stop(Exception):
        pass

    def frame(type_, task_id, task_data=None):
        d = {"type": type_, "task_id": task_id}
        if task_data is not None:
            d["task_data"] = task_data
            d["checksum"] = calculate_checksum(task_data, "unit-key")
        return json.dumps(d)

    frames = [
        frame("run_task", "t_old", {"sleep": 30}),
        frame("cancel", "t_old"),
        frame("run_task", "t_new", {"sleep": 30}),
    ]
    snapshot = {}
    w = _worker()

    class _WS:
        async def send(self, message):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            # Yield first so the previously dispatched frame's effects
            # (task creation / cancellation finallies) settle.
            await original_sleep(0.02)
            if frames:
                return frames.pop(0)
            snapshot["active"] = set(w.active_tasks)
            snapshot["running"] = set(w.running_tasks)
            raise StopAsyncIteration

    class _Ctx:
        async def __aenter__(self):
            return _WS()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def fake_connect(uri, **kwargs):
        return _Ctx()

    async def fast_sleep(delay):
        if delay == 10.0:  # heartbeat interval
            await original_sleep(0)
            return
        if delay >= 20:  # execute_task workload; cancelled later anyway
            await original_sleep(delay)
            return
        raise _Stop()  # reconnect backoff -> end the test loop

    monkeypatch.setattr("websockets.connect", fake_connect)
    monkeypatch.setattr("ag_core.distributed.worker.asyncio.sleep", fast_sleep)

    with pytest.raises(_Stop):
        await w.run_production_loop("127.0.0.1", 8000)

    # t_new (accepted AFTER the cancel of t_old) survived the cancel.
    assert snapshot["active"] == {"t_new"}
    assert snapshot["running"] == {"t_new"}


@pytest.mark.asyncio
async def test_reconnect_waits_for_cancelled_task_cleanup(monkeypatch):
    """The reconnect loop must not start a new session until every cancelled
    task's finally has fully completed."""

    class _Stop(Exception):
        pass

    events = []

    async def slow_cleanup_task(self, task_id, task_data):
        self.active_tasks.add(task_id)
        try:
            await original_sleep(30)
        finally:
            await original_sleep(0.02)  # deliberately slow cleanup
            self.running_tasks.pop(task_id, None)
            self.active_tasks.discard(task_id)
            events.append("task_cleanup_done")

    monkeypatch.setattr(ClientWorker, "execute_task", slow_cleanup_task)

    from ag_core.utils.security import calculate_checksum

    task_data = {"sleep": 30}
    run_frame = json.dumps(
        {
            "type": "run_task",
            "task_id": "t1",
            "task_data": task_data,
            "checksum": calculate_checksum(task_data, "unit-key"),
        }
    )
    delivered = {"done": False}

    class _WS:
        async def send(self, message):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not delivered["done"]:
                delivered["done"] = True
                return run_frame
            # Socket drops right after handing over the task.
            raise StopAsyncIteration

    class _Ctx:
        async def __aenter__(self):
            return _WS()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    connects = []

    def fake_connect(uri, **kwargs):
        # Snapshot how many cleanups had completed when this session started.
        connects.append(len(events))
        return _Ctx()

    async def fast_sleep(delay):
        if delay == 10.0:  # heartbeat interval
            await original_sleep(0)
            return
        # Reconnect backoff sleep: stop the loop after the second session.
        if len(connects) >= 2:
            raise _Stop()
        await original_sleep(0)

    monkeypatch.setattr("websockets.connect", fake_connect)
    monkeypatch.setattr("ag_core.distributed.worker.asyncio.sleep", fast_sleep)

    w = _worker()
    with pytest.raises(_Stop):
        await w.run_production_loop("127.0.0.1", 8000)

    assert len(connects) == 2
    # The second session must have started only AFTER the cancelled task's
    # slow finally finished (1 cleanup event already recorded), and nothing
    # from the first session may leak into the new one.
    assert connects[1] == 1
    assert events == ["task_cleanup_done"]
    assert w.running_tasks == {}
    assert w.active_tasks == set()
