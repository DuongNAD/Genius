"""Hub credential separation (GENIUS_HUB_ADMIN_KEY) and the
/write_workspace_file gate (GENIUS_HUB_WORKSPACE_WRITE).

The shared SKILL_API_KEY stays the worker/client credential (register,
heartbeat, dispatch, report, deregister). When an admin key is configured,
the endpoints that mutate hub-wide state or dump every task's payload
(/update_config, /tasks, /write_workspace_file) additionally require it in
an X-Admin-Key header. Without an admin key the legacy single-credential
model is byte-identical (pinned by tests/test_distributed.py).
"""

import os

import pytest

from ag_core.distributed.hub import ADMIN_ENDPOINTS, CentralHub

SHARED_KEY = "unit-shared-key"
ADMIN_KEY = "unit-admin-key"


@pytest.fixture
def hub():
    h = CentralHub(api_key=SHARED_KEY)
    yield h
    h.stop_sweeper()


def _headers(h, payload, admin=None):
    headers = h.create_headers(payload)
    if admin is not None:
        headers["X-Admin-Key"] = admin
    return headers


# --- admin gate ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_key_alone_is_rejected_on_admin_endpoints(hub, monkeypatch):
    monkeypatch.setenv("GENIUS_HUB_ADMIN_KEY", ADMIN_KEY)
    payload = {"config": {"max_workers": 3}}
    status, body, _ = await hub.handle_request(
        "/update_config", payload, _headers(hub, payload)
    )
    assert status == 403
    assert "Admin credential" in body["error"]
    assert hub.config["max_workers"] == 10  # unchanged default


@pytest.mark.asyncio
async def test_admin_header_grants_admin_endpoints(hub, monkeypatch):
    monkeypatch.setenv("GENIUS_HUB_ADMIN_KEY", ADMIN_KEY)
    payload = {"config": {"max_workers": 3}}
    status, _body, _ = await hub.handle_request(
        "/update_config", payload, _headers(hub, payload, admin=ADMIN_KEY)
    )
    assert status == 200
    assert hub.config["max_workers"] == 3


@pytest.mark.asyncio
async def test_wrong_admin_key_is_rejected(hub, monkeypatch):
    monkeypatch.setenv("GENIUS_HUB_ADMIN_KEY", ADMIN_KEY)
    payload = {}
    status, _body, _ = await hub.handle_request(
        "/tasks", payload, _headers(hub, payload, admin="not-the-admin-key")
    )
    assert status == 403


@pytest.mark.asyncio
async def test_tasks_dump_requires_admin_key_when_configured(hub, monkeypatch):
    monkeypatch.setenv("GENIUS_HUB_ADMIN_KEY", ADMIN_KEY)
    payload = {}
    status, _body, _ = await hub.handle_request(
        "/tasks", payload, _headers(hub, payload)
    )
    assert status == 403
    status, body, _ = await hub.handle_request(
        "/tasks", payload, _headers(hub, payload, admin=ADMIN_KEY)
    )
    assert status == 200
    assert isinstance(body, dict)


@pytest.mark.asyncio
async def test_admin_header_is_case_insensitive(hub, monkeypatch):
    monkeypatch.setenv("GENIUS_HUB_ADMIN_KEY", ADMIN_KEY)
    payload = {"config": {"heartbeat_timeout": 2.0}}
    headers = hub.create_headers(payload)
    headers["x-admin-key"] = ADMIN_KEY
    status, _body, _ = await hub.handle_request("/update_config", payload, headers)
    assert status == 200


@pytest.mark.asyncio
async def test_worker_endpoints_do_not_need_the_admin_key(hub, monkeypatch):
    monkeypatch.setenv("GENIUS_HUB_ADMIN_KEY", ADMIN_KEY)
    payload = {"worker_id": "w1", "roles": ["codex"]}
    status, _body, _ = await hub.handle_request(
        "/register", payload, _headers(hub, payload)
    )
    assert status == 200

    hb = {"worker_id": "w1"}
    status, _body, _ = await hub.handle_request("/heartbeat", hb, _headers(hub, hb))
    assert status == 200

    status, _body, _ = await hub.handle_request("/deregister", hb, _headers(hub, hb))
    assert status == 200


@pytest.mark.asyncio
async def test_legacy_mode_without_admin_key_is_unchanged(hub, monkeypatch):
    monkeypatch.delenv("GENIUS_HUB_ADMIN_KEY", raising=False)
    payload = {"config": {"max_workers": 5}}
    status, _body, _ = await hub.handle_request(
        "/update_config", payload, _headers(hub, payload)
    )
    assert status == 200
    assert hub.config["max_workers"] == 5


