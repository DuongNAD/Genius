import asyncio
import json
import time
import os
import base64
import pytest
from unittest.mock import patch
from websockets.exceptions import ConnectionClosed

# Add project root to sys.path if not present
import sys
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from client_app.genius_worker import run_worker
from ag_core.utils.jwt import decode_jwt

class StopTestException(Exception):
    """Exception to break the reconnect loop."""
    pass

class MockWebSocket:
    def __init__(self):
        self.sent_messages = []
        self.closed = False
        self.incoming_messages = asyncio.Queue()
        self.fail_heartbeats = False

    async def send(self, message):
        if self.closed:
            raise Exception("Connection closed")
        if self.fail_heartbeats and "heartbeat" in message:
            raise Exception("Heartbeat write failed")
        self.sent_messages.append(message)

    async def recv(self):
        if self.closed:
            raise ConnectionClosed(None, None)
        return await self.incoming_messages.get()

    async def __anext__(self):
        try:
            return await self.recv()
        except Exception:
            raise StopAsyncIteration

    def __aiter__(self):
        return self

    async def close(self):
        self.closed = True


# Save original asyncio.sleep to use inside mocks without recursing
original_sleep = asyncio.sleep


@pytest.mark.asyncio
async def test_network_drop_exponential_backoff():
    """
    Test 1: Verify exponential backoff on reconnection failures and reset on success.
    """
    sleep_times = []

    async def mock_sleep(delay):
        sleep_times.append(delay)
        if len(sleep_times) >= 4:
            raise StopTestException()
        await original_sleep(0.001)

    # Patch websockets.connect to always fail
    def mock_connect(uri):
        raise Exception("Connection refused")

    with patch("websockets.connect", mock_connect), \
         patch("ag_core.distributed.worker.asyncio.sleep", mock_sleep):
        
        with pytest.raises(StopTestException):
            await run_worker("127.0.0.1", 8000, ["grok"], "test-worker-backoff")

    # Reconnection sleep times should double: 1.0 -> 2.0 -> 4.0 -> 8.0...
    # (plus a random jitter between 0.0 and 1.0s)
    assert len(sleep_times) >= 3
    assert 1.0 <= sleep_times[0] <= 2.0
    assert 2.0 <= sleep_times[1] <= 3.0
    assert 4.0 <= sleep_times[2] <= 5.0


@pytest.mark.asyncio
async def test_network_drop_backoff_reset():
    """
    Verify backoff is reset to 1.0s after a successful connection.
    """
    sleep_times = []
    connect_count = 0

    class MockContextManager:
        def __init__(self, ws):
            self.ws = ws
        async def __aenter__(self):
            return self.ws
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    def mock_connect(uri):
        nonlocal connect_count
        connect_count += 1
        if connect_count == 2:
            # Succeed on the second attempt, but ws is immediately closed to trigger reconnect
            ws = MockWebSocket()
            ws.closed = True
            return MockContextManager(ws)
        # Fail otherwise
        raise Exception("Connection refused")

    async def mock_sleep(delay):
        sleep_times.append(delay)
        if len(sleep_times) >= 3:
            raise StopTestException()
        await original_sleep(0.001)

    with patch("websockets.connect", mock_connect), \
         patch("ag_core.distributed.worker.asyncio.sleep", mock_sleep):

        with pytest.raises(StopTestException):
            await run_worker("127.0.0.1", 8000, ["grok"], "test-worker-reset")

    # 1st attempt: fails -> sleeps backoff=1.0 -> backoff doubles to 2.0
    # 2nd attempt: succeeds -> resets backoff to 1.0 -> exits -> sleeps backoff=1.0 -> backoff doubles to 2.0
    # 3rd attempt: fails -> sleeps backoff=2.0 -> raises StopTestException
    # (each sleep has a random jitter between 0.0 and 1.0s added)
    assert len(sleep_times) == 3
    assert 1.0 <= sleep_times[0] <= 2.0
    assert 1.0 <= sleep_times[1] <= 2.0
    assert 2.0 <= sleep_times[2] <= 3.0


@pytest.mark.asyncio
async def test_token_regeneration_on_reconnect():
    """
    Test 3: Verify that fresh JWT tokens with updated expiration times
    are generated on every reconnect attempt.
    """
    captured_uris = []
    current_mocked_time = 1700000000.0

    class MockContextManager:
        def __init__(self, ws):
            self.ws = ws
        async def __aenter__(self):
            return self.ws
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    def mock_connect(uri):
        captured_uris.append(uri)
        ws = MockWebSocket()
        ws.closed = True
        return MockContextManager(ws)

    async def mock_sleep(delay):
        nonlocal current_mocked_time
        # Advance time on each reconnect sleep
        current_mocked_time += 10.0
        if len(captured_uris) >= 3:
            raise StopTestException()
        await original_sleep(0.001)

    def mock_time():
        return current_mocked_time

    with patch("websockets.connect", mock_connect), \
         patch("ag_core.distributed.worker.asyncio.sleep", mock_sleep), \
         patch("time.time", mock_time):

        with pytest.raises(StopTestException):
            await run_worker("127.0.0.1", 8000, ["grok"], "test-worker-token")

    assert len(captured_uris) == 3
    
    # Verify each connection has an updated JWT with updated exp
    last_exp = 0
    for uri in captured_uris:
        # Extract token from ws://127.0.0.1:8000/ws/connect?token=...
        parts = uri.split("token=")
        assert len(parts) == 2
        token = parts[1]
        
        # Parse token payload manually to avoid env / time.time() mismatch outside the patch block
        token_parts = token.split('.')
        assert len(token_parts) == 3
        payload_b64 = token_parts[1]
        
        # Add padding
        rem = len(payload_b64) % 4
        if rem > 0:
            payload_b64 += '=' * (4 - rem)
        
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode('utf-8')).decode('utf-8'))
        assert payload["sub"] == "test-worker-token"
        
        exp = payload["exp"]
        assert exp > last_exp
        last_exp = exp


@pytest.mark.asyncio
async def test_heartbeat_failure_triggers_reconnect():
    """
    Test 2: Verify that a heartbeat write failure triggers reconnection.
    """
    connect_calls = 0
    ws_instance = None

    class MockContextManager:
        def __init__(self, ws):
            self.ws = ws
        async def __aenter__(self):
            return self.ws
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    def mock_connect(uri):
        nonlocal connect_calls, ws_instance
        connect_calls += 1
        ws_instance = MockWebSocket()
        # Enable heartbeat write failures specifically:
        ws_instance.fail_heartbeats = True
        return MockContextManager(ws_instance)

    async def mock_sleep(delay):
        # Override the heartbeat 10.0s sleep to make it fail fast and yield
        if delay == 10.0:
            await original_sleep(0.01)
        else:
            await original_sleep(0.001)

    with patch("websockets.connect", mock_connect), \
         patch("ag_core.distributed.worker.asyncio.sleep", mock_sleep):

        try:
            await asyncio.wait_for(
                run_worker("127.0.0.1", 8000, ["grok"], "test-worker-hang"),
                timeout=0.2
            )
        except asyncio.TimeoutError:
            pass

    # Verify that websockets.connect was called multiple times (attempted reconnects)
    assert connect_calls > 1
    assert ws_instance is not None
    # Check that the worker stayed hung rather than reconnecting (which would increment connect_calls)
