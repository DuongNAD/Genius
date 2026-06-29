import pytest
import asyncio
import time
import json
import hashlib
from typing import Dict, List, Optional, Any, Callable

# -----------------------------------------------------------------------------
# Classes for Behavior Verification
# -----------------------------------------------------------------------------

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

    async def send_to_hub(self, endpoint: str, payload: Any, headers: Dict[str, str]) -> tuple[int, Any]:
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

    async def send_to_worker(self, worker_id: str, endpoint: str, payload: Any, headers: Dict[str, str]) -> tuple[int, Any]:
        self.request_log.append(("worker", worker_id, endpoint, payload, headers, time.time()))
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


from ag_core.distributed import CentralHub, ClientWorker

async def call_api_with_retry(network: MockNetworkProtocol, endpoint: str, payload: Any, api_key: str, max_retries=3, base_delay=0.005):
    retries = 0
    while True:
        serialized = json.dumps(payload, sort_keys=True).encode('utf-8')
        checksum = hashlib.sha256(serialized).hexdigest()
        headers = {
            "X-API-Key": api_key,
            "X-Payload-SHA256": checksum
        }
        try:
            status_code, body = await network.send_to_hub(endpoint, payload, headers)
            if status_code in (200, 201, 202):
                return status_code, body
            elif status_code in (429, 503) and retries < max_retries:
                retries += 1
                # If base_delay is negative, handle or use abs value to prevent negative sleep
                delay = abs(base_delay) * (2 ** retries)
                await asyncio.sleep(delay)
            else:
                return status_code, body
        except (asyncio.TimeoutError, ConnectionError) as e:
            if retries < max_retries:
                retries += 1
                delay = abs(base_delay) * (2 ** retries)
                await asyncio.sleep(delay)
            else:
                raise e

async def wait_for_task(hub, task_id, timeout=1.0):
    start = time.time()
    while time.time() - start < timeout:
        status = hub.tasks[task_id]["status"]
        if status in ("completed", "failed"):
            return status
        await asyncio.sleep(0.001)
    return hub.tasks[task_id]["status"]

# -----------------------------------------------------------------------------
# Pytest Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def network():
    return MockNetworkProtocol()

@pytest.fixture
def hub(network):
    h = CentralHub(api_key="valid-api-key")
    h.set_network(network)
    yield h
    h.stop_sweeper()

# =============================================================================
# TIER 1: FEATURE COVERAGE (30 TESTS, >=5 PER FEATURE)
# =============================================================================

# --- FEATURE 1: Worker Registration and Heartbeats ---

@pytest.mark.asyncio
async def test_t1_f1_successful_worker_registration(network, hub):
    worker = ClientWorker("worker_1", ["grok_researcher"])
    worker.set_network(network)
    status_code, body = await worker.register()
    assert status_code == 200
    assert body["status"] == "registered"
    assert "worker_1" in hub.workers
    assert hub.workers["worker_1"]["roles"] == ["grok_researcher"]

@pytest.mark.asyncio
async def test_t1_f1_successful_heartbeat(network, hub):
    worker = ClientWorker("worker_1", ["grok_researcher"])
    worker.set_network(network)
    await worker.register()
    
    status_code, body = await worker.send_heartbeat()
    assert status_code == 200
    assert body["status"] == "alive"

@pytest.mark.asyncio
async def test_t1_f1_heartbeat_loop_sends_periodic_updates(network, hub):
    worker = ClientWorker("worker_1", ["grok_researcher"])
    worker.set_network(network)
    await worker.register()
    
    t_start = hub.workers["worker_1"]["last_heartbeat"]
    await worker.start_heartbeats()
    await asyncio.sleep(0.12)
    await worker.stop_heartbeats()
    
    t_end = hub.workers["worker_1"]["last_heartbeat"]
    assert t_end > t_start

@pytest.mark.asyncio
async def test_t1_f1_multiple_workers_registration(network, hub):
    w1 = ClientWorker("worker_1", ["grok_researcher"])
    w2 = ClientWorker("worker_2", ["claude_architect"])
    w1.set_network(network)
    w2.set_network(network)
    
    s1, r1 = await w1.register()
    s2, r2 = await w2.register()
    assert s1 == 200 and s2 == 200
    assert len(hub.workers) == 2
    assert "worker_1" in hub.workers and "worker_2" in hub.workers

@pytest.mark.asyncio
async def test_t1_f1_heartbeat_updates_timestamp(network, hub):
    worker = ClientWorker("worker_1", ["grok_researcher"])
    worker.set_network(network)
    await worker.register()
    
    t1 = hub.workers["worker_1"]["last_heartbeat"]
    # Wait until time.time() has actually increased to avoid clock resolution limitations (especially on Windows)
    while time.time() <= t1:
        await asyncio.sleep(0.005)
    await worker.send_heartbeat()
    t2 = hub.workers["worker_1"]["last_heartbeat"]
    assert t2 > t1


# --- FEATURE 2: API Authentication and Checksums ---

@pytest.mark.asyncio
async def test_t1_f2_valid_auth_and_checksum_accepted(network, hub):
    payload = {"role": "grok_researcher", "task_data": "valid_request"}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 202
    assert "task_id" in body

@pytest.mark.asyncio
async def test_t1_f2_invalid_api_key_rejected(network, hub):
    payload = {"role": "grok_researcher", "task_data": "some_request"}
    headers = hub.create_headers(payload)
    headers["X-API-Key"] = "wrong-api-key"
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 401
    assert "error" in body

