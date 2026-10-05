"""Tasks: dependency relations, constraints and state changes."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.db.models import Project, Task
from web_department.db.repositories import ProjectRepository, TaskRepository
from web_department.db.states import InvalidTransition, TaskState


async def _task(repo: TaskRepository, project: Project, display_id: str) -> Task:
    return await repo.create(
        display_id=display_id, project_id=project.id, title=f"Task {display_id}", role="backend"
    )


async def test_create_task_defaults(session: AsyncSession, project: Project) -> None:
    task = await _task(TaskRepository(session), project, "WEB-001")

    assert task.id.startswith("tsk_")
    assert task.state is TaskState.CREATED
    assert task.retries == 0
    assert task.inputs == {}
    assert task.outputs == {}
    assert task.acceptance_checks == []
    assert task.assigned_agent_id is None
    assert task.depends_on == []
    assert task.dependents == []


async def test_dependencies_are_recorded_in_both_directions(
    session: AsyncSession, project: Project
) -> None:
    repo = TaskRepository(session)
    schema = await _task(repo, project, "WEB-001")
    api = await _task(repo, project, "WEB-002")
    ui = await _task(repo, project, "WEB-003")

    await repo.add_dependency(api, schema)
    await repo.add_dependency(ui, schema)
    await repo.add_dependency(ui, api)

    assert api.depends_on == [schema]
    assert {t.id for t in ui.depends_on} == {schema.id, api.id}
    assert {t.id for t in schema.dependents} == {api.id, ui.id}
    assert schema.depends_on == []

    await session.commit()
    session.expunge_all()
    reloaded = await repo.get(ui.id)
    assert reloaded is not None
    assert {t.display_id for t in reloaded.depends_on} == {"WEB-001", "WEB-002"}


async def test_dependency_across_projects_is_rejected(
    session: AsyncSession, project: Project
) -> None:
    other = await ProjectRepository(session).create(title="Other", requester_id="u-2")
    repo = TaskRepository(session)
    mine = await _task(repo, project, "WEB-001")
    theirs = await _task(repo, other, "WEB-001")

    with pytest.raises(ValueError, match="same project"):
        await repo.add_dependency(mine, theirs)


async def test_task_cannot_depend_on_itself(session: AsyncSession, project: Project) -> None:
    repo = TaskRepository(session)
    task = await _task(repo, project, "WEB-001")

    with pytest.raises(IntegrityError, match="no_self_dependency"):
        await repo.add_dependency(task, task)


async def test_duplicate_dependency_is_rejected(session: AsyncSession, project: Project) -> None:
    repo = TaskRepository(session)
    a = await _task(repo, project, "WEB-001")
    b = await _task(repo, project, "WEB-002")
    await repo.add_dependency(b, a)
    await session.commit()

    with pytest.raises(IntegrityError):
        await repo.add_dependency(b, a)


async def test_display_id_is_unique_per_project_only(
    session: AsyncSession, project: Project
) -> None:
    other = await ProjectRepository(session).create(title="Other", requester_id="u-2")
    repo = TaskRepository(session)
    await _task(repo, project, "WEB-001")
    await _task(repo, other, "WEB-001")
    await session.commit()

    with pytest.raises(IntegrityError):
        await _task(repo, project, "WEB-001")


async def test_task_state_transitions(session: AsyncSession, project: Project) -> None:
    repo = TaskRepository(session)
    task = await _task(repo, project, "WEB-001")

    for state in (TaskState.READY, TaskState.ASSIGNED, TaskState.IN_PROGRESS, TaskState.REVIEW):
        await repo.update_state(task.id, state)
    with pytest.raises(InvalidTransition):
        await repo.update_state(task.id, TaskState.CREATED)
    await repo.update_state(task.id, TaskState.COMPLETED)

    assert task.state is TaskState.COMPLETED


async def test_list_tasks_of_a_project(session: AsyncSession, project: Project) -> None:
    other = await ProjectRepository(session).create(title="Other", requester_id="u-2")
    repo = TaskRepository(session)
    first = await _task(repo, project, "WEB-001")
    second = await _task(repo, project, "WEB-002")
    await _task(repo, other, "WEB-003")

    tasks = await repo.list(project_id=project.id)

    assert [t.id for t in tasks] == [first.id, second.id]


async def test_set_retries_records_how_many_failed_attempts_a_task_used(
    session: AsyncSession, project: Project
) -> None:
    repo = TaskRepository(session)
    task = await _task(repo, project, "WEB-001")

    updated = await repo.set_retries(task.id, 2)

    assert updated.retries == 2
    assert task.retries == 2  # same row, in-session


async def test_set_retries_of_an_unknown_task_raises(session: AsyncSession) -> None:
    with pytest.raises(LookupError):
        await TaskRepository(session).set_retries("tsk_missing", 1)
