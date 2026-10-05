"""DR-003: events and approvals reject UPDATE/DELETE in the ORM and in the database."""

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.db.models import AppendOnlyViolation, Approval, Event, Project
from web_department.db.repositories import ApprovalRepository, EventRepository


async def _event(session: AsyncSession, project: Project) -> Event:
    event = await EventRepository(session).create(
        type="TASK_CREATED", project_id=project.id, payload={"n": 1}
    )
    await session.commit()
    return event


async def _approval(session: AsyncSession, project: Project) -> Approval:
    approval = await ApprovalRepository(session).create(
        project_id=project.id,
        action_type="production_deployment",
        summary="Deploy v1",
        status="approved",
        approver_id="boss",
    )
    await session.commit()
    return approval


async def test_events_can_be_inserted_and_read(session: AsyncSession, project: Project) -> None:
    repo = EventRepository(session)
    first = await repo.create(type="TASK_CREATED", project_id=project.id, payload={"a": 1})
    second = await repo.create(type="TASK_STARTED", project_id=project.id)

    assert first.id.startswith("evt_")
    assert first.timestamp is not None
    assert second.payload == {}
    assert [e.id for e in await repo.list(project_id=project.id)] == [first.id, second.id]


async def test_approvals_can_be_inserted_and_read(session: AsyncSession, project: Project) -> None:
    approval = await _approval(session, project)

    assert approval.id.startswith("apr_")
    assert approval.decided_at is None
    assert await ApprovalRepository(session).get(approval.id) is approval


async def test_orm_rejects_updating_an_event(session: AsyncSession, project: Project) -> None:
    event = await _event(session, project)

    event.type = "TASK_COMPLETED"
    with pytest.raises(AppendOnlyViolation, match="Event"):
        await session.flush()


async def test_orm_rejects_deleting_an_event(session: AsyncSession, project: Project) -> None:
    event = await _event(session, project)

    await session.delete(event)
    with pytest.raises(AppendOnlyViolation, match="Event"):
        await session.flush()


async def test_orm_rejects_updating_and_deleting_an_approval(
    session: AsyncSession, project: Project
) -> None:
    approval = await _approval(session, project)

    approval.status = "rejected"
    with pytest.raises(AppendOnlyViolation, match="Approval"):
        await session.flush()
    await session.rollback()

    await session.delete(approval)
    with pytest.raises(AppendOnlyViolation, match="Approval"):
        await session.flush()


async def test_orm_rejects_bulk_update_and_delete(session: AsyncSession, project: Project) -> None:
    await _event(session, project)
    await _approval(session, project)

    with pytest.raises(AppendOnlyViolation):
        await session.execute(update(Event).values(type="X"))
    with pytest.raises(AppendOnlyViolation):
        await session.execute(delete(Event))
    with pytest.raises(AppendOnlyViolation):
        await session.execute(update(Approval).values(status="X"))
    with pytest.raises(AppendOnlyViolation):
        await session.execute(delete(Approval))


@pytest.mark.parametrize("table", ["events", "approvals"])
async def test_database_trigger_rejects_update(
    session: AsyncSession, project: Project, table: str
) -> None:
    await _event(session, project)
    await _approval(session, project)

    column = "type" if table == "events" else "status"
    with pytest.raises(DBAPIError, match="UPDATE on .* is not allowed: table is append-only"):
        await session.execute(text(f"UPDATE {table} SET {column} = 'x'"))


@pytest.mark.parametrize("table", ["events", "approvals"])
async def test_database_trigger_rejects_delete(
    session: AsyncSession, project: Project, table: str
) -> None:
    await _event(session, project)
    await _approval(session, project)

    with pytest.raises(DBAPIError, match="DELETE on .* is not allowed: table is append-only"):
        await session.execute(text(f"DELETE FROM {table}"))


async def test_rows_survive_rejected_mutations(session: AsyncSession, project: Project) -> None:
    event_id = (await _event(session, project)).id

    with pytest.raises(DBAPIError):
        await session.execute(text("UPDATE events SET type = 'x'"))
    await session.rollback()
    session.expunge_all()

    reloaded = await EventRepository(session).get(event_id)
    assert reloaded is not None
    assert reloaded.type == "TASK_CREATED"


async def test_project_with_events_cannot_be_deleted(
    session: AsyncSession, project: Project
) -> None:
    await _event(session, project)

    with pytest.raises(DBAPIError):
        await session.execute(text("DELETE FROM projects"))