@pytest.mark.asyncio
async def test_t1_f2_missing_api_key_rejected(network, hub):
    payload = {"role": "grok_researcher", "task_data": "some_request"}
    headers = hub.create_headers(payload)
    del headers["X-API-Key"]
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 401

@pytest.mark.asyncio
async def test_t1_f2_invalid_checksum_rejected(network, hub):
    payload = {"role": "grok_researcher", "task_data": "some_request"}
    headers = hub.create_headers(payload)
    headers["X-Payload-SHA256"] = "wrongchecksumwrongchecksumwrongchecksumwrongchecksumwrongchecksum12"
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 400
    assert body["error"] == "Bad Checksum"

@pytest.mark.asyncio
async def test_t1_f2_missing_checksum_rejected(network, hub):
    payload = {"role": "grok_researcher", "task_data": "some_request"}
    headers = hub.create_headers(payload)
    del headers["X-Payload-SHA256"]
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 400


# --- FEATURE 3: Async Task Processing & Execution ---

@pytest.mark.asyncio
async def test_t1_f3_async_task_state_transition(network, hub):
    worker = ClientWorker("worker_1", ["grok_researcher"])
    worker.set_network(network)
    await worker.register()
    
    payload = {"role": "grok_researcher", "task_data": "run_quick"}
    headers = hub.create_headers(payload)
    _, dispatch_res = await network.send_to_hub("/dispatch", payload, headers)
    
    task_id = dispatch_res["task_id"]
    # Wait for execution to start (up to 0.5s) to avoid fragile timing
    for _ in range(100):
        if worker.status == "busy":
            break
        await asyncio.sleep(0.005)
    assert worker.status == "busy"
    
    await wait_for_task(hub, task_id)  # Let task complete
    assert worker.status == "idle"
    assert hub.tasks[task_id]["status"] == "completed"

@pytest.mark.asyncio
async def test_t1_f3_successful_task_execution_result(network, hub):
    worker = ClientWorker("worker_1", ["grok_researcher"])
    worker.set_network(network)
    await worker.register()
    
    payload = {"role": "grok_researcher", "task_data": "hello"}
    headers = hub.create_headers(payload)
    _, body = await network.send_to_hub("/dispatch", payload, headers)
    
    task_id = body["task_id"]
    await wait_for_task(hub, task_id)
    assert hub.tasks[task_id]["result"]["output"] == "Processed: hello"

@pytest.mark.asyncio
async def test_t1_f3_failed_task_execution_result(network, hub):
    worker = ClientWorker("worker_1", ["grok_researcher"])
    worker.set_network(network)
    await worker.register()
    
    payload = {"role": "grok_researcher", "task_data": "fail_instantly"}
    headers = hub.create_headers(payload)
    _, body = await network.send_to_hub("/dispatch", payload, headers)
    
    task_id = body["task_id"]
    await wait_for_task(hub, task_id)
    assert hub.tasks[task_id]["status"] == "failed"
    assert "error" in hub.tasks[task_id]["result"]

@pytest.mark.asyncio
async def test_t1_f3_task_status_polling_during_execution(network, hub):
    worker = ClientWorker("worker_1", ["grok_researcher"])
    worker.set_network(network)
    await worker.register()
    
    payload = {"role": "grok_researcher", "task_data": "hello"}
    headers = hub.create_headers(payload)
    _, body = await network.send_to_hub("/dispatch", payload, headers)
    task_id = body["task_id"]
    
    # Poll immediately
    poll_payload = {"task_id": task_id}
    poll_headers = hub.create_headers(poll_payload)
    status_code, status_body = await network.send_to_hub("/task_status", poll_payload, poll_headers)
    assert status_code == 200
    assert status_body["status"] in ("pending", "running")
    
    await wait_for_task(hub, task_id)
    # Poll after completion
    status_code, status_body = await network.send_to_hub("/task_status", poll_payload, poll_headers)
    assert status_code == 200
    assert status_body["status"] == "completed"

@pytest.mark.asyncio
async def test_t1_f3_task_execution_payload_integrity(network, hub):
    worker = ClientWorker("worker_1", ["grok_researcher"])
    worker.set_network(network)
    await worker.register()
    
    payload = {"role": "grok_researcher", "task_data": "hello"}
    headers = hub.create_headers(payload)
    _, body = await network.send_to_hub("/dispatch", payload, headers)
    await wait_for_task(hub, body["task_id"])
    
    # Verify reported result request matches checksum format
    assert len(network.request_log) > 0
    report_req = [req for req in network.request_log if req[1] == "/report_result"][0]
    rep_payload = report_req[2]
    rep_headers = report_req[3]
    assert hub.verify_checksum(rep_payload, rep_headers)


# --- FEATURE 4: Routing & Dispatch (Orchestrator Polling & Routing) ---

@pytest.mark.asyncio
async def test_t1_f4_route_task_by_matching_role(network, hub):
    w1 = ClientWorker("worker_1", ["grok_researcher"])
    w2 = ClientWorker("worker_2", ["claude_architect"])
    w1.set_network(network)
    w2.set_network(network)
    await w1.register()
    await w2.register()
    
    payload = {"role": "claude_architect", "task_data": "design_doc"}
    headers = hub.create_headers(payload)
    _, body = await network.send_to_hub("/dispatch", payload, headers)
    await wait_for_task(hub, body["task_id"])
    
    assert w2.tasks_completed == 1
    assert w1.tasks_completed == 0

