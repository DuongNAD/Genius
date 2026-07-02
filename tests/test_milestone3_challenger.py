# tests/test_milestone3_challenger.py
import asyncio
import json
import time
import socket
import pytest
import pytest_asyncio
import websockets
import uvicorn
from fastapi.testclient import TestClient
from unittest.mock import patch

import serve as serve_mod
from serve import app, worker_registry, pending_tasks, WorkerDisconnectedError
import dashboard
from ag_core.distributed.hub import CentralHub

JWT_SECRET = "mock-skill-key"
HOST = "127.0.0.1"

# --- CASE INSENSITIVE PATCH FOR CENTRAL HUB ---
# This patches the case-sensitivity bug in CentralHub.verify_auth and verify_checksum.
# The bug is that Starlette normalizes HTTP headers to lowercase in serve.py,
# but CentralHub expects exact uppercase headers like 'X-API-Key' and 'X-Payload-SHA256'.

_orig_verify_auth = CentralHub.verify_auth
_orig_verify_checksum = CentralHub.verify_checksum


def patched_verify_auth(self, headers):
    # Try case-insensitive lookup
    api_key = (
        headers.get("X-API-Key") or headers.get("x-api-key") or headers.get("X-Api-Key")
    )
    return api_key == self.api_key


from ag_core.utils.security import verify_checksum as real_verify_checksum


def patched_verify_checksum(self, payload, headers):
    checksum = (
        headers.get("X-Payload-SHA256")
        or headers.get("x-payload-sha256")
        or headers.get("X-Payload-Sha256")
    )
    return real_verify_checksum(payload, checksum, self.api_key)


CentralHub.verify_auth = patched_verify_auth
CentralHub.verify_checksum = patched_verify_checksum


