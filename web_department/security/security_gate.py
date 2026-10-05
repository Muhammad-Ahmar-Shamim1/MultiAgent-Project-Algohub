"""Security gate (FR-112, FR-113): block on High/Critical, fix tasks, warnings, exceptions.

- Any High or Critical finding blocks release and gets a READY fix task for the role owning its
  file (one per finding; a finding that already has an open fix task reuses it).
- Medium findings are warnings: reported, not blocking.
- A scanner that failed to run blocks too - "the scan didn't happen" is not "the scan was
  clean" (FR-110 requires the scans before approval). A scanner *skipped* because its input does
  not exist yet (``trivy image`` with no exported image) is reported as a warning.
- A finding an Approver has accepted (``POST /api/v1/security/exceptions``, FR-113) is listed as
  excepted and does not block. Exceptions are append-only ``Approval`` rows
  (``action_type="security_exception"``) plus an APPROVED event - the audit log (DR-003).

The report is saved as ``artifacts/security-report.json`` and ``.md``. The Security role holds no
write permission (SRS section 10), so this platform code writes those two files into the
workspace itself (path-checked by ``tools.resolve_workspace_path``) rather than through a
worker's tool call.
"""

import json
from collections.abc import Iterable

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus, EventType, emit
from web_department.config.permissions import Role
from web_department.db.models import Approval, Task, Workstation
from web_department.db.repositories import TaskRepository
from web_department.db.states import TaskState
from web_department.security.scanners import Finding, ScannerRun
from web_department.workstation.tools import resolve_workspace_path

EXCEPTION_ACTION = "security_exception"
REPORT_JSON_PATH = "artifacts/security-report.json"
REPORT_MD_PATH = "artifacts/security-report.md"
BLOCKING = frozenset({"high", "critical"})
_CLOSED = (TaskState.COMPLETED, TaskState.CANCELLED)


class SecurityReport(BaseModel):
    """The gate's verdict over one security run."""

    passed: bool
    blocking: list[Finding] = Field(default_factory=list)
    warnings: list[Finding] = Field(default_factory=list)
    informational: list[Finding] = Field(default_factory=list)
    excepted: list[Finding] = Field(default_factory=list)
    scanners: list[ScannerRun] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    fix_task_ids: list[str] = Field(default_factory=list)


def evaluate(
    findings: Iterable[Finding], runs: Iterable[ScannerRun], excepted: set[str]
) -> SecurityReport:
    """Sort findings by severity and decide pass/block (no side effects)."""
    report = SecurityReport(passed=True, scanners=list(runs))
    seen: set[str] = set()
    for finding in findings:
        if finding.fingerprint in seen:
            continue
        seen.add(finding.fingerprint)
        if finding.fingerprint in excepted:
            report.excepted.append(finding)
        elif finding.severity in BLOCKING:
            report.blocking.append(finding)
        elif finding.severity == "medium":
            report.warnings.append(finding)
        else:
            report.informational.append(finding)

    if report.blocking:
        report.reasons.append(f"{len(report.blocking)} high/critical finding(s)")
    errored = [r.tool for r in report.scanners if r.status == "error"]
    if errored:
        report.reasons.append(f"scanner(s) failed to run: {errored}")
    report.passed = not report.reasons
    return report


async def approved_exceptions(session: AsyncSession, project_id: str) -> set[str]:
    """Fingerprints of findings an Approver has accepted for ``project_id`` (FR-113)."""
    rows = (
        await session.scalars(
            select(Approval).where(
                Approval.project_id == project_id,
                Approval.action_type == EXCEPTION_ACTION,
                Approval.status == "approved",
            )
        )
    ).all()
    fingerprints: set[str] = set()
    for row in rows:
        try:
            fingerprints.add(str(json.loads(row.summary)["fingerprint"]))
        except (ValueError, KeyError, TypeError):
            continue
    return fingerprints


def owner_role(finding: Finding) -> Role:
    """The worker role that owns the code a finding points at."""
    file = (finding.file or "").replace("\\", "/")
    name = file.rsplit("/", 1)[-1].lower()
    if finding.tool == "trivy-image" or name.startswith(("dockerfile", "docker-compose", ".env")):
        return Role.DEVOPS
    if file.startswith((".github/", "infra/")):
        return Role.DEVOPS
    if file.startswith("frontend/") or finding.tool == "npm-audit":
        return Role.FRONTEND
    return Role.BACKEND


