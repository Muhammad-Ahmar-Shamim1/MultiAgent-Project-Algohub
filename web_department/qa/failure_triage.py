"""Failure triage and escalation (FR-102, FR-103).

``suspect_task`` maps one QA failure to the task most likely responsible, trying in order:

1. ``git blame`` of the application line the failure's traceback points at (``backend/app/...``
   or ``frontend/src/...``) - every worker commits as ``"<WEB-NNN>: <title>"``
   (``agents/base.py``), so the blamed commit's summary names the task;
2. the test's criterion tag (``test_F02_1_...``) -> its feature -> the task implementing that
   feature (``inputs.feature``; the API/Integration task first, then any non-QA one, then QA);
3. the last commit that touched the failing test file itself (a broken test is the test writer's).

``triage`` then turns each responsible task's failures into one fix cycle, escalating after
``Settings.max_fix_cycles`` cycles of the same task: (1) replan the task, (2) rerun it on the
strongest available model, (3) BLOCKED + a human review event. The fix target follows
``db.states.TASK_TRANSITIONS``: a task in REVIEW moves to FIX_REQUIRED itself; one that is
already FIX_REQUIRED just gets the new failures; any other (e.g. COMPLETED, a terminal state)
gets a new READY "Fix ..." task for the same role. The failure output is attached to the fix
target's ``inputs`` (``fix_context``), which the worker's prompt shows it, and to the
FIX_REQUIRED event.
"""

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus, EventType, emit
from web_department.config.permissions import Role
from web_department.config.settings import get_settings
from web_department.db.models import Task, Workstation
from web_department.db.repositories import TaskRepository
from web_department.db.states import TaskState
from web_department.llm.prompts import render as render_prompt
from web_department.llm.router import Purpose, complete
from web_department.logging import get_logger
from web_department.orchestrator.wrs_schema import WebRequirementsSpec
from web_department.qa.test_runner import Failure, QAReport, criteria_for_test, criteria_from_wrs
from web_department.workstation import tools
from web_department.workstation.manager import WorkstationManager

log = get_logger(__name__)

ROLE = Role.QA
FIX_CONTEXT_LIMIT = 4000
# The purpose whose routing is "the strongest free API model" (SRS 11.1) - what level 2 of the
# escalation ladder switches a task's worker to.
STRONGEST_PURPOSE = Purpose.PLANNING

_APP_LINE = re.compile(
    r"(?:(?P<side>backend|frontend)/)?(?P<path>(?:app|src)/[\w./-]+\.(?:py|tsx?|jsx?)):(?P<line>\d+)"
)
_TASK_IN_SUMMARY = re.compile(r"^(WEB-\d+):")


class EscalationLevel(StrEnum):
    """What a fix cycle does, by how many cycles the task has already had (FR-103)."""

    FIX = "fix"
    REPLAN = "replan"
    STRONGER_MODEL = "stronger_model"
    HUMAN_REVIEW = "human_review"


def escalation_for(fix_cycle: int, max_fix_cycles: int) -> EscalationLevel:
    """Cycles 1..max are plain fixes; then replan, then the strongest model, then a human."""
    if fix_cycle <= max_fix_cycles:
        return EscalationLevel.FIX
    if fix_cycle == max_fix_cycles + 1:
        return EscalationLevel.REPLAN
    if fix_cycle == max_fix_cycles + 2:
        return EscalationLevel.STRONGER_MODEL
    return EscalationLevel.HUMAN_REVIEW


class ReplanResult(BaseModel):
    """The LLM's revised approach for a task that kept failing (escalation level 1)."""

    model_config = ConfigDict(extra="forbid")

    diagnosis: str
    approach: str
    steps: list[str] = Field(min_length=1)


Replanner = Callable[[Task, str], Awaitable[ReplanResult]]


@dataclass(frozen=True)
class TriageOutcome:
    """What triage did for one responsible task."""

    responsible_task_id: str | None
    fix_task_id: str | None
    level: EscalationLevel | None
    failures: list[str]


# --- suspecting ------------------------------------------------------------------------------


