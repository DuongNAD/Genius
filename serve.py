#!/usr/bin/env python3
import argparse
import asyncio
import importlib.util
import os
import signal
import sys
import time
import traceback
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from ag_core.utils.jwt import decode_jwt, jwt_max_lifetime

# Add project root to sys.path
root_dir = os.path.dirname(os.path.abspath(__file__))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from orchestrator import run_pipeline, run_e2e_pipeline

from ag_core.distributed.hub import CentralHub
from ag_core.runtime import under_pytest
from fastapi import Request, Response
import json
from ag_core.utils.db import init_db

init_db()

central_hub = CentralHub()


class BoundedPendingTasks(dict):
    def __setitem__(self, key, value):
        if len(self) >= 10000:
            for t_id, fut in list(self.items()):
                if fut.done():
                    self.pop(t_id, None)
            while len(self) >= 10000:
                first_key = next(iter(self))
                fut = self.pop(first_key, None)
                if fut and not fut.done():
                    fut.cancel()
        super().__setitem__(key, value)


pending_tasks = BoundedPendingTasks()


class WorkerDisconnectedError(Exception):
    pass


class WorkerRegistry:
    def __init__(self):
        pass

    @property
    def workers(self):
        return central_hub.workers

    @property
    def lock(self):
        if not hasattr(self, "_lock"):
            self._lock = asyncio.Lock()
        return self._lock

    async def select_idle_worker(self, role: str):
        # Both sides go through canonical_role so old workers advertising the
        # legacy "grok"/"grok_researcher" role ids still match "researcher"
        # dispatches (and vice versa).
        from ag_core.provider_factory import canonical_role

        want = canonical_role(role)
        async with self.lock:
            now = time.time()
            timeout = central_hub.config.get("heartbeat_timeout", 30.0)
            for worker_id, info in list(self.workers.items()):
                worker_roles = [canonical_role(r) for r in info.get("roles", [])]
                role_matched = False
                for r in worker_roles:
                    if (
                        r == want
                        or (want == "researcher" and "researcher" in r)
                        or (want == "claude" and "claude" in r)
                        or (want == "codex" and "codex" in r)
                        or (want == "tester" and "tester" in r)
                        or (want == "security" and "security" in r)
                        or (want == "devops" and "devops" in r)
                    ):
                        role_matched = True
                        break
                if role_matched and info.get("status") == "idle":
                    if now - info.get("last_heartbeat", 0) < timeout:
                        info["status"] = "busy"
                        return worker_id
            return None

    async def register(
        self, worker_id: str, roles: list, ws, status: str = "idle"
    ) -> bool:
        current_status = status
        if worker_id in central_hub.workers:
            if central_hub.workers[worker_id].get("status") == "busy":
                current_status = "busy"
        for t_info in central_hub.tasks.values():
            if (
                t_info.get("worker_id") == worker_id
                and t_info.get("status") == "running"
            ):
                current_status = "busy"
                break

        payload = {"worker_id": worker_id, "roles": roles}
        headers = central_hub.create_headers(payload)
        status_code, _body, _headers = await central_hub.handle_request(
            "/register", payload, headers
        )
        registered = 200 <= status_code < 300 and worker_id in central_hub.workers
        if registered:
            central_hub.workers[worker_id]["ws"] = ws
            central_hub.workers[worker_id]["status"] = current_status
        return registered

    async def unregister(self, worker_id: str, ws=None):
        worker = await self.get_worker(worker_id)
        if worker and (ws is None or worker.get("ws") == ws):
            active_tasks = []
            for t_id, t_info in list(central_hub.tasks.items()):
                if (
                    t_info.get("worker_id") == worker_id
                    and t_info.get("status") == "running"
                ):
                    active_tasks.append(t_id)

            payload = {"worker_id": worker_id}
            headers = central_hub.create_headers(payload)
            await central_hub.handle_request("/deregister", payload, headers)

            for t_id in active_tasks:
                if t_id in central_hub.tasks:
                    central_hub.tasks[t_id]["status"] = "failed"
                    central_hub.tasks[t_id]["result"] = {"error": "Worker disconnected"}
                fut = pending_tasks.pop(t_id, None)
                if fut and not fut.done():
                    fut.set_exception(WorkerDisconnectedError("Worker disconnected"))

    async def update_heartbeat(self, worker_id: str):
        payload = {"worker_id": worker_id}
        headers = central_hub.create_headers(payload)
        await central_hub.handle_request("/heartbeat", payload, headers)

    async def get_worker(self, worker_id: str):
        return central_hub.workers.get(worker_id)


worker_registry = WorkerRegistry()


