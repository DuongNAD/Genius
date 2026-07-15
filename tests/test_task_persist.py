"""Durable skill-server task state (GENIUS_TASK_PERSIST).

Off (the default): byte-identical in-memory behavior — a restarted server
forgets its tasks (legacy 404, pinned here). On: every task state transition
is journaled to SQLite and restored on boot, so pollers see real terminal
states across restarts and idempotent retries never rerun the agent.
"""

import hashlib
import json
import os
import sqlite3

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from ag_core import skill_app
from ag_core.skill_app import create_skill_app


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    temp_db = tmp_path / "genius_task_persist.db"
    monkeypatch.setenv("GENIUS_DB_PATH", str(temp_db))
    import ag_core.utils.db as db

    monkeypatch.setattr(db, "DB_PATH", str(temp_db))
    db.init_db()
    return temp_db


def _jwt():
    import time

    from ag_core.utils.jwt import encode_jwt

    return encode_jwt({"sub": "test", "exp": time.time() + 300}, "mock-skill-key")


def _headers(body_bytes=b""):
    return {
        "X-API-Key": _jwt(),
        "Content-Type": "application/json",
        "X-Payload-SHA256": hashlib.sha256(body_bytes).hexdigest(),
    }


def _post(client, body, idempotency_key=None):
    body_bytes = json.dumps(body).encode("utf-8")
    headers = _headers(body_bytes)
    if idempotency_key is not None:
        headers["X-Idempotency-Key"] = idempotency_key
    return client.post("/run", headers=headers, content=body_bytes)


def _mock_agent(result="RESULT"):
    agent = MagicMock()
    agent.run = AsyncMock(return_value=result)
    return agent


def test_completed_task_survives_restart(temp_db, monkeypatch):
    monkeypatch.setenv("GENIUS_TASK_PERSIST", "1")
    app1 = create_skill_app("codex")
    with TestClient(app1) as client, patch(
        "ag_core.skill_app.build_agent", return_value=_mock_agent()
    ):
        task_id = _post(client, {"prompt": "hi"}).json()["task_id"]
        assert client.get(f"/status/{task_id}", headers=_headers()).json()[
            "status"
        ] == "completed"

    # "Restart": a brand-new app instance restores from the journal.
    app2 = create_skill_app("codex")
    with TestClient(app2) as client:
        r = client.get(f"/status/{task_id}", headers=_headers())
        assert r.status_code == 200
        assert r.json() == {"status": "completed", "result": "RESULT"}


def test_processing_at_crash_becomes_terminal_failure(temp_db, monkeypatch):
    monkeypatch.setenv("GENIUS_TASK_PERSIST", "1")
    # Journal a task that never finished (simulates dying mid-run).
    skill_app._persist_task(
        "codex", "deadbeef", {"status": "processing", "result": None}
    )

    app = create_skill_app("codex")
    with TestClient(app) as client:
        r = client.get("/status/deadbeef", headers=_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "failed"
        assert "restarted" in body["error"]


def test_idempotency_key_survives_restart_and_prevents_rerun(temp_db, monkeypatch):
    monkeypatch.setenv("GENIUS_TASK_PERSIST", "1")
    app1 = create_skill_app("codex")
    with TestClient(app1) as client, patch(
        "ag_core.skill_app.build_agent", return_value=_mock_agent()
    ):
        task_id = _post(client, {"prompt": "hi"}, idempotency_key="key-R").json()[
            "task_id"
        ]

    app2 = create_skill_app("codex")
    with TestClient(app2) as client, patch(
        "ag_core.skill_app.build_agent", return_value=_mock_agent()
    ) as rebuilt:
        r = _post(client, {"prompt": "hi"}, idempotency_key="key-R")
        assert r.json()["task_id"] == task_id
        assert r.json()["status"] == "completed"
        rebuilt.assert_not_called()


def test_persistence_off_by_default_restart_forgets(temp_db):
    assert not os.environ.get("GENIUS_TASK_PERSIST")
    app1 = create_skill_app("codex")
    with TestClient(app1) as client, patch(
        "ag_core.skill_app.build_agent", return_value=_mock_agent()
    ):
        task_id = _post(client, {"prompt": "hi"}).json()["task_id"]

    app2 = create_skill_app("codex")
    with TestClient(app2) as client:
        r = client.get(f"/status/{task_id}", headers=_headers())
        assert r.status_code == 404


def test_journal_is_bounded_per_role(temp_db, monkeypatch):
    monkeypatch.setenv("GENIUS_TASK_PERSIST", "1")
    for i in range(skill_app.MAX_TRACKED_TASKS + 20):
        skill_app._persist_task(
            "codex", f"task-{i}", {"status": "completed", "result": i}
        )
    conn = sqlite3.connect(str(temp_db))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM skill_tasks WHERE role = 'codex'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == skill_app.MAX_TRACKED_TASKS
