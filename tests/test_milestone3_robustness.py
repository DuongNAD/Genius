import pytest
import asyncio
import time
import json
import hashlib
from typing import Dict, List, Optional, Any, Callable
from ag_core.distributed import CentralHub, ClientWorker
from serve import WorkerDisconnectedError, WorkerRegistry, pending_tasks
import orchestrator


class MockNetworkProtocol:
    def __init__(self):
        self.latency = 0.0
        self.drop_rate = 0.0
        self.error_generators: List[Callable[[str, str, Any], Optional[tuple]]] = []
        self.request_log = []
        self.hub = None
        self.workers: Dict[str, Any] = {}

    def set_hub(self, hub):
        self.hub = hub

    def register_worker(self, worker_id: str, worker):
        self.workers[worker_id] = worker

    def unregister_worker(self, worker_id: str):
        if worker_id in self.workers:
            del self.workers[worker_id]

    async def send_to_hub(
        self, endpoint: str, payload: Any, headers: Dict[str, str]
    ) -> tuple[int, Any]:
        self.request_log.append(("hub", endpoint, payload, headers, time.time()))
        if not self.hub:
            return 503, {"error": "Hub unreachable"}
        status_code, body, _ = await self.hub.handle_request(endpoint, payload, headers)
        return status_code, body

    async def send_to_worker(
        self, worker_id: str, endpoint: str, payload: Any, headers: Dict[str, str]
    ) -> tuple[int, Any]:
        self.request_log.append(
            ("worker", worker_id, endpoint, payload, headers, time.time())
        )
        worker = self.workers.get(worker_id)
        if not worker:
            return 404, {"error": "Worker unreachable"}
        status_code, body, _ = await worker.handle_request(endpoint, payload, headers)
        return status_code, body


@pytest.fixture
def network():
    return MockNetworkProtocol()


@pytest.fixture
def hub(network):
    h = CentralHub(api_key="valid-api-key")
    h.set_network(network)
    yield h
    h.stop_sweeper()


@pytest.mark.asyncio
async def test_workers_endpoint(network, hub):
    worker = ClientWorker("w1", ["grok"])
    worker.set_network(network)
    await worker.register()

    # Query workers endpoint
    payload = {}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/workers", payload, headers)
    assert status_code == 200
    assert "w1" in body
    assert body["w1"]["roles"] == ["grok"]
    assert body["w1"]["status"] == "idle"


@pytest.mark.asyncio
async def test_worker_disconnected_error_on_deregister(network, hub):
    # Setup worker and dispatch a task to it
    worker = ClientWorker("w1", ["grok"])
    worker.set_network(network)
    await worker.register()

    payload = {"role": "grok", "task_data": "execute"}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 202
    task_id = body["task_id"]

    # Setup pending_tasks future
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    pending_tasks[task_id] = fut

    # Check that task status is running
    assert hub.tasks[task_id]["status"] == "running"

    # Worker unregisters/disconnects
    reg = WorkerRegistry()
    # We must mock get_worker and registry workers in serve.py to match central_hub workers
    # Serve.py uses central_hub workers directly. Let's make sure server unregister works
    import serve

    orig_hub = serve.central_hub
    serve.central_hub = hub
    try:
        # Register w1 ws connection mock in hub workers
        hub.workers["w1"]["ws"] = "mock_ws"
        await reg.unregister("w1", "mock_ws")

        assert hub.tasks[task_id]["status"] == "failed"
        assert "disconnect" in hub.tasks[task_id]["result"]["error"].lower()
        with pytest.raises(WorkerDisconnectedError):
            await fut
    finally:
        serve.central_hub = orig_hub
        pending_tasks.pop(task_id, None)


@pytest.mark.asyncio
async def test_heartbeat_sweep_fails_active_tasks(network, hub):
    hub.config["heartbeat_timeout"] = 1.0
    worker = ClientWorker("w1", ["grok"])
    worker.set_network(network)
    await worker.register()

    payload = {"role": "grok", "task_data": "sleep:0.5"}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    task_id = body["task_id"]

    # Sleep to let the background dispatch reach the worker
    await asyncio.sleep(0.05)
    assert hub.tasks[task_id]["status"] == "running"

    # Simulate worker silent heartbeat timeout deterministically
    hub.config["heartbeat_timeout"] = 0.05
    hub.workers["w1"]["last_heartbeat"] -= 1.0
    await hub.sweep()

    # The running task should be marked failed with disconnect error
    assert "w1" not in hub.workers
    assert hub.tasks[task_id]["status"] == "failed"
    assert "disconnect" in hub.tasks[task_id]["result"]["error"].lower()


@pytest.mark.asyncio
async def test_task_timeout_sends_cancel_message(network, hub):
    hub.config["task_timeout"] = 1.0
    worker = ClientWorker("w1", ["grok"])
    worker.set_network(network)
    await worker.register()

    payload = {"role": "grok", "task_data": "sleep:0.5"}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    task_id = body["task_id"]

    # Sleep to let the background dispatch reach the worker
    await asyncio.sleep(0.05)
    assert hub.tasks[task_id]["status"] == "running"

    # Simulate task timeout deterministically
    hub.config["task_timeout"] = 0.05
    hub.tasks[task_id]["started_at"] -= 1.0
    await hub.sweep()

    assert hub.tasks[task_id]["status"] == "failed"
    assert "timed out" in hub.tasks[task_id]["result"]["error"].lower()

    # The cancel message should have been sent to the worker via the network simulator
    cancel_requests = [
        req
        for req in network.request_log
        if req[0] == "worker" and req[1] == "w1" and req[2] == "/cancel"
    ]
    assert len(cancel_requests) == 1
    assert cancel_requests[0][3]["task_id"] == task_id


@pytest.mark.asyncio
async def test_worker_cancellation_handling(network, hub):
    worker = ClientWorker("w1", ["grok"])
    worker.set_network(network)
    await worker.register()

    # Dispatch via hub
    payload = {"role": "grok", "task_data": "sleep:0.5"}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 202
    task_id = body["task_id"]

    # Sleep to let the background dispatch reach the worker
    await asyncio.sleep(0.05)
    assert task_id in worker.running_tasks
    assert worker.status == "busy"

    # Send cancel
    payload_cancel = {"task_id": task_id}
    headers_cancel = worker.create_headers(payload_cancel)
    status_code, body = await network.send_to_worker(
        "w1", "/cancel", payload_cancel, headers_cancel
    )
    assert status_code == 200

    # Wait a bit for cancel to propagate
    await asyncio.sleep(0.05)
    assert worker.status == "idle"
    assert task_id not in worker.running_tasks