async def suspect_task(
    session: AsyncSession,
    ws: Workstation,
    agent_id: str | None,
    manager: WorkstationManager,
    failure: Failure,
    wrs: WebRequirementsSpec | None,
    *,
    bus: EventBus | None = None,
) -> str | None:
    """The id of the task most likely responsible for ``failure``, or None (see module doc)."""
    tasks = (await session.scalars(select(Task).where(Task.project_id == ws.project_id))).all()
    by_display_id = {t.display_id: t for t in tasks}

    for match in _APP_LINE.finditer(failure.message):
        side = match["side"] or ("backend" if match["path"].startswith("app/") else "frontend")
        path = f"{side}/{match['path']}"
        line = match["line"]
        result = await tools.run_command(
            session,
            ws,
            ROLE,
            agent_id,
            manager,
            ["git", "blame", "-L", f"{line},{line}", "--porcelain", "--", path],
            bus=bus,
        )
        task = _task_from_git_output(result.stdout, by_display_id, porcelain=True)
        if result.exit_code == 0 and task is not None:
            return task.id

    if wrs is not None:
        for criterion in criteria_for_test(failure.test, criteria_from_wrs(wrs)):
            task = implementing_task(tasks, criterion.feature_id)
            if task is not None:
                return task.id

    if failure.file:
        result = await tools.run_command(
            session,
            ws,
            ROLE,
            agent_id,
            manager,
            ["git", "log", "-1", "--format=%s", "--", failure.file],
            bus=bus,
        )
        task = _task_from_git_output(result.stdout, by_display_id, porcelain=False)
        if result.exit_code == 0 and task is not None:
            return task.id
    return None


def _task_from_git_output(
    output: str, by_display_id: dict[str, Task], *, porcelain: bool
) -> Task | None:
    for line in output.splitlines():
        summary = line.removeprefix("summary ") if porcelain else line
        if porcelain and not line.startswith("summary "):
            continue
        match = _TASK_IN_SUMMARY.match(summary.strip())
        if match and match[1] in by_display_id:
            return by_display_id[match[1]]
    return None


def implementing_task(tasks: Sequence[Task], feature_id: str) -> Task | None:
    """The task that implements ``feature_id``: API/Integration first, then any non-QA, then QA."""
    candidates = [t for t in tasks if t.inputs.get("feature") == feature_id]

    def rank(task: Task) -> tuple[int, str]:
        if task.role == Role.API_INTEGRATION.value:
            return (0, task.display_id)
        if task.role != Role.QA.value:
            return (1, task.display_id)
        return (2, task.display_id)

    return min(candidates, key=rank) if candidates else None


# --- fixing and escalating -------------------------------------------------------------------


async def llm_replan(task: Task, failure_output: str) -> ReplanResult:
    """Escalation level 1: ask the planning model for a new approach to ``task``."""
    result = await complete(
        [
            {
                "role": "user",
                "content": render_prompt(
                    "qa_replan",
                    task_title=task.title,
                    task_role=task.role,
                    acceptance_checks=task.acceptance_checks,
                    failure_output=failure_output,
                    fix_cycles=task.inputs.get("fix_cycles", 0),
                ),
            }
        ],
        purpose=Purpose.PLANNING,
        confidential=get_settings().coder_confidential,
        json_schema=ReplanResult,
        task_id=task.id,
    )
    assert isinstance(result.parsed, ReplanResult)  # complete() parsed our own schema
    return result.parsed


async def triage(
    session: AsyncSession,
    project_id: str,
    report: QAReport,
    *,
    replanner: Replanner = llm_replan,
    bus: EventBus | None = None,
) -> list[TriageOutcome]:
    """One fix cycle (or escalation) per responsible task for every failure in ``report``.

    Failures with no suspected task go to the project's latest task in REVIEW (the last thing
    built); if there is none either, an ALERT_RAISED event records them for a human.
    """
    task_repo = TaskRepository(session, bus=bus)
    grouped: dict[str | None, list[Failure]] = {}
    for failure in report.failures:
        grouped.setdefault(failure.suspected_task_id, []).append(failure)

    outcomes: list[TriageOutcome] = []
    unassigned = grouped.pop(None, [])
    if unassigned:
        fallback = await _latest_in_review(session, project_id)
        if fallback is not None:
            grouped.setdefault(fallback.id, []).extend(unassigned)
        else:
            tests = [f.test for f in unassigned]
            await emit(
                EventType.ALERT_RAISED,
                session=session,
                bus=bus,
                project_id=project_id,
                payload={"reason": "qa_failures_without_a_responsible_task", "failures": tests},
            )
            outcomes.append(TriageOutcome(None, None, None, tests))

    for task_id, failures in grouped.items():
        assert task_id is not None
        outcomes.append(await _fix_one(session, task_repo, task_id, failures, replanner, bus))
    return outcomes


