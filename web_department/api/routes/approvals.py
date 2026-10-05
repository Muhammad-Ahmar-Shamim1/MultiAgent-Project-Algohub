"""Approval requests and decisions (FR-130..FR-132, SRS 8.2)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web_department.api.auth import UserRole, require_role
from web_department.api.deps import get_bus, get_db_factory, get_redis
from web_department.api.schemas import ApprovalDecisionOut, ApprovalDecisionRequest, ApprovalOut
from web_department.collaboration.events import EventBus
from web_department.db.models import Approval
from web_department.orchestrator import runner
from web_department.security import approvals

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])

DbFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_db_factory)]
AnyRole = Annotated[
    UserRole, Depends(require_role(UserRole.REQUESTER, UserRole.APPROVER, UserRole.OPERATOR))
]


async def _to_out(session: AsyncSession, request: Approval) -> ApprovalOut:
    decision = await approvals.decision_for(session, request.id)
    return ApprovalOut(
        id=request.id,
        project_id=request.project_id,
        action_type=request.action_type,
        summary=request.summary,
        created_at=request.created_at,
        evidence=request.evidence,
        decision=ApprovalDecisionOut(**decision.model_dump()) if decision else None,
    )


@router.get("", response_model=list[ApprovalOut])
async def list_approvals(
    db_factory: DbFactory,
    _role: AnyRole,
    project_id: str | None = None,
    pending: bool = False,
) -> list[ApprovalOut]:
    """Approval requests, newest first; ``pending=true`` keeps only undecided ones."""
    async with db_factory() as session:
        stmt = select(Approval).where(
            Approval.status == approvals.PENDING, Approval.decides_id.is_(None)
        )
        if project_id is not None:
            stmt = stmt.where(Approval.project_id == project_id)
        rows = (await session.scalars(stmt.order_by(Approval.id.desc()).limit(200))).all()
        out = [await _to_out(session, row) for row in rows]
        return [a for a in out if a.decision is None] if pending else out


@router.get("/{approval_id}", response_model=ApprovalOut)
async def get_approval(approval_id: str, db_factory: DbFactory, _role: AnyRole) -> ApprovalOut:
    """One request with its evidence bundle (FR-131) and decision."""
    async with db_factory() as session:
        try:
            request = await approvals.get_request(session, approval_id)
        except approvals.ApprovalNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval request not found") from None
        return await _to_out(session, request)


@router.post("/{approval_id}", response_model=ApprovalDecisionOut)
async def decide_approval(
    approval_id: str,
    request: ApprovalDecisionRequest,
    bus: Annotated[EventBus, Depends(get_bus)],
    db_factory: DbFactory,
    redis: Annotated[Redis, Depends(get_redis)],
    _role: Annotated[UserRole, Depends(require_role(UserRole.APPROVER))],
) -> ApprovalDecisionOut:
    """Approve or reject (Approver only, FR-131), then resume the paused project run.

    The decision is committed first; re-enqueuing the project lets the runner pick up the paused
    graph and resume it with this decision (``runner._resume_input``).
    """
    async with db_factory() as session:
        try:
            decision = await approvals.decide(
                session,
                approval_id,
                request.decision,
                approver_id=request.approver_id,
                comment=request.comment,
                bus=bus,
            )
        except approvals.ApprovalNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval request not found") from None
        except approvals.AlreadyDecided:
            raise HTTPException(status.HTTP_409_CONFLICT, "Already decided") from None
        project_id = (await approvals.get_request(session, approval_id)).project_id
        await session.commit()
        await bus.dispatch_pending(session)
    await runner.enqueue(project_id, redis)
    return ApprovalDecisionOut(**decision.model_dump())
