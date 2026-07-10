"""Regression tests: distributed-mode transient transport failures must surface
from ``call_api`` as ``PipelineError`` (not a bare ``asyncio.TimeoutError`` or a
raw ``httpx`` error), so the pipeline's self-heal loops — which guard their
agent calls with ``except PipelineError`` — retry a distributed timeout / hub
failure exactly as they already retry the equivalent local-mode failure.

Before the fix, the in-memory WS path re-raised a bare ``asyncio.TimeoutError``
and the HTTP hub-poll path let ``httpx`` errors escape, so neither was caught by
``except PipelineError`` — silently defeating per-attempt retry in distributed
mode (and, in the E2E pipeline, aborting every sibling file).
"""

import time

import httpx
import pytest

import orchestrator
import serve as serve_mod
from orchestrator import PipelineError, call_api


@pytest.fixture(autouse=True)
def _distributed_clean(monkeypatch):
    # Force the direct-call distributed path and keep poll timeouts short:
    # effective_poll_timeout() only honors a sub-CLI-timeout poll deadline
    # under pytest when GENIUS_CLI_TIMEOUT is unset.
    monkeypatch.delenv("GENIUS_CLI_TIMEOUT", raising=False)
    monkeypatch.setattr(orchestrator, "DISTRIBUTED_MODE", True)
    serve_mod.central_hub.workers.clear()
    serve_mod.central_hub.tasks.clear()
    serve_mod.pending_tasks.clear()
    yield
    serve_mod.central_hub.workers.clear()
    serve_mod.central_hub.tasks.clear()
    serve_mod.pending_tasks.clear()


class _SilentWS:
    """A worker WebSocket that accepts the dispatch (and cancel) frames but
    never reports a result, so the orchestrator's wait_for(future) times out."""

    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_ws_task_timeout_surfaces_as_pipeline_error():
    ws = _SilentWS()
    serve_mod.central_hub.workers["w1"] = {
        "worker_id": "w1",
        "roles": ["researcher"],
        "status": "idle",
        "last_heartbeat": time.time(),
        "ws": ws,
    }

    with pytest.raises(PipelineError) as exc_info:
        await call_api(
            url="http://localhost:8001",  # -> researcher role
            api_key="mock-skill-key",
            prompt="do research",
            poll_timeout=0.3,
        )

    assert "timed out" in str(exc_info.value).lower()
    # The task really was dispatched to the worker before it timed out.
    assert any(m.get("type") == "dispatch" for m in ws.sent)


@pytest.mark.asyncio
async def test_hub_transport_error_surfaces_as_pipeline_error(monkeypatch):
    # No in-memory workers -> the HTTP hub-poll path. A connection failure to
    # the hub must not escape call_api as a raw httpx error.
    async def boom(self, *args, **kwargs):
        raise httpx.ConnectError("hub unreachable")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)

    with pytest.raises(PipelineError) as exc_info:
        await call_api(
            url="http://localhost:8001",
            api_key="mock-skill-key",
            prompt="do research",
            poll_timeout=1.0,
        )

    assert "failed" in str(exc_info.value).lower()


# --- hub HTTP response-integrity verification --------------------------------


class _FakeResp:
    def __init__(self, content: bytes, headers: dict):
        self.content = content
        self.headers = headers


def _hub_secret():
    import os

    from ag_core.config import load_config

    return load_config().skill_api_key or os.getenv("SKILL_API_KEY", "")


def test_verify_hub_response_accepts_valid_signature():
    from ag_core.utils.security import calculate_checksum

    body = b'{"status": "completed"}'
    sig = calculate_checksum(body, _hub_secret())
    orchestrator._verify_hub_response_if_signed(
        _FakeResp(body, {"X-Payload-SHA256": sig})
    )  # must not raise


def test_verify_hub_response_rejects_tampered_body():
    from ag_core.utils.security import calculate_checksum

    # Signature computed over a DIFFERENT body than what is delivered.
    sig = calculate_checksum(b'{"status": "completed"}', _hub_secret())
    tampered = _FakeResp(b'{"status": "hacked"}', {"X-Payload-SHA256": sig})
    with pytest.raises(orchestrator.ChecksumMismatchError):
        orchestrator._verify_hub_response_if_signed(tampered)


def test_verify_hub_response_skips_when_unsigned():
    # Backward compatible: an older hub sends no signature -> skip, don't raise.
    orchestrator._verify_hub_response_if_signed(_FakeResp(b'{"x": 1}', {}))
