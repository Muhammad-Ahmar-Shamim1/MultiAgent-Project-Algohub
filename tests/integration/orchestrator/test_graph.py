"""The project graph (FR-010..FR-014): reaches COMPLETED with mocked workers, and resumes a
killed run from its last checkpoint without redoing finished nodes.

Uses LangGraph's in-memory checkpointer rather than the real Postgres one runner.py uses in
production - the checkpoint protocol itself is checkpointer-agnostic, and this keeps the tests
fast and dependency-light while exercising exactly the same interrupt/resume semantics.
"""

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web_department.agents.base import BaseWorker, TaskResult
from web_department.collaboration.events import EventBus, set_event_bus
from web_department.db.models import Approval, Event
from web_department.db.repositories import ProjectRepository, TaskRepository, WorkstationRepository
from web_department.db.states import ProjectState, TaskState
from web_department.llm.router import CompletionResult
from web_department.orchestrator import graph as graph_module
from web_department.orchestrator import planner as planner_module
from web_department.orchestrator import requirement_analyzer as analyzer_module
from web_department.orchestrator import runner
from web_department.orchestrator.graph import build_graph, initial_state
from web_department.orchestrator.planner import ExtraTasksResponse
from web_department.orchestrator.wrs_schema import Feature, WebRequirementsSpec
from web_department.security import approvals
from web_department.security.approvals import Decision
from web_department.workstation import tools
from web_department.workstation.manager import ExecResult, WorkstationManager

MINIMAL_WRS = WebRequirementsSpec(
    project_type="web_application",
    domain="todo",
    roles=["user"],
    features=[Feature(id="F-01", name="auth", description="Login", acceptance=["User can log in"])],
)


@pytest_asyncio.fixture(autouse=True)
async def _wire_default_event_bus(bus: EventBus) -> AsyncIterator[None]:
    """graph.py's nodes never thread a bus through GraphState (it isn't checkpoint-serializable),
    so they always use the process-wide default bus - correct for runner.py's own long-lived
    event loop, but that global singleton otherwise reuses a Redis connection tied to whichever
    event loop first created it, which breaks across pytest-asyncio's per-test event loops.
    """
    set_event_bus(bus)
    yield
    set_event_bus(None)


@pytest_asyncio.fixture
async def committed_project(session_factory: async_sessionmaker[AsyncSession]) -> str:
    async with session_factory() as own:
        project = await ProjectRepository(own).create(title="Todo app", requester_id="u-1")
        await own.commit()
        return project.id


