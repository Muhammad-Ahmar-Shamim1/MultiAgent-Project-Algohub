"""The Security worker (FR-110..FR-112): scanners + an LLM checklist review, through the gate.

Unlike the other workers it writes no code (the Security role has no write permission, SRS
section 10), so it replaces ``BaseWorker.run``'s plan -> edit -> check loop with:

1. every scanner in ``security/scanners.py``, inside the workstation;
2. one LLM review of the application code against ``CHECKLIST`` (FR-111), with
   ``confidential=True`` - the code under review may hold the very secrets being looked for, so it
   only ever goes to a private (local) provider (IR-024), whatever ``CODER_CONFIDENTIAL`` says;
3. ``security/security_gate.enforce`` over both: fix tasks for High/Critical findings, the
   SECURITY_BLOCKED event, and the saved report.

The task moves to REVIEW when the gate passes and to BLOCKED when it does not - it is the fix
tasks, not this one, that get reworked; a later security task re-runs the scan.

Context for the review is the app's own source (``backend/app``, ``frontend/src``), the API
contract and the deployment config - with line numbers so findings can cite them - capped in
size; tests, dependencies and virtualenvs are left out.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.agents.base import BaseWorker, TaskResult
from web_department.collaboration.events import EventBus, EventType, emit
from web_department.config.permissions import Role
from web_department.db.models import Agent, Task, Workstation
from web_department.db.states import TaskState
from web_department.llm.prompts import render as render_prompt
from web_department.llm.router import AllProvidersFailedError, Purpose, complete
from web_department.logging import get_logger
from web_department.security import scanners, security_gate
from web_department.security.scanners import Finding, ScannerRun, Severity
from web_department.workstation import tools
from web_department.workstation.manager import WorkstationManager

log = get_logger(__name__)

ChecklistItem = Literal[
    "authn",
    "authz",
    "input_validation",
    "access_control",
    "rate_limiting",
    "secrets",
    "data_isolation",
    "env_separation",
    "audit_logging",
]

CHECKLIST: dict[str, str] = {
    "authn": "Authentication: passwords hashed, tokens signed/expiring, no auth bypass.",
    "authz": "Server-side authorization on EVERY protected endpoint (admin routes included) - "
    "never only hidden in the UI.",
    "input_validation": "Every request body/query/path value validated (Pydantic/zod), no raw "
    "SQL string building, no unvalidated redirects.",
    "access_control": "Users can only read/change their own records (no IDOR); roles enforced.",
    "rate_limiting": "Login, signup and other abuse-prone API endpoints are rate limited.",
    "secrets": "No secret, key or password hard-coded in source; all come from environment.",
    "data_isolation": "Queries are scoped to the owning user/tenant; no cross-tenant leaks.",
    "env_separation": "Dev/test/prod config separated; debug off and CORS restricted in prod.",
    "audit_logging": "Security-relevant actions (login, role change, admin actions) are logged.",
}

_SOURCE_DIRS = ("backend/app", "frontend/src")
_CONFIG_FILES = (
    "openapi.yaml",
    "docker-compose.yml",
    ".env.example",
    "backend/.env.example",
    "frontend/next.config.ts",
)
_SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx")
_SKIP_DIRS = frozenset({"node_modules", ".venv", "__pycache__", "tests", ".next"})
_FILE_LIMIT = 8_000
_TOTAL_LIMIT = 60_000


class ReviewFinding(BaseModel):
    """One checklist finding as the LLM reports it."""

    model_config = ConfigDict(extra="forbid")

    checklist_item: ChecklistItem
    severity: Severity
    file: str | None = None
    line: int | None = None
    message: str


class SecurityReview(BaseModel):
    """The LLM's whole review; an empty list means it found nothing."""

    model_config = ConfigDict(extra="forbid")

    findings: list[ReviewFinding] = Field(default_factory=list)


