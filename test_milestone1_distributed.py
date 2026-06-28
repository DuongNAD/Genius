import asyncio
import os
import time
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from ag_core.utils.jwt import encode_jwt

# Import the hub components from serve
from serve import app, worker_registry, WorkerRegistry, prune_stale_workers

JWT_SECRET = os.getenv("SKILL_API_KEY", "mock-skill-key")

@pytest.fixture(autouse=True)
def clear_registry():
    # Make sure registry is clean before each test
    worker_registry.workers.clear()
    yield

@pytest.mark.asyncio
async def test_registry_correctness():
    """Test WorkerRegistry methods: register, unregister, heartbeat update."""
    registry = WorkerRegistry()
    worker_id = "test-worker-1"
    roles = ["grok", "claude"]
    mock_ws = object() # dummy object for unit testing registry
    
    # 1. Register
    await registry.register(worker_id, roles, mock_ws)
    worker = await registry.get_worker(worker_id)
    assert worker is not None
    assert worker["roles"] == roles
    assert worker["ws"] is mock_ws
    assert worker["status"] == "idle"
    initial_hb = worker["last_heartbeat"]
    assert initial_hb <= time.time()
    
    # 2. Heartbeat update
    # Wait until time.time() has actually increased to avoid clock resolution limitations (especially on Windows)
    while time.time() <= initial_hb:
        await asyncio.sleep(0.005)
    await registry.update_heartbeat(worker_id)
    worker = await registry.get_worker(worker_id)
    assert worker["last_heartbeat"] > initial_hb
    
    # 3. Unregister
    await registry.unregister(worker_id)
    worker = await registry.get_worker(worker_id)
    assert worker is None

def test_websocket_jwt_auth_unauthorized():
    """Verify that WebSocket connection with missing or invalid JWT is rejected with code 4001."""
    client = TestClient(app)
    
    # Invalid token test
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/connect?token=invalid-token") as websocket:
            websocket.receive_json()
    assert exc.value.code == 4001

    # Missing token test
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/connect") as websocket:
            websocket.receive_json()

def test_websocket_connection_and_registration():
    """Verify standard WebSocket handshake, JWT authentication, and registration."""
    client = TestClient(app)
    worker_id = "worker-jwt-test"
    
    # Generate valid JWT
    token = encode_jwt({"sub": worker_id, "exp": time.time() + 60}, JWT_SECRET)
    
    with client.websocket_connect(f"/ws/connect?token={token}") as websocket:
        # Register the worker
        reg_payload = {
            "type": "register",
            "worker_id": worker_id,
            "roles": ["grok"]
        }
        websocket.send_json(reg_payload)
        
        # Expect registered response
        resp = websocket.receive_json()
        assert resp["type"] == "registered"
        assert resp["status"] == "success"
        
        # Verify in global registry
        async def check_registry():
            w = await worker_registry.get_worker(worker_id)
            assert w is not None
            assert w["roles"] == ["grok"]
            
        asyncio.run(check_registry())

@pytest.mark.asyncio
async def test_heartbeat_pruning():
    """Verify that silent workers are pruned when heartbeats are stale."""
    worker_id = "stale-worker"
    mock_ws = object()
    
    await worker_registry.register(worker_id, ["grok"], mock_ws)
    
    # Artificially age the heartbeat
    async with worker_registry.lock:
        worker_registry.workers[worker_id]["last_heartbeat"] = time.time() - 40.0
        
    # Run the pruning function manually with a 30s threshold
    prune_task = asyncio.create_task(prune_stale_workers(timeout_sec=30.0, check_interval=0.01))
    # Wait until the worker is pruned or we timeout (up to 1.0 second) to avoid fragile timing
    for _ in range(100):
        worker = await worker_registry.get_worker(worker_id)
        if worker is None:
            break
        await asyncio.sleep(0.01)
    prune_task.cancel()
    try:
        await prune_task
    except asyncio.CancelledError:
        pass
        
    # Verify worker is pruned
    worker = await worker_registry.get_worker(worker_id)
    assert worker is None
