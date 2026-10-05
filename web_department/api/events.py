"""Server-Sent Events feed of a project's events (IR-003, SRS 8.2)."""

import re
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web_department.api.deps import get_bus, get_db_factory
from web_department.collaboration.events import EventBus, StreamedEvent
from web_department.db.repositories import ProjectRepository

router = APIRouter(prefix="/api/v1")

HEARTBEAT_MS = 15_000
_STREAM_ID = re.compile(r"^(\$|\d+(-\d+)?)$")


def format_sse(item: StreamedEvent) -> str:
    """Render one event as an SSE frame; ``id`` is the stream id so clients can resume."""
    return (
        f"id: {item.stream_id}\n"
        f"event: {item.event.event.value}\n"
        f"data: {item.event.model_dump_json()}\n\n"
    )


async def event_frames(
    bus: EventBus, request: Request, project_id: str, start: str
) -> AsyncIterator[str]:
    """Yield SSE frames for ``project_id`` until the client disconnects."""
    last = await bus.resolve_start(project_id, start)
    yield ": connected\n\n"
    while not await request.is_disconnected():
        items = await bus.read(project_id, last, block_ms=HEARTBEAT_MS)
        if not items:
            yield ": keepalive\n\n"
            continue
        for item in items:
            last = item.stream_id
            yield format_sse(item)


@router.get("/projects/{project_id}/events")
async def stream_project_events(
    project_id: str,
    request: Request,
    bus: Annotated[EventBus, Depends(get_bus)],
    db_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_db_factory)],
    from_id: Annotated[
        str, Query(pattern=r"^(\$|\d+(-\d+)?)$", description="$ = new only, 0 = replay all")
    ] = "$",
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Stream a project's events as ``text/event-stream``.

    Resume with the standard ``Last-Event-ID`` header, or ``?from_id=<stream id>``;
    ``?from_id=0`` replays the retained history before going live.
    """
    if last_event_id is not None and not _STREAM_ID.match(last_event_id):
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID")
    async with db_factory() as session:
        if await ProjectRepository(session).get(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
    return StreamingResponse(
        event_frames(bus, request, project_id, last_event_id or from_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
