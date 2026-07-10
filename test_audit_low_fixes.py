"""Regression tests for the low-severity audit fixes."""

import asyncio
import hashlib
import hmac
import json

import pytest


# --- jwt: non-object header/payload -> clean 401, not a 500 ------------------


def _b64(obj) -> str:
    from ag_core.utils.jwt import base64url_encode

    return base64url_encode(json.dumps(obj).encode("utf-8"))


def _sign(header_obj, payload_obj, secret: str) -> str:
    from ag_core.utils.jwt import base64url_encode

    h, p = _b64(header_obj), _b64(payload_obj)
    sig = hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{base64url_encode(sig)}"


def test_decode_jwt_rejects_non_object_header():
    from ag_core.utils.jwt import decode_jwt

    # A JSON array header is valid JSON but not an object; header.get() would
    # otherwise raise AttributeError and escape as a 500.
    token = f"{_b64([1, 2, 3])}.{_b64({'sub': 'x'})}.whatever"
    with pytest.raises(ValueError):
        decode_jwt(token, "secret")


def test_decode_jwt_rejects_non_object_payload():
    from ag_core.utils.jwt import decode_jwt

    secret = "secret"
    token = _sign({"alg": "HS256", "typ": "JWT"}, [1, 2, 3], secret)
    with pytest.raises(ValueError):
        decode_jwt(token, secret)


# --- mcp: a non-UTF-8 artifact is served best-effort, not a crash -----------


def test_read_resource_tolerates_non_utf8(tmp_path):
    import mcp_server

    (tmp_path / "research.md").write_bytes(b"ok \xff\xfe not-utf8")
    out = mcp_server._read_resource(
        "genius://artifacts/research.md", workspace=str(tmp_path)
    )
    assert out and "text" in out[0]  # returned (with replacement chars), no raise


# --- vector_store: dimension-mismatched rows are skipped, not scored 0.0 -----


def test_query_skips_dimension_mismatched_rows(tmp_path):
    from ag_core.memory.vector_store import SimpleTFIDFEmbedding, VectorMemory

    vm = VectorMemory(
        collection_name="t", use_chroma=False, db_path=str(tmp_path / "m.db")
    )
    vm.sentence_transformer_model = None  # deterministic TF-IDF path

    vm.embedder = SimpleTFIDFEmbedding(vector_dim=128)
    vm.add("alpha beta gamma")  # stored at dim 128
    vm.embedder = SimpleTFIDFEmbedding(vector_dim=64)
    vm.add("alpha beta gamma delta")  # stored at dim 64; query vector is dim 64

    texts = [r["text"] for r in vm.query("alpha beta gamma", n_results=5)]
    assert "alpha beta gamma" not in texts  # dim-128 row can't be compared -> skipped
    assert "alpha beta gamma delta" in texts


# --- worker: received_tasks history is bounded ------------------------------


def test_received_tasks_history_is_bounded(monkeypatch):
    monkeypatch.setenv("GENIUS_WORKER_TASK_HISTORY", "10")
    from ag_core.distributed.worker import ClientWorker

    w = ClientWorker("w1", ["grok"])
    for i in range(50):
        w.received_tasks.append((f"t{i}", {"data": i}))
    assert len(w.received_tasks) == 10


# --- control_panel: PANEL_JOBS is capped, evicting oldest finished ----------


def test_prune_panel_jobs_evicts_oldest_finished(monkeypatch):
    import control_panel as cp

    monkeypatch.setattr(cp, "_PANEL_JOBS_MAX", 3)
    cp.PANEL_JOBS.clear()
    cp.PANEL_JOBS["a"] = {"status": "completed", "finished_at": 1.0}
    cp.PANEL_JOBS["b"] = {"status": "completed", "finished_at": 2.0}
    cp.PANEL_JOBS["r"] = {"status": "running", "finished_at": None}
    try:
        cp._prune_panel_jobs()
        assert "a" not in cp.PANEL_JOBS  # oldest finished evicted
        assert "b" in cp.PANEL_JOBS
        assert "r" in cp.PANEL_JOBS  # a running job is never dropped
    finally:
        cp.PANEL_JOBS.clear()


# --- hub: /write_workspace_file still writes (now off the event loop) --------


@pytest.mark.asyncio
async def test_write_workspace_file_still_writes(tmp_path, monkeypatch):
    from ag_core.distributed.hub import CentralHub

    monkeypatch.chdir(tmp_path)
    hub = CentralHub(api_key="valid-api-key")
    hub._sweeper_running = True

    payload = {"path": "sub/out.txt", "content": "hello"}
    headers = hub.create_headers(payload)
    status, _body, _hdrs = await hub.handle_request(
        "/write_workspace_file", payload, headers
    )
    assert status == 200
    assert (tmp_path / "sub" / "out.txt").read_text(encoding="utf-8") == "hello"


# --- grok: a JSON `usage: null` does not discard a valid answer -------------


def test_grok_null_usage_does_not_crash():
    import os
    from unittest.mock import AsyncMock, patch

    from ag_core.providers.grok_provider import GrokProvider

    async def run():
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate.return_value = (
            json.dumps({"result": "the answer", "usage": None}).encode("utf-8"),
            b"",
        )

        async def fake_exec(*args, **kwargs):
            return proc

        with (
            patch("shutil.which", return_value="/usr/local/bin/grok"),
            patch.dict("os.environ", {"GROK_API_KEY": "fake"}),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            os.environ.pop("GENIUS_GROK_MODEL", None)
            resp = await GrokProvider().send_prompt("hi")

        assert resp["content"] == "the answer"
        assert resp["usage"]["total_tokens"] == 0

    asyncio.run(run())
