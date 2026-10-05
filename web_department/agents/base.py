"""BaseWorker: the generic plan -> edit -> check -> retry loop every worker role shares.

FR-100..FR-104 describe this discipline for the QA worker specifically, but the same rule -
never mark a task done without its checks passing, retry a bounded number of times, then escalate
- applies to every worker's own task, so it lives here rather than being duplicated per role.

Design note: the step spec that introduced this module described the constructor loosely as
``BaseWorker(agent_record, blueprint_dict, workstation, router)``. The LLM router is a stateless
module (``llm/router.py``), not something instantiated per worker - every other caller (WebAgent,
Planner) imports ``complete`` at module level and tests fake it with
``monkeypatch.setattr(module, "complete", ...)`` (AGENTS.md rule 6). This module follows that same
convention rather than accepting a callable, and takes the ``WorkstationManager`` every tool call
actually needs instead (which the loose signature omitted).
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus, EventType, emit
from web_department.config.permissions import Role
from web_department.config.settings import get_settings
from web_department.db.models import Agent, Task, Workstation
from web_department.db.repositories import RequirementSpecRepository, TaskRepository
from web_department.db.states import TaskState
from web_department.llm.prompts import render as render_prompt
from web_department.llm.router import Purpose, complete
from web_department.logging import get_logger
from web_department.orchestrator.wrs_schema import WebRequirementsSpec
from web_department.qa.checks import CHECKS, CheckResult
from web_department.workstation import tools
from web_department.workstation.manager import WorkstationManager

log = get_logger(__name__)

TEMPLATE_GUIDE_PATH = "TEMPLATE_GUIDE.md"
DEFAULT_TEST_PATH = "."


class FileEdit(BaseModel):
    """One file to write, in full (never a diff)."""

    model_config = ConfigDict(extra="forbid")

    path: str
    content: str


class WorkerPlan(BaseModel):
    """The LLM's response for one attempt: the files to write and a one-line summary."""

    model_config = ConfigDict(extra="forbid")

    files: list[FileEdit] = Field(min_length=1)
    summary: str


@dataclass(frozen=True)
class TaskResult:
    """The outcome of one ``BaseWorker.run()`` call."""

    success: bool
    files_changed: list[str]
    tests_passed: bool
    summary: str
    attempts: int


@dataclass(frozen=True)
class CheckFailure:
    """Why a task's checks failed, shaped for the ``worker_fix`` prompt's feedback."""

    command: str
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class _AttemptOutcome:
    files_changed: list[str]
    summary: str
    passed: bool
    command: str
    exit_code: int
    stdout: str
    stderr: str


