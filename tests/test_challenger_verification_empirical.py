import sys

sys.modules["sentence_transformers"] = None
sys.modules["transformers"] = None
sys.modules["peft"] = None
sys.modules["torch"] = None
sys.modules["tensorflow"] = None


import asyncio
import json
import socket
import pytest
import pytest_asyncio
import uvicorn
from unittest.mock import patch

import serve as serve_mod
from serve import app, worker_registry, pending_tasks
from orchestrator import call_api, PipelineError
from ag_core.distributed.worker import ClientWorker

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


@pytest.mark.asyncio
async def test_concurrent_task_dispatches_resolution(run_server):
    """
    1. Run concurrent task dispatches and check for future resolution race conditions.
    We start multiple workers and dispatch multiple tasks concurrently.
    We check that each task resolves to its specific expected result.
    """
    port = run_server

    # Let's start 3 workers
    workers = []
    worker_tasks = []
    for i in range(3):
        w_id = f"worker-{i}"
        worker = ClientWorker(worker_id=w_id, roles=["grok"])
        workers.append(worker)
        task = asyncio.create_task(worker.run_production_loop(HOST, port))
        worker_tasks.append(task)

    await asyncio.sleep(0.5)  # Wait for them to connect

    # Verify they are all registered and idle
    for i in range(3):
        w_info = await worker_registry.get_worker(f"worker-{i}")
        assert w_info is not None
        assert w_info["status"] == "idle"

    # We will dispatch 6 concurrent tasks
    import orchestrator

    orchestrator.DISTRIBUTED_MODE = True

    # Mock dynamic agent execution to return a result that includes the prompt to distinguish them
    async def mock_agent_run(self, prompt, context_data=None):
        await asyncio.sleep(0.1)  # Simulate some execution delay
        return f"Result for: {prompt}"

    try:
        with patch(
            "ag_core.agents.grok_researcher.GrokResearcherAgent.run", mock_agent_run
        ):
            tasks = [
                call_api(
                    url="http://localhost:8001",
                    api_key="mock-key",
                    prompt=f"Prompt-{idx}",
                    context={"idx": idx},
                    poll_timeout=5.0,
                )
                for idx in range(6)
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for idx, res in enumerate(results):
                assert not isinstance(res, Exception), f"Task {idx} failed with: {res}"
                assert res == f"Result for: Prompt-{idx}"
    finally:
        orchestrator.DISTRIBUTED_MODE = False
        for t in worker_tasks:
            t.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_websocket_closure_pending_future_failure(run_server):
    """
    2. Check network disconnects and WebSocket closures.
    Verify that pending futures are correctly failed and workers set to idle/removed.
    *BUG FINDING*: When a worker's websocket closes, the task is set to 'pending'
    and requeued, but the future waiting in call_api hangs indefinitely.
    Here we verify that the future remains pending (hangs) rather than being failed,
    demonstrating the resilience issue.
    """
    port = run_server
    worker_id = "temp-worker"

    # We want a task that hangs so we can disconnect the worker while it's executing
    task_started_event = asyncio.Event()
    task_hang_event = asyncio.Event()

    async def mock_agent_run(self, prompt, context_data=None):
        task_started_event.set()
        await task_hang_event.wait()
        return "Done"

    worker = ClientWorker(worker_id=worker_id, roles=["grok"])
    worker_task = asyncio.create_task(worker.run_production_loop(HOST, port))
    await asyncio.sleep(0.5)

    import orchestrator

    orchestrator.DISTRIBUTED_MODE = True

    try:
        with patch(
            "ag_core.agents.grok_researcher.GrokResearcherAgent.run", mock_agent_run
        ):
            # Dispatch the API call in a background task
            api_task = asyncio.create_task(
                call_api(
                    url="http://localhost:8001",
                    api_key="mock-key",
                    prompt="Hanging Prompt",
                    context={},
                    poll_timeout=5.0,
                )
            )

            # Wait for worker to start executing the task
            await asyncio.wait_for(task_started_event.wait(), timeout=2.0)

            # Verify task is running and worker is busy
            w_info = await worker_registry.get_worker(worker_id)
            assert w_info["status"] == "busy"

            # Now simulate WebSocket closure by cancelling the worker loop
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)

            # Wait for deregistration to process
            await asyncio.sleep(0.5)

            # Verify WebSocket is closed and worker is deregistered/removed from registry
            w_info_after = await worker_registry.get_worker(worker_id)
            assert (
                w_info_after is None
            )  # Worker was deregistered and removed from registry!

            # Verify that the pending future is failed with WorkerDisconnectedError
            from serve import WorkerDisconnectedError

            with pytest.raises(WorkerDisconnectedError):
                await api_task

    finally:
        orchestrator.DISTRIBUTED_MODE = False
        task_hang_event.set()  # Unhang if still waiting