async def prune_stale_workers(timeout_sec: float = 30.0, check_interval: float = 5.0):
    central_hub.config["heartbeat_timeout"] = timeout_sec
    while True:
        try:
            await asyncio.sleep(check_interval)
            await central_hub.sweep()
            for task_id, fut in list(pending_tasks.items()):
                if task_id in central_hub.tasks:
                    task_info = central_hub.tasks[task_id]
                    if task_info.get("status") == "failed":
                        pending_tasks.pop(task_id, None)
                        if fut and not fut.done():
                            result = task_info.get("result") or {}
                            error_msg = (
                                result.get(
                                    "error", "Task timed out or failed on worker"
                                )
                                if isinstance(result, dict)
                                else str(result)
                            )
                            if (
                                "timed out" in error_msg.lower()
                                or "timeout" in error_msg.lower()
                            ):
                                fut.set_exception(asyncio.TimeoutError(error_msg))
                            elif (
                                "disconnect" in error_msg.lower()
                                or "offline" in error_msg.lower()
                                or "disappeared" in error_msg.lower()
                            ):
                                fut.set_exception(WorkerDisconnectedError(error_msg))
                            else:
                                fut.set_exception(ValueError(error_msg))
        except asyncio.CancelledError:
            # Shutdown: propagate so the task actually stops.
            raise
        except Exception:
            # One bad sweep must not kill the pruner for the process lifetime;
            # log and keep looping so future stale workers still get swept.
            print(
                "[Hub] prune_stale_workers: sweep iteration failed",
                file=sys.stderr,
            )
            traceback.print_exc()


@asynccontextmanager
async def lifespan(app: FastAPI):
    central_hub.start_sweeper()
    prune_task = asyncio.create_task(prune_stale_workers())
    yield
    central_hub.stop_sweeper()
    prune_task.cancel()
    try:
        await prune_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Genius Central Hub", lifespan=lifespan)

# The catch-all POST below buffers the whole body (request.json()) BEFORE
# central_hub.handle_request authenticates it — cap the receive stream at the
# ASGI layer so a chunked/length-less flood can't exhaust RAM pre-auth.
from ag_core.utils.security import BodySizeLimitMiddleware  # noqa: E402

app.add_middleware(BodySizeLimitMiddleware)