@pytest.mark.asyncio
async def test_t1_f4_task_queued_when_no_worker_available(network, hub):
    payload = {"role": "grok_researcher", "task_data": "queued_test"}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 202
    assert body["status"] == "pending"
    assert hub.task_queue.qsize() == 1

@pytest.mark.asyncio
async def test_t1_f4_task_queued_when_all_workers_busy(network, hub):
    w1 = ClientWorker("worker_1", ["grok_researcher"])
    w1.set_network(network)
    await w1.register()
    
    payload1 = {"role": "grok_researcher", "task_data": "task_1"}
    headers1 = hub.create_headers(payload1)
    await network.send_to_hub("/dispatch", payload1, headers1)
    
    payload2 = {"role": "grok_researcher", "task_data": "task_2"}
    headers2 = hub.create_headers(payload2)
    _, body2 = await network.send_to_hub("/dispatch", payload2, headers2)
    assert body2["status"] == "pending"
    assert hub.task_queue.qsize() == 1

@pytest.mark.asyncio
async def test_t1_f4_poll_non_existent_task_returns_404(network, hub):
    poll_payload = {"task_id": "non_existent_task_123"}
    poll_headers = hub.create_headers(poll_payload)
    status_code, _ = await network.send_to_hub("/task_status", poll_payload, poll_headers)
    assert status_code == 404

@pytest.mark.asyncio
async def test_t1_f4_route_task_to_specific_worker_among_many(network, hub):
    w1 = ClientWorker("w1", ["grok"])
    w2 = ClientWorker("w2", ["claude"])
    w1.set_network(network)
    w2.set_network(network)
    await w1.register()
    await w2.register()
    
    payload = {"role": "claude", "task_data": "data"}
    headers = hub.create_headers(payload)
    await network.send_to_hub("/dispatch", payload, headers)
    await asyncio.sleep(0.02)
    assert hub.tasks["task_1"]["worker_id"] == "w2"


# --- FEATURE 5: Resilient HTTP & Connection Retries ---

@pytest.mark.asyncio
async def test_t1_f5_retry_on_transient_http_429(network, hub):
    attempts = 0
    def gen_429(dest, endpoint, payload):
        nonlocal attempts
        if endpoint == "/dispatch" and attempts < 2:
            attempts += 1
            return 429, {"error": "Rate limit exceeded"}
        return None
    network.error_generators.append(gen_429)
    
    payload = {"role": "grok", "task_data": "data"}
    status_code, body = await call_api_with_retry(network, "/dispatch", payload, hub.api_key)
    assert status_code == 202
    assert attempts == 2

@pytest.mark.asyncio
async def test_t1_f5_retry_on_transient_http_503(network, hub):
    attempts = 0
    def gen_503(dest, endpoint, payload):
        nonlocal attempts
        if endpoint == "/dispatch" and attempts < 1:
            attempts += 1
            return 503, {"error": "Server Unavailable"}
        return None
    network.error_generators.append(gen_503)
    
    payload = {"role": "grok", "task_data": "data"}
    status_code, body = await call_api_with_retry(network, "/dispatch", payload, hub.api_key)
    assert status_code == 202
    assert attempts == 1

@pytest.mark.asyncio
async def test_t1_f5_recovery_after_network_timeout(network, hub):
    attempts = 0
    original_send = network.send_to_hub
    
    async def mocked_send(endpoint, payload, headers):
        nonlocal attempts
        if endpoint == "/dispatch" and attempts < 1:
            attempts += 1
            raise asyncio.TimeoutError("Simulated network drop")
        return await original_send(endpoint, payload, headers)
    
    network.send_to_hub = mocked_send
    payload = {"role": "grok", "task_data": "data"}
    status_code, body = await call_api_with_retry(network, "/dispatch", payload, hub.api_key)
    assert status_code == 202
    assert attempts == 1

@pytest.mark.asyncio
async def test_t1_f5_exponential_backoff_timing(network, hub):
    attempts = 0
    def gen_429(dest, endpoint, payload):
        nonlocal attempts
        if endpoint == "/dispatch" and attempts < 2:
            attempts += 1
            return 429, {"error": "Rate limit exceeded"}
        return None
    network.error_generators.append(gen_429)
    
    payload = {"role": "grok", "task_data": "data"}
    t1 = time.time()
    await call_api_with_retry(network, "/dispatch", payload, hub.api_key, base_delay=0.01)
    t2 = time.time()
    # Retries are at 1 and 2, delays should be ~0.02s + ~0.04s = ~0.06s.
    assert (t2 - t1) >= 0.05

@pytest.mark.asyncio
async def test_t1_f5_persistent_network_failure_raises_error(network, hub):
    async def mocked_send(endpoint, payload, headers):
        raise asyncio.TimeoutError("Persistent network drop")
    network.send_to_hub = mocked_send
    
    payload = {"role": "grok", "task_data": "data"}
    with pytest.raises(asyncio.TimeoutError):
        await call_api_with_retry(network, "/dispatch", payload, hub.api_key, max_retries=2, base_delay=0.001)


# --- FEATURE 6: Workspace & Configuration Management ---

@pytest.mark.asyncio
async def test_t1_f6_dynamic_config_update_max_workers(network, hub):
    payload = {"config": {"max_workers": 1}}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/update_config", payload, headers)
    assert status_code == 200
    assert hub.config["max_workers"] == 1
    
    w1 = ClientWorker("w1", ["grok"])
    w2 = ClientWorker("w2", ["claude"])
    w1.set_network(network)
    w2.set_network(network)
    
    s1, _ = await w1.register()
    s2, _ = await w2.register()
    assert s1 == 200
    assert s2 == 503  # Rejected since max workers = 1

