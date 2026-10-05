"""Human approval gates (FR-130..FR-132).

``request_approval()`` is called from inside a LangGraph node. It records an append-only
``Approval`` request with its evidence bundle, emits APPROVAL_REQUESTED, posts a chat
notification, and pauses the run with ``interrupt()``. ``decide()`` (behind
``POST /api/v1/approvals/{id}``, Approver only) records the decision as a second append-only row
pointing at the request, and the runner (``orchestrator/runner.py``) then resumes the paused
graph with that decision as the interrupt's resume value.

Two LangGraph details shape this module:

- On resume LangGraph re-runs the *whole* node, so everything before ``interrupt()`` runs twice.
  The request is therefore keyed by the caller's ``request_key`` (unique per project): a
  re-run finds the existing row instead of creating - and notifying about - a second one.
- ``interrupt()`` works by raising. Raised inside a ``session_scope`` it would roll the request
  back, so the request is committed in its own session *before* ``interrupt()`` is called.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from langgraph.types import interrupt
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus, EventType, emit
from web_department.collaboration.notifications import notify
from web_department.config.settings import get_settings
from web_department.db.base import session_scope
from web_department.db.models import Approval
from web_department.db.repositories import ApprovalRepository, ProjectRepository
from web_department.logging import get_logger

log = get_logger(__name__)

PENDING = "pending"


class ActionType(StrEnum):
    """Actions that always need a human Approver (FR-130)."""

    PRODUCTION_DEPLOY = "production_deploy"
    DESTRUCTIVE_DB = "destructive_db"
    SECRET_CHANGE = "secret_change"
    MAJOR_ARCHITECTURE = "major_architecture"
    INFRASTRUCTURE = "infrastructure"
    SECURITY_EXCEPTION = "security_exception"


class Decision(StrEnum):
    """An Approver's answer."""

    APPROVE = "approve"
    REJECT = "reject"


class ApprovalDecision(BaseModel):
    """A recorded decision - also the value a paused graph is resumed with."""

    approval_id: str
    decision: Decision
    comment: str | None = None
    approver_id: str


class ApprovalNotFound(LookupError):
    """No approval *request* with that id."""


class AlreadyDecided(Exception):
    """The request already has its (one) decision."""


Evidence = dict[str, Any] | Callable[[], Awaitable[dict[str, Any]]]


async def get_request(session: AsyncSession, approval_id: str) -> Approval:
    """The approval request ``approval_id`` (not a decision row), or ``ApprovalNotFound``."""
    approval = await ApprovalRepository(session).get(approval_id)
    if approval is None or approval.decides_id is not None or approval.status != PENDING:
        raise ApprovalNotFound(approval_id)
    return approval


async def decision_for(session: AsyncSession, approval_id: str) -> ApprovalDecision | None:
    """The decision recorded for request ``approval_id``, if any."""
    row = (
        await session.scalars(select(Approval).where(Approval.decides_id == approval_id))
    ).first()
    if row is None:
        return None
    return ApprovalDecision(
        approval_id=approval_id,
        decision=Decision.APPROVE if row.status == "approved" else Decision.REJECT,
        comment=row.comment,
        approver_id=row.approver_id or "",
    )


async def create_request(
    session: AsyncSession,
    project_id: str,
    action_type: ActionType,
    summary: str,
    evidence: dict[str, Any],
    *,
    request_key: str | None = None,
    bus: EventBus | None = None,
) -> Approval:
    """Record a pending request and emit APPROVAL_REQUESTED (no notification, no pause)."""
    approval = await ApprovalRepository(session).create(
        project_id=project_id,
        action_type=action_type.value,
        summary=summary,
        status=PENDING,
        request_key=request_key,
        evidence=evidence,
    )
    await emit(
        EventType.APPROVAL_REQUESTED,
        session=session,
        bus=bus,
        project_id=project_id,
        requires_review=True,
        status=PENDING,
        payload={"approval_id": approval.id, "action_type": action_type.value, "summary": summary},
    )
    return approval


async def decide(
    session: AsyncSession,
    approval_id: str,
    decision: Decision,
    *,
    approver_id: str,
    comment: str | None,
    bus: EventBus | None = None,
) -> ApprovalDecision:
    """Record the Approver's decision on request ``approval_id`` (FR-131) and emit
    APPROVED/REJECTED. Raises ``ApprovalNotFound`` or ``AlreadyDecided``.
    """
    request = await get_request(session, approval_id)
    if await decision_for(session, approval_id) is not None:
        raise AlreadyDecided(approval_id)
    status = "approved" if decision is Decision.APPROVE else "rejected"
    await ApprovalRepository(session).create(
        project_id=request.project_id,
        action_type=request.action_type,
        summary=request.summary,
        status=status,
        approver_id=approver_id,
        comment=comment,
        decided_at=datetime.now(UTC),
        decides_id=request.id,
    )
    await emit(
        EventType.APPROVED if decision is Decision.APPROVE else EventType.REJECTED,
        session=session,
        bus=bus,
        project_id=request.project_id,
        status=status,
        payload={
            "approval_id": request.id,
            "action_type": request.action_type,
            "approver_id": approver_id,
            "comment": comment,
        },
    )
    return ApprovalDecision(
        approval_id=request.id, decision=decision, comment=comment, approver_id=approver_id
    )


def notification_text(approval: Approval, project_title: str | None = None) -> str:
    """The chat message announcing a new request."""
    base = get_settings().public_api_base_url.rstrip("/")
    who = f"'{project_title}'" if project_title else approval.project_id
    return (
        f"Approval needed: {approval.action_type} for project {who}\n"
        f"{approval.summary}\n"
        f"Review: GET {base}/api/v1/approvals/{approval.id}\n"
        f"Decide: POST {base}/api/v1/approvals/{approval.id} "
        '{"decision": "approve"|"reject", "comment": "..."}'
    )


async def request_approval(
    project_id: str,
    action_type: ActionType,
    summary: str,
    evidence: Evidence,
    *,
    request_key: str,
    bus: EventBus | None = None,
) -> ApprovalDecision:
    """Ask a human (FR-130) and pause the graph until they answer; returns their decision.

    Call from inside a LangGraph node. ``request_key`` must be unique per project per request
    and stable across re-runs of the same node (e.g. ``"production_deploy:<round>"``).
    ``evidence`` may be a coroutine factory, so building it (git, reports) only happens once,
    when the request is first created.
    """
    async with session_scope() as session:
        existing = (
            await session.scalars(
                select(Approval).where(
                    Approval.project_id == project_id, Approval.request_key == request_key
                )
            )
        ).first()
        created = existing is None
        if existing is None:
            bundle = evidence if isinstance(evidence, dict) else await evidence()
            approval = await create_request(
                session,
                project_id,
                action_type,
                summary,
                bundle,
                request_key=request_key,
                bus=bus,
            )
        else:
            approval = existing
        approval_id = approval.id
        project = await ProjectRepository(session).get(project_id)
        text = notification_text(approval, project.title if project else None)
    # Committed (session_scope exited) before interrupt() raises - see the module docstring.

    if created:
        log.info("approval_requested", project_id=project_id, approval_id=approval_id)
        await notify(text)

    resumed = interrupt(
        {"approval_id": approval_id, "action_type": action_type.value, "summary": summary}
    )
    decision = ApprovalDecision.model_validate(resumed)
    if decision.approval_id != approval_id:
        raise ValueError(
            f"resumed with a decision for {decision.approval_id!r}, expected {approval_id!r}"
        )
    return decision