class BaseWorker:
    """Generic plan -> edit files via tools -> run checks -> retry loop (FR-100..FR-104).

    Subclasses set ``role`` (for permission checks) and may override ``purpose``,
    ``confidential``, ``system_prompt_name`` and ``evaluation_checks``, plus three hooks:
    ``_build_initial_messages`` (the role's own context builder), ``_reject_plan`` (refuse a plan
    before anything is written) and ``_run_checks`` (what "this task's checks pass" means for the
    role - by default, pytest).
    """

    role: Role
    purpose: Purpose = Purpose.CODING
    confidential: bool = True
    system_prompt_name: str = "worker_developer"
    # Names from qa/checks.py's CHECKS registry, normally the role's Blueprint ``evaluation``
    # list (agents/registry.py copies it onto the class). The default ``_run_checks`` ignores
    # these - it is the pytest-only behaviour roles had before specialisation - so a subclass
    # opts in by calling ``_run_evaluation_checks()`` from its own ``_run_checks``.
    evaluation_checks: tuple[str, ...] = ()

    def __init__(
        self,
        session: AsyncSession,
        agent: Agent,
        blueprint: dict[str, Any],
        workstation: Workstation,
        manager: WorkstationManager,
        bus: EventBus | None = None,
        fs_guard: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    ) -> None:
        self.session = session
        self.agent = agent
        self.blueprint = blueprint
        self.workstation = workstation
        self.manager = manager
        self.bus = bus
        self._task_repo = TaskRepository(session, bus=bus)
        self._llm_calls = 0
        # Called around every file-writing attempt, not just once around run(): when several
        # tasks share one workstation (orchestrator/scheduler.py, FR-034..FR-037), this is how a
        # caller serializes the filesystem-touching part of each attempt (and checks out that
        # task's own git branch first) while still letting attempts' LLM calls - the slow part -
        # happen concurrently. Defaults to a no-op, so single-task callers are unaffected.
        self._fs_guard = fs_guard or (lambda: nullcontext())

    async def run(self, task: Task) -> TaskResult:
        """Drive ``task`` to completion or exhaustion (FR-100..FR-104).

        Loops plan -> write files -> run checks -> (on failure) feed the exact error output back,
        up to ``Settings.max_task_retries`` attempts, bounded overall by the
        ``Settings.max_llm_calls_per_task`` hard cap. On success: commits as
        ``"<display_id>: <title>"``, moves the task to REVIEW and emits TASK_COMPLETED. On
        exhaustion: moves the task to BLOCKED and emits TEST_FAILED.
        """
        settings = get_settings()
        max_llm_calls = settings.max_llm_calls_per_task

        if task.state in (TaskState.ASSIGNED, TaskState.FIX_REQUIRED):
            # FIX_REQUIRED (qa/failure_triage.py) can only reach REVIEW again via IN_PROGRESS.
            await self._task_repo.update_state(task.id, TaskState.IN_PROGRESS)

        messages = await self._build_initial_messages(task)
        last_outcome: _AttemptOutcome | None = None

        for attempt in range(1, settings.max_task_retries + 1):
            if self._llm_calls >= max_llm_calls:
                log.warning(
                    "worker_llm_call_budget_exceeded", task_id=task.id, calls=self._llm_calls
                )
                break

            plan = await self._ask(messages, task)
            async with self._fs_guard():
                outcome = await self._apply_and_check(task, plan)
            last_outcome = outcome

            if outcome.passed:
                await self._task_repo.update_state(task.id, TaskState.REVIEW)
                await emit(
                    EventType.TASK_COMPLETED,
                    session=self.session,
                    bus=self.bus,
                    project_id=task.project_id,
                    task_id=task.id,
                    agent_id=self.agent.id,
                    payload={
                        "files_changed": outcome.files_changed,
                        "tests_passed": True,
                        "attempts": attempt,
                        "summary": outcome.summary,
                    },
                )
                return TaskResult(
                    success=True,
                    files_changed=outcome.files_changed,
                    tests_passed=True,
                    summary=outcome.summary,
                    attempts=attempt,
                )

            await self._task_repo.set_retries(task.id, attempt)
            messages.append({"role": "assistant", "content": plan.model_dump_json()})
            messages.append(
                {
                    "role": "user",
                    "content": render_prompt(
                        "worker_fix",
                        command=outcome.command,
                        exit_code=outcome.exit_code,
                        stdout=outcome.stdout,
                        stderr=outcome.stderr,
                    ),
                }
            )

        return await self._exhaust(task, last_outcome)

    async def _exhaust(self, task: Task, last_outcome: _AttemptOutcome | None) -> TaskResult:
        failure_summary = (
            f"exit_code={last_outcome.exit_code}\n{last_outcome.stdout}\n{last_outcome.stderr}"
            if last_outcome is not None
            else "No LLM call succeeded within this task's call budget."
        )
        await self._task_repo.update_state(task.id, TaskState.BLOCKED)
        await emit(
            EventType.TEST_FAILED,
            session=self.session,
            bus=self.bus,
            project_id=task.project_id,
            task_id=task.id,
            agent_id=self.agent.id,
            payload={"failure_summary": failure_summary},
        )
        return TaskResult(
            success=False,
            files_changed=last_outcome.files_changed if last_outcome else [],
            tests_passed=False,
            summary=failure_summary,
            attempts=self._llm_calls,
        )

    async def _ask(self, messages: list[dict[str, str]], task: Task) -> WorkerPlan:
        # FR-103 escalation level 2 (qa/failure_triage.py) reroutes one task to the strongest
        # model by naming the purpose whose routing prefers it.
        purpose = Purpose(task.inputs.get("escalation_purpose", self.purpose))
        result = await complete(
            messages,
            purpose=purpose,
            confidential=self.confidential,
            json_schema=WorkerPlan,
            agent_id=self.agent.id,
            task_id=task.id,
        )
        self._llm_calls += 1
        assert isinstance(result.parsed, WorkerPlan)  # complete() parsed our own schema
        return result.parsed

    async def _apply_and_check(self, task: Task, plan: WorkerPlan) -> _AttemptOutcome:
        rejection = self._reject_plan(task, plan)
        if rejection is not None:
            return _AttemptOutcome(
                files_changed=[],
                summary=plan.summary,
                passed=False,
                command="plan review (nothing was written)",
                exit_code=1,
                stdout=rejection,
                stderr="",
            )

        files_changed: list[str] = []
        for file_edit in plan.files:
            await tools.write_file(
                self.session,
                self.workstation,
                self.role,
                self.agent.id,
                file_edit.path,
                file_edit.content,
                bus=self.bus,
            )
            files_changed.append(file_edit.path)

        failure = await self._run_checks(task)
        if failure is not None:
            return _AttemptOutcome(
                files_changed=files_changed,
                summary=plan.summary,
                passed=False,
                command=failure.command,
                exit_code=failure.exit_code,
                stdout=failure.stdout,
                stderr=failure.stderr,
            )

        commit_result = await tools.git_commit(
            self.session,
            self.workstation,
            self.role,
            self.agent.id,
            self.manager,
            f"{task.display_id}: {task.title}",
            bus=self.bus,
        )
        return _AttemptOutcome(
            files_changed=files_changed,
            summary=plan.summary,
            passed=commit_result.exit_code == 0,
            command="git commit",
            exit_code=commit_result.exit_code,
            stdout=commit_result.stdout,
            stderr=commit_result.stderr,
        )

    def _reject_plan(self, task: Task, plan: WorkerPlan) -> str | None:
        """Return why ``plan`` must not be applied at all, or None to apply it (default: None).

        A rejection costs one attempt like a failed check does, and its message is fed back to
        the LLM the same way, but nothing is written to the workspace.
        """
        return None

    async def _run_checks(self, task: Task) -> CheckFailure | None:
        """Run this task's checks after its files were written; None means they all passed.

        Default: ``pytest {task.inputs["test_path"]}`` (or the whole repo).
        """
        test_path = str(task.inputs.get("test_path", DEFAULT_TEST_PATH))
        check_result = await tools.run_tests(
            self.session,
            self.workstation,
            self.role,
            self.agent.id,
            self.manager,
            path=test_path,
            bus=self.bus,
        )
        if check_result.exit_code == 0:
            return None
        return CheckFailure(
            command=f"pytest {test_path}",
            exit_code=check_result.exit_code,
            stdout=check_result.stdout,
            stderr=check_result.stderr,
        )

    async def _run_evaluation_checks(self) -> CheckFailure | None:
        """Run every check in ``evaluation_checks`` (qa/checks.py); report all failures at once.

        All checks run even after one fails, so a single retry can fix every problem rather than
        discovering them one attempt at a time.
        """
        failures: list[CheckResult] = []
        for name in self.evaluation_checks:
            result = await CHECKS[name](
                self.session,
                self.workstation,
                self.role,
                self.agent.id,
                self.manager,
                bus=self.bus,
            )
            if not result.passed:
                failures.append(result)
        if not failures:
            return None
        return CheckFailure(
            command="evaluation checks: " + ", ".join(self.evaluation_checks),
            exit_code=1,
            stdout="\n".join(f"{f.name}: {f.detail}" for f in failures),
            stderr="",
        )

    async def _build_initial_messages(self, task: Task) -> list[dict[str, str]]:
        prompt = render_prompt(
            self.system_prompt_name,
            template_guide=await self._template_guide(),
            task_title=task.title,
            task_inputs=_format_inputs(task.inputs),
            acceptance_checks=task.acceptance_checks,
            wrs_excerpt=await self._wrs_excerpt(task),
            relevant_files=await self._relevant_files(),
        )
        return [{"role": "user", "content": prompt}]

    async def _template_guide(self) -> str:
        try:
            return await tools.read_file(
                self.session,
                self.workstation,
                self.role,
                self.agent.id,
                TEMPLATE_GUIDE_PATH,
                bus=self.bus,
            )
        except OSError:
            log.warning("worker_missing_template_guide", workstation_id=self.workstation.id)
            return "(TEMPLATE_GUIDE.md not found in this workspace.)"

    async def _relevant_files(self) -> str:
        lines: list[str] = []
        for directory in (".", "backend/app", "frontend/src"):
            try:
                names = await tools.list_dir(
                    self.session,
                    self.workstation,
                    self.role,
                    self.agent.id,
                    directory,
                    bus=self.bus,
                )
            except OSError:
                continue
            lines.append(f"{directory}/:")
            lines.extend(f"  {name}" for name in names)
        return "\n".join(lines)

    async def _load_wrs(self, task: Task) -> WebRequirementsSpec | None:
        """The project's latest validated WRS, or None if it has none yet."""
        spec = await RequirementSpecRepository(self.session).latest_for_project(task.project_id)
        if spec is None:
            return None
        return WebRequirementsSpec.model_validate(spec.json)

    async def _wrs_excerpt(self, task: Task) -> str | None:
        wrs = await self._load_wrs(task)
        if wrs is None:
            return None

        entity_name = task.inputs.get("entity")
        if entity_name:
            entity = next((e for e in wrs.entities if e.name == entity_name), None)
            if entity is not None:
                return entity.model_dump_json(indent=2)

        feature_id = task.inputs.get("feature")
        if feature_id:
            feature = next((f for f in wrs.features if f.id == feature_id), None)
            if feature is not None:
                return feature.model_dump_json(indent=2)

        route = task.inputs.get("route")
        if route:
            page = next((p for p in wrs.pages if p.route == route), None)
            if page is not None:
                return page.model_dump_json(indent=2)

        return f"{wrs.project_type} / {wrs.domain}"


def _format_inputs(inputs: dict[str, Any]) -> str:
    return "\n".join(f"- {key}: {value}" for key, value in inputs.items())
