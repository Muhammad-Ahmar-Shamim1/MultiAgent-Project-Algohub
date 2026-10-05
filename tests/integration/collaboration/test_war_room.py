"""War Room (FR-080, FR-081): team formation from the e-commerce plan, the view, requests, close."""

import hashlib
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.orchestrator.test_planner import ECOMMERCE_WRS
from web_department.blueprints.registry import BLUEPRINTS_DIR, BlueprintRegistry, load_and_sync
from web_department.collaboration import war_room
from web_department.collaboration.events import EventBus
from web_department.config.permissions import Role
from web_department.config.settings import get_settings
from web_department.db.models import Event, Project, Task, WarRoom
from web_department.db.repositories import (
    AgentRepository,
    ProjectRepository,
    RequirementSpecRepository,
    TaskRepository,
    WarRoomRepository,
    WorkstationRepository,
)
from web_department.db.states import AgentState, ProjectState, TaskState
from web_department.llm.router import CompletionResult
from web_department.orchestrator import agent_factory
from web_department.orchestrator import planner as planner_module
from web_department.orchestrator.planner import ExtraTasksResponse, Planner

TEAM_CODES = ["ARCH", "FE", "BE", "DB", "API", "QA", "SEC", "DEVOPS"]


@pytest.fixture
async def registry(session: AsyncSession) -> BlueprintRegistry:
    return await load_and_sync(session, BLUEPRINTS_DIR)


