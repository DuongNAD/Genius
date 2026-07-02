"""Tests for idempotent /run dispatch (M1).

A retried /run POST (e.g. after a transient network error where the server
already accepted the first request) must NOT run the agent twice. The client
sends a stable X-Idempotency-Key; the server returns the existing task for a
repeated key.
"""

import hashlib
import json

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from ag_core.skill_app import MAX_TRACKED_TASKS, create_skill_app, evict_oldest


def _jwt():
    import time
    from ag_core.utils.jwt import encode_jwt

    return encode_jwt({"sub": "test", "exp": time.time() + 300}, "mock-skill-key")


def _post(client, body, idempotency_key=None):
    body_bytes = json.dumps(body).encode("utf-8")
    headers = {
        "X-API-Key": _jwt(),
        "Content-Type": "application/json",
        "X-Payload-SHA256": hashlib.sha256(body_bytes).hexdigest(),
    }
    if idempotency_key is not None:
        headers["X-Idempotency-Key"] = idempotency_key
    return client.post("/run", headers=headers, content=body_bytes)


def _mock_agent():
    agent = MagicMock()
    agent.run = AsyncMock(return_value="RESULT")
    return agent


# --- Server-side idempotency ------------------------------------------------


def test_same_key_returns_same_task_and_runs_agent_once():
    app = create_skill_app("grok")
    client = TestClient(app)
    agent = _mock_agent()
    with patch("ag_core.skill_app.build_agent", return_value=agent):
        r1 = _post(client, {"prompt": "hi"}, idempotency_key="key-A")
        r2 = _post(client, {"prompt": "hi"}, idempotency_key="key-A")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["task_id"] == r2.json()["task_id"]
    # TestClient runs the background task synchronously, so the agent has run
    # for the first request only; the second is a dedup hit.
    assert agent.run.call_count == 1


def test_repeated_key_reports_completed_status():
    app = create_skill_app("grok")
    client = TestClient(app)
    with patch("ag_core.skill_app.build_agent", return_value=_mock_agent()):
        r1 = _post(client, {"prompt": "hi"}, idempotency_key="key-B")
        r2 = _post(client, {"prompt": "hi"}, idempotency_key="key-B")
    # First request's background task finished -> dedup hit reflects completion.
    assert r1.json()["status"] == "processing"
    assert r2.json()["status"] == "completed"


def test_no_key_creates_distinct_tasks_and_runs_twice():
    app = create_skill_app("grok")
    client = TestClient(app)
    agent = _mock_agent()
    with patch("ag_core.skill_app.build_agent", return_value=agent):
        r1 = _post(client, {"prompt": "hi"})
        r2 = _post(client, {"prompt": "hi"})
    assert r1.json()["task_id"] != r2.json()["task_id"]
    assert agent.run.call_count == 2


def test_different_keys_create_distinct_tasks():
    app = create_skill_app("grok")
    client = TestClient(app)
    agent = _mock_agent()
    with patch("ag_core.skill_app.build_agent", return_value=agent):
        r1 = _post(client, {"prompt": "hi"}, idempotency_key="key-C")
        r2 = _post(client, {"prompt": "hi"}, idempotency_key="key-D")
    assert r1.json()["task_id"] != r2.json()["task_id"]
    assert agent.run.call_count == 2


# --- Bounded task / idempotency stores (F7) ----------------------------------


def test_evict_oldest_drops_oldest_insertions():
    store = {f"k{i}": i for i in range(10)}
    evict_oldest(store, cap=4)
    assert len(store) == 4
    # Insertion order preserved -> the oldest six were dropped.
    assert list(store) == ["k6", "k7", "k8", "k9"]


def test_evict_oldest_noop_under_cap():
    store = {"a": 1, "b": 2}
    evict_oldest(store, cap=4)
    assert store == {"a": 1, "b": 2}


def test_run_endpoint_task_store_is_bounded():
    app = create_skill_app("grok")
    client = TestClient(app)
    with patch("ag_core.skill_app.build_agent", return_value=_mock_agent()):
        for i in range(MAX_TRACKED_TASKS + 25):
            r = _post(client, {"prompt": "hi"}, idempotency_key=f"key-{i}")
            assert r.status_code == 200
        # The newest task must still be queryable...
        last_task = r.json()["task_id"]
        headers = {
            "X-API-Key": _jwt(),
            "X-Payload-SHA256": hashlib.sha256(b"").hexdigest(),
        }
        status = client.get(f"/status/{last_task}", headers=headers)
        assert status.status_code == 200
    # ...and a fresh repeat of the OLDEST key is treated as new (evicted), not
    # as an unbounded-memory dedup hit.
    # (The stores are closures; the observable contract is bounded memory, so
    # this asserts the dedup map forgot the oldest key rather than growing.)
    with patch("ag_core.skill_app.build_agent", return_value=_mock_agent()) as _:
        r_old = _post(client, {"prompt": "hi"}, idempotency_key="key-0")
        assert r_old.status_code == 200
        assert r_old.json()["status"] == "processing"


class _FakeResp:
    def __init__(self, json_data):
        self._json = json_data
        self.content = b""
        self.headers = {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


@pytest.mark.asyncio
async def test_call_api_reuses_idempotency_key_across_retries():
    import orchestrator

    post_keys = []

    class FakeClient:
        def __init__(self):
            self._attempt = 0

        async def post(self, url, content=None, headers=None):
            if url.endswith("/run"):
                post_keys.append(headers.get("X-Idempotency-Key"))
                self._attempt += 1
                if self._attempt == 1:
                    # Transient failure AFTER (notionally) the server accepted it.
                    raise httpx.ConnectError("transient blip")
                return _FakeResp({"task_id": "T1", "status": "processing"})
            raise AssertionError(f"unexpected POST {url}")

        async def get(self, url, headers=None):
            return _FakeResp({"status": "completed", "result": "DONE"})

    fake = FakeClient()
    with patch("orchestrator.verify_response_checksum", lambda r: None), patch(
        "asyncio.sleep", new=AsyncMock()
    ):
        result = await orchestrator.call_api(
            "http://localhost:8001",
            "mock-skill-key",
            "do work",
            client=fake,
        )

    assert result == "DONE"
    # The POST was retried once; both attempts carried the SAME non-empty key.
    assert len(post_keys) == 2
    assert post_keys[0] and post_keys[0] == post_keys[1]
