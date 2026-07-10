"""Regression tests for two hub error-handling fixes in serve.py:

- The streaming HTTP branch must propagate the real status code, not report a
  blanket HTTP 200 for an auth/validation/backpressure failure.
- WorkerRegistry.register must report whether the hub actually accepted the
  registration, so the WS endpoint can stop claiming success for a rejected one.
"""

import pytest
from fastapi.testclient import TestClient

import serve

client = TestClient(serve.app)


def test_stream_branch_propagates_non_200_status():
    # No valid auth -> hub.handle_request returns 401. Before the fix the
    # streamed reply went out as HTTP 200 regardless.
    r = client.post("/dispatch?stream=true", json={"stream": True, "role": "x"})
    assert r.status_code == 401


def test_hub_http_response_is_signed():
    # The hub signs its (non-stream) HTTP responses so the orchestrator's
    # hub-poll path can verify integrity. Even the unauthenticated 401 body is
    # signed with the shared secret over the exact bytes sent.
    from ag_core.utils.security import verify_checksum

    r = client.post("/dispatch", json={"role": "x"})
    sig = r.headers.get("X-Payload-SHA256")
    assert sig
    assert verify_checksum(r.content, sig, serve.central_hub.api_key)


@pytest.mark.asyncio
async def test_register_returns_false_when_hub_rejects():
    hub = serve.central_hub
    saved = hub.config.get("max_workers")
    hub.workers.clear()
    hub.config["max_workers"] = 0  # drain state: reject all new registrations
    try:
        ok = await serve.worker_registry.register(
            "reject-me", ["researcher"], ws=object(), status="idle"
        )
        assert ok is False
        assert "reject-me" not in hub.workers
    finally:
        hub.config["max_workers"] = saved
        hub.workers.clear()


@pytest.mark.asyncio
async def test_register_returns_true_on_success():
    hub = serve.central_hub
    saved = hub.config.get("max_workers")
    hub.workers.clear()
    hub.config["max_workers"] = 10
    ws = object()
    try:
        ok = await serve.worker_registry.register(
            "accept-me", ["researcher"], ws=ws, status="idle"
        )
        assert ok is True
        assert hub.workers["accept-me"]["ws"] is ws
    finally:
        hub.config["max_workers"] = saved
        hub.workers.clear()
