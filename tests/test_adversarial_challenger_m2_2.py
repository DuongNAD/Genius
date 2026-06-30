# tests/test_adversarial_challenger_m2_2.py
import asyncio
import json
import time
import hashlib
import socket
import pytest
import pytest_asyncio
import websockets
import uvicorn

import serve as serve_mod
from serve import app, worker_registry, pending_tasks, WorkerDisconnectedError
import orchestrator
from orchestrator import call_api, PipelineError
from ag_core.distributed.worker import ClientWorker
from ag_core.utils.jwt import encode_jwt

JWT_SECRET = "mock-skill-key"
HOST = "127.0.0.1"


def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest_asyncio.fixture
async def run_server():
    # Start serve FastAPI app on a distinct port in background
    port = get_free_port()
    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.5)
    yield port
    server.should_exit = True
    await server_task


@pytest.fixture(autouse=True)
def clean_registry_and_tasks():
    worker_registry.workers.clear()
    pending_tasks.clear()
    serve_mod.central_hub.workers.clear()
    serve_mod.central_hub.tasks.clear()
    yield


# =============================================================================
# 1. Concurrent Task Dispatches, Race Conditions, Memory Leaks
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_dispatches_under_load(run_server):
    """
    Verify correctness, race-freedom, and memory status under concurrent dispatches.
    We register 3 workers and dispatch 15 tasks concurrently.
    """
    port = run_server
    workers = []
    worker_tasks = []

    # Register 3 workers
    for idx in range(3):
        w_id = f"concurrent-worker-{idx}"
        worker = ClientWorker(worker_id=w_id, roles=["grok"])

        # Mock execute_task to run quickly
        async def quick_execute(task_id, task_data, w=worker):
            await asyncio.sleep(0.05)
            # simulate result reporting
            res = {"output": f"Processed {task_data}"}
            w.tasks_completed += 1
            w.status = "idle"
            w.current_task = None

            # Send result back via WS
            serialized_res = json.dumps(res, sort_keys=True).encode("utf-8")
            checksum = hashlib.sha256(serialized_res).hexdigest()
            payload = {
                "type": "result",
                "task_id": task_id,
                "worker_id": w.worker_id,
                "status": "completed",
                "result": res,
                "checksum": checksum,
            }
            await w.ws.send(json.dumps(payload))

        worker.execute_task = quick_execute
        worker_task = asyncio.create_task(worker.run_production_loop(HOST, port))
        workers.append(worker)
        worker_tasks.append(worker_task)

    # Wait for workers to connect and register
    await asyncio.sleep(0.5)

    # Assert workers are registered and idle
    for worker in workers:
        w_info = await worker_registry.get_worker(worker.worker_id)
        assert w_info is not None
        assert w_info["status"] == "idle"

    # Setup orchestrator distributed mode
    orchestrator.DISTRIBUTED_MODE = True

    try:
        # Dispatch 15 tasks concurrently
        async def run_one(t_idx):
            return await call_api(
                url="http://localhost:8001",
                api_key="mock-key",
                prompt=f"Task {t_idx}",
                context={"idx": t_idx},
                poll_timeout=5.0,
            )

        tasks = [run_one(i) for i in range(15)]
        results = await asyncio.gather(*tasks)

        # Verify correctness
        assert len(results) == 15
        for i in range(15):
            assert f"Processed" in results[i]

        # Verify no future resolution memory leaks: pending_tasks dict must be empty
        assert (
            len(pending_tasks) == 0
        ), f"Leaked futures in pending_tasks: {pending_tasks}"

        # Verify workers returned to idle status
        for worker in workers:
            w_info = await worker_registry.get_worker(worker.worker_id)
            assert w_info["status"] == "idle"

        # Verify CentralHub memory leak: central_hub.tasks retains all task metadata forever
        assert len(serve_mod.central_hub.tasks) == 15, "Tasks should be recorded"
        # Since there is no task eviction/cleanup mechanism, this grows indefinitely under load.

    finally:
        orchestrator.DISTRIBUTED_MODE = False
        for wt in worker_tasks:
            wt.cancel()
            try:
                await wt
            except asyncio.CancelledError:
                pass


# =============================================================================
# 2. Network Failures: Worker Disconnection & WebSocket Close
# =============================================================================


