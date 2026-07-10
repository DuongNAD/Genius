import asyncio
import time
import os
import uuid
from collections import OrderedDict
from typing import Dict, Optional, Any


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


class TaskQueue(list):
    def qsize(self) -> int:
        return len(self)

    def empty(self) -> bool:
        return len(self) == 0

    def put_nowait(self, item: Any):
        self.append(item)

    async def put(self, item: Any):
        self.append(item)

    def get_nowait(self) -> Any:
        if self.empty():
            raise asyncio.QueueEmpty()
        return self.pop(0)

    def task_done(self):
        pass


class BoundedTasks(dict):
    """Task store bounded at ~MAX_TASKS entries.

    Eviction only ever removes TERMINAL (completed/failed) records: dropping
    a pending/running record would leak its client future, 404 its
    /report_result and 404 status polls. Live records are bounded upstream
    instead — /dispatch refuses new work (503) once MAX_QUEUED_TASKS are
    already waiting, so an all-live overflow cannot build up.
    """

    MAX_TASKS = 10000

    def __setitem__(self, key, value):
        if len(self) >= self.MAX_TASKS:
            to_evict = []
            for t_id, task in self.items():
                if task.get("status") in ("completed", "failed"):
                    to_evict.append(t_id)
            for t_id in to_evict:
                self.pop(t_id, None)
        super().__setitem__(key, value)


