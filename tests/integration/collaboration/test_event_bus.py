"""EventBus: DB audit row + Redis streams, ordering, deferral, failure handling, auto-emit."""

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from web_department.collaboration.events import (
    Event,
    EventBus,
    EventType,
    StreamedEvent,
    emit,
    set_event_bus,
)
from web_department.db import base
from web_department.db.models import Agent, Project, Task
from web_department.db.models import Event as EventRow
from web_department.db.repositories import (
    AgentRepository,
    BlueprintRepository,
    ProjectRepository,
    TaskRepository,
)
from web_department.db.states import AgentState, ProjectState, TaskState


async def _rows(engine: AsyncEngine, project_id: str) -> list[EventRow]:
    async with async_sessionmaker(engine)() as check:
        result = await check.scalars(
            select(EventRow).where(EventRow.project_id == project_id).order_by(EventRow.id)
        )
        return list(result.all())


async def _take(stream: AsyncIterator[StreamedEvent], n: int) -> list[StreamedEvent]:
    async def collect() -> list[StreamedEvent]:
        return [await anext(stream) for _ in range(n)]

    return await asyncio.wait_for(collect(), timeout=10)


async def test_publish_writes_db_row_and_both_streams(
    bus: EventBus, redis_client: Redis, engine: AsyncEngine, committed_project_id: str
) -> None:
    event = Event(
        event=EventType.TASK_COMPLETED,
        project_id=committed_project_id,
        status="completed",
        requires_review=True,
        payload={"files_changed": 4},
    )

    returned = await bus.publish(event)

    assert returned is event
    (row,) = await _rows(engine, committed_project_id)
    assert row.id == event.event_id
    assert row.type == "TASK_COMPLETED"
    assert row.status == "completed"
    assert row.requires_review is True
    assert row.payload == {"files_changed": 4}
    assert row.timestamp == event.timestamp
    for key in (f"events:{committed_project_id}", "events:all"):
        entries = await redis_client.xrange(key)
        assert len(entries) == 1
        assert entries[0][1]["type"] == "TASK_COMPLETED"
        assert entries[0][1]["event_id"] == event.event_id
        assert Event.model_validate_json(entries[0][1]["data"]) == event


async def test_publish_with_session_defers_streaming_until_dispatch(
    bus: EventBus, redis_client: Redis, session: AsyncSession, project: Project
) -> None:
    event = Event(event=EventType.TASK_CREATED, project_id=project.id)

    await bus.publish(event, session=session)

    assert await session.get(EventRow, event.event_id) is not None
    assert await redis_client.xlen(f"events:{project.id}") == 0

    await session.commit()
    assert await bus.dispatch_pending(session) == 1

    assert await redis_client.xlen(f"events:{project.id}") == 1
    assert await bus.dispatch_pending(session) == 0
    assert await redis_client.xlen(f"events:{project.id}") == 1


async def test_rolled_back_events_are_never_streamed(
    bus: EventBus, redis_client: Redis, session: AsyncSession, project: Project
) -> None:
    project_id = project.id
    await session.commit()
    await bus.publish(Event(event=EventType.TASK_CREATED, project_id=project_id), session=session)

    await session.rollback()

    assert await bus.dispatch_pending(session) == 0
    assert await redis_client.xlen(f"events:{project_id}") == 0
    assert await redis_client.xlen("events:all") == 0


async def test_publish_for_unknown_project_fails_and_streams_nothing(
    bus: EventBus, redis_client: Redis
) -> None:
    with pytest.raises(IntegrityError):
        await bus.publish(Event(event=EventType.TASK_CREATED, project_id="prj_does_not_exist"))

    assert await redis_client.xlen("events:all") == 0


async def test_event_without_project_only_goes_to_global_stream(
    bus: EventBus, redis_client: Redis, engine: AsyncEngine
) -> None:
    event = Event(event=EventType.ALERT_RAISED, payload={"why": "pool"})

    await bus.publish(event)

    assert await redis_client.keys("events:*") == ["events:all"]
    async with async_sessionmaker(engine)() as check:
        row = await check.get(EventRow, event.event_id)
    assert row is not None
    assert row.project_id is None