@pytest.mark.asyncio
async def test_t1_f6_dynamic_config_update_heartbeat_timeout(network, hub):
    payload = {"config": {"heartbeat_timeout": 1.5}}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/update_config", payload, headers)
    assert status_code == 200
    assert hub.config["heartbeat_timeout"] == 1.5

@pytest.mark.asyncio
async def test_t1_f6_worker_deregistration_removes_from_registry(network, hub):
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    assert "w1" in hub.workers
    
    payload = {"worker_id": "w1"}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/deregister", payload, headers)
    assert status_code == 200
    assert "w1" not in hub.workers

@pytest.mark.asyncio
async def test_t1_f6_workspace_state_sync_between_workers(network, hub):
    # Simulate custom endpoint for writing file to workspace and checking status
    payload = {"path": "design_spec.json", "content": "{\"layout\": \"clean\"}"}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/write_workspace_file", payload, headers)
    assert status_code == 200
    assert body["status"] == "file_written"

@pytest.mark.asyncio
async def test_t1_f6_dynamic_role_addition_updates_worker(network, hub):
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    assert hub.workers["w1"]["roles"] == ["grok"]
    
    # Update roles
    w1.roles = ["grok", "codex"]
    await w1.register()
    assert hub.workers["w1"]["roles"] == ["grok", "codex"]


# =============================================================================
# TIER 2: BOUNDARY & CORNER CASES (30 TESTS, >=5 PER FEATURE)
# =============================================================================

# --- FEATURE 1 Boundaries: Worker Registration and Heartbeats ---

@pytest.mark.asyncio
async def test_t2_f1_register_worker_with_empty_id(network, hub):
    payload = {"worker_id": "", "roles": ["grok"]}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/register", payload, headers)
    assert status_code == 400

@pytest.mark.asyncio
async def test_t2_f1_register_worker_with_empty_roles(network, hub):
    payload = {"worker_id": "w1", "roles": None}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/register", payload, headers)
    assert status_code == 400

@pytest.mark.asyncio
async def test_t2_f1_register_worker_exceeding_max_limit(network, hub):
    hub.config["max_workers"] = 0
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    status_code, body = await w1.register()
    assert status_code == 503

@pytest.mark.asyncio
async def test_t2_f1_heartbeat_for_non_existent_worker(network, hub):
    payload = {"worker_id": "non_existent"}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/heartbeat", payload, headers)
    assert status_code == 404

@pytest.mark.asyncio
async def test_t2_f1_duplicate_worker_registration_overwrites(network, hub):
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    w1_alt = ClientWorker("w1", ["claude"])
    w1_alt.set_network(network)
    status_code, body = await w1_alt.register()
    assert status_code == 200
    assert hub.workers["w1"]["roles"] == ["claude"]


# --- FEATURE 2 Boundaries: API Authentication and Checksums ---

@pytest.mark.asyncio
async def test_t2_f2_empty_api_key_header(network, hub):
    payload = {"role": "grok", "task_data": "data"}
    headers = hub.create_headers(payload)
    headers["X-API-Key"] = ""
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 401

@pytest.mark.asyncio
async def test_t2_f2_empty_checksum_header(network, hub):
    payload = {"role": "grok", "task_data": "data"}
    headers = hub.create_headers(payload)
    headers["X-Payload-SHA256"] = ""
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 400

@pytest.mark.asyncio
async def test_t2_f2_extremely_large_payload_checksum(network, hub):
    large_data = "a" * (1024 * 1024) # 1MB string
    payload = {"role": "grok", "task_data": large_data}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 202

@pytest.mark.asyncio
async def test_t2_f2_corrupted_payload_with_valid_checksum_format(network, hub):
    payload = {"role": "grok", "task_data": "data"}
    headers = hub.create_headers(payload)
    # Modify payload content but keep headers intact
    payload["task_data"] = "corrupted"
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 400
    assert body["error"] == "Bad Checksum"

@pytest.mark.asyncio
async def test_t2_f2_unicode_characters_in_payload_checksum(network, hub):
    unicode_data = "Chào thế giới! 🌍🤖"
    payload = {"role": "grok", "task_data": unicode_data}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 202


# --- FEATURE 3 Boundaries: Async Task Processing & Execution ---

@pytest.mark.asyncio
async def test_t2_f3_dispatch_task_with_empty_data(network, hub):
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    payload = {"role": "grok", "task_data": ""}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 202
    await wait_for_task(hub, body["task_id"])
    assert w1.tasks_completed == 1

@pytest.mark.asyncio
async def test_t2_f3_dispatch_task_with_extremely_large_data(network, hub):
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    payload = {"role": "grok", "task_data": "x" * 500000}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 202
    await wait_for_task(hub, body["task_id"])
    assert w1.tasks_completed == 1

@pytest.mark.asyncio
async def test_t2_f3_task_timeout_handling(network, hub):
    # If client worker fails to respond, hub marks it dead or tasks remain outstanding
    hub.config["task_timeout"] = 0.01
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    # Mock task worker slow run
    async def slow_execute(task_id, task_data):
        await asyncio.sleep(0.1) # longer than timeout
    w1.execute_task = slow_execute
    
    payload = {"role": "grok", "task_data": "data"}
    headers = hub.create_headers(payload)
    _, body = await network.send_to_hub("/dispatch", payload, headers)
    await wait_for_task(hub, body["task_id"])
    # Check that hub correctly timed out and marked task failed
    await hub.sweep()
    assert hub.tasks[body["task_id"]]["status"] == "failed"

