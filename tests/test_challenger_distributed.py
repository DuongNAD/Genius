import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
import time
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from ag_core.utils.jwt import encode_jwt
from ag_core.distributed import CentralHub, ClientWorker
from test_distributed import MockNetworkProtocol
from serve import app, worker_registry

JWT_SECRET = os.getenv("SKILL_API_KEY", "mock-skill-key")


@pytest.fixture
def network():
    return MockNetworkProtocol()


@pytest.fixture
def hub(network):
    h = CentralHub(api_key="valid-api-key")
    h.set_network(network)
    yield h
    h.stop_sweeper()


# =<ctrl42>= CHALLENGE 1: Graceful Deregistration Task Stall =cat=
@pytest.mark.asyncio
async def test_graceful_deregistration_task_stall(network, hub):
    """
    Verify that when a worker deregisters gracefully during task execution,
    its tasks are requeued and immediately routed to another available worker.
    """
    # Register w1 and w2
    w1 = ClientWorker("w1", ["grok"])
    w2 = ClientWorker("w2", ["grok"])
    w1.set_network(network)
    w2.set_network(network)
    await w1.register()
    await w2.register()

    # Dispatch a task. It should go to one of them, e.g., w1 or w2.
    payload = {"role": "grok", "task_data": "slow_task"}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 202
    task_id = body["task_id"]

    # Let the background dispatch task finish its work first
    await asyncio.sleep(0.01)

    assigned_worker_id = hub.tasks[task_id]["worker_id"]
    idle_worker_id = "w2" if assigned_worker_id == "w1" else "w1"
    idle_worker = w2 if assigned_worker_id == "w1" else w1

    assert assigned_worker_id is not None
    assert hub.tasks[task_id]["status"] == "running"
    assert hub.workers[idle_worker_id]["status"] == "idle"

    # Now deregister the assigned worker gracefully
    dereg_payload = {"worker_id": assigned_worker_id}
    dereg_headers = hub.create_headers(dereg_payload)
    status_code, _ = await network.send_to_hub(
        "/deregister", dereg_payload, dereg_headers
    )
    assert status_code == 200

    # Wait to process deregistration
    await asyncio.sleep(0.05)

    # Task is failed due to worker disconnection, and not completed by the idle worker
    assert idle_worker.tasks_completed == 0
    assert hub.tasks[task_id]["status"] == "failed"
    assert hub.tasks[task_id]["result"] == {"error": "Worker disconnected"}


@pytest.mark.asyncio
async def test_deregister_race_keyerror_crash(network, hub):
    """
    Verify the race condition where deregistering a worker immediately after
    dispatching a task does not cause a KeyError in the hub background task.
    """
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()

    payload = {"role": "grok", "task_data": "some_task"}
    headers = hub.create_headers(payload)
    status_code, body = await network.send_to_hub("/dispatch", payload, headers)
    assert status_code == 202
    task_id = body["task_id"]

    # Deregister immediately without sleeping, causing a race condition
    dereg_payload = {"worker_id": "w1"}
    dereg_headers = hub.create_headers(dereg_payload)
    status_code, _ = await network.send_to_hub(
        "/deregister", dereg_payload, dereg_headers
    )
    assert status_code == 200

    # Wait for the background task to execute and fail cleanly
    await asyncio.sleep(0.02)

    # Task is marked failed due to the communication error, but background task does not crash
    assert hub.tasks[task_id]["status"] == "failed"
    assert "error" in hub.tasks[task_id]["result"]
    assert (
        "unreachable" in str(hub.tasks[task_id]["result"]["error"]).lower()
        or "dispatch" in str(hub.tasks[task_id]["result"]["error"]).lower()
    )


# =<ctrl42>= CHALLENGE 2: JWT Spoofing / Identity Bypass =cat=
def test_jwt_identity_spoofing_bypass():
    """
    Verify that a client connecting to /ws/connect with a valid JWT for worker-A
    is rejected and disconnected if they send a register request for worker-B.
    """
    client = TestClient(app)
    token = encode_jwt(
        {"sub": "worker-A", "exp": time.time() + 60},
        os.getenv("SKILL_API_KEY", "mock-skill-key"),
    )

    # Clear registry
    worker_registry.workers.clear()

    with client.websocket_connect(f"/ws/connect?token={token}") as websocket:
        # Register as worker-B, bypassing the JWT sub claim of worker-A
        reg_payload = {"type": "register", "worker_id": "worker-B", "roles": ["grok"]}
        try:
            websocket.send_json(reg_payload)
            resp = websocket.receive_json()
            assert resp.get("type") == "error"
        except (WebSocketDisconnect, Exception):
            pass

        # Verify the worker registered on the hub is NOT worker-B
        assert "worker-B" not in worker_registry.workers


# =<ctrl42>= CHALLENGE 3: Stale Worker Orphan State (Silent Failure) =cat=
@pytest.mark.asyncio
async def test_stale_worker_orphan_state(network, hub):
    """
    Verify that if a worker is pruned as stale due to heartbeat timeout,
    its heartbeat messages trigger re-registration and recover the worker state.
    """
    hub.config["heartbeat_timeout"] = 0.05
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()

    # Artificially age heartbeat
    hub.workers["w1"]["last_heartbeat"] = time.time() - 0.1
    await hub.sweep()

    # Worker w1 is pruned
    assert "w1" not in hub.workers

    # Now worker sends a heartbeat
    status_code, body = await w1.send_heartbeat()
    # Hub rejects it because worker is not found
    assert status_code == 404
    assert body["error"] == "Worker not found"

    # Start heartbeat loop and verify worker gets re-registered
    w1.heartbeat_interval = 0.01
    await w1.start_heartbeats()
    await asyncio.sleep(0.04)
    await w1.stop_heartbeats()

    # Worker is re-registered automatically
    assert "w1" in hub.workers