@app.post("/{path:path}")
async def hub_http_route(path: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    headers = dict(request.headers)

    if payload.get("stream") or request.query_params.get("stream") == "true":
        from fastapi.responses import StreamingResponse

        # Resolve the request BEFORE building the streaming response so the real
        # status code and headers propagate. Computing them inside the generator
        # meant a streamed reply always went out as HTTP 200 — an auth,
        # validation, or backpressure (503) failure was reported as success.
        status_code, body, resp_headers = await central_hub.handle_request(
            "/" + path, payload, headers
        )
        body_text = json.dumps(body) if isinstance(body, dict) else str(body)

        async def stream_generator():
            yield body_text

        return StreamingResponse(
            stream_generator(),
            status_code=status_code,
            media_type="application/json",
            headers=resp_headers,
        )

    status_code, body, resp_headers = await central_hub.handle_request(
        "/" + path, payload, headers
    )
    body_str = json.dumps(body)
    # Sign the response so the orchestrator's distributed hub-poll path can
    # verify the integrity of the worker-generated result relayed through the
    # hub — matching the local skill-server path. Checksum is over the exact
    # bytes sent, keyed by the same shared secret used for request auth.
    from ag_core.utils.security import calculate_checksum

    resp_headers = dict(resp_headers or {})
    resp_headers.setdefault(
        "X-Payload-SHA256",
        calculate_checksum(body_str.encode("utf-8"), central_hub.api_key or ""),
    )
    return Response(
        content=body_str,
        status_code=status_code,
        media_type="application/json",
        headers=resp_headers,
    )


IS_DISTRIBUTED = False


@app.websocket("/ws/connect")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(default="")):
    is_pytest = under_pytest()
    if not IS_DISTRIBUTED and not is_pytest:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=404, detail="WebSocket not enabled in local mode"
        )

    # Workers send the JWT in an `Authorization: Bearer` header by default
    # (query strings end up in access/proxy logs; headers generally do not).
    # `?token=` stays accepted for backward compatibility with older workers.
    auth_header = websocket.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        credential = auth_header[7:].strip()
    else:
        credential = (token or "").strip()
    if not credential:
        await websocket.accept()
        await websocket.close(code=4001)
        return

    # Resolve the JWT secret the same way the hub/worker do, so tokens verify
    # consistently. Fail closed (don't accept connections) if no secret is
    # configured in production rather than silently using an empty secret.
    secret = central_hub.api_key or os.getenv("SKILL_API_KEY", "")
    if not secret and not is_pytest:
        await websocket.accept()
        await websocket.close(code=4001)
        return
    try:
        # decode_jwt records the token's jti in the anti-replay table via the
        # single blocking SQLite writer thread. Offload it so a DB-contended
        # write can't freeze the hub event loop mid-handshake — which would
        # stall heartbeat processing and the sweeper and trigger spurious
        # worker eviction (and, in --auto-pilot, freeze every skill server too).
        payload = await asyncio.to_thread(
            decode_jwt,
            credential,
            secret,
            require_exp=True,
            max_lifetime=jwt_max_lifetime(),
        )
        worker_id_from_jwt = payload.get("sub") or payload.get("worker_id")
    except Exception:
        await websocket.accept()
        await websocket.close(code=4001)
        return

    await websocket.accept()
    registered_worker_id = None

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "register":
                payload_worker_id = data.get("worker_id")
                if payload_worker_id and payload_worker_id != worker_id_from_jwt:
                    await websocket.send_json(
                        {"type": "error", "error": "Identity spoofing detected"}
                    )
                    await websocket.close(code=4003)
                    return
                worker_id = worker_id_from_jwt
                roles = data.get("roles") or data.get("role") or []
                if isinstance(roles, str):
                    roles = [r.strip() for r in roles.split(",") if r.strip()]

                ok = await worker_registry.register(
                    worker_id, roles, websocket, status="idle"
                )
                if not ok:
                    # The hub refused the registration (e.g. max-workers). Tell
                    # the worker the truth and close, instead of reporting
                    # success for a registration that never happened.
                    await websocket.send_json(
                        {"type": "error", "error": "registration_rejected"}
                    )
                    await websocket.close(code=4004)
                    return
                registered_worker_id = worker_id
                await websocket.send_json({"type": "registered", "status": "success"})

            elif msg_type == "heartbeat":
                payload_worker_id = data.get("worker_id")
                if payload_worker_id and payload_worker_id != registered_worker_id:
                    await websocket.send_json(
                        {"type": "error", "error": "Identity spoofing detected"}
                    )
                    await websocket.close(code=4003)
                    return
                worker_id = payload_worker_id or registered_worker_id
                if not worker_id or worker_id not in worker_registry.workers:
                    await websocket.send_json(
                        {"type": "error", "error": "not_registered"}
                    )
                else:
                    await worker_registry.update_heartbeat(worker_id)
                    await websocket.send_json({"type": "pong"})

            elif msg_type in ("report_result", "result"):
                pass

                task_id = data.get("task_id")
                payload_worker_id = data.get("worker_id")
                if payload_worker_id and payload_worker_id != registered_worker_id:
                    await websocket.send_json(
                        {"type": "error", "error": "Identity spoofing detected"}
                    )
                    await websocket.close(code=4003)
                    return
                worker_id = payload_worker_id or registered_worker_id
                status = data.get("status")
                result = data.get("result")
                checksum = data.get("checksum")
                if not checksum:
                    print("[Hub] Missing result checksum from worker!")
                    if worker_id and worker_id in worker_registry.workers:
                        worker_registry.workers[worker_id]["status"] = "idle"
                    if task_id and task_id in central_hub.tasks:
                        central_hub.tasks[task_id]["status"] = "failed"
                        central_hub.tasks[task_id]["result"] = {
                            "error": "Missing result checksum"
                        }
                    fut = pending_tasks.pop(task_id, None)
                    if fut and not fut.done():
                        fut.set_exception(ValueError("Missing result checksum"))
                    continue

                from ag_core.utils.security import verify_checksum

                if not verify_checksum(result, checksum, central_hub.api_key):
                    print(f"[Hub] Result checksum mismatch! Expected {checksum}")
                    if worker_id and worker_id in worker_registry.workers:
                        worker_registry.workers[worker_id]["status"] = "idle"
                    if task_id and task_id in central_hub.tasks:
                        central_hub.tasks[task_id]["status"] = "failed"
                        central_hub.tasks[task_id]["result"] = {
                            "error": "Result checksum validation failed"
                        }
                    fut = pending_tasks.pop(task_id, None)
                    if fut and not fut.done():
                        fut.set_exception(
                            ValueError("Result checksum validation failed")
                        )
                    continue

                if task_id and worker_id:
                    payload = {
                        "task_id": task_id,
                        "worker_id": worker_id,
                        "status": status,
                        "result": result,
                    }
                    headers = central_hub.create_headers(payload)
                    sc, _, _ = await central_hub.handle_request(
                        "/report_result", payload, headers
                    )
                    if sc != 200:
                        # The hub refused the report — unknown task, or its
                        # assignment guard fired (task belongs to another
                        # worker, e.g. after a re-dispatch reused the id).
                        # A stale/foreign report must not resolve the current
                        # attempt's future or flip states here.
                        continue

                    if worker_id in worker_registry.workers:
                        worker_registry.workers[worker_id]["status"] = "idle"

                    fut = pending_tasks.pop(task_id, None)
                    if fut and not fut.done():
                        if status == "completed":
                            output = (
                                result.get("output", result)
                                if isinstance(result, dict)
                                else result
                            )
                            fut.set_result(output)
                        else:
                            error_msg = (
                                result.get("error", "Unknown worker error")
                                if isinstance(result, dict)
                                else str(result)
                            )
                            fut.set_exception(Exception(error_msg))

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if registered_worker_id:
            await worker_registry.unregister(registered_worker_id, websocket)