@pytest.mark.asyncio
async def test_admin_gate_still_requires_base_auth(hub, monkeypatch):
    """The admin key supplements the shared credential; it never replaces it."""
    monkeypatch.setenv("GENIUS_HUB_ADMIN_KEY", ADMIN_KEY)
    payload = {"config": {"max_workers": 4}}
    status, _body, _ = await hub.handle_request(
        "/update_config",
        payload,
        {"X-API-Key": "wrong-shared-key", "X-Admin-Key": ADMIN_KEY},
    )
    assert status == 401


def test_admin_endpoint_set_is_exactly_the_state_mutators():
    assert ADMIN_ENDPOINTS == {"/update_config", "/tasks", "/write_workspace_file"}


@pytest.mark.asyncio
async def test_distributed_dispatch_and_poll_work_with_admin_key_set(
    hub, monkeypatch
):
    """The full worker-credential flow (register -> dispatch -> /task_status
    poll -> report) must keep working when GENIUS_HUB_ADMIN_KEY is set: the
    orchestrator polls per-task /task_status, never the admin-gated /tasks."""
    monkeypatch.setenv("GENIUS_HUB_ADMIN_KEY", ADMIN_KEY)

    reg = {"worker_id": "w1", "roles": ["codex"]}
    status, _body, _ = await hub.handle_request("/register", reg, _headers(hub, reg))
    assert status == 200

    disp = {"role": "codex", "task_data": {"prompt": "p"}}
    status, body, _ = await hub.handle_request("/dispatch", disp, _headers(hub, disp))
    assert status == 202
    task_id = body["task_id"]

    st = {"task_id": task_id}
    status, body, _ = await hub.handle_request("/task_status", st, _headers(hub, st))
    assert status == 200
    assert body["task_id"] == task_id
    assert body["status"] in ("running", "pending")

    rep = {
        "task_id": task_id,
        "worker_id": "w1",
        "status": "completed",
        "result": {"output": "done"},
    }
    status, _body, _ = await hub.handle_request(
        "/report_result", rep, _headers(hub, rep)
    )
    assert status == 200

    status, body, _ = await hub.handle_request("/task_status", st, _headers(hub, st))
    assert status == 200
    assert body["status"] == "completed"
    assert body["result"] == {"output": "done"}


# --- /write_workspace_file gate ------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_write_disabled_without_env(hub, monkeypatch):
    # Env-only gate: no pytest signal opens it. Unset -> disabled, even under
    # the test suite (which otherwise turns it on in conftest).
    monkeypatch.delenv("GENIUS_HUB_WORKSPACE_WRITE", raising=False)
    payload = {"path": "x.txt", "content": "hi"}
    status, body, _ = await hub.handle_request(
        "/write_workspace_file", payload, _headers(hub, payload)
    )
    assert status == 403
    assert "disabled" in body["error"]


@pytest.mark.asyncio
async def test_workspace_write_opt_in_env_and_pinned_root(hub, monkeypatch, tmp_path):
    monkeypatch.setenv("GENIUS_HUB_WORKSPACE_WRITE", "1")
    monkeypatch.setenv("GENIUS_HUB_WORKSPACE_ROOT", str(tmp_path))
    payload = {"path": "out/x.txt", "content": "hi"}
    status, body, _ = await hub.handle_request(
        "/write_workspace_file", payload, _headers(hub, payload)
    )
    assert status == 200
    assert body["status"] == "file_written"
    assert (tmp_path / "out" / "x.txt").read_text(encoding="utf-8") == "hi"


@pytest.mark.asyncio
async def test_workspace_write_traversal_still_rejected(hub, monkeypatch, tmp_path):
    monkeypatch.setenv("GENIUS_HUB_WORKSPACE_ROOT", str(tmp_path))
    payload = {"path": "../evil.txt", "content": "hi"}
    status, _body, _ = await hub.handle_request(
        "/write_workspace_file", payload, _headers(hub, payload)
    )
    assert status == 400
    assert not (tmp_path.parent / "evil.txt").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
async def test_workspace_write_symlink_escape_rejected(hub, monkeypatch, tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside)
    monkeypatch.setenv("GENIUS_HUB_WORKSPACE_ROOT", str(root))
    payload = {"path": "link/escape.txt", "content": "hi"}
    status, _body, _ = await hub.handle_request(
        "/write_workspace_file", payload, _headers(hub, payload)
    )
    assert status == 400
    assert not (outside / "escape.txt").exists()
