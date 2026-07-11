"""Shared factory for the per-agent FastAPI skill servers.

Each agent under ``.agents/skills/<agent>/api.py`` exposes a FastAPI ``app``
built by :func:`create_skill_app`, and a CLI ``run.py`` that calls
:func:`build_agent`. This keeps the skill layer in one tested place instead of
duplicating the wiring across six near-identical files.

The agent/provider wiring mirrors ``ag_core.distributed.worker.execute_task``
(the proven distributed path) so both transports behave identically.
"""

import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from ag_core import agent_factory
from ag_core.provider_factory import canonical_role
from ag_core.utils.db import init_db
from ag_core.utils.logger import logger
from ag_core.utils.rate_limiter import make_rate_limit_dependency
from ag_core.utils.security import checksum_middleware, verify_api_key

# role -> (agent module, agent class). Derived from the shared factory table
# (ag_core.agent_factory.AGENT_CLASSES) — same shape as the historical
# hand-written map. Keys are CANONICAL role ids; legacy ids ("grok",
# "grok_researcher") are folded in by canonical_role() before lookup.
ROLE_MAP = dict(agent_factory.AGENT_CLASSES)


class RunRequest(BaseModel):
    prompt: str
    context: Optional[Any] = None
    # Per-request reasoning effort (e.g. threaded from a pipeline @deep). None ->
    # the agent falls back to its own prompt-derived effort / env, as before.
    effort: Optional[str] = None


# Cap for the per-app task/idempotency stores so a long-lived skill server
# cannot grow them without bound. Dicts preserve insertion order, so evicting
# the first key drops the oldest entry (a plain FIFO, dependency-free).
MAX_TRACKED_TASKS = 500


def evict_oldest(store: dict, cap: int = MAX_TRACKED_TASKS) -> None:
    """Drop entries until ``len(store) <= cap``.

    When values carry a ``status`` (the task store), finished tasks are evicted
    first — oldest-first — so an in-flight ``processing`` task isn't dropped out
    from under its ``/status`` poller (which would then 404 and fail the whole
    pipeline for a task that actually completed). Only if the store is still
    over cap after removing every finished task (i.e. it is full of live tasks)
    does it fall back to plain FIFO eviction. Status-less stores (idempotency
    map) always use FIFO.
    """
    if len(store) <= cap:
        return
    for key in list(store):
        if len(store) <= cap:
            return
        val = store.get(key)
        if isinstance(val, dict) and val.get("status") in ("completed", "failed"):
            store.pop(key, None)
    while len(store) > cap:
        store.pop(next(iter(store)))


def build_agent(role: str, stateless: bool = True):
    """Instantiate the agent + provider for ``role``.

    When ``stateless`` is True (the API server case) the agent neither writes
    output files nor touches the vector memory DB, so a request leaves no trace
    on the server's working directory.
    """
    return agent_factory.build_agent(role, stateless=stateless)


def create_skill_app(role: str) -> FastAPI:
    """Build the FastAPI skill server for a single agent role. The /health
    body reports the CANONICAL role id (legacy input ids are normalized)."""
    role = canonical_role(role)
    if role not in ROLE_MAP:
        raise ValueError(f"Unknown role: {role}")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Ensure the DB — including the seen_jtis anti-replay table that
        # decode_jwt writes to on every authenticated request — exists even when
        # this app is served standalone (`uvicorn <role>_agent.api:app`) rather
        # than through serve.py, which init_db's at import. Without it every
        # authenticated request 401s with "no such table: seen_jtis" while
        # /health still reports ok. Idempotent (CREATE TABLE IF NOT EXISTS).
        init_db()
        yield

    app = FastAPI(title=f"Genius {role} Skill Server", lifespan=lifespan)
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

    # This role's own rate-limit bucket (not a single process-global one shared
    # by every co-hosted skill server — that would 429 normal fan-out).
    rate_limit = make_rate_limit_dependency(role)

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
        # Rate-limit BEFORE auth so a flood that's going to be 429'd doesn't
        # first consume its JWT's one-time jti in the anti-replay table (which
        # would then reject the client's legitimate retry as a replay).
        _rl: None = Depends(rate_limit),
        _auth: dict = Depends(verify_api_key),
    ):
        if strict_prompt and (not request.prompt or not request.prompt.strip()):
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        # A repeated idempotency key returns the in-flight/finished task. The
        # check-and-record below is atomic: the handler never awaits between
        # them, so concurrent retries cannot both create a task.
        if idempotency_key and idempotency_key in idempotency:
            existing_id = idempotency[idempotency_key]
            existing = tasks.get(existing_id)
            if existing is not None:
                return {
                    "task_id": existing_id,
                    "status": existing.get("status", "processing"),
                }
            # The deduped task was evicted from the (separately bounded)
            # task store: handing back its id would 404 every /status poll
            # and hard-fail the caller's pipeline. Treat the key as new and
            # dispatch a fresh task instead.
            idempotency.pop(idempotency_key, None)

        task_id = uuid.uuid4().hex
        tasks[task_id] = {"status": "processing", "result": None}
        evict_oldest(tasks)
        if idempotency_key:
            idempotency[idempotency_key] = task_id
            evict_oldest(idempotency)

        async def _execute():
            try:
                agent = build_agent(role, stateless=True)
                # Only pass effort when set, so the common (None) path is
                # byte-identical to a plain run(prompt, context_data) call and
                # agent mocks without an effort param keep working.
                run_kwargs = {
                    "prompt": request.prompt,
                    "context_data": request.context,
                }
                if request.effort:
                    run_kwargs["effort"] = request.effort
                output = await agent.run(**run_kwargs)
                tasks[task_id] = {"status": "completed", "result": output}
            except Exception as exc:  # noqa: BLE001 - report failure to caller
                # Server-side traceback too: str(exc) relayed to the client
                # is all the caller sees, and a CLI/provider failure with no
                # server log is undebuggable in production.
                logger.exception("[skill:%s] background task %s failed", role, task_id)
                tasks[task_id] = {"status": "failed", "error": str(exc)}

        background_tasks.add_task(_execute)
        return {"task_id": task_id, "status": "processing"}

    @app.get("/status/{task_id}")
    async def status_endpoint(
        task_id: str,
        _rl: None = Depends(rate_limit),
        _auth: dict = Depends(verify_api_key),
    ):
        if task_id not in tasks:
            raise HTTPException(status_code=404, detail="Task not found")
        return tasks[task_id]

    return app