@pytest.fixture
def mock_analyzer_llm(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    result = CompletionResult(
        content="{}",
        parsed=MINIMAL_WRS,
        provider="test",
        model="test/model",
        cached=False,
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
    )
    mock = AsyncMock(return_value=result)
    monkeypatch.setattr(analyzer_module, "complete", mock)
    return mock


@pytest.fixture
def mock_extra_tasks(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    result = CompletionResult(
        content="{}",
        parsed=ExtraTasksResponse(tasks=[]),
        provider="test",
        model="test/model",
        cached=False,
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
    )
    mock = AsyncMock(return_value=result)
    monkeypatch.setattr(planner_module, "complete", mock)
    return mock


@pytest.fixture
def mock_provision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _fake_create(self: WorkstationManager, project_id: str) -> object:
        repo_path = tmp_path / project_id
        repo_path.mkdir(parents=True, exist_ok=True)
        return await WorkstationRepository(self.session).create(
            project_id=project_id,
            container_id="fake-container",
            repo_path=str(repo_path),
            limits={},
            status="running",
        )

    monkeypatch.setattr(WorkstationManager, "create", _fake_create)


@pytest.fixture
def mock_run_command(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=ExecResult(0, "ok", "", False, False))
    monkeypatch.setattr(tools, "run_command", mock)
    return mock


@pytest.fixture
def mock_coder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A plain function (not ``AsyncMock``) so ``self`` still auto-binds when set on the class -
    an ``AsyncMock`` instance isn't a descriptor, so ``worker.run(task)`` would call it with only
    ``task`` and drop ``self`` entirely. Calls are tracked in the returned list instead of via
    ``.await_count``.
    """
    calls: list[str] = []

    async def _fake_run(self: BaseWorker, task: object) -> TaskResult:
        calls.append(task.id)  # type: ignore[attr-defined]
        repo = TaskRepository(self.session)
        await repo.update_state(task.id, TaskState.IN_PROGRESS)  # type: ignore[attr-defined]
        await repo.update_state(task.id, TaskState.REVIEW)  # type: ignore[attr-defined]
        return TaskResult(
            success=True, files_changed=["fake.py"], tests_passed=True, summary="ok", attempts=1
        )

    monkeypatch.setattr(BaseWorker, "run", _fake_run)
    return calls


@pytest.fixture
def mocked_pipeline(
    mock_analyzer_llm: AsyncMock,
    mock_extra_tasks: AsyncMock,
    mock_provision: None,
    mock_run_command: AsyncMock,
    mock_coder: list[str],
) -> Callable[[], "tuple[AsyncMock, AsyncMock, AsyncMock, list[str]]"]:
    def _mocks() -> tuple[AsyncMock, AsyncMock, AsyncMock, list[str]]:
        return mock_analyzer_llm, mock_extra_tasks, mock_run_command, mock_coder

    return _mocks


async def _decide_pending(
    session_factory: async_sessionmaker[AsyncSession],
    compiled: Any,
    config: RunnableConfig,
    decision: Decision,
    comment: str | None = None,
) -> str:
    """Record an Approver's decision on the paused run's request and resume the graph with it,
    as POST /approvals/{id} + runner.py do in production. Returns the approval id.
    """
    snapshot = await compiled.aget_state(config)
    approval_id: str = snapshot.interrupts[0].value["approval_id"]
    async with session_factory() as own:
        recorded = await approvals.decide(
            own, approval_id, decision, approver_id="approver-1", comment=comment
        )
        await own.commit()
    await compiled.ainvoke(Command(resume=recorded.model_dump(mode="json")), config=config)
    return approval_id


async def test_graph_reaches_completed_with_mocked_workers(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    committed_project: str,
    mocked_pipeline: Callable[[], tuple[AsyncMock, AsyncMock, AsyncMock, list[str]]],
) -> None:
    analyzer_mock, extra_tasks_mock, run_command_mock, coder_calls = mocked_pipeline()
    checkpointer = InMemorySaver()
    compiled = build_graph().compile(checkpointer=checkpointer)
    config: RunnableConfig = {"configurable": {"thread_id": committed_project}}

    await compiled.ainvoke(initial_state(committed_project), config=config)
    paused = await ProjectRepository(session).get(committed_project)
    assert paused is not None and paused.state is ProjectState.AWAITING_APPROVAL
    await _decide_pending(session_factory, compiled, config, Decision.APPROVE)

    project = await ProjectRepository(session).get(committed_project)
    assert project is not None
    await session.refresh(project)
    assert project.state is ProjectState.COMPLETED
    assert analyzer_mock.await_count == 1
    assert extra_tasks_mock.await_count == 1
    assert len(coder_calls) >= 1
    assert run_command_mock.await_count >= 2  # backend install + make test e2e

    tasks = await TaskRepository(session).list(project_id=committed_project)
    assert tasks  # the template expansion produced real tasks
    assert all(t.state is TaskState.REVIEW for t in tasks)


async def test_killed_mid_run_resumes_without_redoing_done_tasks(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    committed_project: str,
    mocked_pipeline: Callable[[], tuple[AsyncMock, AsyncMock, AsyncMock, list[str]]],
) -> None:
    analyzer_mock, extra_tasks_mock, _run_command_mock, coder_calls = mocked_pipeline()
    checkpointer = InMemorySaver()
    config: RunnableConfig = {"configurable": {"thread_id": committed_project}}

    # First "process": stops right after "plan", as if killed there.
    interrupted = build_graph().compile(checkpointer=checkpointer, interrupt_after=["plan"])
    await interrupted.ainvoke(initial_state(committed_project), config=config)

    assert analyzer_mock.await_count == 1
    assert coder_calls == []  # never reached execute_tasks yet
    tasks_after_plan = await TaskRepository(session).list(project_id=committed_project)
    assert tasks_after_plan  # plan already ran and persisted tasks

    # Second "process": a fresh compile against the same checkpointer/thread_id, no interrupt -
    # this is what runner.py does on restart (graph.aget_state shows an existing checkpoint, so
    # it resumes with None input rather than replaying from initial_state()).
    resumed = build_graph().compile(checkpointer=checkpointer)
    await resumed.ainvoke(None, config=config)
    await _decide_pending(session_factory, resumed, config, Decision.APPROVE)

    assert analyzer_mock.await_count == 1  # not redone
    assert extra_tasks_mock.await_count == 1  # planning not redone either
    assert coder_calls  # forward progress did happen this time

    project = await ProjectRepository(session).get(committed_project)
    assert project is not None
    await session.refresh(project)
    assert project.state is ProjectState.COMPLETED


async def test_execute_tasks_skips_tasks_already_in_review(
    committed_project: str,
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    mock_run_command: AsyncMock,
) -> None:
    """The task-level idempotency execute_tasks_node relies on when a node itself re-runs.

    Uses ``session_factory`` (a real, separately-committed transaction) rather than the test's
    own ``session`` fixture: ``execute_tasks_node`` opens its own connection via
    ``session_scope()``, which under READ COMMITTED only ever sees committed data - the
    ``session`` fixture's rows stay invisible to it until the whole test's outer transaction
    commits, which never happens (it is rolled back at teardown).
    """
    async with session_factory() as own:
        await WorkstationRepository(own).create(
            project_id=committed_project,
            container_id="fake",
            repo_path=str(tmp_path),
            limits={},
            status="running",
        )
        already_done = await TaskRepository(own).create(
            display_id="WEB-001",
            project_id=committed_project,
            title="Already done",
            role="backend",
            state=TaskState.REVIEW,
            inputs={},
            acceptance_checks=["n/a"],
        )
        await own.commit()
        already_done_id = already_done.id

    called_with: list[str] = []

    async def _fake_run(self: BaseWorker, task: object) -> TaskResult:
        called_with.append(task.id)  # type: ignore[attr-defined]
        return TaskResult(
            success=True, files_changed=[], tests_passed=True, summary="ok", attempts=1
        )

    monkeypatch.setattr(BaseWorker, "run", _fake_run)

    await graph_module.execute_tasks_node(initial_state(committed_project))

    assert already_done_id not in called_with


# --- the approval gate (FR-130..FR-132) ---------------------------------------------------------


AWAITING = ProjectState.AWAITING_APPROVAL


@pytest.fixture
def mock_notify(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr(approvals, "notify", mock)
    return mock


async def _requests(
    session_factory: async_sessionmaker[AsyncSession], project_id: str
) -> list[Approval]:
    async with session_factory() as own:
        stmt = (
            select(Approval)
            .where(Approval.project_id == project_id, Approval.decides_id.is_(None))
            .order_by(Approval.id)
        )
        return list((await own.scalars(stmt)).all())


async def _project_state(
    session_factory: async_sessionmaker[AsyncSession], project_id: str
) -> ProjectState:
    async with session_factory() as own:
        project = await ProjectRepository(own).get(project_id)
        assert project is not None
        return project.state


async def test_graph_pauses_for_approval_and_the_runner_resumes_it_once_approved(
    session_factory: async_sessionmaker[AsyncSession],
    committed_project: str,
    redis_client: Redis,
    mocked_pipeline: Callable[[], tuple[AsyncMock, AsyncMock, AsyncMock, list[str]]],
    mock_notify: AsyncMock,
) -> None:
    mocked_pipeline()
    compiled = build_graph().compile(checkpointer=InMemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": committed_project}}

    # 1. The runner drives the project until the approval gate pauses it.
    await runner.run_project(compiled, redis_client, committed_project)

    assert await _project_state(session_factory, committed_project) is AWAITING
    snapshot = await compiled.aget_state(config)
    assert snapshot.next == ("approval",)
    [request] = await _requests(session_factory, committed_project)
    assert snapshot.interrupts[0].value["approval_id"] == request.id
    assert request.action_type == "production_deploy"
    assert request.request_key == "production_deploy:0"
    assert request.evidence is not None
    assert set(request.evidence) == {
        "diff", "qa_report", "security_report", "screenshots", "deployment_plan"
    }  # fmt: skip
    mock_notify.assert_awaited_once()
    assert mock_notify.await_args is not None
    assert request.id in mock_notify.await_args.args[0]

    # 2. Re-run with no decision yet: still paused, nothing duplicated.
    await runner.run_project(compiled, redis_client, committed_project)
    assert await _project_state(session_factory, committed_project) is AWAITING
    assert len(await _requests(session_factory, committed_project)) == 1

    # 3. An Approver approves; the runner resumes the graph with that decision.
    async with session_factory() as own:
        await approvals.decide(
            own, request.id, Decision.APPROVE, approver_id="approver-1", comment="ship it"
        )
        await own.commit()
    await runner.run_project(compiled, redis_client, committed_project)

    assert await _project_state(session_factory, committed_project) is ProjectState.COMPLETED
    assert (
        len(await _requests(session_factory, committed_project)) == 1
    )  # node re-run was idempotent
    mock_notify.assert_awaited_once()


async def test_a_rejection_returns_to_developing_with_the_comment_on_a_fix_task(
    session_factory: async_sessionmaker[AsyncSession],
    committed_project: str,
    mocked_pipeline: Callable[[], tuple[AsyncMock, AsyncMock, AsyncMock, list[str]]],
    mock_notify: AsyncMock,
) -> None:
    _analyzer, _extra, _run_command, coder_calls = mocked_pipeline()
    compiled = build_graph().compile(checkpointer=InMemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": committed_project}}
    await compiled.ainvoke(initial_state(committed_project), config=config)
    calls_before = len(coder_calls)

    first_id = await _decide_pending(
        session_factory, compiled, config, Decision.REJECT, comment="Checkout has no CSRF token"
    )

    async with session_factory() as own:
        tasks = await TaskRepository(own).list(project_id=committed_project, limit=1000)
        [fix] = [t for t in tasks if "rejection_comment" in t.inputs]
        assert fix.inputs["rejection_comment"] == "Checkout has no CSRF token"
        assert fix.inputs["approval_id"] == first_id
        assert fix.role == "backend"
        moves = (
            await own.scalars(
                select(Event).where(
                    Event.project_id == committed_project, Event.type == "STATE_CHANGED"
                )
            )
        ).all()
        assert any(
            e.payload == {**e.payload, "from": "AWAITING_APPROVAL", "to": "DEVELOPING"}
            for e in moves
        )
    # The fix task was worked, and the build came back for a *new* approval request.
    assert fix.id in coder_calls[calls_before:]
    requests = await _requests(session_factory, committed_project)
    assert [r.request_key for r in requests] == ["production_deploy:0", "production_deploy:1"]
    assert await _project_state(session_factory, committed_project) is AWAITING
    snapshot = await compiled.aget_state(config)
    assert snapshot.interrupts[0].value["approval_id"] == requests[1].id
