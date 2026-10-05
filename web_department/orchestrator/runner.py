"""Background worker process: pulls project ids off a Redis queue and drives them through the
LangGraph project pipeline (``orchestrator/graph.py``, FR-010..FR-014), one at a time.

    python -m web_department.orchestrator.runner

Uses the LangGraph Postgres checkpointer keyed by ``thread_id=project_id``, so killing and
restarting this process resumes every in-flight project from its last completed node instead of
starting over - nothing here tracks progress itself.

Pause/resume/cancel (the API step) are Redis-only runner control, not a persisted
``ProjectState`` - see ``graph.py``'s module docstring for why. ``request_pause``/
``request_cancel``/``clear_control`` are called by the API routes; ``_control_state`` is read
here between each graph step.
"""

import asyncio
from collections.abc import Awaitable
from typing import Any, cast

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from redis.asyncio import Redis

from web_department.collaboration.events import EventType, emit
from web_department.config.settings import get_settings
from web_department.db.base import session_scope
from web_department.db.repositories import ProjectRepository
from web_department.db.states import ProjectState
from web_department.logging import configure_logging, get_logger
from web_department.orchestrator.graph import build_graph, initial_state
from web_department.security import approvals

log = get_logger(__name__)

PROJECT_QUEUE = "queue:projects"
CONTROL_KEY_PREFIX = "control:project:"
POLL_TIMEOUT_SECONDS = 5


def psycopg_url(database_url: str) -> str:
    """``langgraph-checkpoint-postgres`` uses psycopg (v3): no ``+asyncpg`` driver tag."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _redis(client: Redis | None) -> Redis:
    return client or Redis.from_url(get_settings().redis_url, decode_responses=True)


async def enqueue(project_id: str, redis: Redis | None = None) -> None:
    """Queue ``project_id`` for the runner to pick up. Called by ``orchestrator.start()``."""
    await cast(Awaitable[int], _redis(redis).rpush(PROJECT_QUEUE, project_id))


async def request_pause(project_id: str, redis: Redis | None = None) -> None:
    """Ask the runner to stop advancing ``project_id`` after its current node."""
    await _redis(redis).set(f"{CONTROL_KEY_PREFIX}{project_id}", "paused")


async def request_cancel(project_id: str, redis: Redis | None = None) -> None:
    """Ask the runner to stop advancing ``project_id`` and mark it FAILED."""
    await _redis(redis).set(f"{CONTROL_KEY_PREFIX}{project_id}", "cancelled")


async def clear_control(project_id: str, redis: Redis | None = None) -> None:
    """Clear any pause/cancel flag for ``project_id`` (called before re-enqueuing on resume)."""
    await _redis(redis).delete(f"{CONTROL_KEY_PREFIX}{project_id}")


async def _control_state(redis: Redis, project_id: str) -> str | None:
    return await cast(Awaitable[str | None], redis.get(f"{CONTROL_KEY_PREFIX}{project_id}"))


async def _mark_cancelled(project_id: str) -> None:
    async with session_scope() as session:
        repo = ProjectRepository(session)
        project = await repo.get(project_id)
        if project is not None and project.state not in (
            ProjectState.COMPLETED,
            ProjectState.FAILED,
        ):
            await repo.update_state(project_id, ProjectState.FAILED)
        await emit(
            EventType.ALERT_RAISED,
            session=session,
            project_id=project_id,
            payload={"reason": "operator_cancel"},
        )


# Sentinel: the run is paused on an approval nobody has decided yet.
_WAITING = object()


async def _resume_input(graph: Any, config: dict[str, Any], project_id: str) -> Any:
    """What to stream into the graph: a fresh state, None (continue from the checkpoint), a
    ``Command(resume=...)`` carrying a recorded approval decision, or ``_WAITING`` when the run
    is paused on an approval nobody has decided yet.
    """
    snapshot = await graph.aget_state(config)
    if not snapshot.values:
        return initial_state(project_id)
    if not snapshot.interrupts:
        return None
    approval_id = snapshot.interrupts[0].value["approval_id"]
    async with session_scope() as session:
        decision = await approvals.decision_for(session, approval_id)
    if decision is None:
        log.info("runner_waiting_for_approval", project_id=project_id, approval_id=approval_id)
        return _WAITING
    return Command(resume=decision.model_dump(mode="json"))


async def run_project(graph: Any, redis: Redis, project_id: str) -> None:
    """Drive one project through the graph, honoring pause/cancel between node executions.

    A run paused on an approval (``security/approvals.py``) stays paused until a decision is
    recorded; ``POST /api/v1/approvals/{id}`` then re-enqueues the project and this resumes it.
    """
    config = {"configurable": {"thread_id": project_id}}
    resume_input = await _resume_input(graph, config, project_id)
    if resume_input is _WAITING:
        return

    async for _step in graph.astream(resume_input, config=config):
        control = await _control_state(redis, project_id)
        if control == "cancelled":
            log.info("runner_project_cancelled", project_id=project_id)
            await _mark_cancelled(project_id)
            return
        if control == "paused":
            log.info("runner_project_paused", project_id=project_id)
            return
    log.info("runner_project_graph_finished", project_id=project_id)


async def main() -> None:
    """Poll the queue forever, running one project's graph at a time (sequential for R1)."""
    settings = get_settings()
    configure_logging(settings.log_level)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)

    async with AsyncPostgresSaver.from_conn_string(psycopg_url(settings.database_url)) as saver:
        await saver.setup()
        graph = build_graph().compile(checkpointer=saver)
        log.info("runner_started")
        while True:
            item = await redis.blpop([PROJECT_QUEUE], timeout=POLL_TIMEOUT_SECONDS)
            if item is None:
                continue
            _key, project_id = item
            log.info("runner_picked_up_project", project_id=project_id)
            try:
                await run_project(graph, redis, project_id)
            except Exception:
                log.exception("runner_project_errored", project_id=project_id)


if __name__ == "__main__":
    asyncio.run(main())
