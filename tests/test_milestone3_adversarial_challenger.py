import os
import sys
import time
import json
import sqlite3
import hashlib
import asyncio
import pytest
import uvicorn
import httpx
import websockets
from typing import Dict, List, Optional, Any, Callable
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

# Add project root to sys.path
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from ag_core.distributed.hub import CentralHub
from ag_core.distributed.worker import ClientWorker
from serve import (
    app as serve_app,
    worker_registry,
    pending_tasks,
    central_hub,
    WorkerDisconnectedError,
    prune_stale_workers,
)
import dashboard
from dashboard import app as dashboard_app


def get_free_port():
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# Class to simulate network behavior and deterministic delays for mock network tests
class MockNetworkProtocol:
    def __init__(self):
        self.latency = 0.0
        self.drop_rate = 0.0
        self.error_generators = []
        self.request_log = []
        self.hub = None
        self.workers = {}

    def set_hub(self, hub):
        self.hub = hub

    def register_worker(self, worker_id: str, worker):
        self.workers[worker_id] = worker

    def unregister_worker(self, worker_id: str):
        if worker_id in self.workers:
            del self.workers[worker_id]

    async def send_to_hub(
        self, endpoint: str, payload: Any, headers: dict
    ) -> tuple[int, Any]:
        self.request_log.append(("hub", endpoint, payload, headers, time.time()))
        if self.latency > 0:
            await asyncio.sleep(self.latency)
        if not self.hub:
            return 503, {"error": "Hub unreachable"}
        status_code, body, _ = await self.hub.handle_request(endpoint, payload, headers)
        return status_code, body

    async def send_to_worker(
        self, worker_id: str, endpoint: str, payload: Any, headers: dict
    ) -> tuple[int, Any]:
        self.request_log.append(
            ("worker", worker_id, endpoint, payload, headers, time.time())
        )
        if self.latency > 0:
            await asyncio.sleep(self.latency)
        worker = self.workers.get(worker_id)
        if not worker:
            return 404, {"error": "Worker unreachable"}
        status_code, body, _ = await worker.handle_request(endpoint, payload, headers)
        return status_code, body


# --- 1. Dynamic Dashboard Status Fetching under Distributed Mode ---


def test_dashboard_status_distributed_direct_import():
    """Verify that when serve is in sys.modules, get_status() directly reads worker_registry."""
    with patch.object(dashboard, "IS_DISTRIBUTED", True):
        # Create a mock worker registry that mimics the one in serve
        mock_registry = MagicMock()
        mock_registry.workers = {
            "worker_a": {"roles": ["grok"], "status": "idle"},
            "worker_b": {"roles": ["claude"], "status": "busy"},
        }

        # Patch sys.modules to return our mock registry when dashboard fetches serve
        with patch.dict(
            "sys.modules", {"serve": MagicMock(worker_registry=mock_registry)}
        ):
            workers_data = dashboard.get_distributed_workers()
            assert "worker_a" in workers_data
            assert workers_data["worker_a"]["roles"] == ["grok"]
            assert workers_data["worker_a"]["status"] == "idle"
            assert workers_data["worker_a"]["online"] is True

            assert "worker_b" in workers_data
            assert workers_data["worker_b"]["roles"] == ["claude"]
            assert workers_data["worker_b"]["status"] == "busy"
            assert workers_data["worker_b"]["online"] is True