@pytest.mark.asyncio
async def test_t2_f3_worker_crashes_during_task_execution(network, hub):
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    payload = {"role": "grok", "task_data": "data"}
    headers = hub.create_headers(payload)
    await network.send_to_hub("/dispatch", payload, headers)
    await asyncio.sleep(0.005)
    assert w1.status == "busy"
    
    # Deregister/crash worker
    dereg_payload = {"worker_id": "w1"}
    dereg_headers = hub.create_headers(dereg_payload)
    await network.send_to_hub("/deregister", dereg_payload, dereg_headers)
    
    # Verify that network registry no longer has worker w1
    assert "w1" not in network.workers

@pytest.mark.asyncio
async def test_t2_f3_task_completed_with_empty_result_string(network, hub):
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    async def empty_result_execute(task_id, task_data):
        payload = {
            "task_id": task_id,
            "worker_id": w1.worker_id,
            "status": "completed",
            "result": {"output": ""}
        }
        headers = w1.create_headers(payload)
        await network.send_to_hub("/report_result", payload, headers)
    
    w1.execute_task = empty_result_execute
    payload = {"role": "grok", "task_data": "data"}
    headers = hub.create_headers(payload)
    _, body = await network.send_to_hub("/dispatch", payload, headers)
    await wait_for_task(hub, body["task_id"])
    assert hub.tasks[body["task_id"]]["status"] == "completed"
    assert hub.tasks[body["task_id"]]["result"]["output"] == ""


# --- FEATURE 4 Boundaries: Routing & Dispatch (Orchestrator Polling & Routing) ---

@pytest.mark.asyncio
async def test_t2_f4_dispatch_task_with_no_role(network, hub):
    payload = {"role": "", "task_data": "data"}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 400

@pytest.mark.asyncio
async def test_t2_f4_dispatch_task_with_invalid_role_never_matched(network, hub):
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    payload = {"role": "impossible_role", "task_data": "data"}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 202
    assert body["status"] == "pending"
    await asyncio.sleep(0.02)
    assert w1.status == "idle"

@pytest.mark.asyncio
async def test_t2_f4_multiple_identical_tasks_routing(network, hub):
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    headers = hub.create_headers({"role": "grok", "task_data": "data"})
    for i in range(5):
        payload = {"role": "grok", "task_data": f"data_{i}"}
        headers = hub.create_headers(payload)
        await network.send_to_hub("/dispatch", payload, headers)
    
    for _ in range(40):
        if w1.tasks_completed == 5:
            break
        await asyncio.sleep(0.05)
    assert w1.tasks_completed == 5

@pytest.mark.asyncio
async def test_t2_f4_poll_task_status_with_empty_task_id(network, hub):
    payload = {"task_id": ""}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/task_status", payload, headers)
    assert status_code == 400

@pytest.mark.asyncio
async def test_t2_f4_worker_heartbeat_timeout_forces_task_requeue(network, hub):
    hub.config["heartbeat_timeout"] = 0.05
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    # Prevent worker from replying to result report (mock network block or similar)
    async def silent_execute(task_id, task_data):
        await asyncio.sleep(0.2) # sleep longer than heartbeat timeout
    w1.execute_task = silent_execute
    
    payload = {"role": "grok", "task_data": "data"}
    headers = hub.create_headers(payload)
    await network.send_to_hub("/dispatch", payload, headers)
    # Wait for task to transition to running (up to 0.5s) to avoid fragile timing
    for _ in range(100):
        if hub.tasks.get("task_1", {}).get("status") == "running":
            break
        await asyncio.sleep(0.005)
    
    assert hub.tasks["task_1"]["status"] == "running"
    
    # Hub checks liveness after heartbeat expires
    await asyncio.sleep(0.06)
    await hub.sweep()
    
    # Worker W1 should be dead, task_1 should be failed with disconnect error
    assert "w1" not in hub.workers
    assert hub.tasks["task_1"]["status"] == "failed"
    assert "disconnect" in hub.tasks["task_1"]["result"]["error"].lower()


# --- FEATURE 5 Boundaries: Resilient HTTP & Connection Retries ---

@pytest.mark.asyncio
async def test_t2_f5_retry_limit_reached_on_persistent_429(network, hub):
    def persistent_429(dest, endpoint, payload):
        return 429, {"error": "Rate limit"}
    network.error_generators.append(persistent_429)
    
    payload = {"role": "grok", "task_data": "data"}
    status_code, body = await call_api_with_retry(network, "/dispatch", payload, hub.api_key, max_retries=2, base_delay=0.001)
    assert status_code == 429

@pytest.mark.asyncio
async def test_t2_f5_retry_limit_reached_on_persistent_503(network, hub):
    def persistent_503(dest, endpoint, payload):
        return 503, {"error": "Unavailable"}
    network.error_generators.append(persistent_503)
    
    payload = {"role": "grok", "task_data": "data"}
    status_code, body = await call_api_with_retry(network, "/dispatch", payload, hub.api_key, max_retries=2, base_delay=0.001)
    assert status_code == 503

