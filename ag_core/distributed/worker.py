import asyncio
import hmac
import time
import json
import os
from collections import deque
from typing import Dict, List, Optional, Any
from ag_core.utils.jwt import encode_jwt

# accepted role id -> (agent module, agent class, provider-factory role).
# Derived from the shared factory tables (ag_core.agent_factory) — same shape
# and exactly the same accepted-id set as the historical hand-written map:
# canonical ids plus the long service aliases ("claude_architect",
# "codex_reviewer", ...) that remote hubs may dispatch with. Legacy role ids
# ("grok", "grok_researcher") are folded in by canonical_role() at the lookup
# site. Module-level: execute_task used to rebuild this dict on every task.
from ag_core.agent_factory import AGENT_CLASSES as _AGENT_CLASSES
from ag_core.agent_factory import LONG_ROLE_ALIASES as _LONG_ROLE_ALIASES

ROLE_AGENT_MAP = {
    **{role: (mod, cls, role) for role, (mod, cls) in _AGENT_CLASSES.items()},
    **{
        alias: (*_AGENT_CLASSES[role], role)
        for alias, role in _LONG_ROLE_ALIASES.items()
    },
}


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


class ClientWorker:
    def __init__(self, worker_id: str, roles: List[str], api_key: Optional[str] = None):
        self.worker_id = worker_id
        self.roles = roles
        self._api_key_override = api_key
        self.active_tasks = set()
        self._status = "idle"
        self._current_task = None
        self.network = None
        self.heartbeat_task = None
        self.heartbeat_interval = 0.05
        self.running = False
        # Bounded task-history ring: retained full task_data (prompts + context)
        # for every task ever accepted, growing without limit on a long-lived
        # worker. Keep only the most recent N (GENIUS_WORKER_TASK_HISTORY).
        self.received_tasks = deque(
            maxlen=max(1, int(os.environ.get("GENIUS_WORKER_TASK_HISTORY") or 500))
        )
        self.tasks_completed = 0
        self.tasks_failed = 0
        self.ws = None
        self.running_tasks = {}

    @property
    def api_key(self) -> str:
        if self._api_key_override is not None:
            return self._api_key_override
        from ag_core.config import load_config

        try:
            config = load_config()
            val = config.skill_api_key or os.getenv("SKILL_API_KEY", "")
            if val:
                return val
        except Exception:
            pass
        return os.getenv("SKILL_API_KEY", "")

    @property
    def status(self) -> str:
        return "busy" if self.active_tasks else "idle"

    @status.setter
    def status(self, value):
        self._status = value
        if value == "idle":
            self.active_tasks.clear()

    @property
    def current_task(self) -> Optional[str]:
        if self.active_tasks:
            return list(self.active_tasks)[-1]
        return None

    @current_task.setter
    def current_task(self, value):
        self._current_task = value
        if value is None:
            self.active_tasks.clear()

    def set_network(self, network):
        self.network = network
        network.register_worker(self.worker_id, self)

    def create_headers(self, payload: Any) -> Dict[str, str]:
        import time
        import uuid
        from ag_core.utils.security import calculate_checksum

        checksum = calculate_checksum(payload, self.api_key)
        # X-Timestamp/X-Nonce let the hub enforce anti-replay when
        # GENIUS_HUB_REPLAY_PROTECTION is on; harmless when it's off.
        return {
            "X-API-Key": self.api_key,
            "X-Payload-SHA256": checksum,
            "X-Timestamp": str(time.time()),
            "X-Nonce": uuid.uuid4().hex,
        }

    async def register(self) -> tuple[int, Any]:
        payload = {"worker_id": self.worker_id, "roles": self.roles}
        headers = self.create_headers(payload)
        return await self.network.send_to_hub("/register", payload, headers)

    async def send_heartbeat(self) -> tuple[int, Any]:
        payload = {"worker_id": self.worker_id}
        headers = self.create_headers(payload)
        return await self.network.send_to_hub("/heartbeat", payload, headers)

    async def start_heartbeats(self):
        self.running = True
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop_heartbeats(self):
        self.running = False
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            try:
                await self.heartbeat_task
            except asyncio.CancelledError:
                pass
            self.heartbeat_task = None

    async def aclose(self):
        """Cancel and await every background task this worker owns — the
        heartbeat loop plus any in-flight ``execute_task`` coroutines. Without
        this, a worker left mid-task (e.g. a still-sleeping dispatched task when
        a test ends) leaks a pending task that the event loop destroys on close,
        surfacing as a PytestUnraisableExceptionWarning. Idempotent and safe to
        call more than once."""
        await self.stop_heartbeats()
        # Snapshot: execute_task's finally clause pops running_tasks as each
        # task settles, mutating the dict while we iterate.
        running = list(self.running_tasks.values())
        for t in running:
            t.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self.running_tasks.clear()
        self.active_tasks.clear()

    async def _heartbeat_loop(self):
        while self.running:
            if self.ws is not None:
                hb_msg = {"type": "heartbeat", "worker_id": self.worker_id}
                await self.ws.send(json.dumps(hb_msg))
            else:
                try:
                    status_code, body = await self.send_heartbeat()
                    if status_code == 404:
                        print(
                            "[Worker] Heartbeat 404: not registered. Re-registering..."
                        )
                        await self.register()
                except Exception:
                    pass
            await asyncio.sleep(self.heartbeat_interval)

    async def handle_request(
        self, endpoint: str, payload: Any, headers: Dict[str, str]
    ) -> tuple[int, Any, Dict[str, str]]:
        # Timing-safe comparison, fail-closed on a missing key on either side
        # (mirrors CentralHub.verify_auth).
        provided = headers.get("X-API-Key")
        if (
            not provided
            or not self.api_key
            or not hmac.compare_digest(str(provided), str(self.api_key))
        ):
            return 401, {"error": "Unauthorized"}, {}

        # Verify Checksum
        from ag_core.utils.security import verify_checksum

        if not verify_checksum(payload, headers.get("X-Payload-SHA256"), self.api_key):
            return 400, {"error": "Bad Checksum"}, {}

        if endpoint == "/run_task":
            if self.status == "busy":
                return 409, {"error": "Worker is busy"}, {}
            task_id = payload.get("task_id")
            task_data = payload.get("task_data")
            self.active_tasks.add(task_id)
            self.received_tasks.append((task_id, task_data))

            t = asyncio.create_task(self.execute_task(task_id, task_data))
            self.running_tasks[task_id] = t
            return 200, {"status": "started"}, {}
        elif endpoint == "/cancel":
            task_id = payload.get("task_id")
            if task_id in self.running_tasks:
                self.running_tasks[task_id].cancel()
            # Per-task cleanup only: the old blanket `status = "idle"` went
            # through the setter that clears active_tasks WHOLESALE, wiping
            # the liveness of every other in-flight task. The cancelled
            # task's own execute_task.finally completes the cleanup when the
            # cancellation lands.
            self.active_tasks.discard(task_id)
            if not self.active_tasks:
                self._status = "idle"
                self._current_task = None
            return 200, {"status": "cancelled"}, {}

        return 404, {"error": "Endpoint not found"}, {}

    async def execute_task(self, task_id: str, task_data: Any):
        self.active_tasks.add(task_id)
        status = "failed"
        result = {"error": "Unknown execution error"}
        try:
            if isinstance(task_data, dict) and "role" in task_data:
                role = task_data.get("role")
                prompt = task_data.get("prompt")
                context = task_data.get("context", {})
                effort = task_data.get("effort")

                from ag_core.provider_factory import canonical_role

                normalized_role = canonical_role(role)
                if normalized_role not in ROLE_AGENT_MAP:
                    status = "failed"
                    result = {
                        "error": f"Role '{role}' is not supported by this worker."
                    }
                    self.tasks_failed += 1
                else:
                    _, _, factory_role = ROLE_AGENT_MAP[normalized_role]
                    try:
                        from ag_core.agent_factory import build_agent

                        # The stateless bundle mirrors the skill-server
                        # hardening: no VectorMemory — its lazy
                        # sentence-transformers model load / O(N) query runs
                        # synchronously on this worker's event loop and would
                        # stall the heartbeat, risking hub-side eviction and
                        # duplicate dispatch — and stateless so a codex task
                        # doesn't run the host's pytest or write files on the
                        # worker.
                        agent = build_agent(factory_role, stateless=True)

                        # Only pass effort when set, so the common (None) path
                        # is byte-identical to a plain run(prompt, context_data)
                        # call — agent mocks without an effort param still work.
                        run_kwargs = {"prompt": prompt, "context_data": context}
                        if effort:
                            run_kwargs["effort"] = effort
                        output = await agent.run(**run_kwargs)
                        status = "completed"
                        result = {"output": output}
                        self.tasks_completed += 1
                    except Exception as e:
                        status = "failed"
                        result = {"error": f"Agent run execution failed: {str(e)}"}
                        self.tasks_failed += 1
            else:
                sleep_dur = 0.01
                if isinstance(task_data, dict) and "sleep" in task_data:
                    sleep_dur = float(task_data["sleep"])
                elif isinstance(task_data, str) and "sleep:" in task_data:
                    try:
                        sleep_dur = float(task_data.split("sleep:")[1])
                    except Exception:
                        pass
                await asyncio.sleep(sleep_dur)
                if "fail" in str(task_data):
                    status = "failed"
                    result = {"error": "Task execution failed due to instruction"}
                    self.tasks_failed += 1
                else:
                    status = "completed"
                    result = {"output": f"Processed: {task_data}"}
                    self.tasks_completed += 1
        except asyncio.CancelledError:
            status = "failed"
            result = {"error": "cancelled"}
            self.tasks_failed += 1
            raise
        except Exception as e:
            status = "failed"
            result = {"error": f"Agent execution failed: {str(e)}"}
            self.tasks_failed += 1
        finally:
            self.running_tasks.pop(task_id, None)
            self.active_tasks.discard(task_id)
            # Per-task cleanup only. The old unconditional `status = "idle"` /
            # `current_task = None` went through setters that CLEAR active_tasks
            # wholesale — one settling (or stale cancelled) task wiped the
            # liveness of every other in-flight task, including tasks accepted
            # after a reconnect. Reset the mirrors only when nothing is active.
            if not self.active_tasks:
                self._status = "idle"
                self._current_task = None

            # Report result
            async def report():
                from ag_core.utils.security import calculate_checksum

                if self.ws is not None:
                    checksum = calculate_checksum(result, self.api_key)
                    payload = {
                        "type": "result",
                        "task_id": task_id,
                        "worker_id": self.worker_id,
                        "status": status,
                        "result": result,
                        "checksum": checksum,
                    }
                    try:
                        await self.ws.send(json.dumps(payload))
                    except Exception as e:
                        print(f"[Worker] WS Result reporting error: {e}")
                else:
                    checksum = calculate_checksum(result, self.api_key)
                    payload = {
                        "task_id": task_id,
                        "worker_id": self.worker_id,
                        "status": status,
                        "result": result,
                        "checksum": checksum,
                    }

                    headers = self.create_headers(payload)
                    backoff = 0.005
                    for attempt in range(5):
                        try:
                            status_code, body = await self.network.send_to_hub(
                                "/report_result", payload, headers
                            )
                            if status_code == 200:
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(backoff)
                        backoff *= 2

            report_coro = report()
            try:
                await asyncio.shield(report_coro)
            except Exception as e:
                print(f"[Worker] Shielded report failed: {e}")
                # shield() may fail before wrapping the coroutine in a task
                # (e.g. no running event loop at shutdown); close it so it
                # doesn't leak a "coroutine was never awaited" warning.
                report_coro.close()

    def generate_jwt(self) -> str:
        # Use the same resolved key as checksums (config.skill_api_key or
        # SKILL_API_KEY). Fail loudly instead of silently signing with an empty
        # secret, which previously left distributed auth effectively disabled.
        secret = self.api_key
        if not secret:
            raise RuntimeError(
                "SKILL_API_KEY (or config.skill_api_key) must be set to "
                "authenticate distributed workers"
            )
        payload = {"sub": self.worker_id, "exp": int(time.time() + 300)}
        return encode_jwt(payload, secret)

    async def run_production_loop(self, hub_ip: str, hub_port: int):
        import websockets

        backoff = 1.0
        max_backoff = 60.0
        backoff_factor = 2.0

        # wss:// when the hub sits behind TLS (GENIUS_HUB_TLS truthy): the ws://
        # transport sends the JWT — and every prompt/result — readable to
        # anyone on the network path.
        scheme = "wss" if _truthy(os.environ.get("GENIUS_HUB_TLS")) else "ws"

        while True:
            token = self.generate_jwt()
            safe_uri = f"{scheme}://{hub_ip}:{hub_port}/ws/connect"
            # The JWT rides the Authorization header by default: query strings
            # land in access/proxy logs, headers generally do not. Hubs older
            # than the header support need GENIUS_WS_TOKEN_QUERY=1 (the token
            # then returns to the query string — never log that URI).
            if _truthy(os.environ.get("GENIUS_WS_TOKEN_QUERY")):
                uri = f"{safe_uri}?token={token}"
                connect_kwargs = {}
            else:
                uri = safe_uri
                connect_kwargs = {
                    "additional_headers": {"Authorization": f"Bearer {token}"}
                }
            try:
                print(f"[Worker] Connecting to {safe_uri} ...")
                async with websockets.connect(uri, **connect_kwargs) as websocket:
                    self.ws = websocket
                    # NB: do NOT reset the reconnect backoff here. A socket that
                    # merely opened is not a stable session — a hub that accepts
                    # the connection but then rejects registration (e.g.
                    # max-workers) would otherwise reset backoff to 1s every
                    # cycle and produce a ~1-2s reconnect storm. Backoff is reset
                    # only once registration is CONFIRMED (see the "registered"
                    # frame handler below).
                    print(f"[Worker] Connected! Registering roles: {self.roles}")

                    # Register
                    reg_payload = {
                        "type": "register",
                        "worker_id": self.worker_id,
                        "roles": self.roles,
                    }
                    await websocket.send(json.dumps(reg_payload))

                    self.heartbeat_interval = 10.0
                    self.running = True
                    hb_task = asyncio.create_task(self._heartbeat_loop())

                    async def read_msg_loop():
                        nonlocal backoff
                        async for message in websocket:
                            # One malformed frame must not escape the loop:
                            # an uncaught parse error tears the connection
                            # down and the reconnect path cancels every
                            # in-flight task.
                            try:
                                data = json.loads(message)
                            except (ValueError, TypeError):
                                print("[Worker] Ignoring non-JSON frame from hub")
                                continue
                            if not isinstance(data, dict):
                                print("[Worker] Ignoring non-object frame from hub")
                                continue
                            msg_type = data.get("type")
                            # Never log task_data: it carries full prompts and
                            # context at unbounded volume on the hot path.
                            print(
                                f"[Worker] Received from hub: type={msg_type} "
                                f"task_id={data.get('task_id')}"
                            )
                            if (
                                msg_type == "registered"
                                and data.get("status") == "success"
                            ):
                                # Registration confirmed: this is a stable
                                # session, so it is finally safe to reset the
                                # reconnect backoff. A hub that accepts the
                                # socket but rejects registration never sends
                                # this frame, so it keeps backing off.
                                backoff = 1.0
                                continue

                            if (
                                msg_type == "error"
                                and data.get("error") == "not_registered"
                            ):
                                print(
                                    "[Worker] Received not_registered error. Re-registering..."
                                )
                                reg_payload = {
                                    "type": "register",
                                    "worker_id": self.worker_id,
                                    "roles": self.roles,
                                }
                                await websocket.send(json.dumps(reg_payload))
                                continue

                            if msg_type == "cancel":
                                task_id = data.get("task_id")
                                print(
                                    f"[Worker] Received cancel message for task {task_id}"
                                )
                                if task_id in self.running_tasks:
                                    self.running_tasks[task_id].cancel()
                                # Same per-task cleanup as the HTTP /cancel
                                # handler: never clear other tasks' liveness.
                                self.active_tasks.discard(task_id)
                                if not self.active_tasks:
                                    self._status = "idle"
                                    self._current_task = None
                                continue

                            if msg_type in ("run_task", "dispatch"):
                                task_id = data.get("task_id")
                                task_data = data.get("task_data")
                                checksum = data.get("checksum")

                                from ag_core.utils.security import (
                                    calculate_checksum,
                                    verify_checksum,
                                )

                                if self.status == "busy":
                                    print(
                                        "[Worker] Worker is busy, rejecting dispatch!"
                                    )
                                    err_res = {"error": "Worker is busy"}
                                    err_chk = calculate_checksum(err_res, self.api_key)
                                    payload = {
                                        "type": "result",
                                        "task_id": task_id,
                                        "worker_id": self.worker_id,
                                        "status": "failed",
                                        "result": err_res,
                                        "checksum": err_chk,
                                    }
                                    await websocket.send(json.dumps(payload))
                                    continue

                                if not checksum:
                                    print("[Worker] Missing checksum in dispatch!")
                                    err_res = {
                                        "error": "Missing checksum validation on worker node."
                                    }
                                    err_chk = calculate_checksum(err_res, self.api_key)
                                    payload = {
                                        "type": "result",
                                        "task_id": task_id,
                                        "worker_id": self.worker_id,
                                        "status": "failed",
                                        "result": err_res,
                                        "checksum": err_chk,
                                    }
                                    await websocket.send(json.dumps(payload))
                                    continue

                                if not verify_checksum(
                                    task_data, checksum, self.api_key
                                ):
                                    print(
                                        f"[Worker] Checksum mismatch! Expected {checksum}"
                                    )
                                    err_res = {
                                        "error": "Bad Checksum validation on worker node."
                                    }
                                    err_chk = calculate_checksum(err_res, self.api_key)
                                    payload = {
                                        "type": "result",
                                        "task_id": task_id,
                                        "worker_id": self.worker_id,
                                        "status": "failed",
                                        "result": err_res,
                                        "checksum": err_chk,
                                    }

                                    await websocket.send(json.dumps(payload))
                                    continue

                                self.active_tasks.add(task_id)
                                self.received_tasks.append((task_id, task_data))
                                t = asyncio.create_task(
                                    self.execute_task(task_id, task_data)
                                )
                                self.running_tasks[task_id] = t

                    read_task = asyncio.create_task(read_msg_loop())

                    try:
                        done, pending = await asyncio.wait(
                            [hb_task, read_task], return_when=asyncio.FIRST_COMPLETED
                        )
                        for task in done:
                            if task.exception() is not None:
                                raise task.exception()
                    finally:
                        self.running = False
                        hb_task.cancel()
                        read_task.cancel()
                        await asyncio.gather(hb_task, read_task, return_exceptions=True)
                        self.ws = None
                        # Cancel in-flight tasks AND await them to completion:
                        # each execute_task's finally (result reporting + state
                        # cleanup) must finish BEFORE the reconnect loop can
                        # accept new work. A cancelled-but-not-awaited task's
                        # finally used to run after reconnect and wipe the
                        # state of freshly-accepted tasks.
                        inflight = list(self.running_tasks.values())
                        for task in inflight:
                            task.cancel()
                        if inflight:
                            await asyncio.gather(*inflight, return_exceptions=True)
                        self.running_tasks.clear()
                        self.active_tasks.clear()
            except Exception as e:
                print(f"[Worker] Connection failed: {e}")

            # Task 8: add random jitter (0 to 1.0 seconds) to backoff
            import random

            sleep_time = backoff + random.uniform(0, 1.0)
            print(f"[Worker] Reconnecting in {sleep_time:.1f}s...")
            await asyncio.sleep(sleep_time)
            backoff = min(backoff * backoff_factor, max_backoff)