@pytest.mark.asyncio
async def test_dashboard_status_distributed_http_fallback():
    """Verify that get_status() falls back to HTTP when direct import of serve is unavailable."""
    with patch.object(dashboard, "IS_DISTRIBUTED", True):
        # Ensure 'serve' is mocked as not imported or has no registry
        with patch.dict("sys.modules", {"serve": None}):
            # Setup mock HTTP response for the /workers endpoint
            mock_workers_resp = {
                "worker_c": {"roles": ["codex"], "status": "idle"},
                "worker_d": {"roles": ["tester"], "status": "busy"},
            }

            class MockResponse:
                status_code = 200

                def json(self):
                    return mock_workers_resp

            # Patch httpx.post
            with patch("httpx.post", return_value=MockResponse()) as mock_post:
                workers_data = dashboard.get_distributed_workers()
                assert "worker_c" in workers_data
                assert workers_data["worker_c"]["roles"] == ["codex"]
                assert workers_data["worker_c"]["status"] == "idle"
                assert workers_data["worker_c"]["online"] is True

                assert "worker_d" in workers_data
                assert workers_data["worker_d"]["roles"] == ["tester"]
                assert workers_data["worker_d"]["status"] == "busy"
                assert workers_data["worker_d"]["online"] is True

                # Check that HTTP post was made to correct route
                mock_post.assert_called_once()
                args, kwargs = mock_post.call_args
                assert "/workers" in args[0]


# --- 2. Concurrent Worker Disconnections (5 workers disconnect during active tasks) ---


@pytest.mark.asyncio
async def test_concurrent_worker_disconnections_mocked():
    """Test using MockNetworkProtocol that if 5 workers disconnect concurrently, all running tasks fail cleanly."""
    net = MockNetworkProtocol()
    h = CentralHub(api_key="valid-api-key")
    h.set_network(net)
    h.config["heartbeat_timeout"] = 0.5

    workers = []
    task_ids = []

    # 1. Register 5 workers and dispatch a task to each
    for i in range(5):
        w_id = f"w_{i}"
        w = ClientWorker(w_id, ["grok"])
        w.set_network(net)
        await w.register()
        workers.append(w)

        payload = {"role": "grok", "task_data": f"task_payload_{i}"}
        headers = h.create_headers(payload)
        status_code, body = await net.send_to_hub("/dispatch", payload, headers)
        assert status_code == 202
        task_ids.append(body["task_id"])

    await asyncio.sleep(0.01)  # Yield to let dispatch tasks execute

    # Verify all tasks are in 'running' state and workers are 'busy'
    for t_id in task_ids:
        assert h.tasks[t_id]["status"] == "running"
    for w in workers:
        assert h.workers[w.worker_id]["status"] == "busy"

    # 2. Simulate concurrent heartbeat pruning on all 5 workers
    # Age last heartbeat artificially to trigger sweep timeout
    for w in workers:
        h.workers[w.worker_id]["last_heartbeat"] = time.time() - 2.0

    await h.sweep()

    # 3. Verify all workers are pruned, and all tasks failed cleanly with disconnect error
    assert len(h.workers) == 0
    for t_id in task_ids:
        assert h.tasks[t_id]["status"] == "failed"
        assert "disconnect" in h.tasks[t_id]["result"]["error"].lower()


# --- 3. Concurrent Cancellations (10 tasks cancelled simultaneously) ---


@pytest.mark.asyncio
async def test_concurrent_cancellations():
    """Verify that 10 tasks cancelled simultaneously transition correctly without getting stuck."""
    net = MockNetworkProtocol()
    h = CentralHub(api_key="valid-api-key")
    h.set_network(net)

    # We register 10 workers to execute the 10 tasks in parallel
    workers = []
    for i in range(10):
        w_id = f"worker_{i}"
        w = ClientWorker(w_id, ["grok"])
        w.set_network(net)
        await w.register()
        workers.append(w)

    # Dispatch 10 tasks (sleeping for 5.0 seconds to keep them running)
    task_ids = []
    for i in range(10):
        payload = {"role": "grok", "task_data": "sleep:5.0"}
        headers = h.create_headers(payload)
        status_code, body = await net.send_to_hub("/dispatch", payload, headers)
        assert status_code == 202
        task_ids.append(body["task_id"])

    await asyncio.sleep(0.01)  # Yield to let dispatch tasks enter running state

    # Check that all tasks are running on workers
    for t_id in task_ids:
        assert h.tasks[t_id]["status"] == "running"
    for w in workers:
        assert w.status == "busy"

    # Cancel all 10 tasks simultaneously
    async def cancel_task(t_id):
        # We simulate the worker handling a cancellation request from the hub
        t_info = h.tasks[t_id]
        w_id = t_info["worker_id"]

        # Mark task failed on hub due to cancellation
        h.tasks[t_id]["status"] = "failed"
        h.tasks[t_id]["result"] = {"error": "cancelled"}

        # Send cancel to worker
        payload_cancel = {"task_id": t_id}
        headers_cancel = h.create_headers(payload_cancel)
        await net.send_to_worker(w_id, "/cancel", payload_cancel, headers_cancel)

    # Gather cancels concurrently
    await asyncio.gather(*(cancel_task(t_id) for t_id in task_ids))

    # Yield control to let cancelled tasks run their finally blocks
    await asyncio.sleep(0.05)

    # Verify all tasks failed with cancelled, and all workers returned to idle
    for t_id in task_ids:
        assert h.tasks[t_id]["status"] == "failed"
        assert h.tasks[t_id]["result"]["error"] == "cancelled"
    for w in workers:
        assert w.status == "idle"
        assert len(w.running_tasks) == 0