def _resolve_service_host(specific_env: str) -> str:
    """Resolve a service bind host with a secure bare-metal default.

    Docker/LAN deployments opt in to ``0.0.0.0`` through the per-service
    variable; a plain local invocation should not expose the control plane to
    every network interface merely because authentication is configured.
    """
    return (
        os.environ.get(specific_env)
        or os.environ.get("GENIUS_BIND_HOST")
        or "127.0.0.1"
    )


async def start_hub_server(port: int):
    # Specific override wins over GENIUS_BIND_HOST; Docker declares 0.0.0.0
    # explicitly, while bare-metal defaults to loopback.
    hub_host = _resolve_service_host("GENIUS_HUB_HOST")
    if hub_host.strip() not in ("127.0.0.1", "localhost", "::1", ""):
        # The hub's own transport is plaintext HTTP/ws: off loopback, the
        # shared secret, worker JWTs and every prompt/result are readable to
        # anyone on the network path. Warn loudly (but don't refuse — LAN/VPN
        # setups are legitimate) and point at the mitigations.
        print(
            f"[Hub] WARNING: binding {hub_host}: the hub speaks plaintext "
            "HTTP/ws. Keep it on a VPN/trusted LAN or front it with TLS "
            "(workers switch to wss:// with GENIUS_HUB_TLS=1), and set "
            "GENIUS_HUB_ADMIN_KEY so the shared worker key cannot administer "
            "the hub.",
            file=sys.stderr,
        )
    config = uvicorn.Config(app, host=hub_host, port=port, log_level="info", ws="auto")
    server = uvicorn.Server(config)
    # Drive the lifecycle manually (startup -> main_loop -> shutdown) instead of
    # server.serve(): serve() installs uvicorn's own SIGINT/SIGTERM handlers,
    # which swallow the FIRST Ctrl+C to gracefully stop only the hub while the
    # manually-driven agent servers (start_server) keep running until a second
    # Ctrl+C. Without those handlers, SIGINT propagates to asyncio.run and
    # main_async's finally cancels every server task at once. Mirrors
    # start_server's manual lifecycle.
    if not config.loaded:
        config.load()
    server.lifespan = config.lifespan_class(config)
    await server.startup()
    try:
        await server.main_loop()
    finally:
        await server.shutdown()


ROUTING_TABLE = {
    "/research": ("researcher", 8001),
    "/summarize": ("researcher", 8001),
    "/fact-check": ("researcher", 8001),
    "/plan": ("claude", 8002),
    "/design": ("claude", 8002),
    "/review-architecture": ("claude", 8002),
    "/code": ("codex", 8003),
    "/refactor": ("codex", 8003),
    "/security": ("security", 8005),
    "/audit": ("security", 8005),
    "/security-audit": ("security", 8005),
    "/unit-test": ("tester", 8004),
    "/stress-test": ("tester", 8004),
    "/deploy": ("devops", 8006),
}


# Accepted --roles / menu spellings per canonical role. The "grok"-flavoured
# tokens are the researcher role's legacy id.
_ROLE_INPUT_ALIASES = {
    "researcher": (
        "1",
        "researcher",
        "researcher api",
        "researcher-api",
        "grok",
        "grok_researcher",
        "grok api",
        "grok-api",
    ),
    "claude": ("2", "claude", "claude_architect", "claude api", "claude-api"),
    "codex": ("3", "codex", "codex_reviewer", "codex api", "codex-api"),
    "tester": ("4", "tester", "tester_agent", "tester api", "tester-api"),
    "orchestrator": ("5", "orchestrator"),
    "dashboard": (
        "6",
        "dashboard",
        "web dashboard",
        "web-dashboard",
        "dashboard api",
        "dashboard-api",
    ),
    "security": ("7", "security", "security_agent", "security api", "security-api"),
    "devops": ("8", "devops", "devops_agent", "devops api", "devops-api"),
}
_ROLE_INPUT_LOOKUP = {
    alias: role for role, aliases in _ROLE_INPUT_ALIASES.items() for alias in aliases
}


def normalize_roles(roles_str: str) -> list:
    raw_roles = [r.strip().lower() for r in roles_str.split(",") if r.strip()]
    return [_ROLE_INPUT_LOOKUP[r] for r in raw_roles if r in _ROLE_INPUT_LOOKUP]


def interactive_prompt() -> list:
    print("=== Antigravity 2.0 Unified Startup Menu ===")
    print("Select specific roles/agents this machine will run (comma-separated):")
    print("1. researcher   - Researcher API (Port 8001)")
    print("2. claude       - Claude Architect API (Port 8002)")
    print("3. codex        - Codex Reviewer API (Port 8003)")
    print("4. tester       - Tester Agent API (Port 8004)")
    print("5. orchestrator - Launch Orchestrator Workflow")
    print("6. dashboard    - Web Dashboard (Port 8080)")
    print("7. security     - Security Agent API (Port 8005)")
    print("8. devops       - DevOps Agent API (Port 8006)")
    try:
        choice = input(
            "Enter selection (e.g. 'researcher,claude' or '5' or '1,2,3,4,5,6,7,8'): "
        ).strip()
        return normalize_roles(choice)
    except KeyboardInterrupt:
        print("\nExiting.")
        sys.exit(0)