class CentralHub:
    # Max tasks allowed to wait in task_queue before /dispatch returns 503
    # (see BoundedTasks: live records must stay bounded upstream).
    MAX_QUEUED_TASKS = 1000

    def __init__(self, api_key: Optional[str] = None):
        self._api_key_override = api_key
        self.workers: Dict[str, Dict[str, Any]] = {}
        self.tasks = BoundedTasks()
        self.network = None
        self.task_queue = TaskQueue()
        self.task_counter = 0
        from ag_core.utils.cli_runner import cli_timeout

        self.config = {
            "max_workers": 10,
            "heartbeat_timeout": 0.5,
            # A task on a live, heartbeating worker legitimately runs as long as
            # the agent CLI (cli_timeout, default 600s), and the orchestrator
            # waits cli_timeout+60 for it. The old 60s default made the sweeper
            # reap healthy long-running agent tasks (cancelling them mid-flight),
            # so distributed mode failed any real LLM call over a minute. Set the
            # backstop safely beyond the worker's own CLI ceiling; tests still
            # override this with a small value to exercise expiry.
            "task_timeout": cli_timeout() + 120.0,
        }
        self._sweeper_task: Optional[asyncio.Task] = None
        self._sweeper_running = False
        self.lock = asyncio.Lock()
        # Bounded set of recently-seen request nonces for opt-in replay
        # protection on the authenticated HTTP path (GENIUS_HUB_REPLAY_PROTECTION).
        self._seen_nonces: "OrderedDict[str, float]" = OrderedDict()

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

    @api_key.setter
    def api_key(self, value: str):
        self._api_key_override = value

    def start_sweeper(self):
        if not self._sweeper_running:
            try:
                asyncio.get_running_loop()
                self._sweeper_running = True
                self._sweeper_task = asyncio.create_task(self._sweeper_loop())
            except RuntimeError:
                pass

    def stop_sweeper(self):
        self._sweeper_running = False
        if self._sweeper_task:
            self._sweeper_task.cancel()
            self._sweeper_task = None

    async def _sweeper_loop(self):
        while self._sweeper_running:
            try:
                await asyncio.sleep(1.0)  # Check every 1.0s to reduce CPU footprint
                await self.sweep()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def sweep(self):
        now = time.time()
        # 1. Prune stale workers (with a 10ms grace margin for scheduling jitter)
        dead_workers = []
        stale_websockets = []
        cancel_notices = []  # (ws_or_None, worker_id, task_id), sent post-lock

        async with self.lock:
            for w_id, w_info in list(self.workers.items()):
                if (
                    now - w_info["last_heartbeat"]
                    >= self.config["heartbeat_timeout"] - 0.01
                ):
                    dead_workers.append(w_id)
                    ws = w_info.get("ws")
                    if ws:
                        stale_websockets.append(ws)

            for w_id in dead_workers:
                # Fail any running tasks assigned to this dead worker
                for t_id, t_info in list(self.tasks.items()):
                    if t_info["worker_id"] == w_id and t_info["status"] == "running":
                        t_info["status"] = "failed"
                        t_info["result"] = {"error": "Worker disconnected"}
                # Remove from workers
                if w_id in self.workers:
                    del self.workers[w_id]
                if self.network:
                    self.network.unregister_worker(w_id)

            # 2. Handle expired tasks (running too long)
            expired_tasks = []
            for t_id, t_info in list(self.tasks.items()):
                if t_info["status"] == "running":
                    started_at = t_info.get("started_at", t_info["created_at"])
                    if now - started_at >= self.config["task_timeout"]:
                        expired_tasks.append(t_id)

            for t_id in expired_tasks:
                t_info = self.tasks[t_id]
                w_id = t_info["worker_id"]
                t_info["status"] = "failed"
                t_info["result"] = {
                    "error": f"Task timed out after {self.config['task_timeout']}s"
                }
                if w_id and w_id in self.workers:
                    self.workers[w_id]["status"] = "idle"
                    cancel_notices.append((self.workers[w_id].get("ws"), w_id, t_id))

            if dead_workers or expired_tasks:
                await self._process_queue()

        # Network I/O strictly outside the lock block: awaiting a send on a
        # hung/half-open worker socket while holding self.lock would stall
        # every heartbeat, dispatch, report and registration hub-wide (the
        # same head-of-line hazard the stale-socket close below avoids).
        for ws in stale_websockets:
            try:
                await ws.close()
            except Exception:
                pass
        for ws, w_id, t_id in cancel_notices:
            if ws:
                try:
                    await ws.send_json({"type": "cancel", "task_id": t_id})
                except Exception:
                    pass
            elif self.network:
                try:
                    payload = {"task_id": t_id}
                    headers = self.create_headers(payload)
                    await self.network.send_to_worker(w_id, "/cancel", payload, headers)
                except Exception:
                    pass

    def set_network(self, network):
        self.network = network
        network.set_hub(self)
        self.start_sweeper()

    def verify_auth(self, headers: Dict[str, str]) -> bool:
        auth_header = headers.get("X-API-Key") or headers.get("x-api-key")
        if not auth_header and hasattr(headers, "items"):
            auth_header = next(
                (v for k, v in headers.items() if k.lower() == "x-api-key"), None
            )
        if not auth_header or not self.api_key:
            return False
        import hmac

        return hmac.compare_digest(str(auth_header), str(self.api_key))

    def _verify_replay(self, headers: Dict[str, str]) -> bool:
        """Opt-in anti-replay for the HTTP path (GENIUS_HUB_REPLAY_PROTECTION).

        Requires a fresh ``X-Timestamp`` (within the window) and an unseen
        ``X-Nonce``. Off by default so existing clients/tests are unchanged.
        NOTE: the HTTP path transmits the raw shared secret in ``X-API-Key``,
        so a party who captures a request already holds the credential; this
        control only helps alongside a transport that hides the secret (TLS)
        or a future move to signature-based auth. It is not a full fix on its
        own.
        """
        if not _truthy(os.getenv("GENIUS_HUB_REPLAY_PROTECTION")):
            return True

        def _get(name: str):
            v = headers.get(name) or headers.get(name.lower())
            if not v and hasattr(headers, "items"):
                v = next(
                    (val for k, val in headers.items() if k.lower() == name.lower()),
                    None,
                )
            return v

        ts_raw = _get("X-Timestamp")
        nonce = _get("X-Nonce")
        if not ts_raw or not nonce:
            return False
        try:
            ts = float(ts_raw)
        except (TypeError, ValueError):
            return False

        now = time.time()
        try:
            window = float(os.getenv("GENIUS_HUB_REPLAY_WINDOW") or 300.0)
        except (TypeError, ValueError):
            window = 300.0
        if abs(now - ts) > window:
            return False

        # Drop nonces older than the window, then reject a reused one.
        cutoff = now - window
        while self._seen_nonces:
            oldest_key = next(iter(self._seen_nonces))
            if self._seen_nonces[oldest_key] < cutoff:
                self._seen_nonces.pop(oldest_key, None)
            else:
                break
        if nonce in self._seen_nonces:
            return False
        self._seen_nonces[nonce] = now
        # Hard cap so a flood of unique nonces can't grow the map unbounded.
        while len(self._seen_nonces) > 10000:
            self._seen_nonces.popitem(last=False)
        return True

    def verify_checksum(self, payload: Any, headers: Dict[str, str]) -> bool:
        checksum = headers.get("X-Payload-SHA256") or headers.get("x-payload-sha256")
        if not checksum and hasattr(headers, "items"):
            checksum = next(
                (v for k, v in headers.items() if k.lower() == "x-payload-sha256"), None
            )
        if not checksum:
            return False
        from ag_core.utils.security import verify_checksum

        return verify_checksum(payload, checksum, self.api_key)

    def check_liveness(self):
        # Keep this for backward compatibility in tests
        now = time.time()
        dead_workers = []
        for w_id, w_info in list(self.workers.items()):
            # Allow a 10ms grace margin for timing/scheduler jitter
            if (
                now - w_info["last_heartbeat"]
                >= self.config["heartbeat_timeout"] - 0.01
            ):
                dead_workers.append(w_id)

        for w_id in dead_workers:
            for t_id, t_info in list(self.tasks.items()):
                if t_info["worker_id"] == w_id and t_info["status"] == "running":
                    t_info["status"] = "pending"
                    t_info["worker_id"] = None
                    if t_id not in self.task_queue:
                        self.task_queue.append(t_id)
            if w_id in self.workers:
                del self.workers[w_id]
            if self.network:
                self.network.unregister_worker(w_id)

    async def handle_request(
        self, endpoint: str, payload: Any, headers: Dict[str, str]
    ) -> tuple[int, Any, Dict[str, str]]:
        if not self._sweeper_running:
            self.start_sweeper()

        if not self.verify_auth(headers):
            return 401, {"error": "Unauthorized"}, {}

        if not self.verify_checksum(payload, headers):
            return 400, {"error": "Bad Checksum"}, {}

        if not self._verify_replay(headers):
            return 401, {"error": "Stale or replayed request"}, {}

        async with self.lock:
            if endpoint == "/register":
                worker_id = payload.get("worker_id")
                roles = payload.get("roles")
                if not worker_id:
                    return 400, {"error": "Missing worker_id"}, {}
                if roles is None:
                    return 400, {"error": "Missing roles"}, {}
                if (
                    len(self.workers) >= self.config["max_workers"]
                    and worker_id not in self.workers
                ):
                    return 503, {"error": "Max workers reached"}, {}

                # If worker is already registered, preserve its ws connection and state
                status = "idle"
                existing_ws = None
                if worker_id in self.workers:
                    existing_ws = self.workers[worker_id].get("ws")
                    if self.workers[worker_id].get("status") == "busy":
                        status = "busy"

                # Verify if there are active tasks assigned to it
                for t_info in self.tasks.values():
                    if (
                        t_info.get("worker_id") == worker_id
                        and t_info.get("status") == "running"
                    ):
                        status = "busy"
                        break

                self.workers[worker_id] = {
                    "roles": roles,
                    "last_heartbeat": time.time(),
                    "status": status,
                    "ws": existing_ws,
                }
                await self._process_queue()
                return 200, {"status": "registered", "worker_id": worker_id}, {}

            elif endpoint == "/heartbeat":
                worker_id = payload.get("worker_id")
                if worker_id not in self.workers:
                    return 404, {"error": "Worker not found"}, {}
                # Clamp to now, never beyond: the old max(now, last + 1ms)
                # bump let a worker heart-beating faster than 1ms/beat push
                # its timestamp into the future and outlive the sweeper
                # long after it actually died.
                self.workers[worker_id]["last_heartbeat"] = time.time()
                return 200, {"status": "alive"}, {}

            elif endpoint == "/dispatch":
                self.task_counter += 1
                task_id = f"task_{self.task_counter}"
                role = payload.get("role")
                task_data = payload.get("task_data")
                if not role or task_data is None:
                    return 400, {"error": "Missing role or task_data"}, {}

                self.tasks[task_id] = {
                    "task_id": task_id,
                    "role": role,
                    "task_data": task_data,
                    "status": "pending",
                    "result": None,
                    "created_at": time.time(),
                    "worker_id": None,
                }

                # Find eligible idle worker
                eligible_worker = self._find_eligible_worker(role)
                if eligible_worker:
                    self.workers[eligible_worker]["status"] = "busy"
                    self.tasks[task_id]["status"] = "running"
                    self.tasks[task_id]["worker_id"] = eligible_worker
                    self.tasks[task_id]["started_at"] = time.time()
                    asyncio.create_task(
                        self._dispatch_to_worker(eligible_worker, task_id)
                    )
                    return 202, {"task_id": task_id, "status": "running"}, {}
                else:
                    # Bounded backlog: an unbounded queue is how the task
                    # store could fill with live records until eviction had
                    # nothing terminal left to drop.
                    if len(self.task_queue) >= self.MAX_QUEUED_TASKS:
                        self.tasks.pop(task_id, None)
                        return (
                            503,
                            {"error": "Task queue full; retry later"},
                            {},
                        )
                    self.task_queue.append(task_id)
                    return 202, {"task_id": task_id, "status": "pending"}, {}

            elif endpoint == "/task_status":
                task_id = payload.get("task_id")
                if not task_id:
                    return 400, {"error": "Missing task_id"}, {}
                if task_id not in self.tasks:
                    return 404, {"error": "Task not found"}, {}
                task = self.tasks[task_id]
                return (
                    200,
                    {
                        "task_id": task_id,
                        "status": task["status"],
                        "result": task["result"],
                    },
                    {},
                )

            elif endpoint == "/update_config":
                new_config = payload.get("config", {})
                for k, v in new_config.items():
                    if k in ("max_workers", "heartbeat_timeout", "task_timeout"):
                        if not isinstance(v, (int, float)) or isinstance(v, bool):
                            return 400, {"error": f"Invalid type for {k}"}, {}
                        if v < 0:
                            return (
                                400,
                                {"error": f"Value for {k} cannot be negative"},
                                {},
                            )
                        # A zero timeout makes every worker/task instantly
                        # "stale" on the next sweep — an authenticated DoS.
                        # (max_workers=0 is allowed: it's the drain/pause state.)
                        if k in ("heartbeat_timeout", "task_timeout") and v <= 0:
                            return (
                                400,
                                {"error": f"Value for {k} must be positive"},
                                {},
                            )
                self.config.update(new_config)
                return 200, {"status": "config_updated", "config": self.config}, {}

            elif endpoint == "/report_result":
                task_id = payload.get("task_id")
                worker_id = payload.get("worker_id")
                status = payload.get("status")
                result = payload.get("result")
                if not task_id or worker_id not in self.workers:
                    return 400, {"error": "Invalid report parameters"}, {}
                if task_id not in self.tasks:
                    return 404, {"error": "Task not found"}, {}
                if self.tasks[task_id]["worker_id"] != worker_id:
                    return (
                        403,
                        {"error": "Forbidden: Worker ID does not match assigned task"},
                        {},
                    )

                # Settle the task AND free the worker only if the report is for
                # the task still running on it. A late report for an
                # already-terminal task (e.g. one the sweeper timed out and the
                # worker was reassigned from) must NOT flip a now-busy worker
                # back to idle — that would let the hub dispatch a second task to
                # a worker still executing its current one.
                if self.tasks[task_id]["status"] == "running":
                    self.tasks[task_id]["status"] = status
                    self.tasks[task_id]["result"] = result
                    self.workers[worker_id]["status"] = "idle"

                await self._process_queue()
                return 200, {"status": "result_acknowledged"}, {}

            elif endpoint == "/deregister":
                worker_id = payload.get("worker_id")
                if worker_id in self.workers:
                    # Fail running tasks
                    for t_id, t_info in list(self.tasks.items()):
                        if (
                            t_info["worker_id"] == worker_id
                            and t_info["status"] == "running"
                        ):
                            t_info["status"] = "failed"
                            t_info["result"] = {"error": "Worker disconnected"}
                    del self.workers[worker_id]
                    if self.network:
                        self.network.unregister_worker(worker_id)
                    await self._process_queue()
                    return 200, {"status": "deregistered"}, {}
                return 404, {"error": "Worker not found"}, {}

            elif endpoint == "/workers":
                serialized_workers = {}
                for w_id, w_info in self.workers.items():
                    serialized_workers[w_id] = {
                        "roles": w_info.get("roles"),
                        "status": w_info.get("status"),
                        "last_heartbeat": w_info.get("last_heartbeat"),
                    }
                return 200, serialized_workers, {}

            elif endpoint == "/tasks":
                return 200, dict(self.tasks), {}

            elif endpoint == "/write_workspace_file":
                path = payload.get("path")
                content = payload.get("content", "")
                import os

                # Reject absolute paths, ".." segments and drive/ADS markers,
                # then confirm the resolved target stays inside the workspace
                # root (also defeats symlink escapes).
                normalized = path.replace("\\", "/").split("/") if path else []
                if not path or os.path.isabs(path) or ":" in path or ".." in normalized:
                    return 400, {"error": "Path traversal detected"}, {}
                base = os.path.realpath(os.getcwd())
                target = os.path.realpath(os.path.join(base, path))
                if target != base and not target.startswith(base + os.sep):
                    return 400, {"error": "Path traversal detected"}, {}

                dirname = os.path.dirname(target)
                if dirname:
                    os.makedirs(dirname, exist_ok=True)
                try:
                    with open(target, "w", encoding="utf-8") as f:
                        f.write(content)
                except Exception as e:
                    return 500, {"error": f"Failed to write file: {str(e)}"}, {}
                return 200, {"status": "file_written"}, {}

        return 404, {"error": "Endpoint not found"}, {}

    def _find_eligible_worker(self, role: str) -> Optional[str]:
        # Canonicalize both sides so a "researcher" dispatch matches workers
        # that registered under the legacy "grok"/"grok_researcher" ids (and
        # vice versa). Non-aliased role strings compare unchanged.
        from ag_core.provider_factory import canonical_role

        want = canonical_role(role)
        now = time.time()
        for w_id, w_info in self.workers.items():
            registered = (canonical_role(r) for r in w_info["roles"])
            if want in registered and w_info["status"] == "idle":
                if now - w_info["last_heartbeat"] < self.config["heartbeat_timeout"]:
                    return w_id
        return None

    async def _dispatch_to_worker(self, worker_id: str, task_id: str):
        # Runs as a detached create_task after the dispatch lock was released:
        # by the time we get here the task may have been re-queued (its worker
        # died: worker_id -> None), re-assigned to ANOTHER worker, or evicted.
        # Read defensively (no KeyError inside a bare create_task) and never
        # fail an attempt that is no longer ours — a stale failure write would
        # clobber the re-dispatched attempt. worker_id None is still "ours":
        # the re-queued-after-our-worker-died form.
        task = self.tasks.get(task_id)
        if task is None:
            return

        def _fail_if_still_ours(error: str) -> None:
            t = self.tasks.get(task_id)
            if t is None or t.get("worker_id") not in (worker_id, None):
                return
            t["status"] = "failed"
            t["result"] = {"error": error}

        # 1. If worker is a production WS worker, dispatch via WS
        w_info = self.workers.get(worker_id)
        if w_info and w_info.get("ws") is not None:
            ws = w_info["ws"]
            task_data = task["task_data"]
            from ag_core.utils.security import calculate_checksum

            checksum = calculate_checksum(task_data, self.api_key)
            payload = {
                "type": "run_task",
                "task_id": task_id,
                "task_data": task_data,
                "checksum": checksum,
            }
            try:
                await ws.send_json(payload)
                return
            except Exception as e:
                async with self.lock:
                    _fail_if_still_ours(f"WS Dispatch error: {str(e)}")
                    if worker_id in self.workers:
                        self.workers[worker_id]["status"] = "idle"
                    await self._process_queue()
                return

        # 2. Fallback/Test dispatch via network simulator
        if not self.network:
            return
        payload = {"task_id": task_id, "task_data": task["task_data"]}
        headers = self.create_headers(payload)
        try:
            status_code, body = await self.network.send_to_worker(
                worker_id, "/run_task", payload, headers
            )
            if status_code != 200:
                async with self.lock:
                    _fail_if_still_ours(f"Worker rejected task: {body}")
                    if worker_id in self.workers:
                        self.workers[worker_id]["status"] = "idle"
                    await self._process_queue()
        except Exception as e:
            async with self.lock:
                _fail_if_still_ours(f"Dispatch communication error: {str(e)}")
                if worker_id in self.workers:
                    self.workers[worker_id]["status"] = "idle"
                await self._process_queue()

    async def _process_queue(self):
        for task_id in list(self.task_queue):
            task = self.tasks.get(task_id)
            if not task or task["status"] != "pending":
                if task_id in self.task_queue:
                    self.task_queue.remove(task_id)

        now = time.time()
        # Find all idle and live workers
        idle_workers = [
            w_id
            for w_id, w_info in self.workers.items()
            if w_info["status"] == "idle"
            and (now - w_info["last_heartbeat"] < self.config["heartbeat_timeout"])
        ]
        if not idle_workers or not self.task_queue:
            return

        processed_tasks = []
        for task_id in list(self.task_queue):
            task = self.tasks.get(task_id)
            if not task or task["status"] != "pending":
                processed_tasks.append(task_id)
                continue

            # Find an idle worker that matches (same alias-tolerant matching
            # as _find_eligible_worker).
            from ag_core.provider_factory import canonical_role

            want = canonical_role(task["role"])
            target_worker = None
            for w_id in idle_workers:
                w_info = self.workers[w_id]
                if want in (canonical_role(r) for r in w_info["roles"]):
                    target_worker = w_id
                    break

            if target_worker:
                self.workers[target_worker]["status"] = "busy"
                task["status"] = "running"
                task["worker_id"] = target_worker
                task["started_at"] = time.time()
                processed_tasks.append(task_id)
                idle_workers.remove(target_worker)
                asyncio.create_task(self._dispatch_to_worker(target_worker, task_id))
                if not idle_workers:
                    break

        for task_id in processed_tasks:
            if task_id in self.task_queue:
                self.task_queue.remove(task_id)

    def create_headers(self, payload: Any) -> Dict[str, str]:
        from ag_core.utils.security import calculate_checksum

        checksum = calculate_checksum(payload, self.api_key)
        # X-Timestamp/X-Nonce are always emitted (harmless when replay
        # protection is off) so enabling GENIUS_HUB_REPLAY_PROTECTION works
        # without a client change.
        return {
            "X-API-Key": self.api_key,
            "X-Payload-SHA256": checksum,
            "X-Timestamp": str(time.time()),
            "X-Nonce": uuid.uuid4().hex,
        }