class SecurityWorker(BaseWorker):
    """Runs the scanners and the checklist review, then enforces the security gate."""

    role = Role.SECURITY
    purpose = Purpose.REVIEW
    confidential = True
    system_prompt_name = "worker_security"

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
        super().__init__(
            session, agent, blueprint, workstation, manager, bus=bus, fs_guard=fs_guard
        )
        self.confidential = True  # never configurable for this role (see module docstring)

    async def run(self, task: Task) -> TaskResult:
        """Scan, review, enforce the gate; REVIEW on pass, BLOCKED on block (FR-112)."""
        if task.state in (TaskState.ASSIGNED, TaskState.FIX_REQUIRED):
            await self._task_repo.update_state(task.id, TaskState.IN_PROGRESS)

        async with self._fs_guard():
            findings, runs = await scanners.run_scanners(
                self.session, self.workstation, self.agent.id, self.manager, bus=self.bus
            )
            review_findings, review_run = await self._review(task)
            report = await security_gate.enforce(
                self.session,
                self.workstation,
                [*findings, *review_findings],
                [*runs, review_run],
                bus=self.bus,
            )

        summary = (
            f"security gate passed ({len(report.warnings)} warning(s))"
            if report.passed
            else "security gate blocked: " + "; ".join(report.reasons)
        )
        if report.passed:
            await self._task_repo.update_state(task.id, TaskState.REVIEW)
            await emit(
                EventType.TASK_COMPLETED,
                session=self.session,
                bus=self.bus,
                project_id=task.project_id,
                task_id=task.id,
                agent_id=self.agent.id,
                payload={"summary": summary, "warnings": len(report.warnings)},
            )
        else:
            await self._task_repo.update_state(task.id, TaskState.BLOCKED)
        return TaskResult(
            success=report.passed,
            files_changed=[],
            tests_passed=report.passed,
            summary=summary,
            attempts=self._llm_calls,
        )

    async def _review(self, task: Task) -> tuple[list[Finding], ScannerRun]:
        prompt = render_prompt(
            self.system_prompt_name,
            template_guide=await self._template_guide(),
            task_title=task.title,
            checklist=CHECKLIST,
            sources=await self._sources(),
        )
        try:
            result = await complete(
                [{"role": "user", "content": prompt}],
                purpose=self.purpose,
                confidential=True,
                json_schema=SecurityReview,
                agent_id=self.agent.id,
                task_id=task.id,
            )
        except AllProvidersFailedError as exc:
            log.warning("security_review_unavailable", task_id=task.id, error=str(exc))
            return [], ScannerRun(tool="llm-review", status="error", detail=str(exc)[-500:])
        self._llm_calls += 1
        assert isinstance(result.parsed, SecurityReview)  # complete() parsed our own schema
        found = [
            Finding(
                tool="llm-review",
                rule=f"checklist:{f.checklist_item}",
                severity=f.severity,
                file=f.file,
                line=f.line,
                message=f.message,
            )
            for f in result.parsed.findings
        ]
        return found, ScannerRun(tool="llm-review", status="ok", findings=len(found))

    async def _sources(self) -> dict[str, str]:
        """``{path: numbered contents}`` for the files the review needs, within the size caps."""
        paths: list[str] = [p for p in _CONFIG_FILES if await self._read(p) is not None]
        for directory in _SOURCE_DIRS:
            paths += await self._walk(directory)
        sources: dict[str, str] = {}
        total = 0
        for path in paths:
            content = await self._read(path)
            if content is None:
                continue
            numbered = "\n".join(
                f"{n:4d} | {line}" for n, line in enumerate(content.splitlines(), start=1)
            )[:_FILE_LIMIT]
            if total + len(numbered) > _TOTAL_LIMIT:
                sources[path] = "(omitted: review size limit reached)"
                continue
            sources[path] = numbered
            total += len(numbered)
        return sources

    async def _walk(self, directory: str) -> list[str]:
        try:
            entries = await tools.list_dir(
                self.session, self.workstation, self.role, self.agent.id, directory, bus=self.bus
            )
        except OSError:
            return []
        found: list[str] = []
        for entry in entries:
            if entry.endswith("/"):
                if entry[:-1] not in _SKIP_DIRS:
                    found += await self._walk(f"{directory}/{entry[:-1]}")
            elif entry.endswith(_SOURCE_SUFFIXES):
                found.append(f"{directory}/{entry}")
        return found

    async def _read(self, path: str) -> str | None:
        try:
            return await tools.read_file(
                self.session, self.workstation, self.role, self.agent.id, path, bus=self.bus
            )
        except (OSError, UnicodeDecodeError):
            return None