# canonical role -> skill directory under .agents/skills/ (role ids and
# directory names diverged historically; this map is the single place that
# records the pairing).
SKILL_APP_DIRS = {
    "researcher": "researcher",
    "claude": "claude_architect",
    "codex": "codex_reviewer",
    "tester": "tester_agent",
    "security": "security_agent",
    "devops": "devops_agent",
}


def get_api_app(role: str):
    from ag_core.provider_factory import canonical_role

    role = canonical_role(role)
    if role == "dashboard":
        path = os.path.join(root_dir, "dashboard.py")
    elif role in SKILL_APP_DIRS:
        path = os.path.join(
            root_dir, ".agents", "skills", SKILL_APP_DIRS[role], "api.py"
        )
    else:
        raise ValueError(f"Unknown role: {role}")

    spec = importlib.util.spec_from_file_location(f"{role}_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


# Default ports for the agent skill servers (the roles that expose /health).
AGENT_DEFAULT_PORTS = {
    "researcher": 8001,
    "claude": 8002,
    "codex": 8003,
    "tester": 8004,
    "security": 8005,
    "devops": 8006,
}


def _under_pytest() -> bool:
    return under_pytest()


def _startup_timeout(default: float = 30.0) -> float:
    """Resolve the server-readiness deadline (seconds) from
    ``GENIUS_STARTUP_TIMEOUT`` or the default."""
    raw = os.environ.get("GENIUS_STARTUP_TIMEOUT")
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return default


def _log_server_task_failure(task: asyncio.Task) -> None:
    """Done-callback for server tasks: surface a crash immediately instead of
    letting gather(..., return_exceptions=True) swallow it until exit."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(f"ERROR: server task '{task.get_name()}' crashed: {exc}")
        traceback.print_exception(type(exc), exc, exc.__traceback__)


def _server_task_crash(server_tasks):
    """Return the exception of the first already-crashed server task, if any."""
    for task in server_tasks:
        if task.done() and not task.cancelled():
            exc = task.exception()
            if exc is not None:
                return exc
    return None


async def wait_for_hub_ready(hub_port: int, server_tasks=(), timeout=None):
    """Wait until the central hub is accepting connections before proceeding.

    The hub exposes no ``GET /health`` (only an authenticated catch-all POST
    and the ``/ws/connect`` upgrade), so readiness is probed at the TCP level:
    a successful connection means uvicorn finished startup and is listening.
    Without this, ``serve.py --distributed --prompt`` launched the orchestrator
    pipeline before the hub was up and the first dispatch died with "All
    connection attempts failed". Raises RuntimeError on deadline or as soon as
    the hub server task has crashed.
    """
    if timeout is None:
        timeout = _startup_timeout()
    deadline = time.time() + timeout
    while True:
        crash = _server_task_crash(server_tasks)
        if crash is not None:
            raise RuntimeError(
                f"The hub server task crashed during startup: {crash!r}"
            ) from crash
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", hub_port)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return
        except (ConnectionRefusedError, OSError):
            pass
        if time.time() > deadline:
            raise RuntimeError(
                f"Central hub on port {hub_port} did not become ready within "
                f"{timeout:.0f}s"
            )
        await asyncio.sleep(0.1)


async def wait_for_servers_ready(agent_ports: dict, server_tasks=(), timeout=None):
    """Poll each launched agent server's ``GET /health`` until it answers 200.

    ``agent_ports`` maps role -> configured port; the service registry is
    consulted on every sweep so a server that fell back to a dynamic port is
    still found. Raises RuntimeError on deadline (listing the roles that never
    became ready) or as soon as a server task has crashed.
    """
    import httpx

    if timeout is None:
        timeout = _startup_timeout()
    deadline = time.time() + timeout
    pending = dict(agent_ports)
    registry_path = _resolve_registry_path()
    async with httpx.AsyncClient(timeout=2.0) as client:
        while pending:
            crash = _server_task_crash(server_tasks)
            if crash is not None:
                raise RuntimeError(
                    f"A server task crashed during startup: {crash!r}"
                ) from crash
            registry = {}
            if os.path.exists(registry_path):
                try:
                    with open(registry_path, "r", encoding="utf-8") as f:
                        registry = json.load(f)
                except Exception:
                    registry = {}
            for role in list(pending):
                port = registry.get(role, pending[role])
                if not isinstance(port, int):
                    port = pending[role]
                try:
                    resp = await client.get(f"http://127.0.0.1:{port}/health")
                    if resp.status_code == 200:
                        pending.pop(role)
                except Exception:
                    pass
            if not pending:
                break
            if time.time() > deadline:
                raise RuntimeError(
                    f"Agent servers did not become ready within {timeout:.0f}s: "
                    f"{', '.join(sorted(pending))}. Check the server logs above "
                    f"for startup errors, or raise GENIUS_STARTUP_TIMEOUT."
                )
            await asyncio.sleep(0.25)


def _resolve_registry_path() -> str:
    """Resolve the service-registry path and ensure its parent dir exists.

    An empty ``GENIUS_SERVICE_REGISTRY`` (as shipped blank in ``.env.example``
    and loaded into ``os.environ`` by python-dotenv) must be treated as unset;
    otherwise ``os.path.dirname("")`` is ``""`` and ``os.makedirs("")`` raises
    ``FileNotFoundError`` on Windows, crashing every agent server on startup.
    """
    registry_path = os.environ.get("GENIUS_SERVICE_REGISTRY") or os.path.join(
        root_dir, ".agents", "service_registry.json"
    )
    registry_dir = os.path.dirname(registry_path)
    if registry_dir:
        os.makedirs(registry_dir, exist_ok=True)
    return registry_path


def _prune_registry_entry(registry_path: str, role: str, port: int) -> None:
    """Drop this role's entry from the (flat ``role -> port``) service registry
    on clean shutdown, so later readers don't route to a dead dynamic port.
    Only prunes when the entry still points at OUR port — a newer instance may
    have re-registered the role in the meantime. Failures are non-fatal."""
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            registry = json.load(f)
        if registry.get(role) == port:
            registry.pop(role, None)
            with open(registry_path, "w", encoding="utf-8") as f:
                json.dump(registry, f, indent=2)
    except Exception:
        pass


async def start_server(role: str, port: int):
    # Canonicalize here (not just in normalize_roles) so direct callers using
    # a legacy role id ("grok") boot the right app AND register the canonical
    # key in the service registry.
    from ag_core.provider_factory import canonical_role

    role = canonical_role(role)
    app = get_api_app(role)

    def _make_server(bind_port: int) -> uvicorn.Server:
        # Specific override wins over GENIUS_BIND_HOST; Docker declares
        # 0.0.0.0 explicitly, while bare-metal defaults to loopback.
        skill_host = _resolve_service_host("GENIUS_SKILL_HOST")
        config = uvicorn.Config(
            app,
            host=skill_host,
            port=bind_port,
            log_level="info",
            ws="auto",
        )
        server = uvicorn.Server(config)
        # uvicorn wires up the loaded config + lifespan inside serve(); since we
        # drive the lifecycle manually (startup -> read bound port -> main_loop),
        # replicate that init here or startup() fails on a missing .lifespan.
        if not config.loaded:
            config.load()
        server.lifespan = config.lifespan_class(config)
        return server

    try:
        server = _make_server(port)
        await server.startup()
    except (OSError, SystemExit):
        # uvicorn's Server.startup() intercepts the bind failure itself
        # (logs the OSError and raises SystemExit(1) via sys.exit), so a bare
        # `except OSError` never fires and the server task just crashed when
        # the configured port was taken. Catch both so the dynamic-port
        # fallback actually runs.
        server = _make_server(0)
        await server.startup()

    bound_port = None
    for s in server.servers:
        for sock in s.sockets:
            bound_port = sock.getsockname()[1]
            break
        if bound_port:
            break
    if not bound_port:
        bound_port = server.config.port

    registry_path = _resolve_registry_path()

    if bound_port != port:
        print(
            f"WARNING: role '{role}' could not bind its configured port {port} "
            f"(already in use?); fell back to dynamic port {bound_port} "
            f"({port} -> {bound_port}). Clients discover the new port via the "
            f"service registry: {registry_path}"
        )

    registry = {}
    if os.path.exists(registry_path):
        try:
            with open(registry_path, "r", encoding="utf-8") as f:
                registry = json.load(f)
        except Exception:
            pass

    registry[role] = bound_port
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)

    try:
        await server.main_loop()
    finally:
        await server.shutdown()
        _prune_registry_entry(registry_path, role, bound_port)


async def main_async():
    parser = argparse.ArgumentParser(
        description="Unified Startup Menu for Genius Microservices"
    )
    parser.add_argument(
        "--roles",
        default=None,
        help=(
            "Comma-separated roles to run (researcher, claude, codex, tester, "
            "security, devops, orchestrator, dashboard); legacy alias 'grok' "
            "still maps to researcher"
        ),
    )
    parser.add_argument("--prompt", default=None, help="Prompt for orchestrator role")
    parser.add_argument(
        "--interactive", action="store_true", help="Interactive design review loop"
    )
    parser.add_argument(
        "--auto-pilot",
        action="store_true",
        help="Auto-pilot: start all servers and run pipeline",
    )
    parser.add_argument(
        "--pipeline",
        choices=["sequential", "e2e", "custom"],
        default="sequential",
        help="Pipeline type to execute (custom = opt-in user-tailored flow)",
    )
    parser.add_argument(
        "--distributed", action="store_true", help="Start the central hub service"
    )
    parser.add_argument(
        "--hub-port",
        type=int,
        default=8000,
        help="Port to run the central hub service on",
    )
    parser.add_argument(
        "--keep-alive",
        action="store_true",
        help=(
            "After the orchestrator pipeline finishes (or fails), keep the hub "
            "and servers running instead of shutting down — so distributed "
            "workers stay connected and more jobs can run. Ctrl+C to stop."
        ),
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run preflight checks (CLI resolution, auth, SKILL_API_KEY) and exit",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help=(
            "With --doctor: additionally send one live canary prompt through "
            "every unique (backend, model) pair in the effective role chains — "
            "catches invalid model pins and logged-out CLIs that --version "
            "checks cannot see. Costs one small real inference per pair."
        ),
    )
    args = parser.parse_args()

    if getattr(args, "doctor", False) is True:
        from ag_core.diagnostics import run_doctor_report_async

        code = await run_doctor_report_async(deep=getattr(args, "deep", False))
        raise SystemExit(code)

    auto_pilot = getattr(args, "auto_pilot", False) is True
    interactive = getattr(args, "interactive", False) is True
    distributed = getattr(args, "distributed", False) is True
    keep_alive = getattr(args, "keep_alive", False) is True

    global IS_DISTRIBUTED
    IS_DISTRIBUTED = distributed

    # Production profile (opt-in): fail closed at startup when the convenience
    # defaults are left fail-open. No-op unless GENIUS_SECURE_DEFAULTS is set,
    # so local / trusted-LAN runs are unchanged.
    from ag_core.security_profile import enforce_secure_defaults

    enforce_secure_defaults(distributed=distributed)

    if auto_pilot:
        selected_roles = [
            "researcher",
            "claude",
            "codex",
            "tester",
            "security",
            "devops",
            "dashboard",
            "orchestrator",
        ]
    elif args.roles:
        selected_roles = normalize_roles(args.roles)
    elif args.prompt is not None:
        selected_roles = ["orchestrator"]
    elif distributed:
        selected_roles = []
    else:
        selected_roles = interactive_prompt()

    # Dynamic role resolution for prompt command execution
    prompt = args.prompt
    if prompt:
        # Detect the routed command on the @modifier-stripped prompt so
        # `@deep /code ...` still launches the codex role's server; args.prompt
        # itself is untouched (it flows to the orchestrator, which re-parses).
        from ag_core.directives import parse_directives

        cleaned = parse_directives(prompt)[0]
        first_word = cleaned.strip().split()[0] if cleaned.strip() else ""
        if first_word.startswith("/") and first_word in ROUTING_TABLE:
            target_role, target_port = ROUTING_TABLE[first_word]
            if target_role not in selected_roles:
                selected_roles.append(target_role)
                print(
                    f"Automatically adding agent role '{target_role}' for command routing of '{first_word}'"
                )

    if not selected_roles and not distributed:
        print("No valid roles selected. Exiting.")
        return

    print(f"Starting selected roles: {selected_roles}")

    server_tasks = []
    # role -> configured port, for the agent servers we must health-check
    # before running a pipeline (hub/dashboard expose no /health).
    agent_ports = {}

    def _spawn(coro, name):
        task = asyncio.create_task(coro, name=name)
        # Surface crashes immediately instead of only at shutdown, where
        # gather(..., return_exceptions=True) would swallow them.
        task.add_done_callback(_log_server_task_failure)
        server_tasks.append(task)

    if distributed:
        print(f"Starting central hub on port {args.hub_port}")
        _spawn(start_hub_server(args.hub_port), "server-hub")

    # Start requested API servers
    for agent_role, agent_port in AGENT_DEFAULT_PORTS.items():
        if agent_role in selected_roles:
            _spawn(start_server(agent_role, agent_port), f"server-{agent_role}")
            agent_ports[agent_role] = agent_port
    if "dashboard" in selected_roles:
        _spawn(start_server("dashboard", 8080), "server-dashboard")

    # If prompt is provided or orchestrator is explicitly selected
    if "orchestrator" in selected_roles or prompt:
        if server_tasks:
            if _under_pytest() and not os.environ.get("GENIUS_STARTUP_TIMEOUT"):
                # Tests patch start_server with mocks that never open a
                # socket; a yield lets those tasks run without a real poll.
                await asyncio.sleep(0)
            else:
                try:
                    if distributed:
                        # Distributed dispatch calls the hub before any agent
                        # server; wait for it to listen or the first /workers
                        # POST dies with "All connection attempts failed".
                        print("Waiting for central hub to become ready...")
                        await wait_for_hub_ready(args.hub_port, server_tasks)
                        print("Central hub is ready.")
                    print("Waiting for API servers to become ready...")
                    await wait_for_servers_ready(agent_ports, server_tasks)
                    print("All requested agent servers are ready.")
                except Exception as e:
                    print(f"Startup aborted: {e}")
                    for task in server_tasks:
                        task.cancel()
                    await asyncio.gather(*server_tasks, return_exceptions=True)
                    raise SystemExit(1)
            # A server task that already died means the pipeline cannot work;
            # abort with that error rather than failing later and murkier.
            crash = _server_task_crash(server_tasks)
            if crash is not None:
                print(f"Startup aborted, a server task crashed: {crash!r}")
                for task in server_tasks:
                    task.cancel()
                await asyncio.gather(*server_tasks, return_exceptions=True)
                raise SystemExit(1)

        if not prompt:
            if auto_pilot:
                print("Error: Prompt is required under auto-pilot mode.")
                for task in server_tasks:
                    task.cancel()
                if server_tasks:
                    await asyncio.gather(*server_tasks, return_exceptions=True)
                return
            try:
                prompt = (
                    await asyncio.to_thread(input, "Enter prompt for orchestrator: ")
                ).strip()
            except KeyboardInterrupt:
                print("\nExiting.")
                return

        if not prompt:
            print("Error: Prompt is required to run the orchestrator.")
            return

        pipeline_run = False
        pipeline_failed = False
        try:
            print(f"Launching orchestrator pipeline with prompt: '{prompt}'")
            pipeline_run = True
            pipeline_kwargs = {}
            if interactive or auto_pilot:
                pipeline_kwargs["interactive"] = interactive
            if distributed:
                pipeline_kwargs["distributed"] = True

            if getattr(args, "pipeline", "sequential") == "e2e":
                e2e_kwargs = {}
                if distributed:
                    e2e_kwargs["distributed"] = True
                await run_e2e_pipeline(prompt, **e2e_kwargs)
            else:
                if getattr(args, "pipeline", "sequential") == "custom":
                    pipeline_kwargs["flow"] = "custom"
                await run_pipeline(prompt, **pipeline_kwargs)
            print("Orchestrator pipeline completed successfully.")
        except Exception as e:
            print(f"Orchestrator pipeline failed: {e}")
            pipeline_failed = True
        finally:
            if pipeline_run and not keep_alive:
                for task in server_tasks:
                    task.cancel()
                if server_tasks:
                    await asyncio.gather(*server_tasks, return_exceptions=True)
                if pipeline_failed:
                    # Propagate the failure to the shell (exit code 1) so
                    # auto-pilot cannot exit 0 on a failed pipeline.
                    raise SystemExit(1)
                return
            elif pipeline_run and keep_alive:
                # A finished OR failed pipeline must NOT take the hub down with
                # it when --keep-alive is set: leave the servers running and fall
                # through to the persistent loop below, so distributed workers
                # stay connected (no more WinError 1225 / connection-refused) and
                # further jobs can run.
                print(
                    "Pipeline "
                    + ("FAILED" if pipeline_failed else "completed")
                    + "; keeping hub/servers up (--keep-alive). Press Ctrl+C to stop."
                )

    # If we started servers, we await them to run continuously
    if server_tasks:
        # Handle SIGTERM (docker stop / systemd / `kill`) the same as Ctrl+C:
        # cancel the servers so the finally-block cleanup runs (workers
        # deregistered, DB queue drained) instead of the process being killed
        # abruptly. POSIX only — add_signal_handler is unsupported on Windows.
        installed_signals = []
        if sys.platform != "win32":
            loop = asyncio.get_running_loop()

            def _request_shutdown():
                for task in server_tasks:
                    task.cancel()

            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, _request_shutdown)
                    installed_signals.append(sig)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass
        try:
            print("FastAPI servers are running. Press Ctrl+C to stop.")
            await asyncio.gather(*server_tasks)
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("Stopping servers...")
        finally:
            for sig in installed_signals:
                try:
                    loop.remove_signal_handler(sig)
                except Exception:
                    pass
            for task in server_tasks:
                task.cancel()
            if server_tasks:
                await asyncio.gather(*server_tasks, return_exceptions=True)
            print("Servers stopped.")


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nExiting.")


if __name__ == "__main__":
    # Launched as `python serve.py`, this module is registered as "__main__".
    # The orchestrator later runs `from serve import worker_registry,
    # central_hub, pending_tasks` at dispatch time; without this alias Python
    # re-imports and RE-EXECUTES serve.py as a separate "serve" module with its
    # own empty CentralHub, so the in-process registry the orchestrator reads is
    # always empty (the distributed in-memory fast path is unreachable) and the
    # dashboard's worker view is permanently blank. Alias so `import serve`
    # returns this same, already-initialized running module.
    sys.modules["serve"] = sys.modules[__name__]
    # mac branch: uvloop as the event loop for the hub + agent servers.
    # serve.py owns its loop via asyncio.run (uvicorn's loop="auto" only
    # applies when uvicorn owns startup), so install the policy explicitly.
    # Guarded: uvloop has no Windows build, and its absence must never stop
    # the servers from booting.
    try:
        import uvloop

        uvloop.install()
    except ImportError:
        pass
    main()
