"""Projects: creation, ids and validated state changes."""

import re

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.db.models import Project
from web_department.db.repositories import ProjectRepository
from web_department.db.states import InvalidTransition, ProjectState


async def test_create_project_sets_defaults(session: AsyncSession) -> None:
    repo = ProjectRepository(session)

    project = await repo.create(title="Clothing store", requester_id="u-9")

    assert re.match(r"^prj_[0-9A-HJKMNP-TV-Z]{26}$", project.id)
    assert project.state is ProjectState.RECEIVED
    assert project.stack == "golden-v1"
    assert project.wrs_version is None
    assert project.created_at is not None
    assert project.updated_at is not None


async def test_get_and_list_projects(session: AsyncSession, project: Project) -> None:
    repo = ProjectRepository(session)
    other = await repo.create(title="Other", requester_id="u-2")

    assert await repo.get(project.id) is project
    assert await repo.get("prj_missing") is None
    assert [p.id for p in await repo.list(requester_id="u-2")] == [other.id]
    assert {p.id for p in await repo.list()} >= {project.id, other.id}
    assert await repo.list(limit=1, offset=10_000) == []


async def test_valid_transition_is_persisted(session: AsyncSession, project: Project) -> None:
    repo = ProjectRepository(session)

    await repo.update_state(project.id, ProjectState.ANALYZING)
    await session.commit()
    session.expunge_all()

    reloaded = await repo.get(project.id)
    assert reloaded is not None
    assert reloaded.state is ProjectState.ANALYZING


async def test_invalid_transition_raises_and_leaves_state_unchanged(
    session: AsyncSession, project: Project
) -> None:
    repo = ProjectRepository(session)

    with pytest.raises(InvalidTransition):
        await repo.update_state(project.id, ProjectState.DEPLOYING)

    session.expunge_all()
    reloaded = await repo.get(project.id)
    assert reloaded is not None
    assert reloaded.state is ProjectState.RECEIVED


async def test_project_can_walk_the_whole_lifecycle(
    session: AsyncSession, project: Project
) -> None:
    repo = ProjectRepository(session)
    path = [
        ProjectState.ANALYZING,
        ProjectState.PLANNING,
        ProjectState.ARCHITECTING,
        ProjectState.DEVELOPING,
        ProjectState.INTEGRATING,
        ProjectState.TESTING,
        ProjectState.DEVELOPING,
        ProjectState.INTEGRATING,
        ProjectState.TESTING,
        ProjectState.SECURITY_REVIEW,
        ProjectState.AWAITING_APPROVAL,
        ProjectState.DEPLOYING,
        ProjectState.MONITORING,
        ProjectState.COMPLETED,
    ]

    for state in path:
        await repo.update_state(project.id, state)

    assert project.state is ProjectState.COMPLETED
    with pytest.raises(InvalidTransition):
        await repo.update_state(project.id, ProjectState.DEVELOPING)


async def test_blocked_project_can_replan(session: AsyncSession, project: Project) -> None:
    repo = ProjectRepository(session)
    for state in (
        ProjectState.ANALYZING,
        ProjectState.PLANNING,
        ProjectState.ARCHITECTING,
        ProjectState.DEVELOPING,
        ProjectState.BLOCKED,
        ProjectState.PLANNING,
    ):
        await repo.update_state(project.id, state)

    assert project.state is ProjectState.PLANNING


async def test_update_state_of_unknown_project_raises(session: AsyncSession) -> None:
    with pytest.raises(LookupError):
        await ProjectRepository(session).update_state("prj_missing", ProjectState.ANALYZING)
