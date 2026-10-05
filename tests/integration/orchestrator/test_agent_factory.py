"""Agent Factory (FR-050..FR-053): create/acquire/release/retire lifecycle, cap, watchdog."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.blueprints.registry import BLUEPRINTS_DIR, BlueprintRegistry, load_and_sync
from web_department.collaboration.events import EventBus
from web_department.config.permissions import Role
from web_department.config.settings import get_settings
from web_department.db.models import Project, Workstation
from web_department.db.repositories import AgentRepository, WorkstationRepository
from web_department.db.states import AgentState
from web_department.orchestrator import agent_factory


@pytest.fixture
async def registry(session: AsyncSession) -> BlueprintRegistry:
    # Agent.blueprint_id/blueprint_version FK-references blueprints - load_and_sync both
    # validates the shipped YAML and upserts the DB rows create()/acquire() need to exist.
    return await load_and_sync(session, BLUEPRINTS_DIR)


@pytest.fixture
async def ws(session: AsyncSession, project: Project, tmp_path: Path) -> Workstation:
    return await WorkstationRepository(session).create(
        project_id=project.id,
        container_id="fake-container",
        repo_path=str(tmp_path),
        limits={},
        status="running",
    )


@pytest.fixture(autouse=True)
def _agent_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_AGENTS_PER_ROLE", "2")
    get_settings.cache_clear()


async def test_create_moves_created_through_provisioned_to_available_and_attaches_workstation(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    registry: BlueprintRegistry,
) -> None:
    agent = await agent_factory.create(session, registry, Role.BACKEND, project.id, bus=bus)

    assert agent.state is AgentState.AVAILABLE
    assert agent.workstation_id == ws.id
    assert agent.blueprint_id == registry.get(Role.BACKEND).id
    assert agent.display_id.startswith("WEB-BE-")


async def test_create_raises_without_a_provisioned_workstation(
    session: AsyncSession, bus: EventBus, project: Project, registry: BlueprintRegistry
) -> None:
    with pytest.raises(agent_factory.NoWorkstation):
        await agent_factory.create(session, registry, Role.BACKEND, project.id, bus=bus)


async def test_display_ids_increment_per_role_across_projects(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    registry: BlueprintRegistry,
) -> None:
    first = await agent_factory.create(session, registry, Role.FRONTEND, project.id, bus=bus)
    second = await agent_factory.create(session, registry, Role.FRONTEND, project.id, bus=bus)

    assert first.display_id != second.display_id
    assert {first.display_id, second.display_id} <= {"WEB-FE-001", "WEB-FE-002"}


async def test_acquire_creates_an_agent_when_none_exist_yet(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    registry: BlueprintRegistry,
) -> None:
    agent = await agent_factory.acquire(session, registry, Role.QA, project.id, bus=bus)

    assert agent.state is AgentState.ASSIGNED
    assert agent.blueprint_id == registry.get(Role.QA).id


async def test_acquire_reuses_an_available_agent_instead_of_creating_a_new_one(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    registry: BlueprintRegistry,
) -> None:
    created = await agent_factory.create(session, registry, Role.QA, project.id, bus=bus)

    acquired = await agent_factory.acquire(session, registry, Role.QA, project.id, bus=bus)

    assert acquired.id == created.id
    assert acquired.state is AgentState.ASSIGNED
    all_qa = await AgentRepository(session).list(project_id=project.id)
    assert len(all_qa) == 1


async def test_acquire_creates_a_second_agent_when_the_first_is_busy(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    registry: BlueprintRegistry,
) -> None:
    first = await agent_factory.acquire(session, registry, Role.QA, project.id, bus=bus)

    second = await agent_factory.acquire(session, registry, Role.QA, project.id, bus=bus)

    assert second.id != first.id
    assert second.state is AgentState.ASSIGNED


async def test_acquire_raises_role_cap_reached_once_the_cap_is_hit(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    registry: BlueprintRegistry,
) -> None:
    # _agent_cap sets MAX_AGENTS_PER_ROLE=2; both are busy (ASSIGNED), so a third must be denied.
    await agent_factory.acquire(session, registry, Role.QA, project.id, bus=bus)
    await agent_factory.acquire(session, registry, Role.QA, project.id, bus=bus)

    with pytest.raises(agent_factory.RoleCapReached):
        await agent_factory.acquire(session, registry, Role.QA, project.id, bus=bus)


async def test_release_returns_an_assigned_agent_to_available(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    registry: BlueprintRegistry,
) -> None:
    agent = await agent_factory.acquire(session, registry, Role.BACKEND, project.id, bus=bus)

    released = await agent_factory.release(session, agent, bus=bus)

    assert released.state is AgentState.AVAILABLE


async def test_retire_moves_an_available_agent_straight_to_retired(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    registry: BlueprintRegistry,
) -> None:
    agent = await agent_factory.create(session, registry, Role.BACKEND, project.id, bus=bus)

    retired = await agent_factory.retire(session, agent, bus=bus)

    assert retired.state is AgentState.RETIRED


async def test_retire_releases_an_assigned_agent_first(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    registry: BlueprintRegistry,
) -> None:
    agent = await agent_factory.acquire(session, registry, Role.BACKEND, project.id, bus=bus)
    assert agent.state is AgentState.ASSIGNED

    retired = await agent_factory.retire(session, agent, bus=bus)

    assert retired.state is AgentState.RETIRED


async def _force_working(session: AsyncSession, agent_id: str, *, minutes_ago: float) -> None:
    """Drive an agent to WORKING and backdate updated_at, without going through a real task."""
    repo = AgentRepository(session)
    await repo.update_state(agent_id, AgentState.ASSIGNED)
    agent = await repo.update_state(agent_id, AgentState.WORKING)
    agent.updated_at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    await session.flush()


async def test_watchdog_blocks_an_agent_working_past_its_blueprint_timeout(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    registry: BlueprintRegistry,
) -> None:
    agent = await agent_factory.create(session, registry, Role.BACKEND, project.id, bus=bus)
    timeout_min = registry.get(Role.BACKEND).budget.timeout_min
    await _force_working(session, agent.id, minutes_ago=timeout_min + 5)

    blocked = await agent_factory.watchdog_tick(session, registry, bus=bus)

    assert [a.id for a in blocked] == [agent.id]
    refreshed = await AgentRepository(session).get(agent.id)
    assert refreshed is not None
    assert refreshed.state is AgentState.BLOCKED


async def test_watchdog_leaves_an_agent_within_its_timeout_alone(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    registry: BlueprintRegistry,
) -> None:
    agent = await agent_factory.create(session, registry, Role.BACKEND, project.id, bus=bus)
    timeout_min = registry.get(Role.BACKEND).budget.timeout_min
    await _force_working(session, agent.id, minutes_ago=max(timeout_min - 5, 0))

    blocked = await agent_factory.watchdog_tick(session, registry, bus=bus)

    assert blocked == []
    refreshed = await AgentRepository(session).get(agent.id)
    assert refreshed is not None
    assert refreshed.state is AgentState.WORKING


async def test_watchdog_ignores_agents_that_are_not_working(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    registry: BlueprintRegistry,
) -> None:
    await agent_factory.create(session, registry, Role.BACKEND, project.id, bus=bus)

    blocked = await agent_factory.watchdog_tick(session, registry, bus=bus)

    assert blocked == []
