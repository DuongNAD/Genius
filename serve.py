#!/usr/bin/env python3
import argparse
import asyncio
import importlib.util
import os
import sys
import time
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from ag_core.utils.jwt import decode_jwt

# Add project root to sys.path
root_dir = os.path.dirname(os.path.abspath(__file__))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from orchestrator import run_pipeline, run_e2e_pipeline

from ag_core.distributed.hub import CentralHub
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
        async with self.lock:
            now = time.time()
            timeout = central_hub.config.get("heartbeat_timeout", 30.0)
            for worker_id, info in list(self.workers.items()):
                worker_roles = [r.lower() for r in info.get("roles", [])]
                role_matched = False
                for r in worker_roles:
                    if (
                        r == role.lower()
                        or (role.lower() == "grok" and "grok" in r)
                        or (role.lower() == "claude" and "claude" in r)
                        or (role.lower() == "codex" and "codex" in r)
                        or (role.lower() == "tester" and "tester" in r)
                        or (role.lower() == "security" and "security" in r)
                        or (role.lower() == "devops" and "devops" in r)
                    ):
                        role_matched = True
                        break
                if role_matched and info.get("status") == "idle":
                    if now - info.get("last_heartbeat", 0) < timeout:
                        info["status"] = "busy"
                        return worker_id
            return None

    async def register(self, worker_id: str, roles: list, ws, status: str = "idle"):
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
        await central_hub.handle_request("/register", payload, headers)
        if worker_id in central_hub.workers:
            central_hub.workers[worker_id]["ws"] = ws
            central_hub.workers[worker_id]["status"] = current_status

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
    try:
        central_hub.config["heartbeat_timeout"] = timeout_sec
        while True:
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
        pass
    except Exception:
        pass


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


@app.post("/{path:path}")
async def hub_http_route(path: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    headers = dict(request.headers)

    if payload.get("stream") or request.query_params.get("stream") == "true":
        from fastapi.responses import StreamingResponse

        async def stream_generator():
            status_code, body, resp_headers = await central_hub.handle_request(
                "/" + path, payload, headers
            )
            if isinstance(body, dict):
                yield json.dumps(body)
            else:
                yield str(body)

        return StreamingResponse(stream_generator(), media_type="application/json")

    status_code, body, resp_headers = await central_hub.handle_request(
        "/" + path, payload, headers
    )
    return Response(
        content=json.dumps(body),
        status_code=status_code,
        media_type="application/json",
        headers=resp_headers,
    )


IS_DISTRIBUTED = False


@app.websocket("/ws/connect")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    import sys

    is_pytest = "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST") is not None
    if not IS_DISTRIBUTED and not is_pytest:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=404, detail="WebSocket not enabled in local mode"
        )

    # Resolve the JWT secret the same way the hub/worker do, so tokens verify
    # consistently. Fail closed (don't accept connections) if no secret is
    # configured in production rather than silently using an empty secret.
    secret = central_hub.api_key or os.getenv("SKILL_API_KEY", "")
    if not secret and not is_pytest:
        await websocket.accept()
        await websocket.close(code=4001)
        return
    try:
        payload = decode_jwt(token, secret)
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

                registered_worker_id = worker_id
                await worker_registry.register(
                    worker_id, roles, websocket, status="idle"
                )
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
                    await central_hub.handle_request("/report_result", payload, headers)

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


async def start_hub_server(port: int):
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