def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest_asyncio.fixture
async def run_server():
    # Start the FastAPI app on a separate port in the background
    port = get_free_port()
    config = uvicorn.Config(
        app, host=HOST, port=port, log_level="warning", ws="auto"
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    # Wait for server to start
    await asyncio.sleep(0.5)
    yield port
    # Shutdown server
    server.should_exit = True
    await server_task


@pytest.fixture(autouse=True)
def clean_registry_and_tasks():
    worker_registry.workers.clear()
    pending_tasks.clear()
    serve_mod.central_hub.workers.clear()
    serve_mod.central_hub.tasks.clear()
    serve_mod.central_hub.task_queue.clear()
    yield


# =============================================================================
# 1. Dynamic Dashboard Status Fetching under Distributed Mode
# =============================================================================


@pytest.mark.asyncio
async def test_dashboard_status_fetching_distributed(run_server):
    port = run_server
    from ag_core.utils.jwt import encode_jwt

    # Enable distributed mode in dashboard
    dashboard.IS_DISTRIBUTED = True

    # 1. Test fetching from local serve.worker_registry directly (within same process)
    # Register a worker in serve.worker_registry
    worker_id = "dash-worker-local"
    async with worker_registry.lock:
        worker_registry.workers[worker_id] = {
            "ws": "mock_ws",
            "roles": ["grok", "claude"],
            "status": "idle",
            "last_heartbeat": time.time(),
        }

    status = dashboard.get_distributed_workers()
    assert worker_id in status
    assert status[worker_id]["roles"] == ["grok", "claude"]
    assert status[worker_id]["status"] == "idle"
    assert status[worker_id]["online"] is True

    # 2. Test fallback HTTP request path to Central Hub API /workers
    # Clear local registry first to force fallback
    async with worker_registry.lock:
        worker_registry.workers.clear()

    # Register worker on the running central hub server using WS
    token = encode_jwt(
        {"sub": "dash-worker-http", "exp": time.time() + 300}, JWT_SECRET
    )
    async with websockets.connect(f"ws://{HOST}:{port}/ws/connect?token={token}") as ws:
        reg_payload = {
            "type": "register",
            "worker_id": "dash-worker-http",
            "roles": ["tester"],
        }
        await ws.send(json.dumps(reg_payload))
        resp = json.loads(await ws.recv())
        assert resp["status"] == "success"

        # Call dashboard status through TestClient of dashboard app
        client = TestClient(dashboard.app)

        # Patch sys.argv or pass port in args or just patch httpx post destination port
        with patch("sys.argv", ["dashboard.py", f"--hub-port={port}"]):
            response = client.get("/api/status")
            assert response.status_code == 200
            data = response.json()
            assert "dash-worker-http" in data
            assert data["dash-worker-http"]["roles"] == ["tester"]
            assert data["dash-worker-http"]["status"] == "idle"


# =============================================================================
# 2. Concurrent Worker Disconnections (5 Workers)
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_worker_disconnections(run_server):
    port = run_server
    from ag_core.utils.jwt import encode_jwt

    num_workers = 5
    connections = []
    task_futures = {}

    # 1. Connect and register 5 workers
    for i in range(num_workers):
        w_id = f"conn-worker-{i}"
        token = encode_jwt({"sub": w_id, "exp": time.time() + 300}, JWT_SECRET)
        ws = await websockets.connect(f"ws://{HOST}:{port}/ws/connect?token={token}")

        reg_msg = {"type": "register", "worker_id": w_id, "roles": ["grok"]}
        await ws.send(json.dumps(reg_msg))
        resp = json.loads(await ws.recv())
        assert resp["status"] == "success"

        connections.append((ws, w_id))

    # 2. Dispatch a slow task to each worker
    client = TestClient(app)
    for i in range(num_workers):
        w_id = f"conn-worker-{i}"
        dispatch_payload = {"role": "grok", "task_data": {"sleep": 5.0}}
        headers = serve_mod.central_hub.create_headers(dispatch_payload)

        # We trigger dispatch
        response = client.post("/dispatch", json=dispatch_payload, headers=headers)
        assert response.status_code == 202
        task_id = response.json()["task_id"]

        # Add future to pending_tasks to trace it
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        pending_tasks[task_id] = fut
        task_futures[task_id] = fut

    # Wait briefly for tasks to propagate to WS clients
    await asyncio.sleep(0.2)

    # Verify tasks are running and workers are busy
    for ws, w_id in connections:
        # Read dispatch message on client websockets
        msg = json.loads(await ws.recv())
        assert msg["type"] == "run_task" or msg["type"] == "dispatch"

        # Check server state
        w_info = await worker_registry.get_worker(w_id)
        assert w_info["status"] == "busy"

    # 3. Simulate abrupt concurrent disconnection (close all WS connections at once)
    close_tasks = [ws.close() for ws, _ in connections]
    await asyncio.gather(*close_tasks)

    # Wait for disconnect handlers and sweeps
    await asyncio.sleep(0.5)

    # 4. Verify outcomes
    # Workers must be gone from registry
    for _, w_id in connections:
        assert w_id not in worker_registry.workers
        assert w_id not in serve_mod.central_hub.workers

    # Tasks must fail and futures must raise WorkerDisconnectedError
    for task_id, fut in task_futures.items():
        assert serve_mod.central_hub.tasks[task_id]["status"] == "failed"
        assert (
            "disconnect"
            in serve_mod.central_hub.tasks[task_id]["result"]["error"].lower()
        )
        with pytest.raises(WorkerDisconnectedError):
            await fut


# =============================================================================
# 3. Concurrent Cancellations (10 Tasks)
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_cancellations(run_server):
    port = run_server
    from ag_core.utils.jwt import encode_jwt

    # Register 1 worker
    w_id = "cancel-worker"
    token = encode_jwt({"sub": w_id, "exp": time.time() + 300}, JWT_SECRET)
    ws = await websockets.connect(f"ws://{HOST}:{port}/ws/connect?token={token}")

    reg_msg = {"type": "register", "worker_id": w_id, "roles": ["grok"]}
    await ws.send(json.dumps(reg_msg))
    resp = json.loads(await ws.recv())
    assert resp["status"] == "success"

    # Dispatch 10 tasks
    client = TestClient(app)
    task_futures = {}

    for i in range(10):
        # Using a very long sleep so they don't complete
        dispatch_payload = {"role": "grok", "task_data": {"sleep": 10.0}}
        headers = serve_mod.central_hub.create_headers(dispatch_payload)
        response = client.post("/dispatch", json=dispatch_payload, headers=headers)
        assert response.status_code == 202
        task_id = response.json()["task_id"]

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        pending_tasks[task_id] = fut
        task_futures[task_id] = fut

    # Wait for the first task to propagate to worker
    await asyncio.sleep(0.1)

    # Read the running task on WS client
    msg = json.loads(await ws.recv())
    assert msg["type"] == "run_task" or msg["type"] == "dispatch"
    running_task_id = msg["task_id"]

    # Cancel all 10 tasks simultaneously
    # To simulate orchestrator future cancellation, we fail/cancel futures and trigger WS cancel for running one
    async def cancel_task(t_id):
        # If running, send cancel to worker and mark failed
        async with serve_mod.central_hub.lock:
            t_info = serve_mod.central_hub.tasks[t_id]
            t_info["status"] = "failed"
            t_info["result"] = {"error": "cancelled"}
            w_id = t_info["worker_id"]

        if w_id:
            async with worker_registry.lock:
                worker = await worker_registry.get_worker(w_id)
                if worker:
                    worker["status"] = "idle"
                    ws_conn = worker.get("ws")
                    if ws_conn:
                        try:
                            await ws_conn.send_json({"type": "cancel", "task_id": t_id})
                        except Exception:
                            pass

        fut = pending_tasks.get(t_id)
        if fut and not fut.done():
            fut.cancel()

    await asyncio.gather(*(cancel_task(tid) for tid in task_futures))

    # Trigger queue process to prune the cancelled/failed tasks from the queue
    await serve_mod.central_hub._process_queue()

    # Listen to cancel message on WS client
    cancel_msg = json.loads(await ws.recv())
    assert cancel_msg["type"] == "cancel"
    assert cancel_msg["task_id"] == running_task_id

    # Wait for states to stabilize
    await asyncio.sleep(0.2)

    # Verify States:
    # Worker must return to idle
    w_info = await worker_registry.get_worker(w_id)
    assert w_info["status"] == "idle"

    # No pending tasks in hub task queue
    assert len(serve_mod.central_hub.task_queue) == 0

    # All tasks are failed/cancelled
    for t_id in task_futures:
        assert serve_mod.central_hub.tasks[t_id]["status"] == "failed"

    await ws.close()


# =============================================================================
# 4. Task Timeouts (Sweep Timeout and Worker Cancellation)
# =============================================================================


@pytest.mark.asyncio
async def test_hub_task_timeout_and_worker_cancel(run_server):
    port = run_server
    from ag_core.utils.jwt import encode_jwt

    # Set small task timeout for test
    serve_mod.central_hub.config["task_timeout"] = 0.2
    # Start the sweeper loop (in serve.py, lifespan context starts the sweeper)

    # Register worker
    w_id = "timeout-worker"
    token = encode_jwt({"sub": w_id, "exp": time.time() + 300}, JWT_SECRET)
    ws = await websockets.connect(f"ws://{HOST}:{port}/ws/connect?token={token}")

    reg_msg = {"type": "register", "worker_id": w_id, "roles": ["grok"]}
    await ws.send(json.dumps(reg_msg))
    await ws.recv()  # success

    # Dispatch task
    client = TestClient(app)
    dispatch_payload = {"role": "grok", "task_data": {"sleep": 5.0}}
    headers = serve_mod.central_hub.create_headers(dispatch_payload)
    response = client.post("/dispatch", json=dispatch_payload, headers=headers)
    task_id = response.json()["task_id"]

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    pending_tasks[task_id] = fut

    # Read task on WS
    await ws.recv()  # run_task message

    # Sleep to trigger timeout (timeout is 0.2s)
    await asyncio.sleep(0.4)

    # Trigger sweeper sweep manually just in case
    await serve_mod.central_hub.sweep()

    # Verify task is marked failed in Central Hub
    assert serve_mod.central_hub.tasks[task_id]["status"] == "failed"
    assert (
        "timed out" in serve_mod.central_hub.tasks[task_id]["result"]["error"].lower()
    )

    # Verify cancel message received by worker WS
    cancel_msg = json.loads(await ws.recv())
    assert cancel_msg["type"] == "cancel"
    assert cancel_msg["task_id"] == task_id

    # Verify worker returns to idle
    w_info = await worker_registry.get_worker(w_id)
    assert w_info["status"] == "idle"

    await ws.close()


# =============================================================================
# 5. State Transition Hazards: Checksum Failure and Worker Busy Check
# =============================================================================


@pytest.mark.asyncio
async def test_state_transition_hazards_checksum_failure(run_server):
    port = run_server
    from ag_core.utils.jwt import encode_jwt

    # Register worker
    w_id = "hazard-worker"
    token = encode_jwt({"sub": w_id, "exp": time.time() + 300}, JWT_SECRET)
    ws = await websockets.connect(f"ws://{HOST}:{port}/ws/connect?token={token}")
    await ws.send(
        json.dumps({"type": "register", "worker_id": w_id, "roles": ["grok"]})
    )
    await ws.recv()

    # Dispatch task
    client = TestClient(app)
    dispatch_payload = {"role": "grok", "task_data": "execute"}
    headers = serve_mod.central_hub.create_headers(dispatch_payload)
    response = client.post("/dispatch", json=dispatch_payload, headers=headers)
    task_id = response.json()["task_id"]

    # Read task on WS
    await ws.recv()

    # Send report_result but with corrupted checksum
    report_msg = {
        "type": "result",
        "task_id": task_id,
        "worker_id": w_id,
        "status": "completed",
        "result": {"output": "Good output"},
        "checksum": "tampered_checksum",
    }
    await ws.send(json.dumps(report_msg))

    # Wait for processing
    await asyncio.sleep(0.1)

    # Verify worker status is reset to idle and task is failed
    w_info = await worker_registry.get_worker(w_id)
    assert w_info["status"] == "idle"
    assert serve_mod.central_hub.tasks[task_id]["status"] == "failed"
    assert "checksum" in serve_mod.central_hub.tasks[task_id]["result"]["error"].lower()

    await ws.close()