@pytest.mark.asyncio
async def test_t2_f5_zero_timeout_network_drop(network, hub):
    network.drop_rate = 1.0 # drop all requests
    payload = {"role": "grok", "task_data": "data"}
    with pytest.raises(asyncio.TimeoutError):
        await call_api_with_retry(network, "/dispatch", payload, hub.api_key, max_retries=0, base_delay=0.001)

@pytest.mark.asyncio
async def test_t2_f5_negative_backoff_config_fallback(network, hub):
    attempts = 0
    def gen_429(dest, endpoint, payload):
        nonlocal attempts
        if endpoint == "/dispatch" and attempts < 1:
            attempts += 1
            return 429, {"error": "Rate limit"}
        return None
    network.error_generators.append(gen_429)
    
    # Passing negative delay should not break implementation (it will use fallback 0 or abs value)
    payload = {"role": "grok", "task_data": "data"}
    status_code, body = await call_api_with_retry(network, "/dispatch", payload, hub.api_key, max_retries=1, base_delay=-0.01)
    assert status_code == 202

@pytest.mark.asyncio
async def test_t2_f5_mixed_errors_sequence_recovery(network, hub):
    errors = [429, 503, "timeout"]
    attempts = 0
    original_send = network.send_to_hub
    
    async def mocked_send(endpoint, payload, headers):
        nonlocal attempts
        if endpoint == "/dispatch" and attempts < len(errors):
            err = errors[attempts]
            attempts += 1
            if err == "timeout":
                raise asyncio.TimeoutError()
            return err, {"error": "mocked"}
        return await original_send(endpoint, payload, headers)
    
    network.send_to_hub = mocked_send
    payload = {"role": "grok", "task_data": "data"}
    status_code, body = await call_api_with_retry(network, "/dispatch", payload, hub.api_key, max_retries=4, base_delay=0.001)
    assert status_code == 202
    assert attempts == 3


# --- FEATURE 6 Boundaries: Configuration & Workspace Management ---

@pytest.mark.asyncio
async def test_t2_f6_config_update_invalid_data_types(network, hub):
    payload = {"config": {"max_workers": "invalid_type_string"}}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/update_config", payload, headers)
    assert status_code == 400

@pytest.mark.asyncio
async def test_t2_f6_workspace_path_traversal_attempt(network, hub):
    payload = {"path": "../../etc/passwd", "content": "malicious"}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/write_workspace_file", payload, headers)
    assert status_code == 400
    assert "error" in body

@pytest.mark.asyncio
async def test_t2_f6_deregister_already_deregistered_worker(network, hub):
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    payload = {"worker_id": "w1"}
    headers = hub.create_headers(payload)
    await network.send_to_hub("/deregister", payload, headers)
    
    # Deregister second time
    status_code, body = await network.send_to_hub("/deregister", payload, headers)
    assert status_code == 404

@pytest.mark.asyncio
async def test_t2_f6_zero_max_workers_configuration(network, hub):
    payload = {"config": {"max_workers": 0}}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/update_config", payload, headers)
    assert status_code == 200
    
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    status_code, body = await w1.register()
    assert status_code == 503

@pytest.mark.asyncio
async def test_t2_f6_negative_heartbeat_timeout_configuration(network, hub):
    payload = {"config": {"heartbeat_timeout": -10.0}}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/update_config", payload, headers)
    assert status_code == 400


# =============================================================================
# TIER 3: CROSS-FEATURE COMBINATIONS (6 TESTS)
# =============================================================================

@pytest.mark.asyncio
async def test_t3_comb_1_auth_failure_during_active_task_execution(network, hub):
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    # Dispatch task
    payload = {"role": "grok", "task_data": "data"}
    headers = hub.create_headers(payload)
    await network.send_to_hub("/dispatch", payload, headers)
    await asyncio.sleep(0.005)
    
    # Change Hub api key during task execution
    hub.api_key = "new-api-key"
    
    # Wait for execution and report result
    await asyncio.sleep(0.02)
    # Result report should be rejected since worker is still using old key
    # Assert worker completed local work but hub has not marked task completed
    assert hub.tasks["task_1"]["status"] != "completed"

@pytest.mark.asyncio
async def test_t3_comb_2_network_drop_during_task_result_reporting(network, hub):
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    attempts = 0
    original_send = network.send_to_hub
    
    async def mock_send_with_drop(endpoint, payload, headers):
        nonlocal attempts
        if endpoint == "/report_result" and attempts < 1:
            attempts += 1
            raise asyncio.TimeoutError("Timeout reporting result")
        return await original_send(endpoint, payload, headers)
        
    network.send_to_hub = mock_send_with_drop
    
    # Dispatch task
    payload = {"role": "grok", "task_data": "data"}
    headers = hub.create_headers(payload)
    _, body = await network.send_to_hub("/dispatch", payload, headers)
    task_id = body["task_id"]
    
    # Let the worker execute, fail the first attempt, and succeed on retry
    await wait_for_task(hub, task_id)
    assert hub.tasks[task_id]["status"] == "completed"

@pytest.mark.asyncio
async def test_t3_comb_3_config_change_affects_active_worker_routing(network, hub):
    # Dispatch task when no worker exists
    payload = {"role": "grok", "task_data": "data"}
    headers = hub.create_headers(payload)
    await network.send_to_hub("/dispatch", payload, headers)
    assert hub.task_queue.qsize() == 1
    
    # Change config max workers to 1
    config_payload = {"config": {"max_workers": 1}}
    config_headers = hub.create_headers(config_payload)
    await network.send_to_hub("/update_config", config_payload, config_headers)
    
    # Register W1 (grok) - should route queued task immediately
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    await wait_for_task(hub, "task_1")
    assert w1.tasks_completed == 1
    assert hub.task_queue.qsize() == 0

