"""Structured event system: every important action is an auditable event (FR-081, NFR-009).

An event is written to the append-only ``events`` table (the audit record) and to Redis Streams
``events:{project_id}`` and ``events:all`` (live feed). When published inside a caller's DB
transaction (``session=...``) the row commits atomically with the change, and the stream entry is
sent only after commit via ``dispatch_pending`` (``session_scope`` does this), so consumers never
see an event for a change that was rolled back.

Three types are not in the SRS 7.3 list: ``PROJECT_RECEIVED`` (named in FR-003),
``STATE_CHANGED`` (the automatic event for every project/task/agent state change), and
``TOOL_CALL`` (every workstation tool invocation, FR-081/FR-062).
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, NamedTuple

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web_department.config.settings import get_settings
from web_department.db.base import PENDING_EVENTS_KEY, get_session_factory, new_id
from web_department.db.models import Event as EventRow
from web_department.logging import get_logger

STREAM_ALL = "events:all"
PROJECT_STREAM_MAXLEN = 10_000
ALL_STREAM_MAXLEN = 100_000
STREAM_ATTEMPTS = 3

log = get_logger(__name__)


class EventType(StrEnum):
    """Event types from SRS 7.3, plus PROJECT_RECEIVED, STATE_CHANGED, TOOL_CALL and the War Room's
    WAR_ROOM_OPENED / WAR_ROOM_CLOSED (FR-080).
    """

    TASK_CREATED = "TASK_CREATED"
    TASK_ASSIGNED = "TASK_ASSIGNED"
    TASK_STARTED = "TASK_STARTED"
    DEPENDENCY_WAITING = "DEPENDENCY_WAITING"
    TASK_COMPLETED = "TASK_COMPLETED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    TEST_FAILED = "TEST_FAILED"
    FIX_REQUIRED = "FIX_REQUIRED"
    SECURITY_BLOCKED = "SECURITY_BLOCKED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DEPLOYED = "DEPLOYED"
    ROLLED_BACK = "ROLLED_BACK"
    ALERT_RAISED = "ALERT_RAISED"
    PROJECT_RECEIVED = "PROJECT_RECEIVED"
    STATE_CHANGED = "STATE_CHANGED"
    TOOL_CALL = "TOOL_CALL"
    WAR_ROOM_OPENED = "WAR_ROOM_OPENED"
    WAR_ROOM_CLOSED = "WAR_ROOM_CLOSED"


class Event(BaseModel):
    """An event as defined by the SRS 7.3 example JSON."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(default_factory=lambda: new_id("evt"), pattern=r"^evt_[0-9A-Z]{26}$")
    event: EventType
    project_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    status: str | None = None
    requires_review: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("timestamp")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class StreamedEvent(NamedTuple):
    """An event together with its Redis stream entry id (usable as SSE ``id`` / resume token)."""

    stream_id: str
    event: Event


def stream_key(project_id: str | None = None) -> str:
    """Redis stream for one project, or the global stream when ``project_id`` is None."""
    return STREAM_ALL if project_id is None else f"events:{project_id}"


def _to_row(event: Event) -> EventRow:
    return EventRow(
        id=event.event_id,
        type=event.event.value,
        project_id=event.project_id,
        task_id=event.task_id,
        agent_id=event.agent_id,
        status=event.status,
        requires_review=event.requires_review,
        payload=event.payload,
        timestamp=event.timestamp,
    )


