"""qa/failure_triage: picking the responsible task, fix targets, and the escalation ladder."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus
from web_department.db.models import Agent, Event, Project, Task, Workstation
from web_department.db.repositories import (
    AgentRepository,
    BlueprintRepository,
    TaskRepository,
    WorkstationRepository,
)
from web_department.db.states import TaskState
from web_department.orchestrator.wrs_schema import Feature, WebRequirementsSpec
from web_department.qa.failure_triage import (
    EscalationLevel,
    ReplanResult,
    suspect_task,
    triage,
)
from web_department.qa.test_runner import Failure, QAReport
from web_department.workstation.manager import ExecResult

WRS = WebRequirementsSpec(
    project_type="web_application",
    domain="ecommerce",
    roles=["customer"],
    features=[
        Feature(id="F-01", name="auth", description="Log in", acceptance=["User can log in"]),
        Feature(id="F-02", name="cart", description="Cart", acceptance=["Cart total updates"]),
    ],
)


@pytest.fixture
async def ws(session: AsyncSession, project: Project, tmp_path: Path) -> Workstation:
    return await WorkstationRepository(session).create(
        project_id=project.id,
        container_id="fake-container",
        repo_path=str(tmp_path),
        limits={},
        status="running",
    )


@pytest.fixture
async def agent(session: AsyncSession, project: Project) -> Agent:
    await BlueprintRepository(session).create(id="triage-bp", version="1", yaml="y", checksum="c")
    return await AgentRepository(session).create(
        display_id="WEB-TRIAGE-001",
        blueprint_id="triage-bp",
        blueprint_version="1",
        project_id=project.id,
    )


async def _task(
    session: AsyncSession,
    project: Project,
    number: int,
    role: str,
    *,
    feature: str | None = None,
    state: TaskState = TaskState.CREATED,
) -> Task:
    repo = TaskRepository(session)
    task = await repo.create(
        display_id=f"WEB-{number:03d}",
        project_id=project.id,
        title=f"{role} task {number}",
        role=role,
        inputs={"feature": feature} if feature else {},
        acceptance_checks=["works"],
    )
    path = [TaskState.READY, TaskState.ASSIGNED, TaskState.IN_PROGRESS, TaskState.REVIEW]
    if state is TaskState.COMPLETED:
        path.append(TaskState.COMPLETED)
    elif state is TaskState.CREATED:
        path = []
    for next_state in path:
        task = await repo.update_state(task.id, next_state)
    return task


@pytest.fixture
async def tasks(session: AsyncSession, project: Project) -> dict[str, Task]:
    return {
        "backend": await _task(session, project, 3, "backend", state=TaskState.REVIEW),
        "wiring": await _task(
            session, project, 9, "api_integration", feature="F-02", state=TaskState.REVIEW
        ),
        "qa": await _task(session, project, 12, "qa", feature="F-02", state=TaskState.REVIEW),
        "done": await _task(session, project, 4, "frontend", state=TaskState.COMPLETED),
    }


def _manager(stdout: str, exit_code: int = 0) -> AsyncMock:
    manager = AsyncMock()
    manager.exec.return_value = ExecResult(exit_code, stdout, "", False, False)
    return manager


# --- suspect_task ----------------------------------------------------------------------------


async def test_blame_of_the_app_line_in_the_traceback_wins(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, tasks: dict[str, Task]
) -> None:
    manager = _manager("abc 42 42 1\nsummary WEB-003: backend task 3\nfilename app/x.py\n")
    failure = Failure(
        test="tests.test_cart::test_F02_1_total",  # the tag would point at WEB-009...
        message="app/routers/cart.py:42: in total\nAssertionError",  # ...but blame says WEB-003
        file="backend/tests/test_cart.py",
    )

    suspected = await suspect_task(session, ws, agent.id, manager, failure, WRS, bus=bus)

    assert suspected == tasks["backend"].id
    command = manager.exec.await_args_list[0].args[1]
    assert command == [
        "git", "blame", "-L", "42,42", "--porcelain", "--", "backend/app/routers/cart.py"
    ]  # fmt: skip


async def test_test_tag_maps_to_the_feature_s_implementing_task(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, tasks: dict[str, Task]
) -> None:
    manager = _manager("")
    failure = Failure(test="tests.test_cart::test_F02_1_total", message="assert 10 == 12")

    suspected = await suspect_task(session, ws, agent.id, manager, failure, WRS, bus=bus)

    assert suspected == tasks["wiring"].id  # API/Integration beats the QA task for F-02
    manager.exec.assert_not_awaited()


async def test_falls_back_to_the_last_commit_on_the_failing_test_file(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, tasks: dict[str, Task]
) -> None:
    manager = _manager("WEB-012: qa task 12\n")
    failure = Failure(test="tests.test_misc::test_untagged", message="boom", file="backend/x.py")

    suspected = await suspect_task(session, ws, agent.id, manager, failure, WRS, bus=bus)

    assert suspected == tasks["qa"].id
    assert manager.exec.await_args_list[0].args[1][:3] == ["git", "log", "-1"]


async def test_no_signal_means_no_suspect(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, tasks: dict[str, Task]
) -> None:
    manager = _manager("Initial commit\n")
    failure = Failure(test="lint: ruff", message="E501 line too long")

    assert await suspect_task(session, ws, agent.id, manager, failure, WRS, bus=bus) is None


# --- triage ----------------------------------------------------------------------------------


async def _events(session: AsyncSession, project: Project, type_: str) -> list[Event]:
    stmt = select(Event).where(Event.project_id == project.id, Event.type == type_)
    return list((await session.scalars(stmt)).all())


def _report(*failures: Failure) -> QAReport:
    return QAReport(failed=len(failures), failures=list(failures))


async def _no_replan(task: Task, output: str) -> ReplanResult:
    raise AssertionError("replanner must not be called below the REPLAN level")


async def test_a_review_task_becomes_fix_required_with_the_output_attached(
    session: AsyncSession, bus: EventBus, project: Project, tasks: dict[str, Task]
) -> None:
    backend = tasks["backend"]
    report = _report(
        Failure(test="t::a", message="assert 1 == 2", suspected_task_id=backend.id),
        Failure(test="t::b", message="KeyError: id", suspected_task_id=backend.id),
    )

    outcomes = await triage(session, project.id, report, replanner=_no_replan, bus=bus)

    assert len(outcomes) == 1  # both failures -> one fix cycle for the one task
    assert outcomes[0].level is EscalationLevel.FIX
    assert outcomes[0].fix_task_id == backend.id
    refreshed = await TaskRepository(session).get(backend.id)
    assert refreshed is not None
    assert refreshed.state is TaskState.FIX_REQUIRED
    assert refreshed.inputs["fix_cycles"] == 1
    assert "assert 1 == 2" in refreshed.inputs["fix_context"]
    assert "KeyError: id" in refreshed.inputs["fix_context"]
    event = (await _events(session, project, "FIX_REQUIRED"))[0]
    assert event.task_id == backend.id
    assert event.payload["failures"] == ["t::a", "t::b"]


async def test_a_completed_task_gets_a_new_ready_fix_task(
    session: AsyncSession, bus: EventBus, project: Project, tasks: dict[str, Task]
) -> None:
    done = tasks["done"]
    report = _report(Failure(test="t::page", message="timeout", suspected_task_id=done.id))

    outcomes = await triage(session, project.id, report, replanner=_no_replan, bus=bus)

    fix = await TaskRepository(session).get(outcomes[0].fix_task_id)
    assert fix is not None
    assert fix.id != done.id
    assert fix.state is TaskState.READY
    assert fix.role == done.role
    assert fix.title == "Fix WEB-004: t::page"
    assert fix.display_id == "WEB-013"
    assert fix.inputs["fix_for"] == "WEB-004"
    assert "timeout" in fix.inputs["fix_context"]
    assert (await TaskRepository(session).get(done.id)).state is TaskState.COMPLETED  # type: ignore[union-attr]


async def _back_to_review(session: AsyncSession, task: Task) -> None:
    repo = TaskRepository(session)
    for state in (TaskState.IN_PROGRESS, TaskState.REVIEW):
        await repo.update_state(task.id, state)


async def test_escalation_order_replan_then_strongest_model_then_human(
    session: AsyncSession, bus: EventBus, project: Project, tasks: dict[str, Task]
) -> None:
    backend = tasks["backend"]
    backend.inputs = {"fix_cycles": 3}  # three plain fix cycles already spent
    await session.flush()
    replans: list[str] = []

    async def replanner(task: Task, output: str) -> ReplanResult:
        replans.append(task.display_id)
        return ReplanResult(diagnosis="wrong query", approach="use a join", steps=["rewrite"])

    def report() -> QAReport:
        return _report(Failure(test="t::a", message="still broken", suspected_task_id=backend.id))

    first = await triage(session, project.id, report(), replanner=replanner, bus=bus)
    task = await TaskRepository(session).get(backend.id)
    assert task is not None
    assert first[0].level is EscalationLevel.REPLAN
    assert replans == ["WEB-003"]
    assert task.inputs["replan"]["approach"] == "use a join"
    assert task.state is TaskState.FIX_REQUIRED

    await _back_to_review(session, task)
    second = await triage(session, project.id, report(), replanner=replanner, bus=bus)
    task = await TaskRepository(session).get(backend.id)
    assert task is not None
    assert second[0].level is EscalationLevel.STRONGER_MODEL
    assert task.inputs["escalation_purpose"] == "planning"
    assert replans == ["WEB-003"]  # not replanned again

    await _back_to_review(session, task)
    third = await triage(session, project.id, report(), replanner=replanner, bus=bus)
    task = await TaskRepository(session).get(backend.id)
    assert task is not None
    assert third[0].level is EscalationLevel.HUMAN_REVIEW
    assert third[0].fix_task_id is None
    assert task.state is TaskState.BLOCKED
    assert task.inputs["fix_cycles"] == 6
    human = await _events(session, project, "REVIEW_REQUIRED")
    assert len(human) == 1
    assert human[0].requires_review is True
    assert human[0].payload["human_review"] is True


async def test_unattributed_failures_go_to_the_latest_review_task(
    session: AsyncSession, bus: EventBus, project: Project, tasks: dict[str, Task]
) -> None:
    report = _report(Failure(test="lint: ruff", message="E501"))

    outcomes = await triage(session, project.id, report, replanner=_no_replan, bus=bus)

    assert outcomes[0].responsible_task_id == tasks["qa"].id  # WEB-012, the highest in REVIEW


async def test_unattributed_failures_with_nothing_in_review_raise_an_alert(
    session: AsyncSession, bus: EventBus, project: Project
) -> None:
    report = _report(Failure(test="lint: ruff", message="E501"))

    outcomes = await triage(session, project.id, report, replanner=_no_replan, bus=bus)

    assert outcomes[0].responsible_task_id is None
    alerts = await _events(session, project, "ALERT_RAISED")
    assert alerts[0].payload["failures"] == ["lint: ruff"]
