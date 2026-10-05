"""The evidence bundle an Approver sees with a request (FR-131).

``build_evidence(project_id)`` gathers, from the project's workstation:

- ``diff``: the commits and the ``git diff --stat`` from the template's initial commit to HEAD;
- ``qa_report`` / ``security_report``: the JSON reports the QA and Security gates saved
  (``artifacts/qa-report.json``, ``artifacts/security-report.json``), or None if not run;
- ``screenshots``: the image paths under ``qa/screenshots/``;
- ``deployment_plan``: target, the commit that would ship, and the steps. ``deployment/`` is
  still a stub, so the plan is the fixed golden-v1 sequence, marked ``draft``.

Every part is best-effort: a missing workstation, report or git history yields a note in that
field rather than an error - an approval request must never fail to be raised because one piece
of evidence is unavailable. Git runs through ``workstation/tools.py`` as the DevOps role (the
role that owns deployment, with RUN_COMMAND); text is truncated to keep the row small.
"""

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from web_department.config.permissions import Role
from web_department.db.base import session_scope
from web_department.db.models import Workstation
from web_department.db.repositories import RequirementSpecRepository, WorkstationRepository
from web_department.orchestrator.wrs_schema import WebRequirementsSpec
from web_department.workstation import tools
from web_department.workstation.manager import WorkstationManager

ROLE = Role.DEVOPS
QA_REPORT = "artifacts/qa-report.json"
SECURITY_REPORT = "artifacts/security-report.json"
SCREENSHOT_DIR = "qa/screenshots"
TEXT_LIMIT = 8_000
REPORT_LIMIT = 40_000
DEPLOY_STEPS = [
    "Build the backend and frontend images from this commit",
    "Run database migrations (alembic upgrade head)",
    "Start the new containers",
    "Post-deploy health check (/health)",
    "On failure: redeploy the previous commit",
]


async def build_evidence(project_id: str) -> dict[str, Any]:
    """The evidence bundle for ``project_id`` (see module docstring)."""
    async with session_scope() as session:
        workstations = await WorkstationRepository(session).list(project_id=project_id)
        spec = await RequirementSpecRepository(session).latest_for_project(project_id)
        target = WebRequirementsSpec.model_validate(spec.json).deployment_target if spec else None
        if not workstations:
            return {
                "note": "no workstation provisioned - no code to show",
                "diff": None,
                "qa_report": None,
                "security_report": None,
                "screenshots": [],
                "deployment_plan": _plan(target, None),
            }
        ws = workstations[-1]
        manager = WorkstationManager(session)
        diff, head = await _diff(session, ws, manager)
        return {
            "diff": diff,
            "qa_report": await _report(session, ws, QA_REPORT),
            "security_report": await _report(session, ws, SECURITY_REPORT),
            "screenshots": await _screenshots(session, ws),
            "deployment_plan": _plan(target, head),
        }


async def _git(
    session: AsyncSession, ws: Workstation, manager: WorkstationManager, *args: str
) -> str | None:
    try:
        result = await tools.run_command(session, ws, ROLE, None, manager, ["git", *args])
    except Exception:  # a broken/stopped workstation must not stop the approval request
        return None
    return result.stdout.strip() if result.exit_code == 0 else None


async def _diff(
    session: AsyncSession, ws: Workstation, manager: WorkstationManager
) -> tuple[dict[str, Any] | None, str | None]:
    head = await _git(session, ws, manager, "rev-parse", "HEAD")
    roots = await _git(session, ws, manager, "rev-list", "--max-parents=0", "HEAD")
    if not head or not roots:
        return None, head
    root = roots.splitlines()[0]
    stat = await _git(session, ws, manager, "diff", "--stat", root, "HEAD") or ""
    log = await _git(session, ws, manager, "log", "--oneline", "-n", "50") or ""
    return {
        "base": root,
        "head": head,
        "stat": stat[-TEXT_LIMIT:],
        "commits": log.splitlines(),
    }, head


async def _report(session: AsyncSession, ws: Workstation, path: str) -> dict[str, Any] | None:
    try:
        raw = await tools.read_file(session, ws, ROLE, None, path)
    except OSError:
        return None
    if len(raw) > REPORT_LIMIT:
        return {"truncated": True, "note": f"{path} is too large to inline; see the artifact"}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"note": f"{path} is not valid JSON"}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


async def _screenshots(session: AsyncSession, ws: Workstation) -> list[str]:
    try:
        names = await tools.list_dir(session, ws, ROLE, None, SCREENSHOT_DIR)
    except OSError:
        return []
    return [f"{SCREENSHOT_DIR}/{n}" for n in names if n.lower().endswith((".png", ".jpg", ".jpeg"))]


def _plan(target: str | None, commit: str | None) -> dict[str, Any]:
    return {
        "status": "draft",
        "target": target or "local-docker",
        "commit": commit,
        "steps": DEPLOY_STEPS,
    }