async def test_subscriber_receives_events_in_publish_order(
    bus: EventBus, committed_project_id: str
) -> None:
    stream = bus.subscribe(committed_project_id)
    reader = asyncio.ensure_future(_take(stream, 3))
    await asyncio.sleep(0.2)
    types = [EventType.TASK_CREATED, EventType.TASK_STARTED, EventType.TASK_COMPLETED]

    published = [
        await emit(t, project_id=committed_project_id, bus=bus, payload={"n": i})
        for i, t in enumerate(types)
    ]
    received = await reader

    assert [r.event.event_id for r in received] == [e.event_id for e in published]
    assert [r.event.event for r in received] == types
    assert [r.event.payload["n"] for r in received] == [0, 1, 2]
    ids = [r.stream_id for r in received]
    assert ids == sorted(ids, key=lambda i: tuple(map(int, i.split("-"))))
    await stream.aclose()  # type: ignore[attr-defined]


async def test_subscribe_replays_from_zero_then_goes_live(
    bus: EventBus, committed_project_id: str
) -> None:
    first = await emit(EventType.TASK_CREATED, project_id=committed_project_id, bus=bus)
    second = await emit(EventType.TASK_STARTED, project_id=committed_project_id, bus=bus)

    stream = bus.subscribe(committed_project_id, from_id="0")
    replayed = await _take(stream, 2)
    live_task = asyncio.ensure_future(_take(stream, 1))
    await asyncio.sleep(0.2)
    third = await emit(EventType.TASK_COMPLETED, project_id=committed_project_id, bus=bus)

    assert [r.event.event_id for r in replayed] == [first.event_id, second.event_id]
    assert [r.event.event_id for r in await live_task] == [third.event_id]
    await stream.aclose()  # type: ignore[attr-defined]


async def test_subscribe_resumes_after_a_given_stream_id(
    bus: EventBus, committed_project_id: str
) -> None:
    await emit(EventType.TASK_CREATED, project_id=committed_project_id, bus=bus)
    await emit(EventType.TASK_STARTED, project_id=committed_project_id, bus=bus)
    third = await emit(EventType.TASK_COMPLETED, project_id=committed_project_id, bus=bus)
    everything = await bus.read(committed_project_id, "0", block_ms=None)

    resumed = await bus.read(committed_project_id, everything[1].stream_id, block_ms=None)

    assert [r.event.event_id for r in resumed] == [third.event_id]


async def test_default_subscription_only_sees_new_events(
    bus: EventBus, committed_project_id: str
) -> None:
    await emit(EventType.TASK_CREATED, project_id=committed_project_id, bus=bus)
    stream = bus.subscribe(committed_project_id)
    reader = asyncio.ensure_future(_take(stream, 1))
    await asyncio.sleep(0.2)

    fresh = await emit(EventType.TASK_STARTED, project_id=committed_project_id, bus=bus)

    assert [r.event.event_id for r in await reader] == [fresh.event_id]
    await stream.aclose()  # type: ignore[attr-defined]