async def enforce(
    session: AsyncSession,
    ws: Workstation,
    findings: list[Finding],
    runs: list[ScannerRun],
    *,
    bus: EventBus | None = None,
) -> SecurityReport:
    """Evaluate, create fix tasks for blocking findings, emit SECURITY_BLOCKED, save the report."""
    report = evaluate(findings, runs, await approved_exceptions(session, ws.project_id))
    task_repo = TaskRepository(session, bus=bus)
    display_ids: list[str] = []
    for finding in report.blocking:
        task = await _fix_task(task_repo, ws.project_id, finding, bus)
        report.fix_task_ids.append(task.id)
        display_ids.append(task.display_id)

    if not report.passed:
        await emit(
            EventType.SECURITY_BLOCKED,
            session=session,
            bus=bus,
            project_id=ws.project_id,
            requires_review=True,
            payload={
                "reasons": report.reasons,
                "blocking": [f"{f.tool}:{f.rule} {f.file}:{f.line}" for f in report.blocking],
                "fix_tasks": display_ids,
            },
        )
    save_report(ws, report, display_ids)
    return report


async def _fix_task(
    task_repo: TaskRepository, project_id: str, finding: Finding, bus: EventBus | None
) -> Task:
    open_tasks = (
        await task_repo.session.scalars(
            select(Task).where(Task.project_id == project_id, Task.state.not_in(_CLOSED))
        )
    ).all()
    for task in open_tasks:
        if task.inputs.get("security_fingerprint") == finding.fingerprint:
            return task

    location = f"{finding.file}:{finding.line}" if finding.line else (finding.file or "the app")
    task = await task_repo.create(
        display_id=await task_repo.next_display_id(project_id),
        project_id=project_id,
        title=f"Security fix: {finding.rule} in {location}"[:300],
        role=owner_role(finding).value,
        inputs={
            "security_fingerprint": finding.fingerprint,
            "security_finding": finding.model_dump(),
            "fix_context": f"[{finding.severity}] {finding.tool} {finding.rule}: {finding.message}",
        },
        outputs={},
        acceptance_checks=[f"{finding.tool} no longer reports {finding.rule} at {location}"],
    )
    await emit(
        EventType.TASK_CREATED,
        session=task_repo.session,
        bus=bus,
        project_id=project_id,
        task_id=task.id,
        payload={"display_id": task.display_id, "title": task.title, "role": task.role},
    )
    return await task_repo.update_state(task.id, TaskState.READY)


def to_markdown(report: SecurityReport, fix_task_labels: list[str]) -> str:
    """A human-readable summary of ``report``."""
    lines = [
        f"# Security report - {'PASS' if report.passed else 'BLOCKED'}",
        "",
        *[f"- {reason}" for reason in report.reasons],
        "",
        "## Scanners",
        "",
        "| Tool | Status | Findings | Detail |",
        "|---|---|---|---|",
        *[f"| {r.tool} | {r.status} | {r.findings} | {_cell(r.detail)} |" for r in report.scanners],
    ]
    sections = (
        ("Blocking (high/critical)", report.blocking),
        ("Warnings (medium)", report.warnings),
        ("Informational (low/info)", report.informational),
        ("Accepted exceptions", report.excepted),
    )
    for title, findings in sections:
        lines += ["", f"## {title}", ""]
        if not findings:
            lines.append("None.")
            continue
        lines += ["| Severity | Tool | Rule | Location | Message |", "|---|---|---|---|---|"]
        lines += [
            f"| {f.severity} | {f.tool} | {_cell(f.rule)} | {f.file or '-'}:{f.line or '-'} | "
            f"{_cell(f.message)} |"
            for f in findings
        ]
    if fix_task_labels:
        lines += ["", "## Fix tasks", "", *[f"- {label}" for label in fix_task_labels]]
    return "\n".join(lines).rstrip() + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")[:300]


def save_report(ws: Workstation, report: SecurityReport, fix_task_labels: list[str]) -> None:
    """Write ``artifacts/security-report.json`` and ``.md`` into the workspace."""
    for path, content in (
        (REPORT_JSON_PATH, json.dumps(report.model_dump(mode="json"), indent=2) + "\n"),
        (REPORT_MD_PATH, to_markdown(report, fix_task_labels)),
    ):
        target = resolve_workspace_path(ws.repo_path, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
