"""GET /api/v1/projects/{id}/events streams events as Server-Sent Events."""

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web_department.api import events as sse
from web_department.collaboration.events import Event, EventBus, EventType, StreamedEvent, emit
from web_department.db.repositories import ProjectRepository


async def _frames(lines: AsyncIterator[str], count: int) -> list[dict[str, str]]:
    """Read ``count`` SSE data frames (skipping comments) into {field: value} dicts."""

    async def read() -> list[dict[str, str]]:
        frames: list[dict[str, str]] = []
        current: dict[str, str] = {}
        async for line in lines:
            if line.startswith(":"):
                continue
            if line == "":
                if current:
                    frames.append(current)
                    current = {}
                    if len(frames) == count:
                        break
                continue
            field, _, value = line.partition(": ")
            current[field] = value
        return frames

    return await asyncio.wait_for(read(), timeout=10)


async def _wait_connected(lines: AsyncIterator[str]) -> None:
    assert await asyncio.wait_for(anext(lines), timeout=10) == ": connected"


async def test_streams_new_events_in_order(
    live_server: str, bus: EventBus, committed_project_id: str
) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        async with client.stream(
            "GET", f"{live_server}/api/v1/projects/{committed_project_id}/events"
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache"
            lines = response.aiter_lines()
            await _wait_connected(lines)

            types = [EventType.TASK_CREATED, EventType.TASK_STARTED, EventType.TASK_COMPLETED]
            published = [
                await emit(t, project_id=committed_project_id, bus=bus, payload={"n": i})
                for i, t in enumerate(types)
            ]
            frames = await _frames(lines, 3)

    assert [f["event"] for f in frames] == [t.value for t in types]
    assert [json.loads(f["data"])["event_id"] for f in frames] == [e.event_id for e in published]
    assert [json.loads(f["data"])["payload"]["n"] for f in frames] == [0, 1, 2]
    assert all(f["id"] for f in frames)
    assert json.loads(frames[0]["data"])["timestamp"].endswith("Z")


async def test_from_id_zero_replays_history_then_goes_live(
    live_server: str, bus: EventBus, committed_project_id: str
) -> None:
    first = await emit(EventType.TASK_CREATED, project_id=committed_project_id, bus=bus)
    async with httpx.AsyncClient(timeout=15) as client:
        async with client.stream(
            "GET", f"{live_server}/api/v1/projects/{committed_project_id}/events?from_id=0"
        ) as response:
            second_task = asyncio.ensure_future(
                emit(EventType.TASK_STARTED, project_id=committed_project_id, bus=bus)
            )
            frames = await _frames(response.aiter_lines(), 2)
            second = await second_task

    assert [json.loads(f["data"])["event_id"] for f in frames] == [first.event_id, second.event_id]


async def test_last_event_id_header_resumes_after_that_event(
    live_server: str, bus: EventBus, committed_project_id: str
) -> None:
    await emit(EventType.TASK_CREATED, project_id=committed_project_id, bus=bus)
    await emit(EventType.TASK_STARTED, project_id=committed_project_id, bus=bus)
    third = await emit(EventType.TASK_COMPLETED, project_id=committed_project_id, bus=bus)
    history = await bus.read(committed_project_id, "0", block_ms=None)

    async with httpx.AsyncClient(timeout=15) as client:
        async with client.stream(
            "GET",
            f"{live_server}/api/v1/projects/{committed_project_id}/events",
            headers={"Last-Event-ID": history[1].stream_id},
        ) as response:
            frames = await _frames(response.aiter_lines(), 1)

    assert [json.loads(f["data"])["event_id"] for f in frames] == [third.event_id]


async def test_only_the_requested_projects_events_are_streamed(
    live_server: str,
    bus: EventBus,
    committed_project_id: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as own:
        other = await ProjectRepository(own).create(title="other", requester_id="u")
        await own.commit()
        other_id = other.id
    async with httpx.AsyncClient(timeout=15) as client:
        async with client.stream(
            "GET", f"{live_server}/api/v1/projects/{committed_project_id}/events"
        ) as response:
            lines = response.aiter_lines()
            await _wait_connected(lines)
            await emit(EventType.TASK_CREATED, project_id=other_id, bus=bus)
            mine = await emit(EventType.TASK_STARTED, project_id=committed_project_id, bus=bus)
            frames = await _frames(lines, 1)

    assert json.loads(frames[0]["data"])["event_id"] == mine.event_id


async def test_unknown_project_is_404_and_cannot_read_the_global_stream(live_server: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        missing = await client.get(f"{live_server}/api/v1/projects/prj_missing/events")
        sneaky = await client.get(f"{live_server}/api/v1/projects/all/events")

    assert missing.status_code == 404
    assert sneaky.status_code == 404


async def test_invalid_resume_ids_are_rejected(live_server: str, committed_project_id: str) -> None:
    url = f"{live_server}/api/v1/projects/{committed_project_id}/events"
    async with httpx.AsyncClient(timeout=15) as client:
        bad_header = await client.get(url, headers={"Last-Event-ID": "nope"})
        bad_query = await client.get(url, params={"from_id": "nope"})

    assert bad_header.status_code == 400
    assert bad_query.status_code == 422


async def test_idle_connection_receives_keepalive_comments(
    live_server: str,
    committed_project_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sse, "HEARTBEAT_MS", 200)
    async with httpx.AsyncClient(timeout=15) as client:
        async with client.stream(
            "GET", f"{live_server}/api/v1/projects/{committed_project_id}/events"
        ) as response:
            lines = response.aiter_lines()
            await _wait_connected(lines)
            comment = ""
            while not comment:
                comment = await asyncio.wait_for(anext(lines), timeout=10)
            assert comment == ": keepalive"


def test_format_sse_frame() -> None:
    event = Event(event=EventType.DEPLOYED, project_id="prj_1", event_id="evt_" + "0" * 26)

    frame = sse.format_sse(StreamedEvent("17-0", event))

    assert frame.startswith("id: 17-0\nevent: DEPLOYED\ndata: {")
    assert frame.endswith("}\n\n")
    assert frame.count("\n") == 4
