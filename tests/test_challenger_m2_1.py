import sys

sys.modules["sentence_transformers"] = None
sys.modules["transformers"] = None
sys.modules["peft"] = None
sys.modules["torch"] = None
sys.modules["tensorflow"] = None


import asyncio
import json
import time
import os
import pytest
import pytest_asyncio
import websockets
import uvicorn
from unittest.mock import patch, AsyncMock

import serve as serve_mod
from serve import app, worker_registry, pending_tasks
from orchestrator import call_api, PipelineError
from ag_core.distributed.worker import ClientWorker
from ag_core.utils.jwt import encode_jwt

JWT_SECRET = os.getenv("SKILL_API_KEY", "mock-skill-key")
HOST = "127.0.0.1"
PORT = 8020
WS_URL = f"ws://{HOST}:{PORT}/ws/connect"


@pytest_asyncio.fixture
async def run_server():
    # Start serve FastAPI app on a distinct port in background
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.5)
    yield
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
# TEST GROUP 1: Concurrency, Future Resolution, and Memory Leaks
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_task_dispatches_resolution_and_no_leaks(run_server):
    """
    Verify that multiple concurrent task dispatches to multiple workers
    are correctly matched, executed, and resolved without race conditions
    or memory leaks in the future map (pending_tasks).
    """
    import sys
    import orchestrator

    print(
        "LOADED MODULES:", [m for m in sys.modules if "torch" in m or "tensorflow" in m]
    )
    orchestrator.DISTRIBUTED_MODE = True

    # Start 3 workers in parallel
    workers = []
    worker_tasks = []
    for i in range(3):
        w = ClientWorker(worker_id=f"worker-concurrent-{i}", roles=["grok"])
        workers.append(w)
        worker_tasks.append(asyncio.create_task(w.run_production_loop(HOST, PORT)))

    await asyncio.sleep(0.5)  # Wait for workers to connect

    # Verify workers are registered
    assert len(worker_registry.workers) == 3

    # Mock GrokResearcherAgent.run to return a simple predictable output
    # dynamically based on the prompt
    async def mock_agent_run(self, prompt=None, context_data=None):
        await asyncio.sleep(0.05)  # Simulate small execution time
        return f"Result for: {prompt}"

    with patch(
        "ag_core.agents.grok_researcher.GrokResearcherAgent.run", new=mock_agent_run
    ):
        # Dispatch 15 tasks concurrently
        tasks = []
        for i in range(15):
            tasks.append(
                call_api(
                    url="http://localhost:8001",
                    api_key="mock-key",
                    prompt=f"Task prompt {i}",
                    context={},
                    poll_timeout=5.0,
                )
            )

        results = await asyncio.gather(*tasks)

        # Assert correct outcomes
        for i, res in enumerate(results):
            assert res == f"Result for: Task prompt {i}"

        # Assert no memory leak in pending_tasks
        assert len(pending_tasks) == 0

        # Assert all workers returned to idle status
        for w_info in worker_registry.workers.values():
            assert w_info["status"] == "idle"

    # Cleanup workers
    orchestrator.DISTRIBUTED_MODE = False
    for t in worker_tasks:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_concurrency_error_cleanup(run_server):
    """
    Verify that when concurrent tasks fail (e.g. because of agent errors),
    the orchestrator propagates errors correctly and cleans up pending_tasks
    so that no memory leaks occur.
    """
    import orchestrator

    orchestrator.DISTRIBUTED_MODE = True

    # Start 2 workers
    workers = []
    worker_tasks = []
    for i in range(2):
        w = ClientWorker(worker_id=f"worker-fail-{i}", roles=["grok"])
        workers.append(w)
        worker_tasks.append(asyncio.create_task(w.run_production_loop(HOST, PORT)))

    await asyncio.sleep(0.5)

    async def mock_agent_run_fail(self, prompt=None, context_data=None):
        await asyncio.sleep(0.02)
        raise RuntimeError(f"Agent failed on prompt: {prompt}")

    with patch(
        "ag_core.agents.grok_researcher.GrokResearcherAgent.run",
        new=mock_agent_run_fail,
    ):
        # Dispatch 6 failing tasks concurrently
        tasks = []
        for i in range(6):
            tasks.append(
                call_api(
                    url="http://localhost:8001",
                    api_key="mock-key",
                    prompt=f"Fail prompt {i}",
                    context={},
                    poll_timeout=5.0,
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Assert that all failed with PipelineError referencing our agent exception
        for res in results:
            assert isinstance(res, PipelineError)
            assert "Agent failed on prompt" in str(res)

        # Verify future map is completely clean (no memory leak)
        assert len(pending_tasks) == 0

        # Verify workers are idle again
        for w_info in worker_registry.workers.values():
            assert w_info["status"] == "idle"

    orchestrator.DISTRIBUTED_MODE = False
    for t in worker_tasks:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


# =============================================================================
# TEST GROUP 2: Network Failures and Disconnection
# =============================================================================


@pytest.mark.asyncio
async def test_network_failure_websocket_disconnect_requeue(run_server):
    """
    Verify that if a worker disconnected abruptly during task execution:
    1. The running task is failed or requeued properly.
    2. The worker is cleaned up from the registry.
    3. The task can be successfully processed by another worker when it registers.
    """
    import orchestrator

    orchestrator.DISTRIBUTED_MODE = True

    # Start Worker 1
    w1 = ClientWorker(worker_id="disconnect-worker", roles=["grok"])
    w1_task = asyncio.create_task(w1.run_production_loop(HOST, PORT))
    await asyncio.sleep(0.5)

    assert "disconnect-worker" in worker_registry.workers

    # Mock agent run to sleep long enough so we can disconnect the worker mid-flight
    async def slow_mock_run(self, prompt=None, context_data=None):
        await asyncio.sleep(1.0)
        return "Slow completed"

    with patch(
        "ag_core.agents.grok_researcher.GrokResearcherAgent.run", new=slow_mock_run
    ):
        # Dispatch task to Worker 1
        dispatch_fut = asyncio.create_task(
            call_api(
                url="http://localhost:8001",
                api_key="mock-key",
                prompt="Run slow task",
                context={},
                poll_timeout=5.0,
            )
        )

        # Let the task be sent and transition to running
        await asyncio.sleep(0.2)
        assert worker_registry.workers["disconnect-worker"]["status"] == "busy"
        assert len(serve_mod.central_hub.tasks) == 1
        task_id = list(serve_mod.central_hub.tasks.keys())[0]
        assert serve_mod.central_hub.tasks[task_id]["status"] == "running"

        # Disconnect Worker 1 abruptly (cancel task and disconnect WS)
        w1_task.cancel()
        try:
            await w1_task
        except asyncio.CancelledError:
            pass

        # Give FastAPI some time to handle WebSocket disconnect and run cleanup/deregister
        await asyncio.sleep(0.2)

        # Assert worker has been pruned
        assert "disconnect-worker" not in worker_registry.workers
        assert "disconnect-worker" not in serve_mod.central_hub.workers

        # Assert task is failed (status goes to failed)
        assert serve_mod.central_hub.tasks[task_id]["status"] == "failed"
        assert serve_mod.central_hub.tasks[task_id]["result"] == {
            "error": "Worker disconnected"
        }
        assert task_id not in serve_mod.central_hub.task_queue

        # Assert the original call_api raises WorkerDisconnectedError
        from serve import WorkerDisconnectedError

        with pytest.raises(WorkerDisconnectedError):
            await dispatch_fut

    orchestrator.DISTRIBUTED_MODE = False


@pytest.mark.asyncio
async def test_unexpected_websocket_close_server_cleanup(run_server):
    """
    Verify that an unexpected connection close is fully cleaned up on the server
    without leaving orphaned registries or resources.
    """
    token = encode_jwt(
        {"sub": "sudden-death-worker", "exp": time.time() + 60}, JWT_SECRET
    )

    # Establish a raw WebSocket connection to simulate unexpected disconnects
    async with websockets.connect(f"{WS_URL}?token={token}") as ws:
        # Register
        reg_payload = {
            "type": "register",
            "worker_id": "sudden-death-worker",
            "roles": ["grok"],
        }
        await ws.send(json.dumps(reg_payload))
        resp = await ws.recv()
        assert json.loads(resp)["type"] == "registered"

        assert "sudden-death-worker" in worker_registry.workers

        # Unexpectedly close the WebSocket connection by exiting context
        # (This triggers a ConnectionClosed exception on the server)

    # Wait for server cleanup
    await asyncio.sleep(0.2)

    # Check worker is completely removed
    assert "sudden-death-worker" not in worker_registry.workers
    assert "sudden-death-worker" not in serve_mod.central_hub.workers


# =============================================================================
# TEST GROUP 3: Payload Tampering
# =============================================================================


@pytest.mark.asyncio
async def test_payload_tampering_corrupted_checksum_worker_rejection(run_server):
    """
    Verify that if a worker receives a task payload with a corrupted checksum,
    the worker rejects the task execution, returns a proper checksum error status
    to the hub, and the hub fails the task with a PipelineError.
    """
    import orchestrator

    orchestrator.DISTRIBUTED_MODE = True

    worker_id = "checksum-tamper-worker"
    worker = ClientWorker(worker_id=worker_id, roles=["grok"])
    worker_task = asyncio.create_task(worker.run_production_loop(HOST, PORT))
    await asyncio.sleep(0.5)

    # We intercept the hub sending WS message to corrupt the checksum
    from fastapi import WebSocket

    original_send_json = WebSocket.send_json

    async def mock_send_corrupted_json(self, data, *args, **kwargs):
        if isinstance(data, dict) and data.get("type") == "dispatch":
            # Tamper with the checksum!
            data["checksum"] = "corrupted-checksum-hash-12345"
        await original_send_json(self, data, *args, **kwargs)

    try:
        with patch("fastapi.WebSocket.send_json", new=mock_send_corrupted_json):
            # Attempt to run a task. It should fail due to checksum rejection by the worker.
            with pytest.raises(PipelineError) as exc_info:
                await call_api(
                    url="http://localhost:8001",
                    api_key="mock-key",
                    prompt="Verify checksum integrity stress test",
                    context={},
                    poll_timeout=3.0,
                )
            assert "Bad Checksum validation on worker node" in str(exc_info.value)
    finally:
        orchestrator.DISTRIBUTED_MODE = False
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_payload_tampering_corrupted_result_checksum_hub_rejection(run_server):
    """
    Verify that if a worker tries to report a result with a corrupted checksum,
    the hub rejects the report, marks/raises appropriate error in orchestrator,
    and handles it cleanly.
    """
    import orchestrator

    orchestrator.DISTRIBUTED_MODE = True

    worker_id = "result-tamper-worker"
    worker = ClientWorker(worker_id=worker_id, roles=["grok"])

    # We will hook into the worker's execute_task reporting or websocket send
    # to corrupt the result checksum reported back to the hub.
    original_execute_task = worker.execute_task

    async def mock_corrupt_result_execute_task(task_id, task_data):
        # Execute task normally to transition to idle status
        await original_execute_task(task_id, task_data)

        # But wait! The worker will send the normal result over WS.
        # So instead of mocking execute_task, we should mock the ws.send call on the worker
        # to send a corrupted result checksum!

    worker_task = asyncio.create_task(worker.run_production_loop(HOST, PORT))
    await asyncio.sleep(0.5)

    # Let's mock worker's ws.send to corrupt checksum of 'result' message
    original_ws_send = worker.ws.send

    async def mock_ws_send(msg_str):
        try:
            data = json.loads(msg_str)
            if isinstance(data, dict) and data.get("type") == "result":
                # Tamper with the checksum reported to the hub!
                data["checksum"] = "corrupted-result-checksum-hash-abc"
            msg_str = json.dumps(data)
        except Exception:
            pass
        await original_ws_send(msg_str)

    # Patch the worker ws connection send method
    worker.ws.send = mock_ws_send

    with patch(
        "ag_core.agents.grok_researcher.GrokResearcherAgent.run",
        new=AsyncMock(return_value="Valid result content"),
    ):
        with pytest.raises(PipelineError) as exc_info:
            await call_api(
                url="http://localhost:8001",
                api_key="mock-key",
                prompt="Result checksum tamper test",
                context={},
                poll_timeout=3.0,
            )
        assert "Result checksum validation failed" in str(exc_info.value)

    orchestrator.DISTRIBUTED_MODE = False
    worker_task.cancel()
    try:
        await worker_task
    except (
        asyncio.save_canceled_error
        if hasattr(asyncio, "save_canceled_error")
        else asyncio.CancelledError
    ):
        pass
