import asyncio
import time
import json
import hashlib
import os
from typing import Dict, List, Optional, Any

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
    def __setitem__(self, key, value):
        if len(self) >= 10000:
            to_evict = []
            for t_id, task in self.items():
                if task.get("status") in ("completed", "failed"):
                    to_evict.append(t_id)
            for t_id in to_evict:
                self.pop(t_id, None)
            while len(self) >= 10000:
                first_key = next(iter(self))
                self.pop(first_key, None)
        super().__setitem__(key, value)

class CentralHub:
    def __init__(self, api_key: Optional[str] = None):
        self._api_key_override = api_key
        self.workers: Dict[str, Dict[str, Any]] = {}
        self.tasks = BoundedTasks()
        self.network = None
        self.task_queue = TaskQueue()
        self.task_counter = 0
        self.config = {
            "max_workers": 10,
            "heartbeat_timeout": 0.5,
            "task_timeout": 60.0,
        }
        self._sweeper_task: Optional[asyncio.Task] = None
        self._sweeper_running = False
        self.lock = asyncio.Lock()

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
        
        async with self.lock:
            for w_id, w_info in list(self.workers.items()):
                if now - w_info["last_heartbeat"] >= self.config["heartbeat_timeout"] - 0.01:
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
                t_info["result"] = {"error": f"Task timed out after {self.config['task_timeout']}s"}
                if w_id and w_id in self.workers:
                    self.workers[w_id]["status"] = "idle"
                    ws = self.workers[w_id].get("ws")
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

            if dead_workers or expired_tasks:
                await self._process_queue()

        # Close WebSockets outside the lock block
        for ws in stale_websockets:
            try:
                await ws.close()
            except Exception:
                pass

    def set_network(self, network):
        self.network = network
        network.set_hub(self)
        self.start_sweeper()

    def verify_auth(self, headers: Dict[str, str]) -> bool:
        auth_header = headers.get("X-API-Key") or headers.get("x-api-key")
        if not auth_header and hasattr(headers, "items"):
            auth_header = next((v for k, v in headers.items() if k.lower() == "x-api-key"), None)
        return auth_header == self.api_key

    def verify_checksum(self, payload: Any, headers: Dict[str, str]) -> bool:
        checksum = headers.get("X-Payload-SHA256") or headers.get("x-payload-sha256")
        if not checksum and hasattr(headers, "items"):
            checksum = next((v for k, v in headers.items() if k.lower() == "x-payload-sha256"), None)
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
            if now - w_info["last_heartbeat"] >= self.config["heartbeat_timeout"] - 0.01:
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

    async def handle_request(self, endpoint: str, payload: Any, headers: Dict[str, str]) -> tuple[int, Any, Dict[str, str]]:
        if not self._sweeper_running:
            self.start_sweeper()

        if not self.verify_auth(headers):
            return 401, {"error": "Unauthorized"}, {}

        if not self.verify_checksum(payload, headers):
            return 400, {"error": "Bad Checksum"}, {}

        async with self.lock:
            if endpoint == "/register":
                worker_id = payload.get("worker_id")
                roles = payload.get("roles")
                if not worker_id:
                    return 400, {"error": "Missing worker_id"}, {}
                if roles is None:
                    return 400, {"error": "Missing roles"}, {}
                if len(self.workers) >= self.config["max_workers"] and worker_id not in self.workers:
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
                    if t_info.get("worker_id") == worker_id and t_info.get("status") == "running":
                        status = "busy"
                        break

                self.workers[worker_id] = {
                    "roles": roles,
                    "last_heartbeat": time.time(),
                    "status": status,
                    "ws": existing_ws
                }
                await self._process_queue()
                return 200, {"status": "registered", "worker_id": worker_id}, {}

            elif endpoint == "/heartbeat":
                worker_id = payload.get("worker_id")
                if worker_id not in self.workers:
                    return 404, {"error": "Worker not found"}, {}
                self.workers[worker_id]["last_heartbeat"] = max(time.time(), self.workers[worker_id]["last_heartbeat"] + 0.001)
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
                    "worker_id": None
                }
                
                # Find eligible idle worker
                eligible_worker = self._find_eligible_worker(role)
                if eligible_worker:
                    self.workers[eligible_worker]["status"] = "busy"
                    self.tasks[task_id]["status"] = "running"
                    self.tasks[task_id]["worker_id"] = eligible_worker
                    self.tasks[task_id]["started_at"] = time.time()
                    asyncio.create_task(self._dispatch_to_worker(eligible_worker, task_id))
                    return 202, {"task_id": task_id, "status": "running"}, {}
                else:
                    self.task_queue.append(task_id)
                    return 202, {"task_id": task_id, "status": "pending"}, {}

            elif endpoint == "/task_status":
                task_id = payload.get("task_id")
                if not task_id:
                    return 400, {"error": "Missing task_id"}, {}
                if task_id not in self.tasks:
                    return 404, {"error": "Task not found"}, {}
                task = self.tasks[task_id]
                return 200, {"task_id": task_id, "status": task["status"], "result": task["result"]}, {}

            elif endpoint == "/update_config":
                new_config = payload.get("config", {})
                for k, v in new_config.items():
                    if k in ("max_workers", "heartbeat_timeout", "task_timeout"):
                        if not isinstance(v, (int, float)):
                            return 400, {"error": f"Invalid type for {k}"}, {}
                        if v < 0:
                            return 400, {"error": f"Value for {k} cannot be negative"}, {}
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
                    return 403, {"error": "Forbidden: Worker ID does not match assigned task"}, {}
                
                # Update task only if it is still running
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
                        if t_info["worker_id"] == worker_id and t_info["status"] == "running":
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
                        "last_heartbeat": w_info.get("last_heartbeat")
                    }
                return 200, serialized_workers, {}

            elif endpoint == "/tasks":
                return 200, dict(self.tasks), {}

            elif endpoint == "/write_workspace_file":
                path = payload.get("path")
                content = payload.get("content", "")
                if not path or ".." in path or path.startswith("/") or ":" in path:
                    return 400, {"error": "Path traversal detected"}, {}
                import os
                dirname = os.path.dirname(path)
                if dirname:
                    os.makedirs(dirname, exist_ok=True)
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                except Exception as e:
                    return 500, {"error": f"Failed to write file: {str(e)}"}, {}
                return 200, {"status": "file_written"}, {}

        return 404, {"error": "Endpoint not found"}, {}

    def _find_eligible_worker(self, role: str) -> Optional[str]:
        now = time.time()
        for w_id, w_info in self.workers.items():
            if role in w_info["roles"] and w_info["status"] == "idle":
                if now - w_info["last_heartbeat"] < self.config["heartbeat_timeout"]:
                    return w_id
        return None

    async def _dispatch_to_worker(self, worker_id: str, task_id: str):
        # 1. If worker is a production WS worker, dispatch via WS
        w_info = self.workers.get(worker_id)
        if w_info and w_info.get("ws") is not None:
            ws = w_info["ws"]
            task_data = self.tasks[task_id]["task_data"]
            from ag_core.utils.security import calculate_checksum
            checksum = calculate_checksum(task_data, self.api_key)
            payload = {
                "type": "run_task",
                "task_id": task_id,
                "task_data": task_data,
                "checksum": checksum
            }
            try:
                await ws.send_json(payload)
                return
            except Exception as e:
                async with self.lock:
                    self.tasks[task_id]["status"] = "failed"
                    self.tasks[task_id]["result"] = {"error": f"WS Dispatch error: {str(e)}"}
                    if worker_id in self.workers:
                        self.workers[worker_id]["status"] = "idle"
                    await self._process_queue()
                return

        # 2. Fallback/Test dispatch via network simulator
        if not self.network:
            return
        payload = {"task_id": task_id, "task_data": self.tasks[task_id]["task_data"]}
        headers = self.create_headers(payload)
        try:
            status_code, body = await self.network.send_to_worker(worker_id, "/run_task", payload, headers)
            if status_code != 200:
                async with self.lock:
                    self.tasks[task_id]["status"] = "failed"
                    self.tasks[task_id]["result"] = {"error": f"Worker rejected task: {body}"}
                    if worker_id in self.workers:
                        self.workers[worker_id]["status"] = "idle"
                    await self._process_queue()
        except Exception as e:
            async with self.lock:
                self.tasks[task_id]["status"] = "failed"
                self.tasks[task_id]["result"] = {"error": f"Dispatch communication error: {str(e)}"}
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
            w_id for w_id, w_info in self.workers.items()
            if w_info["status"] == "idle" and (now - w_info["last_heartbeat"] < self.config["heartbeat_timeout"])
        ]
        if not idle_workers or not self.task_queue:
            return

        processed_tasks = []
        for task_id in list(self.task_queue):
            task = self.tasks.get(task_id)
            if not task or task["status"] != "pending":
                processed_tasks.append(task_id)
                continue
            
            # Find an idle worker that matches
            target_worker = None
            for w_id in idle_workers:
                w_info = self.workers[w_id]
                if task["role"] in w_info["roles"]:
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
        return {
            "X-API-Key": self.api_key,
            "X-Payload-SHA256": checksum
        }
