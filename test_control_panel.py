"""Smoke tests for the Genius Control Panel web UI."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import control_panel

client = TestClient(control_panel.app)


@pytest.fixture(autouse=True)
def _fake_doctor():
    # Avoid spawning real CLI `--version` subprocesses in the suite; keep the
    # status view deterministic regardless of what's installed.
    async def fake():
        clis = [
            {"cli": n, "status": "OK", "detail": f"{n} ok", "dependents": "role"}
            for n in ("grok", "claude", "codex", "agy")
        ]
        clis.append(
            {
                "cli": "notebooklm",
                "status": "MISSING",
                "detail": "nlm not found",
                "dependents": "opt",
            }
        )
        return clis

    with patch("control_panel.run_doctor_async", fake):
        yield


def test_index_serves_html():
    r = client.get("/")
    assert r.status_code == 200
    assert "Genius" in r.text
    assert "Pipeline workflow" in r.text


def test_status_shape_covers_all_roles():
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert "stages" in data and "clis" in data
    roles = {s.get("role") for s in data["stages"]}
    assert {
        "researcher",
        "claude",
        "codex",
        "tester",
        "security",
        "devops",
    } <= roles
    # Each stage carries a backend + model (unless the chain failed to resolve).
    for s in data["stages"]:
        if "error" not in s:
            assert s["backend"]
            assert s["model"]


def test_orchestrate_requires_prompt():
    r = client.post("/api/orchestrate", json={"prompt": "   "})
    assert r.status_code == 400


def test_unknown_job_is_404():
    r = client.get("/api/jobs/does-not-exist")
    assert r.status_code == 404