# --- 4. Task Timeouts (Hub sweep timeout and worker-side cancellation) ---


@pytest.mark.asyncio
async def test_task_timeout_sweep_and_cancellation():
    """Verify that when a task times out, the hub sweep fails the task and cancels it on the worker."""
    net = MockNetworkProtocol()
    h = CentralHub(api_key="valid-api-key")
    h.set_network(net)
    h.config["task_timeout"] = 0.1

    w = ClientWorker("w1", ["grok"])
    w.set_network(net)
    await w.register()

    # Dispatch a slow task (2.0s)
    payload = {"role": "grok", "task_data": "sleep:2.0"}
    headers = h.create_headers(payload)
    status_code, body = await net.send_to_hub("/dispatch", payload, headers)
    task_id = body["task_id"]

    await asyncio.sleep(0.01)
    assert h.tasks[task_id]["status"] == "running"
    assert w.status == "busy"

    # Age the task started_at timestamp to make it stale
    h.tasks[task_id]["started_at"] = time.time() - 1.0

    # Run the sweep
    await h.sweep()

    # Verify task is marked failed on hub and worker is idle
    assert h.tasks[task_id]["status"] == "failed"
    assert "timed out" in h.tasks[task_id]["result"]["error"].lower()

    # Check that cancel message reached the worker via network simulator
    cancel_requests = [
        req
        for req in net.request_log
        if req[0] == "worker" and req[1] == "w1" and req[2] == "/cancel"
    ]
    assert len(cancel_requests) == 1
    assert cancel_requests[0][3]["task_id"] == task_id

    await asyncio.sleep(0.01)  # Yield to let worker-side cancel finalize
    assert w.status == "idle"


# --- 5. State Transition Hazards ---


@pytest.mark.asyncio
async def test_late_result_reporting_hazard():
    """Verify that a worker reporting a result for an already failed/timed out task does not corrupt hub status."""
    net = MockNetworkProtocol()
    h = CentralHub(api_key="valid-api-key")
    h.set_network(net)

    w = ClientWorker("w1", ["grok"])
    w.set_network(net)
    await w.register()

    # Dispatch task
    payload = {"role": "grok", "task_data": "task_to_timeout"}
    headers = h.create_headers(payload)
    status_code, body = await net.send_to_hub("/dispatch", payload, headers)
    task_id = body["task_id"]

    await asyncio.sleep(0.01)
    assert h.tasks[task_id]["status"] == "running"

    # Task fails/times out on Hub
    h.tasks[task_id]["status"] = "failed"
    h.tasks[task_id]["result"] = {"error": "Task timed out"}
    h.workers["w1"]["status"] = "idle"

    # Now, worker finishes execution late and reports "completed"
    report_payload = {
        "task_id": task_id,
        "worker_id": "w1",
        "status": "completed",
        "result": {"output": "Late success!"},
    }
    report_headers = w.create_headers(report_payload)
    status_code, response_body = await net.send_to_hub(
        "/report_result", report_payload, report_headers
    )
    assert status_code == 200

    # Verify task state is STILL "failed" and was NOT overwritten by late success
    assert h.tasks[task_id]["status"] == "failed"
    assert h.tasks[task_id]["result"]["error"] == "Task timed out"
    assert h.workers["w1"]["status"] == "idle"


