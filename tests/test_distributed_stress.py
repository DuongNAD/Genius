import pytest
import asyncio
import time
from typing import Dict, List, Optional, Any, Callable
from ag_core.distributed import CentralHub, ClientWorker


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
        if self.latency > 0:
            await asyncio.sleep(self.latency)
        if self.drop_rate > 0:
            import random

            if random.random() < self.drop_rate:
                raise asyncio.TimeoutError("Timeout sending to hub")
        for gen in self.error_generators:
            res = gen("hub", endpoint, payload)
            if res is not None:
                return res[0], res[1]

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
        if self.latency > 0:
            await asyncio.sleep(self.latency)
        if self.drop_rate > 0:
            import random

            if random.random() < self.drop_rate:
                raise asyncio.TimeoutError("Timeout sending to worker")
        for gen in self.error_generators:
            res = gen(f"worker:{worker_id}", endpoint, payload)
            if res is not None:
                return res[0], res[1]

        worker = self.workers.get(worker_id)
        if not worker:
            return 404, {"error": "Worker unreachable"}

        status_code, body, _ = await worker.handle_request(endpoint, payload, headers)
        return status_code, body


@pytest.mark.asyncio
async def test_race_condition_worker_disconnect_during_dispatch():
    network = MockNetworkProtocol()
    hub = CentralHub(api_key="valid-api-key")
    hub.set_network(network)

    # Set worker heartbeat timeout very low
    hub.config["heartbeat_timeout"] = 0.05
    # Keep latency 0 for registration and dispatch request
    network.latency = 0.0

    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()

    # Dispatch a task.
    payload = {"role": "grok", "task_data": "data"}
    headers = hub.create_headers(payload)

    # Trigger the dispatch
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 202
    assert body["status"] == "running"  # Task must be dispatched to w1

    # Now set the latency to 0.1 so that send_to_worker will sleep
    network.latency = 0.1

    # Yield control briefly to let _dispatch_to_worker start and enter send_to_worker
    await asyncio.sleep(0.001)

    # Find the background task created by asyncio.create_task(self._dispatch_to_worker)
    dispatch_tasks = [
        t for t in asyncio.all_tasks() if t.get_coro().__name__ == "_dispatch_to_worker"
    ]
    assert len(dispatch_tasks) == 1
    dispatch_task = dispatch_tasks[0]

    # Let time pass so that the worker's heartbeat expires
    await asyncio.sleep(0.06)

    # Trigger liveness check/sweeper on hub to prune the worker
    hub.check_liveness()
    assert "w1" not in hub.workers  # Worker must be pruned

    # Now await the dispatch task. We expect it to finish without raising KeyError!
    await dispatch_task

    # Verify that the task status is failed due to connection error, not crashed with KeyError
    assert hub.tasks[body["task_id"]]["status"] == "failed"
    hub.stop_sweeper()