@pytest.mark.asyncio
async def test_worker_unexpected_websocket_close(run_server):
    """
    Test unexpected WebSocket close: Verify running tasks are failed or requeued,
    and detect the hanging/dangling future leak in serve/orchestrator.
    """
    port = run_server
    worker_id = "flaky-worker"
    worker = ClientWorker(worker_id=worker_id, roles=["grok"])

    # We will connect and listen manually
    token = encode_jwt({"sub": worker_id, "exp": time.time() + 300}, JWT_SECRET)

    ws_client = None
    task_started_event = asyncio.Event()

    async def run_flaky_worker():
        nonlocal ws_client
        async with websockets.connect(
            f"ws://{HOST}:{port}/ws/connect?token={token}"
        ) as ws:
            ws_client = ws
            # Register
            reg_payload = {
                "type": "register",
                "worker_id": worker_id,
                "roles": ["grok"],
            }
            await ws.send(json.dumps(reg_payload))

            # Read registered response
            resp = await ws.recv()
            assert json.loads(resp)["type"] == "registered"

            # Listen for task dispatch
            async for message in ws:
                data = json.loads(message)
                if data.get("type") in ("run_task", "dispatch"):
                    # Task is dispatched! Signal the test to trigger disconnection
                    task_started_event.set()
                    # Do not report result; just hold connection to simulate executing task
                    break

    worker_task = asyncio.create_task(run_flaky_worker())
    await asyncio.sleep(0.2)  # wait for registry

    w_info = await worker_registry.get_worker(worker_id)
    assert w_info is not None
    assert w_info["status"] == "idle"

    orchestrator.DISTRIBUTED_MODE = True

    async def dispatch_task():
        return await call_api(
            url="http://localhost:8001",
            api_key="mock-key",
            prompt="Slow research prompt",
            context={},
            poll_timeout=2.0,
        )

    dispatch_coro = asyncio.create_task(dispatch_task())

    # Wait for the task to be dispatched and started
    await asyncio.wait_for(task_started_event.wait(), timeout=1.0)

    # Check that task is marked running on server
    assert len(serve_mod.central_hub.tasks) == 1
    task_id = list(serve_mod.central_hub.tasks.keys())[0]
    assert serve_mod.central_hub.tasks[task_id]["status"] == "running"
    assert serve_mod.central_hub.tasks[task_id]["worker_id"] == worker_id

    # Check that there is a future in pending_tasks
    assert task_id in pending_tasks
    future = pending_tasks[task_id]
    assert not future.done()

    # SIMULATE NETWORK FAILURE: Close WebSocket connection abruptly from client side
    await ws_client.close()

    # Wait for server to process disconnect (which unregisters the worker)
    await asyncio.sleep(0.5)

    # 1. Verify worker is unregistered
    assert worker_id not in serve_mod.central_hub.workers
    assert worker_id not in worker_registry.workers

    # 2. Verify running task is marked failed
    assert serve_mod.central_hub.tasks[task_id]["status"] == "failed"
    assert serve_mod.central_hub.tasks[task_id]["result"] == {
        "error": "Worker disconnected"
    }
    assert task_id not in serve_mod.central_hub.task_queue

    # 3. Verify future is resolved with WorkerDisconnectedError and not leaked/hung
    assert future.done()
    with pytest.raises(WorkerDisconnectedError):
        await dispatch_coro

    # Clean up dispatch coroutine (already completed or cancelled)
    dispatch_coro.cancel()
    try:
        await dispatch_coro
    except (asyncio.CancelledError, WorkerDisconnectedError):
        pass

    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

    orchestrator.DISTRIBUTED_MODE = False


# =============================================================================
# 3. Payload Tampering: Corrupted Checksums
# =============================================================================


@pytest.mark.asyncio
async def test_result_payload_tampering_leaks_worker(run_server):
    """
    Test payload tampering: Worker sends a result with a corrupted checksum.
    Verify that the server rejects the result, raises appropriate errors,
    but fails to clean up the worker and task states, causing resources to leak.
    """
    port = run_server
    worker_id = "malicious-worker"
    token = encode_jwt({"sub": worker_id, "exp": time.time() + 300}, JWT_SECRET)

    task_dispatched_event = asyncio.Event()

    async def run_tampering_worker():
        async with websockets.connect(
            f"ws://{HOST}:{port}/ws/connect?token={token}"
        ) as ws:
            # Register
            reg_payload = {
                "type": "register",
                "worker_id": worker_id,
                "roles": ["grok"],
            }
            await ws.send(json.dumps(reg_payload))
            await ws.recv()  # read registered

            # Listen for task dispatch
            async for message in ws:
                data = json.loads(message)
                if data.get("type") in ("run_task", "dispatch"):
                    task_id = data.get("task_id")

                    # Construct result payload but tamper with the checksum
                    res_payload = {
                        "type": "result",
                        "task_id": task_id,
                        "worker_id": worker_id,
                        "status": "completed",
                        "result": {"output": "Tampered data"},
                        "checksum": "corrupted_checksum_hash_value",
                    }
                    await ws.send(json.dumps(res_payload))
                    task_dispatched_event.set()
                    break

    worker_task = asyncio.create_task(run_tampering_worker())
    await asyncio.sleep(0.2)

    orchestrator.DISTRIBUTED_MODE = True

    # Dispatch task through call_api
    with pytest.raises(PipelineError) as exc_info:
        await call_api(
            url="http://localhost:8001",
            api_key="mock-key",
            prompt="Trigger tampering test",
            context={},
            poll_timeout=2.0,
        )

    # Verify server raises appropriate error (Result checksum validation failed)
    assert "Result checksum validation failed" in str(exc_info.value)

    # Wait for worker loop to finish
    await task_dispatched_event.wait()

    # Under the fixed codebase, the worker status is cleaned up and set to idle
    w_info = await worker_registry.get_worker(worker_id)
    assert (
        w_info["status"] == "idle"
    ), "Worker status should be cleaned up and set to idle!"

    # Under the fixed codebase, the task should be marked as failed
    task_id = list(serve_mod.central_hub.tasks.keys())[0]
    assert (
        serve_mod.central_hub.tasks[task_id]["status"] == "failed"
    ), "Task should be marked as failed!"

    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

    orchestrator.DISTRIBUTED_MODE = False
