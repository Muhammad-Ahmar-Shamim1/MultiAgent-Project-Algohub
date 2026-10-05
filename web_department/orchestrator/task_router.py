"""Task Router (FR-036): which Agent runs a given task.

Role comes straight off ``task.role`` (set by the Planner). ``agent_factory.acquire()`` already
handles "reuse an AVAILABLE agent of that role, else create one, capped by
Settings.max_agents_per_role" (FR-050, FR-051); this module only supplies *which* AVAILABLE agent
to prefer when more than one fits, via ``acquire()``'s ``select`` hook:

1. one that already has memory of this project - it has been assigned at least one task here
   before (since every Agent is created for, and stays scoped to, one project - FR-050 - "memory
   of this project" and "has handled a task before" are the same signal here);
2. among agents tied on that, the least-loaded one - fewest tasks ever assigned to it, so work
   spreads out rather than always landing on the same agent.

A brand-new agent (zero tasks ever) is only picked once no experienced one is AVAILABLE.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.blueprints.registry import BlueprintRegistry
from web_department.collaboration.events import EventBus
from web_department.config.permissions import Role
from web_department.db.models import Agent, Task
from web_department.db.repositories import TaskRepository
from web_department.orchestrator import agent_factory


async def _tasks_handled(session: AsyncSession, agent_id: str) -> int:
    count = await session.scalar(
        select(func.count()).select_from(Task).where(Task.assigned_agent_id == agent_id)
    )
    return int(count or 0)


async def _prefer_experienced_then_least_loaded(
    session: AsyncSession, available: list[Agent]
) -> Agent:
    loads = {agent.id: await _tasks_handled(session, agent.id) for agent in available}
    return min(available, key=lambda a: (0 if loads[a.id] > 0 else 1, loads[a.id]))


async def choose_agent(
    session: AsyncSession,
    registry: BlueprintRegistry,
    task: Task,
    project_id: str,
    *,
    bus: EventBus | None = None,
) -> Agent:
    """Acquire the Agent that should run ``task`` and record the assignment on it (FR-036)."""
    role = Role(task.role)

    async def select(available: list[Agent]) -> Agent:
        return await _prefer_experienced_then_least_loaded(session, available)

    agent = await agent_factory.acquire(session, registry, role, project_id, bus=bus, select=select)
    await TaskRepository(session, bus=bus).assign_agent(task.id, agent.id)
    return agent