@pytest.mark.asyncio
async def test_result_tampering_corrupted_checksum(run_server):
    """
    3. Test payload tampering: send dispatch/result messages with missing or corrupted checksums
    and verify they are rejected.
    Here we test a worker sending a RESULT message with a bad checksum.
    """
    port = run_server
    worker_id = "tamper-worker"

    # We will mock the worker's execute_task to send a bad result checksum
    original_execute = ClientWorker.execute_task

    # We want to override the execute_task behaviour to send a tampered checksum
    async def mock_execute_task(self, task_id, task_data):
        # Instead of normal report, send result message with tampered checksum
        tampered_result = {"output": "Tampered Output"}
        payload = {
            "type": "result",
            "task_id": task_id,
            "worker_id": self.worker_id,
            "status": "completed",
            "result": tampered_result,
            "checksum": "bad-checksum-value",
        }
        await self.ws.send(json.dumps(payload))
        self.status = "idle"
        self.current_task = None

    worker = ClientWorker(worker_id=worker_id, roles=["grok"])

    import orchestrator

    orchestrator.DISTRIBUTED_MODE = True

    try:
        with patch.object(ClientWorker, "execute_task", mock_execute_task):
            worker_task = asyncio.create_task(worker.run_production_loop(HOST, port))
            await asyncio.sleep(0.5)

            # This should fail because the hub rejects the result due to checksum mismatch
            with pytest.raises(PipelineError) as exc_info:
                await call_api(
                    url="http://localhost:8001",
                    api_key="mock-key",
                    prompt="Tamper Test",
                    context={},
                    poll_timeout=5.0,
                )
            assert "Result checksum validation failed" in str(exc_info.value)
    finally:
        orchestrator.DISTRIBUTED_MODE = False
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_result_tampering_missing_checksum(run_server):
    """
    3b. Test payload tampering: send result message with missing checksum and verify it is rejected.
    """
    port = run_server
    worker_id = "tamper-worker-missing"

    async def mock_execute_task(self, task_id, task_data):
        payload = {
            "type": "result",
            "task_id": task_id,
            "worker_id": self.worker_id,
            "status": "completed",
            "result": {"output": "No Checksum Output"},
            # Missing checksum field
        }
        await self.ws.send(json.dumps(payload))
        self.status = "idle"
        self.current_task = None

    worker = ClientWorker(worker_id=worker_id, roles=["grok"])

    import orchestrator

    orchestrator.DISTRIBUTED_MODE = True

    try:
        with patch.object(ClientWorker, "execute_task", mock_execute_task):
            worker_task = asyncio.create_task(worker.run_production_loop(HOST, port))
            await asyncio.sleep(0.5)

            with pytest.raises(PipelineError) as exc_info:
                await call_api(
                    url="http://localhost:8001",
                    api_key="mock-key",
                    prompt="Tamper Missing Test",
                    context={},
                    poll_timeout=5.0,
                )
            assert "Missing result checksum" in str(exc_info.value)
    finally:
        orchestrator.DISTRIBUTED_MODE = False
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
