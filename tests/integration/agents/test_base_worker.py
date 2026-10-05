"""BaseWorker: the plan -> edit -> check -> retry loop (FR-100..FR-104)."""

from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.agents import base as base_module
from web_department.agents.base import BaseWorker, FileEdit, WorkerPlan
from web_department.collaboration.events import EventBus
from web_department.config.permissions import Role
from web_department.config.settings import get_settings
from web_department.db.models import Agent, Event, Project, Task, Workstation
from web_department.db.repositories import RequirementSpecRepository, TaskRepository
from web_department.db.states import TaskState
from web_department.llm.router import CompletionResult, Purpose
from web_department.workstation.manager import ExecResult


class FakeCoder(BaseWorker):
    """A concrete worker for testing the base loop directly."""

    role = Role.BACKEND


def _plan_result(*, path: str = "app/ping.py", content: str = "x = 1") -> CompletionResult:
    plan = WorkerPlan(files=[FileEdit(path=path, content=content)], summary="did the thing")
    return CompletionResult(
        content=plan.model_dump_json(),
        parsed=plan,
        provider="test",
        model="test/model",
        cached=False,
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
    )


@pytest.fixture
def mock_complete(monkeypatch: pytest.MonkeyPatch) -> Callable[..., AsyncMock]:
    def _set(*results: CompletionResult) -> AsyncMock:
        mock = AsyncMock(side_effect=list(results))
        monkeypatch.setattr(base_module, "complete", mock)
        return mock

    return _set


def _worker(
    session: AsyncSession, agent: Agent, ws: Workstation, manager: AsyncMock, bus: EventBus
) -> FakeCoder:
    return FakeCoder(session, agent, {}, ws, manager, bus=bus)


async def _events(session: AsyncSession, task: Task, type_: str) -> list[Event]:
    result = await session.scalars(
        select(Event).where(Event.task_id == task.id, Event.type == type_)
    )
    return list(result.all())


async def test_retries_after_a_failing_check_then_commits_on_success(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    task: Task,
    fake_manager: AsyncMock,
    mock_complete: Callable[..., AsyncMock],
) -> None:
    complete_mock = mock_complete(_plan_result(), _plan_result())
    fake_manager.exec.side_effect = [
        ExecResult(1, "", "1 failed", False, False),  # attempt 1: pytest fails
        ExecResult(0, "1 passed", "", False, False),  # attempt 2: pytest passes
        ExecResult(0, "", "", False, False),  # git add
        ExecResult(0, "[main abc] WEB-001", "", False, False),  # git commit
    ]
    worker = _worker(session, agent, ws, fake_manager, bus)

    result = await worker.run(task)

    assert result.success is True
    assert result.attempts == 2
    assert result.files_changed == ["app/ping.py"]
    assert complete_mock.await_count == 2
    assert fake_manager.exec.await_args_list[-1].args[1] == [
        "git",
        "commit",
        "-m",
        "WEB-001: Add GET /api/ping",
    ]
    refreshed = await TaskRepository(session).get(task.id)
    assert refreshed is not None
    assert refreshed.retries == 1  # one failed attempt before the success that won

    refreshed = await TaskRepository(session).get(task.id)
    assert refreshed is not None
    assert refreshed.state is TaskState.REVIEW
    (event,) = await _events(session, task, "TASK_COMPLETED")
    assert event.payload["files_changed"] == ["app/ping.py"]
    assert event.payload["tests_passed"] is True
    assert event.payload["attempts"] == 2


async def test_feeds_the_exact_check_output_back_on_retry(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    task: Task,
    fake_manager: AsyncMock,
    mock_complete: Callable[..., AsyncMock],
) -> None:
    complete_mock = mock_complete(_plan_result(), _plan_result())
    fake_manager.exec.side_effect = [
        ExecResult(1, "collected 1 item", "AssertionError: boom", False, False),
        ExecResult(0, "1 passed", "", False, False),
        ExecResult(0, "", "", False, False),
        ExecResult(0, "", "", False, False),
    ]
    worker = _worker(session, agent, ws, fake_manager, bus)

    await worker.run(task)

    second_call_messages = complete_mock.await_args_list[1].args[0]
    fix_message = second_call_messages[-1]["content"]
    assert "AssertionError: boom" in fix_message
    assert "collected 1 item" in fix_message


async def test_always_failing_checks_stop_at_max_task_retries_and_block(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    task: Task,
    fake_manager: AsyncMock,
    mock_complete: Callable[..., AsyncMock],
) -> None:
    settings = get_settings()
    complete_mock = mock_complete(*([_plan_result()] * settings.max_task_retries))
    fake_manager.exec.side_effect = [
        ExecResult(1, "", "still broken", False, False) for _ in range(settings.max_task_retries)
    ]
    worker = _worker(session, agent, ws, fake_manager, bus)

    result = await worker.run(task)

    assert result.success is False
    assert result.tests_passed is False
    assert complete_mock.await_count == settings.max_task_retries
    assert fake_manager.exec.await_count == settings.max_task_retries  # only pytest, never git

    refreshed = await TaskRepository(session).get(task.id)
    assert refreshed is not None
    assert refreshed.state is TaskState.BLOCKED
    assert refreshed.retries == settings.max_task_retries
    (event,) = await _events(session, task, "TEST_FAILED")
    assert "still broken" in event.payload["failure_summary"]


