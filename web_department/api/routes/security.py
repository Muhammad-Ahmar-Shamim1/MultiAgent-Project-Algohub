"""Security exceptions (FR-113, FR-130): an Approver accepts one finding, on the audit log."""

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web_department.api.auth import UserRole, require_role
from web_department.api.deps import get_bus, get_db_factory
from web_department.api.schemas import SecurityExceptionOut, SecurityExceptionRequest
from web_department.collaboration.events import EventBus, EventType, emit
from web_department.db.repositories import ApprovalRepository, ProjectRepository
from web_department.security.scanners import fingerprint
from web_department.security.security_gate import EXCEPTION_ACTION

router = APIRouter(prefix="/api/v1/security", tags=["security"])


@router.post(
    "/exceptions", status_code=status.HTTP_201_CREATED, response_model=SecurityExceptionOut
)
async def create_security_exception(
    request: SecurityExceptionRequest,
    bus: Annotated[EventBus, Depends(get_bus)],
    db_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_db_factory)],
    _role: Annotated[UserRole, Depends(require_role(UserRole.APPROVER))],
) -> SecurityExceptionOut:
    """Accept one finding for one project so the security gate stops blocking on it.

    Approver-only. Recorded as an append-only ``Approval`` (``action_type="security_exception"``)
    and an APPROVED event carrying the justification - the audit trail (FR-113, DR-003).
    """
    async with db_factory() as session:
        if await ProjectRepository(session).get(request.project_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
        finding_id = fingerprint(request.tool, request.rule, request.file, request.line)
        decided_at = datetime.now(UTC)
        approval = await ApprovalRepository(session).create(
            project_id=request.project_id,
            action_type=EXCEPTION_ACTION,
            summary=json.dumps(
                {
                    "fingerprint": finding_id,
                    "tool": request.tool,
                    "rule": request.rule,
                    "file": request.file,
                    "line": request.line,
                }
            ),
            status="approved",
            approver_id=request.approver_id,
            comment=request.justification,
            decided_at=decided_at,
        )
        await emit(
            EventType.APPROVED,
            session=session,
            bus=bus,
            project_id=request.project_id,
            status="approved",
            payload={
                "action_type": EXCEPTION_ACTION,
                "approval_id": approval.id,
                "fingerprint": finding_id,
                "tool": request.tool,
                "rule": request.rule,
                "file": request.file,
                "line": request.line,
                "approver_id": request.approver_id,
                "justification": request.justification,
            },
        )
        await session.commit()
        await bus.dispatch_pending(session)
        return SecurityExceptionOut(
            id=approval.id,
            project_id=request.project_id,
            fingerprint=finding_id,
            approver_id=request.approver_id,
            justification=request.justification,
            decided_at=decided_at,
        )
