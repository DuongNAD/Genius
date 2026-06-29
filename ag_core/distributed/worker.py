import asyncio
import time
import json
import hashlib
import os
from typing import Dict, List, Optional, Any
from ag_core.utils.jwt import encode_jwt

class ClientWorker:
    def __init__(self, worker_id: str, roles: List[str], api_key: str = "valid-api-key"):
        self.worker_id = worker_id
        self.roles = roles
        self.api_key = api_key
        self.active_tasks = set()
        self._status = "idle"
        self._current_task = None
        self.network = None
        self.heartbeat_task = None
        self.heartbeat_interval = 0.05
        self.running = False
        self.received_tasks = []
        self.tasks_completed = 0
        self.tasks_failed = 0
        self.ws = None
        self.running_tasks = {}

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
        from ag_core.utils.security import calculate_checksum
        checksum = calculate_checksum(payload, self.api_key)
        return {
            "X-API-Key": self.api_key,
            "X-Payload-SHA256": checksum
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

    async def _heartbeat_loop(self):
        while self.running:
            if self.ws is not None:
                hb_msg = {
                    "type": "heartbeat",
                    "worker_id": self.worker_id
                }
                await self.ws.send(json.dumps(hb_msg))
            else:
                try:
                    status_code, body = await self.send_heartbeat()
                    if status_code == 404:
                        print(f"[Worker] Heartbeat 404: not registered. Re-registering...")
                        await self.register()
                except Exception:
                    pass
            await asyncio.sleep(self.heartbeat_interval)

    async def handle_request(self, endpoint: str, payload: Any, headers: Dict[str, str]) -> tuple[int, Any, Dict[str, str]]:
        if headers.get("X-API-Key") != self.api_key:
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
            self.status = "idle"
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
                
                ROLE_AGENT_MAP = {
                    "grok": ("ag_core.agents.grok_researcher", "GrokResearcherAgent", "ag_core.providers.grok_provider", "GrokProvider", "grok"),
                    "grok_researcher": ("ag_core.agents.grok_researcher", "GrokResearcherAgent", "ag_core.providers.grok_provider", "GrokProvider", "grok"),
                    "claude": ("ag_core.agents.claude_architect", "ClaudeArchitectAgent", "ag_core.providers.anthropic_provider", "AnthropicProvider", "claude"),
                    "claude_architect": ("ag_core.agents.claude_architect", "ClaudeArchitectAgent", "ag_core.providers.anthropic_provider", "AnthropicProvider", "claude"),
                    "codex": ("ag_core.agents.codex_reviewer", "CodexReviewerAgent", "ag_core.providers.openai_provider", "OpenAIProvider", "codex"),
                    "codex_reviewer": ("ag_core.agents.codex_reviewer", "CodexReviewerAgent", "ag_core.providers.openai_provider", "OpenAIProvider", "codex"),
                    "tester": ("ag_core.agents.tester", "TesterAgent", "ag_core.providers.openai_provider", "OpenAIProvider", "tester"),
                    "tester_agent": ("ag_core.agents.tester", "TesterAgent", "ag_core.providers.openai_provider", "OpenAIProvider", "tester"),
                    "security": ("ag_core.agents.security_agent", "SecurityAgent", "ag_core.providers.openai_provider", "OpenAIProvider", "security"),
                    "security_agent": ("ag_core.agents.security_agent", "SecurityAgent", "ag_core.providers.openai_provider", "OpenAIProvider", "security"),
                    "devops": ("ag_core.agents.devops_agent", "DevOpsAgent", "ag_core.providers.openai_provider", "OpenAIProvider", "devops"),
                    "devops_agent": ("ag_core.agents.devops_agent", "DevOpsAgent", "ag_core.providers.openai_provider", "OpenAIProvider", "devops"),
                }
                
                normalized_role = role.lower()
                if normalized_role not in ROLE_AGENT_MAP:
                    status = "failed"
                    result = {"error": f"Role '{role}' is not supported by this worker."}
                    self.tasks_failed += 1
                else:
                    agent_mod_name, agent_cls_name, prov_mod_name, prov_cls_name, default_model = ROLE_AGENT_MAP[normalized_role]
                    try:
                        import importlib
                        agent_mod = importlib.import_module(agent_mod_name)
                        agent_class = getattr(agent_mod, agent_cls_name)
                        
                        prov_mod = importlib.import_module(prov_mod_name)
                        provider_class = getattr(prov_mod, prov_cls_name)
                        
                        from ag_core.config import load_config
                        config = load_config()
                        
                        prefix = "grok" if "grok" in normalized_role else ("anthropic" if "claude" in normalized_role else "openai")
                        provider_key = getattr(config, f"{prefix}_api_key", None) or os.getenv(f"{prefix.upper()}_API_KEY", "mock-key")
                        model_name = getattr(config.models, prefix, default_model)
                        
                        provider = provider_class(api_key=provider_key, model_name=model_name)
                        agent = agent_class(provider=provider, config=config, output_file="None")
                        
                        output = await agent.run(prompt=prompt, context_data=context)
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
            self.status = "idle"
            self.current_task = None
            
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
                        "checksum": checksum
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
                        "checksum": checksum
                    }

                    headers = self.create_headers(payload)
                    backoff = 0.005
                    for attempt in range(5):
                        try:
                            status_code, body = await self.network.send_to_hub("/report_result", payload, headers)
                            if status_code == 200:
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(backoff)
                        backoff *= 2
            try:
                await asyncio.shield(report())
            except Exception as e:
                print(f"[Worker] Shielded report failed: {e}")

    def generate_jwt(self) -> str:
        secret = os.getenv("SKILL_API_KEY", "mock-skill-key")
        payload = {
            "sub": self.worker_id,
            "exp": int(time.time() + 300)
        }
        return encode_jwt(payload, secret)

    async def run_production_loop(self, hub_ip: str, hub_port: int):
        import websockets
        backoff = 1.0
        max_backoff = 60.0
        backoff_factor = 2.0

        while True:
            token = self.generate_jwt()
            uri = f"ws://{hub_ip}:{hub_port}/ws/connect?token={token}"
            try:
                print(f"[Worker] Connecting to {uri}...")
                async with websockets.connect(uri) as websocket:
                    self.ws = websocket
                    backoff = 1.0  # Reset backoff
                    print(f"[Worker] Connected! Registering roles: {self.roles}")
                    
                    # Register
                    reg_payload = {
                        "type": "register",
                        "worker_id": self.worker_id,
                        "roles": self.roles
                    }
                    await websocket.send(json.dumps(reg_payload))
                    
                    self.heartbeat_interval = 10.0
                    self.running = True
                    hb_task = asyncio.create_task(self._heartbeat_loop())
                    
                    async def read_msg_loop():
                        async for message in websocket:
                            data = json.loads(message)
                            print(f"[Worker] Received from hub: {data}")
                            msg_type = data.get("type")
                            if msg_type == "error" and data.get("error") == "not_registered":
                                print(f"[Worker] Received not_registered error. Re-registering...")
                                reg_payload = {
                                    "type": "register",
                                    "worker_id": self.worker_id,
                                    "roles": self.roles
                                }
                                await websocket.send(json.dumps(reg_payload))
                                continue

                            if msg_type == "cancel":
                                task_id = data.get("task_id")
                                print(f"[Worker] Received cancel message for task {task_id}")
                                if task_id in self.running_tasks:
                                    self.running_tasks[task_id].cancel()
                                self.status = "idle"
                                continue

                            if msg_type in ("run_task", "dispatch"):
                                task_id = data.get("task_id")
                                task_data = data.get("task_data")
                                checksum = data.get("checksum")
                                
                                from ag_core.utils.security import calculate_checksum, verify_checksum
                                if self.status == "busy":
                                    print(f"[Worker] Worker is busy, rejecting dispatch!")
                                    err_res = {"error": "Worker is busy"}
                                    err_chk = calculate_checksum(err_res, self.api_key)
                                    payload = {
                                        "type": "result",
                                        "task_id": task_id,
                                        "worker_id": self.worker_id,
                                        "status": "failed",
                                        "result": err_res,
                                        "checksum": err_chk
                                    }
                                    await websocket.send(json.dumps(payload))
                                    continue

                                if not checksum:
                                    print(f"[Worker] Missing checksum in dispatch!")
                                    err_res = {"error": "Missing checksum validation on worker node."}
                                    err_chk = calculate_checksum(err_res, self.api_key)
                                    payload = {
                                        "type": "result",
                                        "task_id": task_id,
                                        "worker_id": self.worker_id,
                                        "status": "failed",
                                        "result": err_res,
                                        "checksum": err_chk
                                    }
                                    await websocket.send(json.dumps(payload))
                                    continue
                                
                                if not verify_checksum(task_data, checksum, self.api_key):
                                    print(f"[Worker] Checksum mismatch! Expected {checksum}")
                                    err_res = {"error": "Bad Checksum validation on worker node."}
                                    err_chk = calculate_checksum(err_res, self.api_key)
                                    payload = {
                                        "type": "result",
                                        "task_id": task_id,
                                        "worker_id": self.worker_id,
                                        "status": "failed",
                                        "result": err_res,
                                        "checksum": err_chk
                                    }

                                    await websocket.send(json.dumps(payload))
                                    continue
                                
                                self.active_tasks.add(task_id)
                                self.received_tasks.append((task_id, task_data))
                                t = asyncio.create_task(self.execute_task(task_id, task_data))
                                self.running_tasks[task_id] = t

                    read_task = asyncio.create_task(read_msg_loop())

                    try:
                        done, pending = await asyncio.wait(
                            [hb_task, read_task],
                            return_when=asyncio.FIRST_COMPLETED
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
                        # Task 7: cancel running tasks and clear list on disconnect
                        for t_id, task in list(self.running_tasks.items()):
                            task.cancel()
                        self.running_tasks.clear()
            except Exception as e:
                print(f"[Worker] Connection failed: {e}")

            # Task 8: add random jitter (0 to 1.0 seconds) to backoff
            import random
            sleep_time = backoff + random.uniform(0, 1.0)
            print(f"[Worker] Reconnecting in {sleep_time:.1f}s...")
            await asyncio.sleep(sleep_time)
            backoff = min(backoff * backoff_factor, max_backoff)