ROUTING_TABLE = {
    "/research": ("grok", 8001),
    "/summarize": ("grok", 8001),
    "/fact-check": ("grok", 8001),
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


def normalize_roles(roles_str: str) -> list:
    raw_roles = [r.strip().lower() for r in roles_str.split(",") if r.strip()]
    normalized = []
    for r in raw_roles:
        if r in ["1", "grok", "grok_researcher", "grok api", "grok-api"]:
            normalized.append("grok")
        elif r in ["2", "claude", "claude_architect", "claude api", "claude-api"]:
            normalized.append("claude")
        elif r in ["3", "codex", "codex_reviewer", "codex api", "codex-api"]:
            normalized.append("codex")
        elif r in ["4", "tester", "tester_agent", "tester api", "tester-api"]:
            normalized.append("tester")
        elif r in ["5", "orchestrator"]:
            normalized.append("orchestrator")
        elif r in [
            "6",
            "dashboard",
            "web dashboard",
            "web-dashboard",
            "dashboard api",
            "dashboard-api",
        ]:
            normalized.append("dashboard")
        elif r in ["7", "security", "security_agent", "security api", "security-api"]:
            normalized.append("security")
        elif r in ["8", "devops", "devops_agent", "devops api", "devops-api"]:
            normalized.append("devops")
    return normalized


def interactive_prompt() -> list:
    print("=== Antigravity 2.0 Unified Startup Menu ===")
    print("Select specific roles/agents this machine will run (comma-separated):")
    print("1. grok         - Grok Researcher API (Port 8001)")
    print("2. claude       - Claude Architect API (Port 8002)")
    print("3. codex        - Codex Reviewer API (Port 8003)")
    print("4. tester       - Tester Agent API (Port 8004)")
    print("5. orchestrator - Launch Orchestrator Workflow")
    print("6. dashboard    - Web Dashboard (Port 8080)")
    print("7. security     - Security Agent API (Port 8005)")
    print("8. devops       - DevOps Agent API (Port 8006)")
    try:
        choice = input(
            "Enter selection (e.g. 'grok,claude' or '5' or '1,2,3,4,5,6,7,8'): "
        ).strip()
        return normalize_roles(choice)
    except KeyboardInterrupt:
        print("\nExiting.")
        sys.exit(0)


def get_api_app(role: str):
    if role == "grok":
        path = os.path.join(root_dir, ".agents", "skills", "grok_researcher", "api.py")
    elif role == "claude":
        path = os.path.join(root_dir, ".agents", "skills", "claude_architect", "api.py")
    elif role == "codex":
        path = os.path.join(root_dir, ".agents", "skills", "codex_reviewer", "api.py")
    elif role == "tester":
        path = os.path.join(root_dir, ".agents", "skills", "tester_agent", "api.py")
    elif role == "security":
        path = os.path.join(root_dir, ".agents", "skills", "security_agent", "api.py")
    elif role == "devops":
        path = os.path.join(root_dir, ".agents", "skills", "devops_agent", "api.py")
    elif role == "dashboard":
        path = os.path.join(root_dir, "dashboard.py")
    else:
        raise ValueError(f"Unknown role: {role}")

    spec = importlib.util.spec_from_file_location(f"{role}_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


async def start_server(role: str, port: int):
    app = get_api_app(role)

    def _make_server(bind_port: int) -> uvicorn.Server:
        config = uvicorn.Config(app, host="0.0.0.0", port=bind_port, log_level="info")
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
    except OSError:
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

    registry_path = os.environ.get(
        "GENIUS_SERVICE_REGISTRY",
        os.path.join(root_dir, ".agents", "service_registry.json"),
    )
    os.makedirs(os.path.dirname(registry_path), exist_ok=True)

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


async def main_async():
    parser = argparse.ArgumentParser(
        description="Unified Startup Menu for Genius Microservices"
    )
    parser.add_argument(
        "--roles",
        default=None,
        help="Comma-separated roles to run (grok, claude, codex, tester, orchestrator, dashboard)",
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
        choices=["sequential", "e2e"],
        default="sequential",
        help="Pipeline type to execute",
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
    args = parser.parse_args()

    auto_pilot = getattr(args, "auto_pilot", False) is True
    interactive = getattr(args, "interactive", False) is True
    distributed = getattr(args, "distributed", False) is True

    global IS_DISTRIBUTED
    IS_DISTRIBUTED = distributed

    if auto_pilot:
        selected_roles = [
            "grok",
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
        first_word = prompt.strip().split()[0] if prompt.strip() else ""
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
    if distributed:
        print(f"Starting central hub on port {args.hub_port}")
        server_tasks.append(asyncio.create_task(start_hub_server(args.hub_port)))

    # Start requested API servers
    if "grok" in selected_roles:
        server_tasks.append(asyncio.create_task(start_server("grok", 8001)))
    if "claude" in selected_roles:
        server_tasks.append(asyncio.create_task(start_server("claude", 8002)))
    if "codex" in selected_roles:
        server_tasks.append(asyncio.create_task(start_server("codex", 8003)))
    if "tester" in selected_roles:
        server_tasks.append(asyncio.create_task(start_server("tester", 8004)))
    if "security" in selected_roles:
        server_tasks.append(asyncio.create_task(start_server("security", 8005)))
    if "devops" in selected_roles:
        server_tasks.append(asyncio.create_task(start_server("devops", 8006)))
    if "dashboard" in selected_roles:
        server_tasks.append(asyncio.create_task(start_server("dashboard", 8080)))

    # If prompt is provided or orchestrator is explicitly selected
    if "orchestrator" in selected_roles or prompt:
        if server_tasks:
            print("Waiting 1 second for API servers to initialize...")
            await asyncio.sleep(1.0)

        if not prompt:
            if auto_pilot:
                print("Error: Prompt is required under auto-pilot mode.")
                for task in server_tasks:
                    task.cancel()
                if server_tasks:
                    await asyncio.gather(*server_tasks, return_exceptions=True)
                return
            try:
                prompt = input("Enter prompt for orchestrator: ").strip()
            except KeyboardInterrupt:
                print("\nExiting.")
                return

        if not prompt:
            print("Error: Prompt is required to run the orchestrator.")
            return

        pipeline_run = False
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
                await run_pipeline(prompt, **pipeline_kwargs)
            print("Orchestrator pipeline completed successfully.")
        except Exception as e:
            print(f"Orchestrator pipeline failed: {e}")
        finally:
            if pipeline_run:
                for task in server_tasks:
                    task.cancel()
                if server_tasks:
                    await asyncio.gather(*server_tasks, return_exceptions=True)
                return

    # If we started servers, we await them to run continuously
    if server_tasks:
        try:
            print("FastAPI servers are running. Press Ctrl+C to stop.")
            await asyncio.gather(*server_tasks)
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("Stopping servers...")
        finally:
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
    main()