async def test_llm_call_budget_stops_the_loop_before_max_task_retries(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    task: Task,
    fake_manager: AsyncMock,
    mock_complete: Callable[..., AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_LLM_CALLS_PER_TASK", "1")
    monkeypatch.setenv("MAX_TASK_RETRIES", "3")
    get_settings.cache_clear()
    complete_mock = mock_complete(_plan_result())
    fake_manager.exec.side_effect = [ExecResult(1, "", "nope", False, False)]
    worker = _worker(session, agent, ws, fake_manager, bus)

    result = await worker.run(task)

    assert result.success is False
    assert complete_mock.await_count == 1  # budget stopped a second attempt
    refreshed = await TaskRepository(session).get(task.id)
    assert refreshed is not None
    assert refreshed.state is TaskState.BLOCKED


async def test_context_includes_template_guide_task_and_acceptance_checks(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    task: Task,
    fake_manager: AsyncMock,
    mock_complete: Callable[..., AsyncMock],
) -> None:
    Path(ws.repo_path, "TEMPLATE_GUIDE.md").write_text("Models go in app/models/.")
    complete_mock = mock_complete(_plan_result())
    fake_manager.exec.side_effect = [
        ExecResult(0, "1 passed", "", False, False),
        ExecResult(0, "", "", False, False),
        ExecResult(0, "", "", False, False),
    ]
    worker = _worker(session, agent, ws, fake_manager, bus)

    await worker.run(task)

    first_call_messages = complete_mock.await_args_list[0].args[0]
    prompt = first_call_messages[0]["content"]
    assert "Models go in app/models/." in prompt
    assert "Add GET /api/ping" in prompt
    assert 'GET /api/ping returns {"pong": true}' in prompt


async def test_run_uses_the_purpose_and_confidentiality_the_subclass_declares(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    task: Task,
    fake_manager: AsyncMock,
    mock_complete: Callable[..., AsyncMock],
) -> None:
    complete_mock = mock_complete(_plan_result())
    fake_manager.exec.side_effect = [
        ExecResult(0, "1 passed", "", False, False),
        ExecResult(0, "", "", False, False),
        ExecResult(0, "", "", False, False),
    ]
    worker = _worker(session, agent, ws, fake_manager, bus)

    await worker.run(task)

    kwargs = complete_mock.await_args_list[0].kwargs
    assert kwargs["purpose"] is Purpose.CODING
    assert kwargs["confidential"] is True
    assert kwargs["agent_id"] == agent.id
    assert kwargs["task_id"] == task.id


async def test_context_includes_the_wrs_entity_the_task_names(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    project: Project,
    fake_manager: AsyncMock,
    mock_complete: Callable[..., AsyncMock],
) -> None:
    wrs_json = {
        "project_type": "e-commerce",
        "domain": "clothing retail",
        "roles": ["customer"],
        "features": [
            {
                "id": "F-01",
                "name": "Checkout",
                "description": "Buy items",
                "acceptance": ["Cart total is correct"],
            }
        ],
        "entities": [
            {
                "name": "Order",
                "fields": [{"name": "total", "type": "decimal", "required": True}],
            }
        ],
    }
    await RequirementSpecRepository(session).create(
        project_id=project.id, version=1, json=wrs_json, created_by="analyzer"
    )
    task = await TaskRepository(session).create(
        display_id="WEB-002",
        project_id=project.id,
        title="Add Order model",
        role="backend",
        state=TaskState.ASSIGNED,
        inputs={"entity": "Order"},
        acceptance_checks=["Order model has a total field"],
    )
    complete_mock = mock_complete(_plan_result())
    fake_manager.exec.side_effect = [
        ExecResult(0, "1 passed", "", False, False),
        ExecResult(0, "", "", False, False),
        ExecResult(0, "", "", False, False),
    ]
    worker = _worker(session, agent, ws, fake_manager, bus)

    await worker.run(task)

    prompt = complete_mock.await_args_list[0].args[0][0]["content"]
    assert '"name": "Order"' in prompt
    assert '"total"' in prompt
    assert "Checkout" not in prompt  # only the matching entity, not the whole spec


async def _to_review(session: AsyncSession, task: Task) -> None:
    repo = TaskRepository(session)
    for state in (TaskState.IN_PROGRESS, TaskState.REVIEW):
        await repo.update_state(task.id, state)


async def test_a_fix_required_task_is_reworked_back_to_review(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    task: Task,
    fake_manager: AsyncMock,
    mock_complete: Callable[..., AsyncMock],
) -> None:
    # qa/failure_triage.py sends a REVIEW task back as FIX_REQUIRED; the worker must be able to
    # take it from there (FIX_REQUIRED -> IN_PROGRESS -> REVIEW), not crash on REVIEW.
    await _to_review(session, task)
    await TaskRepository(session).update_state(task.id, TaskState.FIX_REQUIRED)
    mock_complete(_plan_result())
    fake_manager.exec.side_effect = [
        ExecResult(0, "1 passed", "", False, False),
        ExecResult(0, "", "", False, False),
        ExecResult(0, "[main abc]", "", False, False),
    ]

    result = await _worker(session, agent, ws, fake_manager, bus).run(task)

    assert result.success is True
    refreshed = await TaskRepository(session).get(task.id)
    assert refreshed is not None
    assert refreshed.state is TaskState.REVIEW


async def test_escalation_purpose_on_the_task_overrides_the_worker_purpose(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    task: Task,
    fake_manager: AsyncMock,
    mock_complete: Callable[..., AsyncMock],
) -> None:
    task.inputs = {"escalation_purpose": "planning"}
    await session.flush()
    complete_mock = mock_complete(_plan_result())
    fake_manager.exec.side_effect = [
        ExecResult(0, "1 passed", "", False, False),
        ExecResult(0, "", "", False, False),
        ExecResult(0, "[main abc]", "", False, False),
    ]

    await _worker(session, agent, ws, fake_manager, bus).run(task)

    assert FakeCoder.purpose is Purpose.CODING
    assert complete_mock.await_args_list[0].kwargs["purpose"] is Purpose.PLANNING