class EventBus:
    """Publishes events to Postgres (audit) and Redis Streams (live), and reads them back."""

    def __init__(self, redis: Redis, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._redis = redis
        self._session_factory = session_factory

    async def publish(self, event: Event, *, session: AsyncSession | None = None) -> Event:
        """Persist ``event`` and stream it.

        Without ``session`` the row is committed in its own transaction, then streamed. With
        ``session`` the row joins the caller's transaction and streaming is deferred until
        ``dispatch_pending(session)`` after the caller commits. A DB failure raises; a Redis
        failure is retried ``STREAM_ATTEMPTS`` times, then logged (the audit row is durable).
        """
        row = _to_row(event)
        if session is not None:
            session.add(row)
            await session.flush()
            session.info.setdefault(PENDING_EVENTS_KEY, []).append(event)
            return event
        async with self._session_factory() as own:
            own.add(row)
            await own.commit()
        await self._stream(event)
        return event

    async def dispatch_pending(self, session: AsyncSession) -> int:
        """Stream events published on ``session``; call once after its transaction committed."""
        pending: list[Event] = session.info.pop(PENDING_EVENTS_KEY, [])
        for event in pending:
            await self._stream(event)
        return len(pending)

    async def _stream(self, event: Event) -> None:
        entry: dict[Any, Any] = {
            "event_id": event.event_id,
            "type": event.event.value,
            "data": event.model_dump_json(),
        }
        for attempt in range(1, STREAM_ATTEMPTS + 1):
            try:
                async with self._redis.pipeline(transaction=True) as pipe:
                    if event.project_id is not None:
                        pipe.xadd(
                            stream_key(event.project_id),
                            entry,
                            maxlen=PROJECT_STREAM_MAXLEN,
                            approximate=True,
                        )
                    pipe.xadd(STREAM_ALL, entry, maxlen=ALL_STREAM_MAXLEN, approximate=True)
                    await pipe.execute()
                return
            except (RedisError, OSError) as exc:
                if attempt == STREAM_ATTEMPTS:
                    log.error(
                        "event_stream_publish_failed",
                        event_id=event.event_id,
                        attempts=attempt,
                        error=repr(exc),
                    )
                    return
                await asyncio.sleep(0.1 * attempt)

    async def resolve_start(self, project_id: str | None, from_id: str = "$") -> str:
        """Turn ``"$"`` (only new events) into the stream's current last id; pass others through.

        Reading with a literal ``"$"`` repeatedly would miss events published between polls.
        """
        if from_id != "$":
            return from_id
        latest = await self._redis.xrevrange(stream_key(project_id), count=1)
        return str(latest[0][0]) if latest else "0-0"

    async def read(
        self,
        project_id: str | None,
        after_id: str,
        *,
        block_ms: int | None = 5000,
        count: int = 100,
    ) -> list[StreamedEvent]:
        """Return up to ``count`` events after ``after_id``; blocks up to ``block_ms``."""
        response = await self._redis.xread(
            {stream_key(project_id): after_id}, count=count, block=block_ms
        )
        items: list[StreamedEvent] = []
        for _key, entries in response or []:
            for stream_id, fields in entries:
                items.append(StreamedEvent(stream_id, Event.model_validate_json(fields["data"])))
        return items

    async def subscribe(
        self, project_id: str | None = None, from_id: str = "$", *, block_ms: int = 5000
    ) -> AsyncIterator[StreamedEvent]:
        """Yield events in order until the consumer stops iterating.

        ``from_id``: ``"$"`` = only new events, ``"0"`` = replay everything retained, or a stream
        id to resume after. ``project_id=None`` follows the global stream.
        """
        last = await self.resolve_start(project_id, from_id)
        while True:
            for item in await self.read(project_id, last, block_ms=block_ms):
                last = item.stream_id
                yield item

    async def close(self) -> None:
        """Close the Redis connection pool."""
        await self._redis.aclose()


_default_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Return the process-wide bus built from ``Settings.redis_url`` and the default engine."""
    global _default_bus
    if _default_bus is None:
        redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
        _default_bus = EventBus(redis, get_session_factory())
    return _default_bus


def set_event_bus(bus: EventBus | None) -> None:
    """Replace (or reset with None) the process-wide bus, e.g. in tests."""
    global _default_bus
    _default_bus = bus


async def close_event_bus() -> None:
    """Close and forget the process-wide bus, if one was created."""
    global _default_bus
    if _default_bus is not None:
        await _default_bus.close()
        _default_bus = None


async def emit(
    event_type: EventType,
    *,
    session: AsyncSession | None = None,
    bus: EventBus | None = None,
    **fields: Any,
) -> Event:
    """Build an ``Event`` from ``fields`` and publish it; the helper other modules call."""
    event = Event(event=event_type, **fields)
    return await (bus or get_event_bus()).publish(event, session=session)
