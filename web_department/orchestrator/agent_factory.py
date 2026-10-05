"""Agent Factory (FR-050..FR-053): create, reuse and retire worker Agents, plus a watchdog for
ones stuck WORKING past their blueprint's timeout.

``create()``/``acquire()`` need a loaded ``BlueprintRegistry`` (``blueprints/registry.py``) to
resolve a ``Role`` to the blueprint id/version an Agent row records and to size its display id -
callers already have one (loaded once at API startup by
``api/deps.get_blueprint_registry_holder``) rather than this module loading its own copy.

Design notes:

- ``display_id`` is ``WEB-<ROLECODE>-NNN`` (FR-050) and the DB enforces it globally unique
  (``Agent.display_id``), so ``NNN`` is a per-role counter across every project, not per-project -
  counting existing rows whose id starts with ``WEB-<ROLECODE>-`` avoids a second counter table.
  This reads-then-inserts without a lock, so two concurrent creates of the same role could in
  theory race for the same NNN; the DB's uniqueness constraint would reject the loser rather than
  silently duplicate an id, so this stays a live TODO rather than a correctness bug (the same
  trade-off ``orchestrator/graph.py``'s still-in-place ``_get_or_create_agent`` placeholder makes
  today - wiring the graph over to this module is a Scheduler-shaped follow-up, not done here,
  since it also needs Step 17's per-role worker specialisation to pick the right class per task).
- ``acquire()``'s cap (``Settings.max_agents_per_role``) counts *live* agents (anything but
  RETIRED) of that role in the project, not just AVAILABLE ones - a project deep in ASSIGNED/
  WORKING agents of one role is still "at the cap" even though none of them are free right now.
- The watchdog reads "how long has this agent been WORKING" off ``Agent.updated_at`` (bumped by
  every state transition, including the one that moved it into WORKING) rather than a dedicated
  timestamp column - nothing else touches an Agent row while it's WORKING.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select as sql_select
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.blueprints.registry import BlueprintRegistry
from web_department.collaboration.events import EventBus, EventType, emit
from web_department.config.permissions import Role
from web_department.config.settings import get_settings
from web_department.db.base import session_scope
from web_department.db.models import Agent
from web_department.db.repositories import AgentRepository, WorkstationRepository
from web_department.db.states import AgentState
from web_department.logging import get_logger

log = get_logger(__name__)

ROLE_CODE: dict[Role, str] = {
    Role.ARCHITECT: "ARCH",
    Role.FRONTEND: "FE",
    Role.BACKEND: "BE",
    Role.DATABASE: "DB",
    Role.API_INTEGRATION: "API",
    Role.QA: "QA",
    Role.SECURITY: "SEC",
    Role.DEVOPS: "DEVOPS",
    Role.MAINTENANCE: "MAINT",
}


class NoWorkstation(Exception):
    """Raised by create() when the project has no provisioned workstation yet."""


class RoleCapReached(Exception):
    """Raised by acquire() when a project already holds Settings.max_agents_per_role live agents
    of the requested role and none of them are free.
    """


async def _next_display_id(session: AsyncSession, role: Role) -> str:
    prefix = f"WEB-{ROLE_CODE[role]}-"
    existing = (
        await session.scalars(
            sql_select(Agent.display_id).where(Agent.display_id.like(f"{prefix}%"))
        )
    ).all()
    return f"{prefix}{len(existing) + 1:03d}"


async def create(
    session: AsyncSession,
    registry: BlueprintRegistry,
    role: Role,
    project_id: str,
    *,
    bus: EventBus | None = None,
) -> Agent:
    """New Agent for ``role`` in ``project_id`` (FR-050): CREATED -> PROVISIONED (attach the
    project's workstation) -> AVAILABLE. Raises ``NoWorkstation`` if none is provisioned yet.
    """
    spec = registry.get(role)
    workstations = await WorkstationRepository(session).list(project_id=project_id)
    if not workstations:
        raise NoWorkstation(f"project {project_id!r} has no workstation yet")
    workstation = workstations[-1]

    repo = AgentRepository(session, bus=bus)
    display_id = await _next_display_id(session, role)
    agent = await repo.create(
        display_id=display_id,
        blueprint_id=spec.id,
        blueprint_version=spec.version,
        project_id=project_id,
    )
    agent = await repo.update_state(agent.id, AgentState.PROVISIONED)
    agent.workstation_id = workstation.id
    await session.flush()
    agent = await repo.update_state(agent.id, AgentState.AVAILABLE)
    log.info("agent_created", agent_id=agent.id, display_id=agent.display_id, role=role.value)
    return agent


async def _first_available(agents: list[Agent]) -> Agent:
    return agents[0]


async def acquire(
    session: AsyncSession,
    registry: BlueprintRegistry,
    role: Role,
    project_id: str,
    *,
    bus: EventBus | None = None,
    select: Callable[[list[Agent]], Awaitable[Agent]] | None = None,
) -> Agent:
    """Reuse an AVAILABLE agent of ``role`` in ``project_id``, else create one (FR-051).

    When more than one agent of that role is AVAILABLE, ``select`` (async, given the non-empty
    list) picks which one - the default keeps this call's original "first available" behaviour;
    ``orchestrator/task_router.py`` supplies its own preference order (FR-036).

    Raises ``RoleCapReached`` if the project already has ``Settings.max_agents_per_role`` live
    (non-RETIRED) agents of this role and none of them are free to reuse.
    """
    spec = registry.get(role)
    repo = AgentRepository(session, bus=bus)
    same_role = [a for a in await repo.list(project_id=project_id) if a.blueprint_id == spec.id]

    available = [a for a in same_role if a.state is AgentState.AVAILABLE]
    if available:
        chosen = await (select or _first_available)(available)
        return await repo.update_state(chosen.id, AgentState.ASSIGNED)

    live = [a for a in same_role if a.state is not AgentState.RETIRED]
    cap = get_settings().max_agents_per_role
    if len(live) >= cap:
        raise RoleCapReached(
            f"project {project_id!r} already has {len(live)} live {role.value} agents "
            f"(MAX_AGENTS_PER_ROLE={cap})"
        )

    agent = await create(session, registry, role, project_id, bus=bus)
    return await repo.update_state(agent.id, AgentState.ASSIGNED)


async def release(session: AsyncSession, agent: Agent, *, bus: EventBus | None = None) -> Agent:
    """Return ``agent`` to AVAILABLE (FR-052) - from ASSIGNED, REVIEW or BLOCKED.

    Not from WORKING: ``db.states.AGENT_TRANSITIONS`` only lets a WORKING agent move to REVIEW or
    BLOCKED, never straight back to AVAILABLE - the caller decides which of those a finished (or
    stuck) task means before calling this, rather than this module guessing.
    """
    repo = AgentRepository(session, bus=bus)
    return await repo.update_state(agent.id, AgentState.AVAILABLE)


async def retire(session: AsyncSession, agent: Agent, *, bus: EventBus | None = None) -> Agent:
    """Move ``agent`` to RETIRED (FR-052), typically for every agent a finished project created.

    An ASSIGNED agent is released to AVAILABLE first (one legal hop, db.states.AGENT_TRANSITIONS);
    a WORKING or REVIEW agent must be released (or the task let finish/block) by the caller first -
    retiring one still doing something is deliberately not automatic.
    """
    repo = AgentRepository(session, bus=bus)
    if agent.state is AgentState.ASSIGNED:
        agent = await repo.update_state(agent.id, AgentState.AVAILABLE)
    return await repo.update_state(agent.id, AgentState.RETIRED)


async def watchdog_tick(
    session: AsyncSession, registry: BlueprintRegistry, *, bus: EventBus | None = None
) -> list[Agent]:
    """One watchdog pass (FR-053): BLOCK and diagnose every WORKING agent stuck past its
    blueprint's ``budget.timeout_min``. Returns the agents it just blocked.
    """
    specs_by_blueprint_id = {spec.id: spec for spec in registry.list()}
    default_timeout_min = get_settings().task_timeout_min

    working = (
        await session.scalars(sql_select(Agent).where(Agent.state == AgentState.WORKING))
    ).all()

    repo = AgentRepository(session, bus=bus)
    now = datetime.now(UTC)
    blocked: list[Agent] = []
    for agent in working:
        spec = specs_by_blueprint_id.get(agent.blueprint_id)
        timeout_min = spec.budget.timeout_min if spec else default_timeout_min
        working_seconds = (now - agent.updated_at).total_seconds()
        if working_seconds <= timeout_min * 60:
            continue

        await repo.update_state(agent.id, AgentState.BLOCKED)
        await emit(
            EventType.ALERT_RAISED,
            session=session,
            bus=bus,
            project_id=agent.project_id,
            agent_id=agent.id,
            payload={
                "reason": "agent_watchdog_timeout",
                "working_seconds": working_seconds,
                "timeout_min": timeout_min,
            },
        )
        log.warning(
            "agent_watchdog_blocked",
            agent_id=agent.id,
            display_id=agent.display_id,
            working_seconds=working_seconds,
            timeout_min=timeout_min,
        )
        blocked.append(agent)
    return blocked


async def run_watchdog(
    registry: BlueprintRegistry, *, interval_seconds: float | None = None
) -> None:
    """Call ``watchdog_tick`` every ``interval_seconds`` (default ``Settings.
    agent_watchdog_interval_seconds``) forever, each pass in its own transaction, until cancelled.

    Not started automatically by anything yet - a caller (``runner.py``, or its own process via
    ``python -m web_department.orchestrator.agent_factory``) is expected to run this as a task.
    """
    interval = interval_seconds or get_settings().agent_watchdog_interval_seconds
    while True:
        try:
            async with session_scope() as session:
                await watchdog_tick(session, registry)
        except Exception:
            log.exception("agent_watchdog_tick_failed")
        await asyncio.sleep(interval)


async def _main() -> None:
    from web_department.blueprints.registry import load_all
    from web_department.logging import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level)
    found = load_all()
    watchdog_registry = BlueprintRegistry([spec for spec, _raw, _checksum in found.values()])
    log.info("agent_watchdog_started")
    await run_watchdog(watchdog_registry)


if __name__ == "__main__":
    asyncio.run(_main())