async def test_global_subscription_sees_every_project(
    bus: EventBus, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = []
    for _ in range(2):
        async with session_factory() as own:
            created = await ProjectRepository(own).create(title="p", requester_id="u")
            await own.commit()
            ids.append(created.id)
    stream = bus.subscribe(None)
    reader = asyncio.ensure_future(_take(stream, 2))
    await asyncio.sleep(0.2)

    for project_id in ids:
        await emit(EventType.TASK_CREATED, project_id=project_id, bus=bus)

    assert [r.event.project_id for r in await reader] == ids
    await stream.aclose()  # type: ignore[attr-defined]


async def test_read_times_out_with_no_events(bus: EventBus, committed_project_id: str) -> None:
    assert await bus.read(committed_project_id, "0", block_ms=50) == []


async def test_redis_outage_keeps_the_audit_row_and_does_not_raise(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    committed_project_id: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from redis.asyncio import Redis as RedisClient

    from web_department.logging import configure_logging

    configure_logging("INFO")
    dead = EventBus(RedisClient.from_url("redis://127.0.0.1:1/0"), session_factory)

    event = await emit(EventType.TASK_CREATED, project_id=committed_project_id, bus=dead)

    assert [r.id for r in await _rows(engine, committed_project_id)] == [event.event_id]
    logged = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    failure = next(r for r in logged if r["event"] == "event_stream_publish_failed")
    assert failure["attempts"] == 3
    assert failure["event_id"] == event.event_id
    await dead.close()


async def test_emit_builds_and_publishes_an_event(
    bus: EventBus, redis_client: Redis, committed_project_id: str
) -> None:
    event = await emit(
        EventType.APPROVAL_REQUESTED,
        project_id=committed_project_id,
        bus=bus,
        requires_review=True,
    )

    assert event.event is EventType.APPROVAL_REQUESTED
    assert event.requires_review is True
    assert await redis_client.xlen(f"events:{committed_project_id}") == 1


async def _events_for(session: AsyncSession, project_id: str) -> list[EventRow]:
    result = await session.scalars(
        select(EventRow).where(EventRow.project_id == project_id).order_by(EventRow.id)
    )
    return list(result.all())


async def test_project_state_change_emits_state_changed(
    bus: EventBus, redis_client: Redis, session: AsyncSession, project: Project
) -> None:
    await ProjectRepository(session, bus=bus).update_state(project.id, ProjectState.ANALYZING)
    await session.commit()
    await bus.dispatch_pending(session)

    (row,) = await _events_for(session, project.id)
    assert row.type == "STATE_CHANGED"
    assert row.status == "analyzing"
    assert row.task_id is None
    assert row.payload == {
        "object": "project",
        "id": project.id,
        "from": "RECEIVED",
        "to": "ANALYZING",
    }
    streamed = await bus.read(project.id, "0", block_ms=None)
    assert [s.event.event_id for s in streamed] == [row.id]


async def test_rejected_transition_emits_nothing(
    bus: EventBus, session: AsyncSession, project: Project
) -> None:
    with pytest.raises(Exception, match="Invalid ProjectState transition"):
        await ProjectRepository(session, bus=bus).update_state(project.id, ProjectState.DEPLOYING)

    assert await _events_for(session, project.id) == []
    assert await bus.dispatch_pending(session) == 0


async def test_task_state_change_carries_task_and_project(
    bus: EventBus, session: AsyncSession, project: Project
) -> None:
    repo = TaskRepository(session, bus=bus)
    task: Task = await repo.create(
        display_id="WEB-001", project_id=project.id, title="t", role="backend"
    )

    await repo.update_state(task.id, TaskState.READY)

    (row,) = await _events_for(session, project.id)
    assert row.task_id == task.id
    assert row.payload["to"] == "READY"
    assert row.payload["object"] == "task"


async def test_pool_agent_state_change_has_no_project(bus: EventBus, session: AsyncSession) -> None:
    await BlueprintRepository(session).create(id="bp", version="1", yaml="y", checksum="c")
    repo = AgentRepository(session, bus=bus)
    agent: Agent = await repo.create(
        display_id="WEB-XX-001", blueprint_id="bp", blueprint_version="1"
    )

    await repo.update_state(agent.id, AgentState.PROVISIONED)

    row = await session.scalar(select(EventRow).where(EventRow.agent_id == agent.id))
    assert row is not None
    assert row.project_id is None
    assert row.payload["object"] == "agent"


async def test_session_scope_commits_then_streams_state_changes(
    bus: EventBus, redis_client: Redis, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(base, "get_engine", lambda: engine)
    set_event_bus(bus)
    try:
        async with base.session_scope() as scoped:
            project = await ProjectRepository(scoped).create(title="scope", requester_id="u")
            await ProjectRepository(scoped).update_state(project.id, ProjectState.ANALYZING)
            await ProjectRepository(scoped).update_state(project.id, ProjectState.PLANNING)
            assert await redis_client.xlen(f"events:{project.id}") == 0
    finally:
        set_event_bus(None)

    streamed = await bus.read(project.id, "0", block_ms=None)
    assert [s.event.payload["to"] for s in streamed] == ["ANALYZING", "PLANNING"]
    assert len(await _rows(engine, project.id)) == 2


async def test_session_scope_streams_nothing_when_the_block_fails(
    bus: EventBus, redis_client: Redis, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(base, "get_engine", lambda: engine)
    set_event_bus(bus)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            async with base.session_scope() as scoped:
                project = await ProjectRepository(scoped).create(title="x", requester_id="u")
                await ProjectRepository(scoped).update_state(project.id, ProjectState.ANALYZING)
                raise RuntimeError("boom")
    finally:
        set_event_bus(None)

    assert await redis_client.xlen("events:all") == 0
    assert await _rows(engine, project.id) == []
