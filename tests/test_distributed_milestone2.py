import asyncio
import json
import socket
import pytest
import pytest_asyncio
import uvicorn
from unittest.mock import patch, AsyncMock

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
    # Start serve FastAPI app on a dynamically allocated free port in background
    port = get_free_port()
    config = uvicorn.Config(
        app, host=HOST, port=port, log_level="warning", ws="websockets-sansio"
    )
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
async def test_distributed_dispatch_and_execution_success(run_server):
    """Test that a task is successfully routed and dispatched to a worker in distributed mode."""
    port = run_server
    worker_id = "test-dist-worker"

    # Mock GrokResearcherAgent.run to avoid hitting real APIs
    with patch(
        "ag_core.agents.grok_researcher.GrokResearcherAgent.run", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = "Mocked dynamic grok research result"

        # Start worker
        worker = ClientWorker(worker_id=worker_id, roles=["grok"])
        worker_task = asyncio.create_task(worker.run_production_loop(HOST, port))

        # Wait for worker to connect and register
        await asyncio.sleep(0.5)

        # Verify worker is registered and idle
        w_info = await worker_registry.get_worker(worker_id)
        assert w_info is not None
        assert w_info["status"] == "idle"
        assert "grok" in w_info["roles"]

        # Invoke call_api in distributed mode
        import orchestrator

        orchestrator.DISTRIBUTED_MODE = True

        try:
            result = await call_api(
                url="http://localhost:8001",
                api_key="mock-key",
                prompt="Research the best framework for real-time task queue",
                context={"test": "context"},
                poll_timeout=5.0,
            )
            assert result == "Mocked dynamic grok research result"
        finally:
            orchestrator.DISTRIBUTED_MODE = False
            # Clean up worker task
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_distributed_dispatch_checksum_validation_failure():
    """Test that a worker rejects a dispatch message with an invalid or missing checksum."""
    worker_id = "test-bad-checksum-worker"

    # 1. Test invalid checksum
    worker = ClientWorker(worker_id=worker_id, roles=["grok"])
    sent_messages = []

    async def mock_send(msg):
        sent_messages.append(json.loads(msg))

    mock_ws = AsyncMock()
    mock_ws.send = mock_send

    dispatch_msg_invalid = {
        "type": "dispatch",
        "task_id": "task_invalid_checksum",
        "task_data": {"role": "grok", "prompt": "test invalid"},
        "checksum": "wrong_checksum",
    }

    mock_ws.__aiter__.return_value = [json.dumps(dispatch_msg_invalid)]

    from unittest.mock import MagicMock

    mock_connect = MagicMock()
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_ws
    mock_connect.return_value = mock_cm

    with patch("websockets.connect", mock_connect):
        worker_task = asyncio.create_task(worker.run_production_loop("127.0.0.1", 8013))

        await asyncio.sleep(0.5)

        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    assert len(sent_messages) >= 2
    assert sent_messages[0]["type"] == "register"

    result_msgs = [m for m in sent_messages if m.get("type") == "result"]
    assert len(result_msgs) == 1
    result_msg = result_msgs[0]
    assert result_msg["task_id"] == "task_invalid_checksum"
    assert result_msg["status"] == "failed"
    assert "Bad Checksum validation on worker node" in result_msg["result"]["error"]

    # 2. Test missing checksum
    worker_2 = ClientWorker(worker_id=worker_id, roles=["grok"])
    sent_messages_2 = []

    async def mock_send_2(msg):
        sent_messages_2.append(json.loads(msg))

    mock_ws_2 = AsyncMock()
    mock_ws_2.send = mock_send_2

    dispatch_msg_missing = {
        "type": "dispatch",
        "task_id": "task_missing_checksum",
        "task_data": {"role": "grok", "prompt": "test missing"},
    }

    mock_ws_2.__aiter__.return_value = [json.dumps(dispatch_msg_missing)]

    mock_connect_2 = MagicMock()
    mock_cm_2 = AsyncMock()
    mock_cm_2.__aenter__.return_value = mock_ws_2
    mock_connect_2.return_value = mock_cm_2

    with patch("websockets.connect", mock_connect_2):
        worker_task_2 = asyncio.create_task(
            worker_2.run_production_loop("127.0.0.1", 8013)
        )

        await asyncio.sleep(0.5)

        worker_task_2.cancel()
        try:
            await worker_task_2
        except asyncio.CancelledError:
            pass

    assert len(sent_messages_2) >= 2
    assert sent_messages_2[0]["type"] == "register"

    result_msgs_2 = [m for m in sent_messages_2 if m.get("type") == "result"]
    assert len(result_msgs_2) == 1
    result_msg_2 = result_msgs_2[0]
    assert result_msg_2["task_id"] == "task_missing_checksum"
    assert result_msg_2["status"] == "failed"
    assert (
        "Missing checksum validation on worker node" in result_msg_2["result"]["error"]
    )


@pytest.mark.asyncio
async def test_distributed_checksum_mismatch_server_handling(run_server):
    port = run_server
    worker_id = "checksum-mismatch-worker"

    # Start worker production loop
    worker = ClientWorker(worker_id=worker_id, roles=["grok"])
    worker_task = asyncio.create_task(worker.run_production_loop(HOST, port))
    await asyncio.sleep(0.5)

    from fastapi import WebSocket

    original_send_json = WebSocket.send_json

    async def mock_send_json(self, data, *args, **kwargs):
        if isinstance(data, dict) and data.get("type") == "dispatch":
            data["checksum"] = "totally-invalid-checksum-value"
        await original_send_json(self, data, *args, **kwargs)

    import orchestrator

    orchestrator.DISTRIBUTED_MODE = True

    try:
        with patch("fastapi.WebSocket.send_json", new=mock_send_json):
            # This should fail because the worker will reject it and report failure due to Bad Checksum
            with pytest.raises(PipelineError) as exc_info:
                await call_api(
                    url="http://localhost:8001",
                    api_key="mock-key",
                    prompt="Research the best framework for real-time task queue",
                    context={"test": "context"},
                    poll_timeout=2.0,
                )
            assert "Bad Checksum validation on worker node" in str(exc_info.value)
    finally:
        orchestrator.DISTRIBUTED_MODE = False
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