@pytest.mark.asyncio
async def test_t3_comb_4_heartbeat_failure_during_network_instability(network, hub):
    hub.config["heartbeat_timeout"] = 0.05
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    # Start task
    payload = {"role": "grok", "task_data": "data"}
    headers = hub.create_headers(payload)
    await network.send_to_hub("/dispatch", payload, headers)
    await asyncio.sleep(0.005)
    
    # Inject network drops for heartbeats
    network.drop_rate = 1.0
    await asyncio.sleep(0.2)
    
    # Hub checks liveness, should fail task_1 since heartbeat missed
    await hub.sweep()
    assert "w1" not in hub.workers
    assert hub.tasks["task_1"]["status"] == "failed"
    assert "disconnect" in hub.tasks["task_1"]["result"]["error"].lower()

@pytest.mark.asyncio
async def test_t3_comb_5_auth_and_config_change_during_heavy_polling(network, hub):
    # Test concurrency/sequence integrity of configuration, polling, and authentication
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    payload = {"role": "grok", "task_data": "data"}
    headers = hub.create_headers(payload)
    await network.send_to_hub("/dispatch", payload, headers)
    
    # Chain of updates
    update_payload = {"config": {"task_timeout": 5.0}}
    update_headers = hub.create_headers(update_payload)
    s1, _ = await network.send_to_hub("/update_config", update_payload, update_headers)
    
    poll_payload = {"task_id": "task_1"}
    poll_headers = hub.create_headers(poll_payload)
    s2, b2 = await network.send_to_hub("/task_status", poll_payload, poll_headers)
    
    assert s1 == 200 and s2 == 200
    assert b2["status"] in ("running", "completed")

@pytest.mark.asyncio
async def test_t3_comb_6_worker_disconnect_requeue_and_reroute(network, hub):
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    # Send a task that will run
    payload = {"role": "grok", "task_data": "data"}
    headers = hub.create_headers(payload)
    await network.send_to_hub("/dispatch", payload, headers)
    await asyncio.sleep(0.005)
    
    # W1 crashes/deregisters
    dereg_payload = {"worker_id": "w1"}
    dereg_headers = hub.create_headers(dereg_payload)
    await network.send_to_hub("/deregister", dereg_payload, dereg_headers)
    
    # Re-queue task manually since worker disconnected during execution
    hub.tasks["task_1"]["status"] = "pending"
    hub.tasks["task_1"]["worker_id"] = None
    hub.task_queue.put_nowait("task_1")
    
    # Register W2 (grok)
    w2 = ClientWorker("w2", ["grok"])
    w2.set_network(network)
    await w2.register()
    
    # Trigger queue process
    await hub._process_queue()
    await wait_for_task(hub, "task_1")
    
    assert w2.tasks_completed == 1
    assert hub.tasks["task_1"]["status"] == "completed"
    assert hub.tasks["task_1"]["worker_id"] == "w2"


# =============================================================================
# TIER 4: REAL-WORLD WORKLOADS (5 TESTS)
# =============================================================================

@pytest.mark.asyncio
async def test_t4_workload_1_multi_stage_agent_pipeline(network, hub):
    # Sequential Pipeline: Grok (Research) -> Claude (Design) -> Codex (Review)
    w_grok = ClientWorker("w_grok", ["grok_researcher"])
    w_claude = ClientWorker("w_claude", ["claude_architect"])
    w_codex = ClientWorker("w_codex", ["codex_reviewer"])
    
    w_grok.set_network(network)
    w_claude.set_network(network)
    w_codex.set_network(network)
    
    await w_grok.register()
    await w_claude.register()
    await w_codex.register()
    
    # Stage 1: Research
    headers = hub.create_headers({"role": "grok_researcher", "task_data": "query_prompt"})
    _, dispatch_res = await network.send_to_hub("/dispatch", {"role": "grok_researcher", "task_data": "query_prompt"}, headers)
    t1_id = dispatch_res["task_id"]
    await wait_for_task(hub, t1_id)
    assert hub.tasks[t1_id]["status"] == "completed"
    research_output = hub.tasks[t1_id]["result"]["output"]
    
    # Stage 2: Design
    design_input = f"Design based on {research_output}"
    headers = hub.create_headers({"role": "claude_architect", "task_data": design_input})
    _, dispatch_res2 = await network.send_to_hub("/dispatch", {"role": "claude_architect", "task_data": design_input}, headers)
    t2_id = dispatch_res2["task_id"]
    await wait_for_task(hub, t2_id)
    assert hub.tasks[t2_id]["status"] == "completed"
    design_output = hub.tasks[t2_id]["result"]["output"]
    
    # Stage 3: Review
    review_input = f"Review design: {design_output}"
    headers = hub.create_headers({"role": "codex_reviewer", "task_data": review_input})
    _, dispatch_res3 = await network.send_to_hub("/dispatch", {"role": "codex_reviewer", "task_data": review_input}, headers)
    t3_id = dispatch_res3["task_id"]
    await wait_for_task(hub, t3_id)
    assert hub.tasks[t3_id]["status"] == "completed"
    
    assert "Processed: Review design: Processed: Design based on Processed: query_prompt" in hub.tasks[t3_id]["result"]["output"]

