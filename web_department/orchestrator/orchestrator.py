"""The hand-off point ``WebAgent.accept()`` calls once a project is accepted (FR-003).

Queues the project for ``runner.py`` to pick up and drive through ``graph.py``'s LangGraph
pipeline (FR-010..FR-014). Enqueuing (not running the graph inline) keeps intake fast and
decoupled from orchestration, and means a project submitted while the runner is briefly down is
still picked up once it comes back (the queue is a Redis list, not a live stream).
"""

from redis.asyncio import Redis

from web_department.logging import get_logger
from web_department.orchestrator import runner

log = get_logger(__name__)


async def start(project_id: str, redis: Redis | None = None) -> None:
    """Kick off orchestration for a newly received project."""
    await runner.enqueue(project_id, redis)
    log.info("orchestrator_enqueued_project", project_id=project_id)