@pytest.fixture
async def planned_project(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Project:
    """The e-commerce reference request, planned, with a workstation on ``tmp_path/ws``."""
    no_extras = CompletionResult(
        content="{}",
        parsed=ExtraTasksResponse(tasks=[]),
        provider="test",
        model="test/model",
        cached=False,
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
    )
    monkeypatch.setattr(planner_module, "complete", AsyncMock(return_value=no_extras))
    repo = ProjectRepository(session)
    await repo.update_state(project.id, ProjectState.ANALYZING)
    project = await repo.update_state(project.id, ProjectState.PLANNING)
    spec = await RequirementSpecRepository(session).create(
        project_id=project.id,
        version=1,
        json=ECOMMERCE_WRS.model_dump(mode="json"),
        created_by="test",
    )
    await Planner(session, bus=bus).plan(project, spec)
    (tmp_path / "ws").mkdir()
    await WorkstationRepository(session).create(
        project_id=project.id,
        container_id="fake",
        repo_path=str(tmp_path / "ws"),
        limits={},
        status="running",
    )
    return project


@pytest.fixture
def archive_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    target = tmp_path / "archive"
    monkeypatch.setenv("WAR_ROOM_ARCHIVE_DIR", str(target))
    get_settings.cache_clear()
    yield target
    get_settings.cache_clear()


def _code(display_id: str) -> str:
    return display_id.split("-")[1]


async def _events(session: AsyncSession, project_id: str, event_type: str) -> list[Event]:
    stmt = select(Event).where(Event.project_id == project_id, Event.type == event_type)
    return list((await session.scalars(stmt)).all())


async def test_ecommerce_plan_forms_the_full_eight_role_team(
    session: AsyncSession, bus: EventBus, registry: BlueprintRegistry, planned_project: Project
) -> None:
    room = await war_room.open(session, registry, planned_project.id, bus=bus)

    assert room.status == "open"
    assert room.required_roles == [
        "architect",
        "frontend",
        "backend",
        "database",
        "api_integration",
        "qa",
        "security",
        "devops",
    ]
    members = (await war_room.view(session, planned_project.id)).members
    assert [_code(m.display_id) for m in members] == TEAM_CODES
    assert [m.role for m in members] == room.required_roles
    assert all(m.state is AgentState.AVAILABLE for m in members)
    agents = await AgentRepository(session).list(project_id=planned_project.id)
    assert {a.id for a in agents} == {m.agent_id for m in members}
    opened = await _events(session, planned_project.id, "WAR_ROOM_OPENED")
    assert len(opened) == 1
    assert opened[0].payload["required_roles"] == room.required_roles


async def test_open_is_idempotent(
    session: AsyncSession, bus: EventBus, registry: BlueprintRegistry, planned_project: Project
) -> None:
    first = await war_room.open(session, registry, planned_project.id, bus=bus)
    second = await war_room.open(session, registry, planned_project.id, bus=bus)

    assert second.id == first.id
    assert len(await AgentRepository(session).list(project_id=planned_project.id)) == 8


async def test_open_reuses_a_live_agent_the_project_already_has(
    session: AsyncSession, bus: EventBus, registry: BlueprintRegistry, planned_project: Project
) -> None:
    existing = await agent_factory.create(session, registry, Role.BACKEND, planned_project.id)

    room = await war_room.open(session, registry, planned_project.id, bus=bus)

    member_ids = [agent.id for _m, agent in await WarRoomRepository(session).members(room.id)]
    assert existing.id in member_ids
    assert len(await AgentRepository(session).list(project_id=planned_project.id)) == 8


async def test_open_without_a_plan_raises(
    session: AsyncSession, bus: EventBus, registry: BlueprintRegistry, project: Project
) -> None:
    with pytest.raises(war_room.NoPlan):
        await war_room.open(session, registry, project.id, bus=bus)


async def test_view_shows_members_task_board_events_and_artifacts(
    session: AsyncSession,
    bus: EventBus,
    registry: BlueprintRegistry,
    planned_project: Project,
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    (ws / "docs").mkdir()
    (ws / "docs" / "architecture.md").write_text("# Architecture")
    (ws / "openapi.yaml").write_text("openapi: 3.1.0")
    (ws / "qa" / "screenshots").mkdir(parents=True)
    (ws / "qa" / "screenshots" / "home.png").write_bytes(b"\x89PNG")
    (ws / "reports").mkdir()
    (ws / "reports" / "security.md").write_text("no findings")
    (ws / "backend").mkdir()
    (ws / "backend" / "main.py").write_text("not an artifact")
    await war_room.open(session, registry, planned_project.id, bus=bus)

    view = await war_room.view(session, planned_project.id)

    assert [_code(m.display_id) for m in view.members] == TEAM_CODES
    all_tasks = await TaskRepository(session).list(project_id=planned_project.id)
    assert sum(len(tasks) for tasks in view.task_board.values()) == len(all_tasks)
    assert set(view.task_board) == set(TaskState)
    assert view.events[0].type == "WAR_ROOM_OPENED"  # newest first
    assert {(a.path, a.kind) for a in view.artifacts} == {
        ("docs/architecture.md", "architecture"),
        ("openapi.yaml", "openapi"),
        ("qa/screenshots/home.png", "screenshot"),
        ("reports/security.md", "report"),
    }


async def test_view_without_a_war_room_raises_lookup_error(
    session: AsyncSession, project: Project
) -> None:
    with pytest.raises(LookupError):
        await war_room.view(session, project.id)


async def _task(session: AsyncSession, project_id: str, title_prefix: str) -> Task:
    tasks = await TaskRepository(session).list(project_id=project_id)
    return next(t for t in tasks if t.title.startswith(title_prefix))


async def test_request_from_creates_a_dependency_task_and_review_required_event(
    session: AsyncSession, bus: EventBus, registry: BlueprintRegistry, planned_project: Project
) -> None:
    await war_room.open(session, registry, planned_project.id, bus=bus)
    requester = await _task(session, planned_project.id, "Page: /cart")
    task_count = len(await TaskRepository(session).list(project_id=planned_project.id))

    request = await war_room.request_from(
        session,
        registry,
        planned_project.id,
        requester.id,
        Role.BACKEND,
        "Add GET /api/cart/total",
        acceptance_checks=["GET /api/cart/total returns the cart total"],
        bus=bus,
    )

    assert request.role == "backend"
    assert request.state is TaskState.READY
    assert request.display_id == f"WEB-{task_count + 1:03d}"
    assert request.inputs["requested_by"] == requester.display_id
    refreshed = await TaskRepository(session).get(requester.id)
    assert refreshed is not None
    assert request.id in [d.id for d in refreshed.depends_on]
    review = await _events(session, planned_project.id, "REVIEW_REQUIRED")
    assert len(review) == 1
    assert review[0].requires_review is True
    assert review[0].task_id == request.id
    assert review[0].payload["requested_by_task"] == requester.display_id


async def test_request_from_a_role_not_on_the_team_recruits_it(
    session: AsyncSession, bus: EventBus, registry: BlueprintRegistry, planned_project: Project
) -> None:
    room = await war_room.open(session, registry, planned_project.id, bus=bus)
    requester = await _task(session, planned_project.id, "Deploy")

    await war_room.request_from(
        session,
        registry,
        planned_project.id,
        requester.id,
        Role.MAINTENANCE,
        "Write a runbook",
        acceptance_checks=["docs/runbook.md exists"],
        blocking=False,
        bus=bus,
    )

    assert "maintenance" in room.required_roles
    members = await WarRoomRepository(session).members(room.id)
    assert "MAINT" in [_code(agent.display_id) for _m, agent in members]
    refreshed = await TaskRepository(session).get(requester.id)
    assert refreshed is not None
    assert all(d.role != "maintenance" for d in refreshed.depends_on)  # non-blocking


async def test_request_from_needs_an_open_war_room(
    session: AsyncSession, bus: EventBus, registry: BlueprintRegistry, planned_project: Project
) -> None:
    requester = await _task(session, planned_project.id, "Deploy")

    with pytest.raises(war_room.WarRoomNotOpen):
        await war_room.request_from(
            session,
            registry,
            planned_project.id,
            requester.id,
            Role.QA,
            "x",
            acceptance_checks=["y"],
            bus=bus,
        )


async def test_close_releases_every_agent_archives_the_room_and_keeps_artifacts(
    session: AsyncSession,
    bus: EventBus,
    registry: BlueprintRegistry,
    planned_project: Project,
    tmp_path: Path,
    archive_dir: Path,
) -> None:
    (tmp_path / "ws" / "openapi.yaml").write_text("openapi: 3.1.0")
    room = await war_room.open(session, registry, planned_project.id, bus=bus)
    members = await WarRoomRepository(session).members(room.id)
    agent_repo = AgentRepository(session)
    # One member mid-review, one assigned - close must still release both.
    reviewing = members[0][1]
    for state in (AgentState.ASSIGNED, AgentState.WORKING, AgentState.REVIEW):
        await agent_repo.update_state(reviewing.id, state)
    await agent_repo.update_state(members[1][1].id, AgentState.ASSIGNED)

    closed = await war_room.close(session, planned_project.id, bus=bus)

    assert closed.status == "archived"
    assert closed.closed_at is not None
    agents = await agent_repo.list(project_id=planned_project.id)
    assert len(agents) == 8
    assert all(a.state is AgentState.RETIRED for a in agents)
    archived = archive_dir / planned_project.id / room.id / "openapi.yaml"
    assert archived.read_text() == "openapi: 3.1.0"
    assert closed.artifacts == [
        {
            "path": "openapi.yaml",
            "kind": "openapi",
            "size_bytes": len("openapi: 3.1.0"),
            "sha256": hashlib.sha256(b"openapi: 3.1.0").hexdigest(),
            "archived_path": str(archived),
        }
    ]
    closed_events = await _events(session, planned_project.id, "WAR_ROOM_CLOSED")
    assert len(closed_events[0].payload["retired"]) == 8
    # The archived room (and its manifest) is what view() shows afterwards.
    view = await war_room.view(session, planned_project.id)
    assert view.war_room.id == room.id
    assert [a.path for a in view.artifacts] == ["openapi.yaml"]


async def test_close_refuses_while_a_member_is_working_and_changes_nothing(
    session: AsyncSession,
    bus: EventBus,
    registry: BlueprintRegistry,
    planned_project: Project,
    archive_dir: Path,
) -> None:
    room = await war_room.open(session, registry, planned_project.id, bus=bus)
    working = (await WarRoomRepository(session).members(room.id))[2][1]
    agent_repo = AgentRepository(session)
    await agent_repo.update_state(working.id, AgentState.ASSIGNED)
    await agent_repo.update_state(working.id, AgentState.WORKING)

    with pytest.raises(war_room.WarRoomBusy, match=working.display_id):
        await war_room.close(session, planned_project.id, bus=bus)

    assert (await session.get(WarRoom, room.id)).status == "open"  # type: ignore[union-attr]
    agents = await agent_repo.list(project_id=planned_project.id)
    assert not any(a.state is AgentState.RETIRED for a in agents)


async def test_close_without_an_open_room_raises(
    session: AsyncSession, bus: EventBus, project: Project
) -> None:
    with pytest.raises(war_room.WarRoomNotOpen):
        await war_room.close(session, project.id, bus=bus)


async def test_a_project_can_reopen_after_close(
    session: AsyncSession,
    bus: EventBus,
    registry: BlueprintRegistry,
    planned_project: Project,
    archive_dir: Path,
) -> None:
    first = await war_room.open(session, registry, planned_project.id, bus=bus)
    await war_room.close(session, planned_project.id, bus=bus)

    second = await war_room.open(session, registry, planned_project.id, bus=bus)

    assert second.id != first.id
    view = await war_room.view(session, planned_project.id)
    assert view.war_room.id == second.id  # the open room wins over the archived one