@pytest.mark.asyncio
async def test_t4_workload_2_high_concurrency_stress(network, hub):
    # Register 3 workers with different overlapping roles
    w1 = ClientWorker("w1", ["grok", "claude"])
    w2 = ClientWorker("w2", ["claude", "codex"])
    w3 = ClientWorker("w3", ["grok", "codex"])
    w1.set_network(network)
    w2.set_network(network)
    w3.set_network(network)
    await w1.register()
    await w2.register()
    await w3.register()
    
    # Dispatch 20 concurrent tasks
    tasks = []
    roles = ["grok", "claude", "codex"]
    for i in range(20):
        role = roles[i % 3]
        payload = {"role": role, "task_data": f"data_{i}"}
        headers = hub.create_headers(payload)
        tasks.append(network.send_to_hub("/dispatch", payload, headers))
        
    responses = await asyncio.gather(*tasks)
    assert all(status == 202 for status, _ in responses)
    
    # Wait for queue to process
    async def wait_for_all_tasks(hub, timeout=1.0):
        start = time.time()
        while time.time() - start < timeout:
            if all(t["status"] in ("completed", "failed") for t in hub.tasks.values()):
                break
            await asyncio.sleep(0.001)
    await wait_for_all_tasks(hub)
    assert all(t["status"] == "completed" for t in hub.tasks.values())
    total_completed = w1.tasks_completed + w2.tasks_completed + w3.tasks_completed
    assert total_completed == 20

@pytest.mark.asyncio
async def test_t4_workload_3_unstable_network_resilience(network, hub):
    # Simulate jitter, drops, and latency on network
    network.latency = 0.002
    network.drop_rate = 0.15 # 15% drop rate
    
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    
    # Retry registration to handle network drops
    for attempt in range(10):
        try:
            await w1.register()
            break
        except Exception:
            if attempt == 9:
                raise
            await asyncio.sleep(0.005)
    
    # Start heartbeat loop
    await w1.start_heartbeats()
    
    # Dispatch 5 tasks using retry client
    success_count = 0
    for i in range(5):
        payload = {"role": "grok", "task_data": f"unstable_{i}"}
        try:
            status_code, body = await call_api_with_retry(network, "/dispatch", payload, hub.api_key, max_retries=5, base_delay=0.001)
            if status_code == 202:
                success_count += 1
        except Exception:
            pass
            
    await asyncio.sleep(0.15)
    await w1.stop_heartbeats()
    
    # Verify that at least some tasks were dispatched and processed successfully despite drops
    assert success_count > 0
    assert w1.tasks_completed > 0

@pytest.mark.asyncio
async def test_t4_workload_4_dynamic_scaling_under_load(network, hub):
    # Dispatch 10 tasks when 0 workers are active
    for i in range(10):
        payload = {"role": "grok", "task_data": f"load_{i}"}
        headers = hub.create_headers(payload)
        await network.send_to_hub("/dispatch", payload, headers)
        
    assert hub.task_queue.qsize() == 10
    
    # Dynamically scale: spin up 3 workers
    workers = [ClientWorker(f"w_{i}", ["grok"]) for i in range(3)]
    for w in workers:
        w.set_network(network)
        await w.register()
        
    # Trigger processing
    await hub._process_queue()
    async def wait_for_all_tasks(hub, timeout=1.0):
        start = time.time()
        while time.time() - start < timeout:
            if all(t["status"] in ("completed", "failed") for t in hub.tasks.values()):
                break
            await asyncio.sleep(0.001)
    await wait_for_all_tasks(hub)
    
    assert hub.task_queue.qsize() == 0
    assert all(t["status"] == "completed" for t in hub.tasks.values())

@pytest.mark.asyncio
async def test_t4_workload_5_chaos_monkey_crash_recovery(network, hub):
    # Sequence of failures:
    # 1. Register W1
    # 2. Dispatch task 1
    # 3. Crash W1 mid-execution
    # 4. Change API Key on Hub
    # 5. Spin up W2 with updated API Key
    # 6. Re-route task 1 to W2 and verify success
    
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()
    
    # Slow execute to catch it mid-flight
    async def slow_execute(task_id, task_data):
        await asyncio.sleep(0.1)
    w1.execute_task = slow_execute
    
    payload = {"role": "grok", "task_data": "critical_task"}
    headers = hub.create_headers(payload)
    await network.send_to_hub("/dispatch", payload, headers)
    # Wait for W1 to receive the task and become busy (up to 0.5s) to avoid race conditions
    for _ in range(100):
        if w1.status == "busy":
            break
        await asyncio.sleep(0.005)
    assert w1.status == "busy"
    assert hub.tasks["task_1"]["status"] == "running"
    
    # Crash W1 (deregister and remove)
    await network.send_to_hub("/deregister", {"worker_id": "w1"}, hub.create_headers({"worker_id": "w1"}))
    
    # Change hub key
    hub.api_key = "new-secure-key"
    
    # Requeue task
    hub.tasks["task_1"]["status"] = "pending"
    hub.tasks["task_1"]["worker_id"] = None
    hub.task_queue.put_nowait("task_1")
    
    # Spin up W2 with updated API Key
    w2 = ClientWorker("w2", ["grok"], api_key="new-secure-key")
    w2.set_network(network)
    await w2.register()
    
    # Trigger queue processing
    await hub._process_queue()
    await wait_for_task(hub, "task_1")
    
    assert hub.tasks["task_1"]["status"] == "completed"
    assert hub.tasks["task_1"]["worker_id"] == "w2"