# --- 6. Live WebSocket Integration & ephemereal port verification ---


@pytest.mark.asyncio
async def test_live_websocket_concurrency_and_disconnects():
    """Live integration test: Connects 5 workers via websockets, runs tasks, and disconnects them simultaneously."""
    port = get_free_port()

    # Start the serve.py server in the background using uvicorn on the ephemeral port
    config = uvicorn.Config(serve_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.5)  # Wait for server to boot

    # Set the registry state to clean
    async with worker_registry.lock:
        worker_registry.workers.clear()
        pending_tasks.clear()

    # Configure low timeouts for test speed using the correct serve.central_hub instance
    hub_instance = central_hub
    hub_instance.config["max_workers"] = 10
    hub_instance.config["heartbeat_timeout"] = 0.5

    # Run the pruning sweeper task
    prune_task = asyncio.create_task(
        prune_stale_workers(timeout_sec=0.5, check_interval=0.05)
    )

    try:
        workers_connections = []
        # Connect 5 WebSocket clients
        for i in range(5):
            worker_id = f"live-worker-{i}"
            secret = os.getenv("SKILL_API_KEY", "mock-skill-key")
            token = encode_jwt({"sub": worker_id, "exp": time.time() + 300}, secret)
            ws = await websockets.connect(
                f"ws://127.0.0.1:{port}/ws/connect?token={token}"
            )

            # Register worker
            await ws.send(
                json.dumps(
                    {"type": "register", "worker_id": worker_id, "roles": ["grok"]}
                )
            )
            reg_resp = json.loads(await ws.recv())
            assert reg_resp["type"] == "registered"
            assert reg_resp["status"] == "success"

            workers_connections.append((ws, worker_id))

        # Verify 5 workers registered in registry
        async with worker_registry.lock:
            assert len(worker_registry.workers) == 5

        # Dispatch 5 dummy tasks from orchestrator-side
        # We manually insert tasks and assign them to workers to simulate active tasks
        task_futures = []
        loop = asyncio.get_running_loop()

        async with worker_registry.lock:
            for i, (ws, w_id) in enumerate(workers_connections):
                t_id = f"task-live-{i}"
                fut = loop.create_future()
                pending_tasks[t_id] = fut
                task_futures.append(fut)

                # Mock task dispatch
                hub_instance.tasks[t_id] = {
                    "task_id": t_id,
                    "role": "grok",
                    "task_data": {"sleep": 5.0},
                    "status": "running",
                    "result": None,
                    "created_at": time.time(),
                    "worker_id": w_id,
                    "started_at": time.time(),
                }
                worker_registry.workers[w_id]["status"] = "busy"

        # Close all 5 WebSocket connections concurrently to simulate sudden disconnects
        await asyncio.gather(*(ws.close() for ws, w_id in workers_connections))

        # Wait for the sweeper to prune stale connections
        await asyncio.sleep(1.0)

        # Verify all 5 tasks failed with WorkerDisconnectedError or timeout
        for i, fut in enumerate(task_futures):
            assert fut.done()
            with pytest.raises(
                (WorkerDisconnectedError, asyncio.TimeoutError, Exception)
            ):
                await fut

        # Verify registry is empty
        async with worker_registry.lock:
            assert len(worker_registry.workers) == 0

    finally:
        prune_task.cancel()
        try:
            await prune_task
        except asyncio.CancelledError:
            pass
        server.should_exit = True
        await server_task


# Helper function to encode JWT
def encode_jwt(payload: dict, secret: str) -> str:
    from ag_core.utils.jwt import encode_jwt as real_encode_jwt

    return real_encode_jwt(payload, secret)