async def _latest_in_review(session: AsyncSession, project_id: str) -> Task | None:
    stmt = (
        select(Task)
        .where(Task.project_id == project_id, Task.state == TaskState.REVIEW)
        .order_by(Task.display_id.desc())
        .limit(1)
    )
    return (await session.scalars(stmt)).first()


def _failure_output(failures: list[Failure]) -> str:
    text = "\n\n".join(
        f"FAILED {f.test}" + (f" ({f.file})" if f.file else "") + f"\n{f.message}" for f in failures
    )
    return text[-FIX_CONTEXT_LIMIT:]


async def _fix_one(
    session: AsyncSession,
    task_repo: TaskRepository,
    task_id: str,
    failures: list[Failure],
    replanner: Replanner,
    bus: EventBus | None,
) -> TriageOutcome:
    task = await task_repo.get(task_id)
    if task is None:
        raise LookupError(f"Task {task_id!r} not found")
    tests = [f.test for f in failures]
    output = _failure_output(failures)
    cycle = int(task.inputs.get("fix_cycles", 0)) + 1
    level = escalation_for(cycle, get_settings().max_fix_cycles)
    task.inputs = {**task.inputs, "fix_cycles": cycle}
    await session.flush()

    if level is EscalationLevel.HUMAN_REVIEW:
        await _block(task_repo, task)
        await emit(
            EventType.REVIEW_REQUIRED,
            session=session,
            bus=bus,
            project_id=task.project_id,
            task_id=task.id,
            requires_review=True,
            payload={
                "human_review": True,
                "reason": "fix_cycles_exhausted",
                "fix_cycles": cycle,
                "failures": tests,
                "output": output,
            },
        )
        log.warning("qa_escalated_to_human", task_id=task.id, fix_cycles=cycle)
        return TriageOutcome(task.id, None, level, tests)

    escalation: dict[str, object] = {"fix_context": output, "escalation_level": level.value}
    if level is EscalationLevel.REPLAN:
        escalation["replan"] = (await replanner(task, output)).model_dump()
    if level is EscalationLevel.STRONGER_MODEL:
        escalation["escalation_purpose"] = STRONGEST_PURPOSE.value

    target = await _fix_target(task_repo, task, tests, escalation, bus)
    await emit(
        EventType.FIX_REQUIRED,
        session=session,
        bus=bus,
        project_id=task.project_id,
        task_id=target.id,
        payload={
            "responsible_task": task.display_id,
            "fix_task": target.display_id,
            "fix_cycle": cycle,
            "level": level.value,
            "failures": tests,
            "output": output,
        },
    )
    return TriageOutcome(task.id, target.id, level, tests)


async def _fix_target(
    task_repo: TaskRepository,
    task: Task,
    tests: list[str],
    escalation: dict[str, object],
    bus: EventBus | None,
) -> Task:
    if task.state in (TaskState.REVIEW, TaskState.FIX_REQUIRED):
        task.inputs = {**task.inputs, **escalation}
        if task.state is TaskState.REVIEW:
            return await task_repo.update_state(task.id, TaskState.FIX_REQUIRED)
        await task_repo.session.flush()
        return task

    fix = await task_repo.create(
        display_id=await task_repo.next_display_id(task.project_id),
        project_id=task.project_id,
        title=f"Fix {task.display_id}: {tests[0]}"[:300],
        role=task.role,
        inputs={**escalation, "fix_for": task.display_id},
        outputs={},
        acceptance_checks=[f"{test} passes" for test in tests],
    )
    await emit(
        EventType.TASK_CREATED,
        session=task_repo.session,
        bus=bus,
        project_id=fix.project_id,
        task_id=fix.id,
        payload={"display_id": fix.display_id, "title": fix.title, "role": fix.role},
    )
    return await task_repo.update_state(fix.id, TaskState.READY)


async def _block(task_repo: TaskRepository, task: Task) -> None:
    if task.state is TaskState.REVIEW:
        task = await task_repo.update_state(task.id, TaskState.FIX_REQUIRED)
    if task.state in (TaskState.FIX_REQUIRED, TaskState.IN_PROGRESS):
        await task_repo.update_state(task.id, TaskState.BLOCKED)
