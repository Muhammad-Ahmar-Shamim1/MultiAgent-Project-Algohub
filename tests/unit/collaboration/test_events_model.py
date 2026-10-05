"""Event model and EventType against SRS 7.3 (no Redis or DB)."""

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from web_department.collaboration.events import Event, EventType, stream_key

SRS_7_3_TYPES = {
    "TASK_CREATED", "TASK_ASSIGNED", "TASK_STARTED", "DEPENDENCY_WAITING", "TASK_COMPLETED",
    "REVIEW_REQUIRED", "TEST_FAILED", "FIX_REQUIRED", "SECURITY_BLOCKED", "APPROVAL_REQUESTED",
    "APPROVED", "REJECTED", "DEPLOYED", "ROLLED_BACK", "ALERT_RAISED",
}  # fmt: skip

SRS_7_3_EXAMPLE = {
    "event_id": "evt_01J8Z3K4M5N6P7Q8R9S0T1V2W3",
    "event": "TASK_COMPLETED",
    "project_id": "project_001",
    "task_id": "WEB-007",
    "agent_id": "WEB-FE-001",
    "status": "completed",
    "requires_review": True,
    "payload": {"files_changed": 4, "tests_passed": 18},
    "timestamp": "2026-09-19T17:00:00Z",
}


def test_event_type_has_every_srs_7_3_type_plus_the_documented_extras() -> None:
    values = {t.value for t in EventType}

    assert SRS_7_3_TYPES <= values
    assert values - SRS_7_3_TYPES == {
        "PROJECT_RECEIVED",
        "STATE_CHANGED",
        "TOOL_CALL",
        "WAR_ROOM_OPENED",
        "WAR_ROOM_CLOSED",
    }


def test_srs_example_json_parses_and_round_trips() -> None:
    event = Event.model_validate(SRS_7_3_EXAMPLE)

    assert event.event is EventType.TASK_COMPLETED
    assert event.requires_review is True
    assert event.payload == {"files_changed": 4, "tests_passed": 18}
    assert event.timestamp == datetime(2026, 9, 19, 17, 0, tzinfo=UTC)
    assert json.loads(event.model_dump_json()) == SRS_7_3_EXAMPLE


def test_defaults() -> None:
    event = Event(event=EventType.TASK_CREATED)

    assert event.event_id.startswith("evt_")
    assert len(event.event_id) == 30
    assert event.project_id is None
    assert event.task_id is None
    assert event.agent_id is None
    assert event.status is None
    assert event.requires_review is False
    assert event.payload == {}
    assert event.timestamp.tzinfo is UTC
    assert abs(datetime.now(UTC) - event.timestamp) < timedelta(seconds=5)


def test_event_ids_are_unique() -> None:
    assert len({Event(event=EventType.TASK_CREATED).event_id for _ in range(50)}) == 50


def test_timestamp_is_normalised_to_utc() -> None:
    plus_five = timezone(timedelta(hours=5))

    event = Event(
        event=EventType.TASK_CREATED, timestamp=datetime(2026, 9, 19, 22, 0, tzinfo=plus_five)
    )

    assert event.timestamp == datetime(2026, 9, 19, 17, 0, tzinfo=UTC)
    assert event.timestamp.utcoffset() == timedelta(0)


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Event(event=EventType.TASK_CREATED, timestamp=datetime(2026, 9, 19, 17, 0))


def test_unknown_event_type_and_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Event.model_validate({"event": "NOT_A_TYPE"})
    with pytest.raises(ValidationError):
        Event.model_validate({"event": "TASK_CREATED", "surprise": 1})


def test_malformed_event_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Event(event=EventType.TASK_CREATED, event_id="not-a-ulid")


def test_event_is_immutable() -> None:
    event = Event(event=EventType.TASK_CREATED)

    with pytest.raises(ValidationError):
        event.status = "x"  # type: ignore[misc]


def test_stream_keys() -> None:
    assert stream_key("prj_1") == "events:prj_1"
    assert stream_key(None) == "events:all"