# =<ctrl42>= CHALLENGE 4: Busy Worker Re-registration Race =cat=
@pytest.mark.asyncio
async def test_busy_worker_reregistration_race(network, hub):
    """
    Verify that re-registering a busy worker preserves its status as busy.
    """
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()

    try:
        # Dispatch first task
        payload = {"role": "grok", "task_data": "task_1 sleep:0.5"}
        headers = hub.create_headers(payload)
        await network.send_to_hub("/dispatch", payload, headers)
        await asyncio.sleep(0.005)

        assert hub.workers["w1"]["status"] == "busy"
        assert hub.tasks["task_1"]["status"] == "running"

        # Re-register w1 while it is busy
        await w1.register()

        # Worker status must still be "busy", and task_1 is still "running"
        assert hub.workers["w1"]["status"] == "busy"
        assert hub.tasks["task_1"]["status"] == "running"

        # Dispatch second task - it should be queued as pending because w1 is busy
        payload2 = {"role": "grok", "task_data": "task_2"}
        headers2 = hub.create_headers(payload2)
        await network.send_to_hub("/dispatch", payload2, headers2)
        await asyncio.sleep(0.005)

        assert hub.tasks["task_2"]["status"] == "pending"
    finally:
        # task_1 is a `sleep:0.5` execution still running on w1 when the asserts
        # finish. Drain it so it doesn't outlive the test as a pending task that
        # the event loop destroys on close (PytestUnraisableExceptionWarning).
        await w1.aclose()


# =<ctrl42>= CHALLENGE 5: ClientWorker Lacks Retry for Result Reporting =cat=
@pytest.mark.asyncio
async def test_client_worker_no_retry_result_reporting(network, hub):
    """
    Verify that ClientWorker retries result reporting if a network drop occurs.
    """
    w1 = ClientWorker("w1", ["grok"])
    w1.set_network(network)
    await w1.register()

    # Inject transient drop only for the first report_result attempt
    original_send = network.send_to_hub
    drop_triggered = False
    drop_count = 0

    async def mock_send_with_drop(endpoint, payload, headers):
        nonlocal drop_triggered, drop_count
        if endpoint == "/report_result" and drop_count < 1:
            drop_count += 1
            drop_triggered = True
            raise asyncio.TimeoutError("Simulated drop")
        return await original_send(endpoint, payload, headers)

    network.send_to_hub = mock_send_with_drop

    # Dispatch task
    payload = {"role": "grok", "task_data": "data"}
    headers = hub.create_headers(payload)
    await network.send_to_hub("/dispatch", payload, headers)

    # Let the worker execute the task and retry
    await asyncio.sleep(0.1)

    assert drop_triggered
    # Worker is back to idle
    assert w1.status == "idle"
    # Hub marked the task as completed after retry
    assert hub.tasks["task_1"]["status"] == "completed"


def test_websocket_heartbeat_identity_spoofing_rejected():
    """
    Verify that if a worker connects with a valid token for worker-A,
    registers as worker-A, and subsequently sends a heartbeat with worker_id set to worker-B,
    the connection is rejected and closed.
    """
    client = TestClient(app)
    token = encode_jwt(
        {"sub": "worker-A", "exp": time.time() + 60},
        os.getenv("SKILL_API_KEY", "mock-skill-key"),
    )

    # Clear registry
    worker_registry.workers.clear()

    with client.websocket_connect(f"/ws/connect?token={token}") as websocket:
        # First, register correctly as worker-A
        websocket.send_json(
            {"type": "register", "worker_id": "worker-A", "roles": ["grok"]}
        )
        resp = websocket.receive_json()
        assert resp.get("type") == "registered"

        # Now, attempt heartbeat spoofing under worker-B
        websocket.send_json({"type": "heartbeat", "worker_id": "worker-B"})

        # We expect to receive error and connection closed with code 4003
        try:
            resp = websocket.receive_json()
            if resp.get("type") == "error":
                assert resp.get("error") == "Identity spoofing detected"
            # Try to receive again to trigger disconnect
            websocket.receive_json()
            assert False, "Connection was not closed"
        except WebSocketDisconnect as e:
            assert e.code == 4003


def test_websocket_result_identity_spoofing_rejected():
    """
    Verify that if a worker connects with a valid token for worker-A,
    registers as worker-A, and subsequently sends a result report with worker_id set to worker-B,
    the connection is rejected and closed.
    """
    client = TestClient(app)
    token = encode_jwt(
        {"sub": "worker-A", "exp": time.time() + 60},
        os.getenv("SKILL_API_KEY", "mock-skill-key"),
    )

    # Clear registry
    worker_registry.workers.clear()

    with client.websocket_connect(f"/ws/connect?token={token}") as websocket:
        # First, register correctly as worker-A
        websocket.send_json(
            {"type": "register", "worker_id": "worker-A", "roles": ["grok"]}
        )
        resp = websocket.receive_json()
        assert resp.get("type") == "registered"

        # Now, attempt result reporting spoofing under worker-B
        websocket.send_json(
            {
                "type": "result",
                "task_id": "task_1",
                "worker_id": "worker-B",
                "status": "completed",
                "result": {"output": "ok"},
                "checksum": "dummy-checksum",
            }
        )

        # We expect to receive error and connection closed with code 4003
        try:
            resp = websocket.receive_json()
            if resp.get("type") == "error":
                assert resp.get("error") == "Identity spoofing detected"
            # Try to receive again to trigger disconnect
            websocket.receive_json()
            assert False, "Connection was not closed"
        except WebSocketDisconnect as e:
            assert e.code == 4003
