import asyncio
import json
import time
import pytest
import pytest_asyncio
import websockets
import uvicorn

import serve as serve_mod

# Import from our module
app = serve_mod.app
worker_registry = serve_mod.worker_registry
WorkerRegistry = serve_mod.WorkerRegistry
prune_stale_workers = serve_mod.prune_stale_workers

from ag_core.utils.jwt import encode_jwt

# Constants for testing
JWT_SECRET = "mock-skill-key"
HOST = "127.0.0.1"
PORT = 8012
WS_URL = f"ws://{HOST}:{PORT}/ws/connect"


class SlowCloseWebSocket:
    def __init__(self):
        self.closed = False

    async def close(self, code=1000):
        # Simulate latency during WebSocket close
        await asyncio.sleep(1.0)
        self.closed = True


@pytest_asyncio.fixture
async def run_server():
    global PORT, WS_URL
    import socket

    s = socket.socket()
    s.bind(("", 0))
    allocated_port = s.getsockname()[1]
    s.close()
    PORT = allocated_port
    WS_URL = f"ws://{HOST}:{PORT}/ws/connect"

    # Start the FastAPI app on a separate port in the background
    config = uvicorn.Config(
        app, host=HOST, port=PORT, log_level="warning", ws="auto"
    )
    server = uvicorn.Server(config)
    # Start server in a background task
    server_task = asyncio.create_task(server.serve())
    # Wait for server to start
    await asyncio.sleep(0.5)
    yield
    # Shutdown server
    server.should_exit = True
    await server_task


@pytest.mark.asyncio
async def test_high_concurrency_registration(run_server):
    # Clear registry first
    async with worker_registry.lock:
        worker_registry.workers.clear()
    serve_mod.central_hub.config["max_workers"] = 100

    num_workers = 30  # 30 concurrent connections is plenty for stress testing

    async def connect_and_register(i):
        worker_id = f"stress-worker-{i}"
        token = encode_jwt({"sub": worker_id, "exp": time.time() + 300}, JWT_SECRET)
        ws = await websockets.connect(f"{WS_URL}?token={token}")

        # Send registration
        reg_msg = {"type": "register", "worker_id": worker_id, "roles": ["grok"]}
        await ws.send(json.dumps(reg_msg))

        # Wait for registration success
        resp = await ws.recv()
        resp_data = json.loads(resp)
        assert resp_data["type"] == "registered"
        assert resp_data["status"] == "success"

        return ws, worker_id

    # Register concurrently
    tasks = [connect_and_register(i) for i in range(num_workers)]
    results = await asyncio.gather(*tasks)

    # Verify all are in the registry
    async with worker_registry.lock:
        assert len(worker_registry.workers) == num_workers
        for i in range(num_workers):
            worker_id = f"stress-worker-{i}"
            assert worker_id in worker_registry.workers
            assert worker_registry.workers[worker_id]["roles"] == ["grok"]

    # Disconnect all
    for ws, worker_id in results:
        await ws.close()

    # Wait briefly for server disconnect handlers to finish unregistering
    await asyncio.sleep(0.2)

    # Verify registry is empty
    async with worker_registry.lock:
        assert len(worker_registry.workers) == 0


@pytest.mark.asyncio
async def test_registry_lock_contention(run_server):
    # Clear registry
    async with worker_registry.lock:
        worker_registry.workers.clear()

    # 1. Register a worker that will become stale and has a slow close method
    slow_ws = SlowCloseWebSocket()
    async with worker_registry.lock:
        worker_registry.workers["stale-slow"] = {
            "ws": slow_ws,
            "roles": ["grok"],
            "status": "idle",
            "last_heartbeat": time.time() - 100.0,  # already stale
        }

    # 2. Start a background task for prune_stale_workers with timeout_sec=30
    prune_task = asyncio.create_task(
        prune_stale_workers(timeout_sec=30.0, check_interval=0.01)
    )

    # Wait a tiny bit to make sure prune_stale_workers runs and starts the close operation (which sleeps for 1.0s)
    await asyncio.sleep(0.05)

    # Now, while prune_stale_workers is sleeping inside ws.close(), try to register a new worker.
    # Since the lock is held, this should block and take roughly the remaining sleep time.
    start_time = time.time()

    # Try to register a new worker (this will attempt to acquire the lock)
    await worker_registry.register("new-worker", ["claude"], None)

    duration = time.time() - start_time

    # Cancel prune task
    prune_task.cancel()
    try:
        await prune_task
    except asyncio.CancelledError:
        pass

    # Verify that the registration was NOT blocked and took very little time (duration < 0.2s)
    print(f"Registration took {duration:.4f} seconds during prune block.")
    assert duration < 0.2, f"Lock was held during close! Took {duration}s"


