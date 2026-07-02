"""Shared factory for the per-agent FastAPI skill servers.

Each agent under ``.agents/skills/<agent>/api.py`` exposes a FastAPI ``app``
built by :func:`create_skill_app`, and a CLI ``run.py`` that calls
:func:`build_agent`. This keeps the skill layer in one tested place instead of
duplicating the wiring across six near-identical files.

The agent/provider wiring mirrors ``ag_core.distributed.worker.execute_task``
(the proven distributed path) so both transports behave identically.
"""

import importlib
import uuid
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from ag_core.config import load_config
from ag_core.provider_factory import make_provider
from ag_core.utils.rate_limiter import rate_limit_dependency
from ag_core.utils.security import checksum_middleware, verify_api_key

# role -> (agent module, agent class). Provider selection lives in
# ag_core.provider_factory (role -> backend chain, env-overridable).
ROLE_MAP = {
    "grok": ("ag_core.agents.grok_researcher", "GrokResearcherAgent"),
    "claude": ("ag_core.agents.claude_architect", "ClaudeArchitectAgent"),
    "codex": ("ag_core.agents.codex_reviewer", "CodexReviewerAgent"),
    "tester": ("ag_core.agents.tester", "TesterAgent"),
    "security": ("ag_core.agents.security_agent", "SecurityAgent"),
    "devops": ("ag_core.agents.devops_agent", "DevOpsAgent"),
}


class RunRequest(BaseModel):
    prompt: str
    context: Optional[Any] = None


# Cap for the per-app task/idempotency stores so a long-lived skill server
# cannot grow them without bound. Dicts preserve insertion order, so evicting
# the first key drops the oldest entry (a plain FIFO, dependency-free).
MAX_TRACKED_TASKS = 500


def evict_oldest(store: dict, cap: int = MAX_TRACKED_TASKS) -> None:
    """Drop the oldest entries (by insertion order) until ``len(store) <= cap``."""
    while len(store) > cap:
        store.pop(next(iter(store)))


def build_agent(role: str, stateless: bool = True):
    """Instantiate the agent + provider for ``role``.

    When ``stateless`` is True (the API server case) the agent neither writes
    output files nor touches the vector memory DB, so a request leaves no trace
    on the server's working directory.
    """
    role = role.lower()
    if role not in ROLE_MAP:
        raise ValueError(f"Unknown role: {role}")

    agent_mod, agent_cls = ROLE_MAP[role]
    agent_class = getattr(importlib.import_module(agent_mod), agent_cls)

    config = load_config()
    provider = make_provider(role, config)

    agent_kwargs = {"provider": provider, "config": config}
    if stateless:
        agent_kwargs["output_file"] = "None"
        agent_kwargs["use_memory"] = False
    return agent_class(**agent_kwargs)


def create_skill_app(role: str) -> FastAPI:
    """Build the FastAPI skill server for a single agent role."""
    role = role.lower()
    if role not in ROLE_MAP:
        raise ValueError(f"Unknown role: {role}")

    app = FastAPI(title=f"Genius {role} Skill Server")
    app.middleware("http")(checksum_middleware)

    # Security and DevOps servers strictly reject empty prompts; the other
    # agents tolerate an empty prompt (they fall back to a sensible default).
    strict_prompt = role in ("security", "devops")

    # In-memory task store: task_id -> {"status", "result"/"error"}.
    # Both stores are FIFO-bounded (see evict_oldest) so they cannot grow
    # without bound on a long-lived server.
    tasks: dict = {}
    # Idempotency map: X-Idempotency-Key -> task_id, so a retried /run (e.g.
    # after a transient network error where the server already accepted the
    # first POST) returns the same task instead of dispatching the agent twice.
    idempotency: dict = {}

    @app.get("/health")
    async def health_endpoint():
        """Unauthenticated liveness probe used by serve.py's startup readiness
        poll (and anything else that needs a cheap 'is this agent up?')."""
        return {"status": "ok", "role": role}

    @app.post("/run")
    async def run_endpoint(
        request: RunRequest,
        background_tasks: BackgroundTasks,
        idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
        _auth: dict = Depends(verify_api_key),
        _rl: None = Depends(rate_limit_dependency),
    ):
        if strict_prompt and (not request.prompt or not request.prompt.strip()):
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        # A repeated idempotency key returns the in-flight/finished task. The
        # check-and-record below is atomic: the handler never awaits between
        # them, so concurrent retries cannot both create a task.
        if idempotency_key and idempotency_key in idempotency:
            existing_id = idempotency[idempotency_key]
            existing = tasks.get(existing_id, {"status": "processing"})
            return {
                "task_id": existing_id,
                "status": existing.get("status", "processing"),
            }

        task_id = uuid.uuid4().hex
        tasks[task_id] = {"status": "processing", "result": None}
        evict_oldest(tasks)
        if idempotency_key:
            idempotency[idempotency_key] = task_id
            evict_oldest(idempotency)

        async def _execute():
            try:
                agent = build_agent(role, stateless=True)
                output = await agent.run(
                    prompt=request.prompt, context_data=request.context
                )
                tasks[task_id] = {"status": "completed", "result": output}
            except Exception as exc:  # noqa: BLE001 - report failure to caller
                tasks[task_id] = {"status": "failed", "error": str(exc)}

        background_tasks.add_task(_execute)
        return {"task_id": task_id, "status": "processing"}

    @app.get("/status/{task_id}")
    async def status_endpoint(
        task_id: str,
        _auth: dict = Depends(verify_api_key),
        _rl: None = Depends(rate_limit_dependency),
    ):
        if task_id not in tasks:
            raise HTTPException(status_code=404, detail="Task not found")
        return tasks[task_id]

    return app
