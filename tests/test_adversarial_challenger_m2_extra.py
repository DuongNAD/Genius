# tests/test_adversarial_challenger_m2_extra.py
import asyncio
import json
import time
import socket
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

JWT_SECRET = "mock-skill-key"
HOST = "127.0.0.1"

def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
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
async def test_duplicate_worker_connection_hijack_prevention(run_server):
    """
    Adversarial Scenario:
    1. Worker-A connects and registers via WebSocket connection 1.
    2. A second connection (e.g., reconnection or replacement) connects as Worker-A via WebSocket connection 2.
    3. WebSocket connection 1 disconnects (closes).
    4. Verify that Worker-A remains registered under connection 2, and connection 1's closure did NOT deregister connection 2.
    5. Verify that a task dispatched to Worker-A is correctly received on connection 2.
    """
    port = run_server
    worker_id = "duplicate-conn-worker"
    token = encode_jwt({"sub": worker_id, "exp": time.time() + 60}, JWT_SECRET)
    ws_url = f"ws://{HOST}:{port}/ws/connect?token={token}"

    # Connection 1 connects & registers
    ws1 = await websockets.connect(ws_url)
    reg_payload1 = {"type": "register", "worker_id": worker_id, "roles": ["grok"]}
    await ws1.send(json.dumps(reg_payload1))
    resp1 = await ws1.recv()
    assert json.loads(resp1)["type"] == "registered"

    w_info = await worker_registry.get_worker(worker_id)
    assert w_info is not None
    assert w_info["ws"] is not None

    # Connection 2 connects & registers
    ws2 = await websockets.connect(ws_url)
    reg_payload2 = {"type": "register", "worker_id": worker_id, "roles": ["grok"]}
    await ws2.send(json.dumps(reg_payload2))
    resp2 = await ws2.recv()
    assert json.loads(resp2)["type"] == "registered"

    w_info2 = await worker_registry.get_worker(worker_id)
    # The active WS in the registry must now be ws2 (connection 2)
    assert w_info2["ws"] is not None

    # Close Connection 1
    await ws1.close()
    await asyncio.sleep(0.1)

    # Verify Worker-A is STILL registered on the hub (was NOT deregistered by ws1's close)
    w_info_after = await worker_registry.get_worker(worker_id)
    assert w_info_after is not None, "Worker was incorrectly deregistered by connection 1 closure"

    # Verify we can dispatch a task and connection 2 receives it
    import orchestrator
    orchestrator.DISTRIBUTED_MODE = True

    task_dispatched = asyncio.Event()

    async def read_conn2():
        async for message in ws2:
            data = json.loads(message)
            if data.get("type") in ("run_task", "dispatch"):
                task_dispatched.set()
                # Report result
                res_payload = {
                    "type": "result",
                    "task_id": data["task_id"],
                    "worker_id": worker_id,
                    "status": "completed",
                    "result": {"output": "Success on Conn 2"},
                    "checksum": data["checksum"] # re-use/mock checksum or computed checksum
                }
                # Let's compute a valid checksum for result
                import hashlib
                res_body = {"output": "Success on Conn 2"}
                serialized = json.dumps(res_body, sort_keys=True).encode('utf-8')
                computed_chk = hashlib.sha256(serialized).hexdigest()
                res_payload["checksum"] = computed_chk
                await ws2.send(json.dumps(res_payload))
                break

    read_task = asyncio.create_task(read_conn2())

    try:
        res = await call_api(
            url="http://localhost:8001",
            api_key="mock-key",
            prompt="Test duplicate connection task",
            context={},
            poll_timeout=2.0
        )
        assert res == "Success on Conn 2"
        assert task_dispatched.is_set()
    finally:
        orchestrator.DISTRIBUTED_MODE = False
        await ws2.close()
        read_task.cancel()
        try:
            await read_task
        except asyncio.CancelledError:
            pass