@pytest.mark.asyncio
async def test_duplicate_worker_id_registrations(run_server):
    # Clear registry
    async with worker_registry.lock:
        worker_registry.workers.clear()

    worker_id = "duplicate-worker"

    # 1. Connect Client A
    token_a = encode_jwt({"sub": worker_id, "exp": time.time() + 300}, JWT_SECRET)
    ws_a = await websockets.connect(f"{WS_URL}?token={token_a}")

    await ws_a.send(
        json.dumps({"type": "register", "worker_id": worker_id, "roles": ["grok"]})
    )
    resp_a = json.loads(await ws_a.recv())
    assert resp_a["status"] == "success"

    # Check that Client A is registered
    async with worker_registry.lock:
        assert worker_id in worker_registry.workers
        assert worker_registry.workers[worker_id]["ws"] is not None

    # 2. Connect Client B (duplicate worker ID)
    token_b = encode_jwt({"sub": worker_id, "exp": time.time() + 300}, JWT_SECRET)
    ws_b = await websockets.connect(f"{WS_URL}?token={token_b}")

    await ws_b.send(
        json.dumps(
            {
                "type": "register",
                "worker_id": worker_id,
                "roles": ["claude"],  # different roles
            }
        )
    )
    resp_b = json.loads(await ws_b.recv())
    assert resp_b["status"] == "success"

    # Check that Client B has overwritten Client A in the registry
    async with worker_registry.lock:
        assert worker_id in worker_registry.workers
        assert worker_registry.workers[worker_id]["roles"] == ["claude"]

    # 3. Disconnect Client A (the original connection)
    await ws_a.close()

    # Wait briefly for Client A's disconnect handler to run
    await asyncio.sleep(0.2)

    # 4. Check if the duplicate worker is still registered
    # BUG HYPOTHESIS: Ws A's disconnection will unregister "duplicate-worker",
    # removing Ws B's registration even though Ws B is still connected!
    async with worker_registry.lock:
        is_corrupted = worker_id not in worker_registry.workers

    # Clean up Client B
    await ws_b.close()
    await asyncio.sleep(0.1)

    assert (
        not is_corrupted
    ), "Registry got corrupted! Client A's disconnect unregistered Client B's registration."


@pytest.mark.asyncio
async def test_heartbeat_pruning_timing(run_server):
    # Clear registry
    async with worker_registry.lock:
        worker_registry.workers.clear()

    worker_id = "prune-test"
    token = encode_jwt({"sub": worker_id, "exp": time.time() + 300}, JWT_SECRET)
    ws = await websockets.connect(f"{WS_URL}?token={token}")

    await ws.send(
        json.dumps({"type": "register", "worker_id": worker_id, "roles": ["grok"]})
    )
    resp = json.loads(await ws.recv())
    assert resp["status"] == "success"

    # Start the pruning task in the background with small values
    # timeout = 0.5s, check_interval = 0.1s
    prune_task = asyncio.create_task(
        prune_stale_workers(timeout_sec=0.5, check_interval=0.1)
    )

    # Initially, it is not pruned
    async with worker_registry.lock:
        assert worker_id in worker_registry.workers

    # Wait 0.3s (less than 0.5s timeout)
    await asyncio.sleep(0.3)
    async with worker_registry.lock:
        assert worker_id in worker_registry.workers, "Worker was pruned too early!"

    # Wait another 0.4s (total 0.7s, which is greater than timeout 0.5s)
    await asyncio.sleep(0.4)
    async with worker_registry.lock:
        is_pruned = worker_id not in worker_registry.workers

    # Cancel prune task and close connection
    prune_task.cancel()
    try:
        await prune_task
    except asyncio.CancelledError:
        pass
    await ws.close()

    assert is_pruned, "Worker was not pruned after timeout!"
