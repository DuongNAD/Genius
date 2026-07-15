"""Strict input validation on the hub's mutating endpoints.

/update_config accepts ONLY the known numeric knobs, and only finite values —
unknown keys used to be stored unvalidated into hub.config, and inf/nan pass
isinstance checks but wedge the sweeper's comparisons (timeouts that never
expire, workers never pruned). /report_result accepts only TERMINAL states.
"""

import pytest

from ag_core.distributed.hub import CentralHub

SHARED_KEY = "unit-shared-key"


@pytest.fixture
def hub():
    h = CentralHub(api_key=SHARED_KEY)
    yield h
    h.stop_sweeper()


def _headers(h, payload):
    return h.create_headers(payload)


@pytest.mark.asyncio
async def test_update_config_rejects_unknown_keys(hub):
    payload = {"config": {"max_workers": 3, "surprise_knob": 1}}
    status, body, _ = await hub.handle_request(
        "/update_config", payload, _headers(hub, payload)
    )
    assert status == 400
    assert "surprise_knob" in body["error"]
    assert "surprise_knob" not in hub.config
    assert hub.config["max_workers"] == 10  # unchanged default


@pytest.mark.asyncio
async def test_update_config_rejects_non_finite_numbers(hub):
    for bad in (float("inf"), float("-inf"), float("nan")):
        payload = {"config": {"task_timeout": bad}}
        status, body, _ = await hub.handle_request(
            "/update_config", payload, _headers(hub, payload)
        )
        assert status == 400, f"accepted {bad!r}"
        assert "finite" in body["error"]


@pytest.mark.asyncio
async def test_update_config_rejects_non_object_config(hub):
    payload = {"config": ["not", "a", "dict"]}
    status, _body, _ = await hub.handle_request(
        "/update_config", payload, _headers(hub, payload)
    )
    assert status == 400


@pytest.mark.asyncio
async def test_update_config_valid_knobs_still_work(hub):
    payload = {"config": {"max_workers": 3, "heartbeat_timeout": 2.5}}
    status, _body, _ = await hub.handle_request(
        "/update_config", payload, _headers(hub, payload)
    )
    assert status == 200
    assert hub.config["max_workers"] == 3
    assert hub.config["heartbeat_timeout"] == 2.5


@pytest.mark.asyncio
async def test_report_result_rejects_non_terminal_status(hub):
    reg = {"worker_id": "w1", "roles": ["codex"]}
    await hub.handle_request("/register", reg, _headers(hub, reg))
    disp = {"role": "codex", "task_data": {"prompt": "p"}}
    _s, body, _ = await hub.handle_request("/dispatch", disp, _headers(hub, disp))
    task_id = body["task_id"]

    for bad in ("running", "pending", "", None, "done"):
        rep = {
            "task_id": task_id,
            "worker_id": "w1",
            "status": bad,
            "result": {"output": "x"},
        }
        status, body2, _ = await hub.handle_request(
            "/report_result", rep, _headers(hub, rep)
        )
        assert status == 400, f"accepted status {bad!r}"

    # The task is untouched by the rejected reports.
    st = {"task_id": task_id}
    status, body3, _ = await hub.handle_request(
        "/task_status", st, _headers(hub, st)
    )
    assert status == 200
    assert body3["status"] in ("running", "pending")

    rep = {
        "task_id": task_id,
        "worker_id": "w1",
        "status": "failed",
        "result": {"error": "boom"},
    }
    status, _body4, _ = await hub.handle_request(
        "/report_result", rep, _headers(hub, rep)
    )
    assert status == 200
