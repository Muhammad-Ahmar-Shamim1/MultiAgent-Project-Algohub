"""CoderWorker: role/purpose/confidentiality wiring on top of BaseWorker."""

from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.agents import base as base_module
from web_department.agents.base import FileEdit, WorkerPlan
from web_department.agents.coder.agent import CoderWorker
from web_department.collaboration.events import EventBus
from web_department.config.permissions import Role
from web_department.config.settings import get_settings
from web_department.db.models import Agent, Task, Workstation
from web_department.db.repositories import TaskRepository
from web_department.db.states import TaskState
from web_department.llm.router import CompletionResult, Purpose
from web_department.workstation.manager import ExecResult


def test_coder_role_and_purpose_match_a_generalist_coding_worker() -> None:
    assert CoderWorker.role is Role.BACKEND
    assert CoderWorker.purpose is Purpose.CODING


def test_coder_confidential_defaults_to_true_ir_024(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent
) -> None:
    worker = CoderWorker(session, agent, {}, ws, AsyncMock(), bus=bus)

    assert worker.confidential is True


def test_coder_confidential_follows_the_coder_confidential_setting(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODER_CONFIDENTIAL", "false")
    get_settings.cache_clear()
    try:
        worker = CoderWorker(session, agent, {}, ws, AsyncMock(), bus=bus)
        assert worker.confidential is False
    finally:
        monkeypatch.delenv("CODER_CONFIDENTIAL", raising=False)
        get_settings.cache_clear()


@pytest.fixture
def mock_complete(monkeypatch: pytest.MonkeyPatch) -> Callable[[CompletionResult], AsyncMock]:
    def _set(result: CompletionResult) -> AsyncMock:
        mock = AsyncMock(return_value=result)
        monkeypatch.setattr(base_module, "complete", mock)
        return mock

    return _set


async def test_coder_writes_a_file_runs_checks_and_commits_on_success(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    task: Task,
    fake_manager: AsyncMock,
    mock_complete: Callable[[CompletionResult], AsyncMock],
) -> None:
    plan = WorkerPlan(
        files=[FileEdit(path="app/routers/ping.py", content="# ping route")],
        summary="Added GET /api/ping",
    )
    mock_complete(
        CompletionResult(
            content=plan.model_dump_json(),
            parsed=plan,
            provider="test",
            model="test/model",
            cached=False,
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
        )
    )
    fake_manager.exec.side_effect = [
        ExecResult(0, "1 passed", "", False, False),
        ExecResult(0, "", "", False, False),
        ExecResult(0, "[main abc] WEB-001", "", False, False),
    ]
    worker = CoderWorker(session, agent, {}, ws, fake_manager, bus=bus)

    result = await worker.run(task)

    assert result.success is True
    assert result.files_changed == ["app/routers/ping.py"]
    refreshed = await TaskRepository(session).get(task.id)
    assert refreshed is not None
    assert refreshed.state is TaskState.REVIEW
