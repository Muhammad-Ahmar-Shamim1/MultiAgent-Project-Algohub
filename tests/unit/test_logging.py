"""Tests for web_department.logging."""

import json

import pytest

from web_department.logging import configure_logging, get_logger, log_context


def _records(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    lines = capsys.readouterr().out.strip().splitlines()
    return [json.loads(line) for line in lines]


def test_logs_are_json_with_level_and_timestamp(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")

    get_logger("t").info("hello", answer=42)

    (record,) = _records(capsys)
    assert record["event"] == "hello"
    assert record["answer"] == 42
    assert record["level"] == "info"
    assert str(record["timestamp"]).endswith("Z")


def test_context_ids_are_attached_inside_block_only(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    log = get_logger("t")

    with log_context(project_id="project_001", task_id="WEB-007", agent_id="WEB-FE-001"):
        log.info("inside")
    log.info("outside")

    inside, outside = _records(capsys)
    assert inside["project_id"] == "project_001"
    assert inside["task_id"] == "WEB-007"
    assert inside["agent_id"] == "WEB-FE-001"
    assert "project_id" not in outside
    assert "task_id" not in outside
    assert "agent_id" not in outside


def test_nested_context_restores_outer_values(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    log = get_logger("t")

    with log_context(project_id="p1"):
        with log_context(task_id="WEB-001"):
            log.info("nested")
        log.info("outer")

    nested, outer = _records(capsys)
    assert nested["project_id"] == "p1"
    assert nested["task_id"] == "WEB-001"
    assert outer["project_id"] == "p1"
    assert "task_id" not in outer


def test_level_filters_lower_severity(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING")
    log = get_logger("t")

    log.info("dropped")
    log.warning("kept")

    (record,) = _records(capsys)
    assert record["event"] == "kept"


def test_exceptions_are_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        get_logger("t").exception("failed")

    (record,) = _records(capsys)
    assert "RuntimeError: boom" in str(record["exception"])
