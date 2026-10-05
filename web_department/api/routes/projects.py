"""Intake and project endpoints (FR-001..FR-005, SRS 8.2)."""

import asyncio
import json
import mimetypes
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web_department.api.auth import UserRole, require_role
from web_department.api.deps import get_bus, get_db_factory, get_redis
from web_department.api.schemas import (
    ClarificationAnswers,
    PlanOut,
    ProjectOut,
    ProjectRequest,
    ReportsOut,
    RequirementsOut,
    TaskOut,
    TimelineEntryOut,
    WarRoomArtifactOut,
    WarRoomEventOut,
    WarRoomMemberOut,
    WarRoomOut,
)
from web_department.collaboration import war_room
from web_department.collaboration.events import EventBus, EventType, emit
from web_department.db.models import Event, Project, Task
from web_department.db.repositories import (
    ProjectRepository,
    RequirementSpecRepository,
    TaskRepository,
)
from web_department.db.states import ProjectState
from web_department.orchestrator import orchestrator, runner
from web_department.orchestrator.planner import plan_to_mermaid
from web_department.orchestrator.requirement_analyzer import RequirementAnalyzer
from web_department.orchestrator.web_agent import NotWebScope, WebAgent
from web_department.qa.test_runner import REPORT_JSON_PATH as QA_REPORT_PATH
from web_department.security.security_gate import REPORT_JSON_PATH as SECURITY_REPORT_PATH

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

DbFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_db_factory)]
RedisDep = Annotated[Redis, Depends(get_redis)]
AnyRole = Annotated[
    UserRole, Depends(require_role(UserRole.REQUESTER, UserRole.APPROVER, UserRole.OPERATOR))
]


def _task_to_out(task: Task) -> TaskOut:
    return TaskOut(
        id=task.id,
        display_id=task.display_id,
        title=task.title,
        role=task.role,
        state=task.state,
        retries=task.retries,
        assigned_agent_id=task.assigned_agent_id,
        depends_on=[dep.id for dep in task.depends_on],
    )


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=ProjectOut)
async def submit_project(
    request: ProjectRequest,
    bus: Annotated[EventBus, Depends(get_bus)],
    db_factory: DbFactory,
    redis: RedisDep,
    _role: Annotated[UserRole, Depends(require_role(UserRole.REQUESTER, UserRole.OPERATOR))],
) -> ProjectOut:
    """Accept a project request (FR-001): classify scope, create the project, hand off."""
    async with db_factory() as session:
        try:
            project = await WebAgent(session, bus=bus).accept(request)
        except NotWebScope as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.reason) from None
        await session.commit()
        await bus.dispatch_pending(session)
        # Defensive: accept() only INSERTs today, but refresh so this stays correct if it ever
        # also UPDATEs the row (see the same refresh in submit_clarifications for why).
        await session.refresh(project)
        # Hand off to the orchestrator only after commit: it enqueues to Redis for runner.py to
        # pick up, and that read must never race this transaction's own commit (web_agent.py).
        await orchestrator.start(project.id, redis)
        return ProjectOut.model_validate(project)


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    db_factory: DbFactory,
    _role: AnyRole,
    state: ProjectState | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[ProjectOut]:
    """Projects, most recently updated first, optionally filtered by state."""
    async with db_factory() as session:
        stmt = select(Project)
        if state is not None:
            stmt = stmt.where(Project.state == state)
        rows = (await session.scalars(stmt.order_by(Project.updated_at.desc()).limit(limit))).all()
        return [ProjectOut.model_validate(p) for p in rows]


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: str, db_factory: DbFactory, _role: AnyRole) -> ProjectOut:
    """Return a project's current state."""
    async with db_factory() as session:
        project = await ProjectRepository(session).get(project_id)
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
        return ProjectOut.model_validate(project)


@router.post("/{project_id}/clarifications", response_model=ProjectOut)
async def submit_clarifications(
    project_id: str,
    body: ClarificationAnswers,
    bus: Annotated[EventBus, Depends(get_bus)],
    db_factory: DbFactory,
    redis: RedisDep,
    _role: Annotated[UserRole, Depends(require_role(UserRole.REQUESTER, UserRole.OPERATOR))],
) -> ProjectOut:
    """Answer the analyzer's open questions and re-run analysis (FR-022, SRS 8.2)."""
    async with db_factory() as session:
        project = await ProjectRepository(session).get(project_id)
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
        await RequirementAnalyzer(session, bus=bus).apply_clarifications(project, body.answers)
        await session.commit()
        await bus.dispatch_pending(session)
        # analyze() may UPDATE this row (state, wrs_version); refresh so onupdate-computed
        # columns like updated_at are loaded before the sync Pydantic validator reads them.
        await session.refresh(project)
        if project.state is not ProjectState.ANALYZING:
            # No more open questions: re-enqueue so the runner resumes the graph past "analyze"
            # (analyze_node is idempotent - it won't re-call the LLM or re-plan).
            await orchestrator.start(project.id, redis)
        return ProjectOut.model_validate(project)


@router.get("/{project_id}/tasks", response_model=list[TaskOut])
async def get_project_tasks(
    project_id: str, db_factory: DbFactory, _role: AnyRole
) -> list[TaskOut]:
    """Return a project's task graph and states."""
    async with db_factory() as session:
        if await ProjectRepository(session).get(project_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
        tasks = await TaskRepository(session).list(project_id=project_id)
        return [_task_to_out(t) for t in tasks]


@router.get("/{project_id}/war-room", response_model=WarRoomOut)
async def get_war_room(project_id: str, db_factory: DbFactory, _role: AnyRole) -> WarRoomOut:
    """Return the project's War Room: team, task board, latest events, artifacts (FR-080)."""
    async with db_factory() as session:
        if await ProjectRepository(session).get(project_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
        try:
            room = await war_room.view(session, project_id)
        except LookupError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project has no war room") from None
        return WarRoomOut(
            id=room.war_room.id,
            project_id=project_id,
            status=room.war_room.status,
            required_roles=room.war_room.required_roles,
            opened_at=room.war_room.created_at,
            closed_at=room.war_room.closed_at,
            members=[
                WarRoomMemberOut(
                    agent_id=m.agent_id, display_id=m.display_id, role=m.role, state=m.state
                )
                for m in room.members
            ],
            task_board={
                state: [_task_to_out(t) for t in tasks] for state, tasks in room.task_board.items()
            },
            events=[
                WarRoomEventOut(
                    event_id=e.id,
                    event=e.type,
                    task_id=e.task_id,
                    agent_id=e.agent_id,
                    status=e.status,
                    requires_review=e.requires_review,
                    payload=e.payload,
                    timestamp=e.timestamp,
                )
                for e in room.events
            ],
            # archived_path is a host filesystem path - deliberately not exposed over the API.
            artifacts=[
                WarRoomArtifactOut(
                    path=a.path, kind=a.kind, size_bytes=a.size_bytes, sha256=a.sha256
                )
                for a in room.artifacts
            ],
        )


async def _require_project(session: AsyncSession, project_id: str) -> Project:
    project = await ProjectRepository(session).get(project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return project


@router.get("/{project_id}/requirements", response_model=RequirementsOut)
async def get_requirements(
    project_id: str, db_factory: DbFactory, _role: AnyRole
) -> RequirementsOut:
    """The latest WRS and its open clarification questions (IR-002 shows them inline)."""
    async with db_factory() as session:
        await _require_project(session, project_id)
        spec = await RequirementSpecRepository(session).latest_for_project(project_id)
        if spec is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project has not been analysed yet")
        return RequirementsOut(
            version=spec.version,
            spec=spec.json,
            open_questions=[str(q) for q in spec.json.get("open_questions") or []],
        )


@router.get("/{project_id}/plan", response_model=PlanOut)
async def get_plan(project_id: str, db_factory: DbFactory, _role: AnyRole) -> PlanOut:
    """The task DAG as a Mermaid flowchart (FR-162)."""
    async with db_factory() as session:
        await _require_project(session, project_id)
        tasks = (
            await session.scalars(
                select(Task)
                .options(*TaskRepository.load_options)
                .where(Task.project_id == project_id)
                .order_by(Task.display_id)
            )
        ).all()
        return PlanOut(mermaid=plan_to_mermaid(list(tasks)))


@router.get("/{project_id}/timeline", response_model=list[TimelineEntryOut])
async def get_timeline(
    project_id: str, db_factory: DbFactory, _role: AnyRole
) -> list[TimelineEntryOut]:
    """Every state the project moved through, oldest first."""
    async with db_factory() as session:
        await _require_project(session, project_id)
        events = (
            await session.scalars(
                select(Event)
                .where(
                    Event.project_id == project_id,
                    Event.type == EventType.STATE_CHANGED.value,
                    Event.payload["object"].astext == "project",
                )
                .order_by(Event.timestamp, Event.id)
            )
        ).all()
        return [
            TimelineEntryOut(
                state=str(e.payload.get("to")),
                previous=e.payload.get("from"),
                timestamp=e.timestamp,
            )
            for e in events
        ]


@router.get("/{project_id}/reports", response_model=ReportsOut)
async def get_reports(project_id: str, db_factory: DbFactory, _role: AnyRole) -> ReportsOut:
    """The project's saved QA and security reports (None for one not produced yet)."""
    async with db_factory() as session:
        await _require_project(session, project_id)
        return ReportsOut(
            qa=await _json_artifact(session, project_id, QA_REPORT_PATH),
            security=await _json_artifact(session, project_id, SECURITY_REPORT_PATH),
        )


async def _json_artifact(
    session: AsyncSession, project_id: str, path: str
) -> dict[str, Any] | None:
    file = await war_room.artifact_file(session, project_id, path)
    if file is None:
        return None
    try:
        data = json.loads(await asyncio.to_thread(file.read_text, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@router.get("/{project_id}/artifacts/{path:path}")
async def get_artifact(
    project_id: str, path: str, db_factory: DbFactory, _role: AnyRole
) -> FileResponse:
    """One artifact file (a report, a screenshot...). Only War Room artifact paths are served."""
    async with db_factory() as session:
        await _require_project(session, project_id)
        file = await war_room.artifact_file(session, project_id, path)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact not found")
    media_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    if file.suffix == ".md":
        media_type = "text/markdown"
    return FileResponse(file, media_type=media_type, headers={"Cache-Control": "no-store"})


async def _existing_project(session: AsyncSession, project_id: str) -> None:
    if await ProjectRepository(session).get(project_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")


OperatorOnly = Annotated[UserRole, Depends(require_role(UserRole.OPERATOR))]


@router.post("/{project_id}/pause", response_model=ProjectOut)
async def pause_project(
    project_id: str, db_factory: DbFactory, redis: RedisDep, _role: OperatorOnly
) -> ProjectOut:
    """Ask the runner to stop advancing this project after its current node (SRS 8.2)."""
    async with db_factory() as session:
        await _existing_project(session, project_id)
        await runner.request_pause(project_id, redis)
        project = await ProjectRepository(session).get(project_id)
        assert project is not None
        return ProjectOut.model_validate(project)


@router.post("/{project_id}/resume", response_model=ProjectOut)
async def resume_project(
    project_id: str, db_factory: DbFactory, redis: RedisDep, _role: OperatorOnly
) -> ProjectOut:
    """Clear a pause/cancel flag and re-queue the project for the runner to pick up."""
    async with db_factory() as session:
        await _existing_project(session, project_id)
        await runner.clear_control(project_id, redis)
        await orchestrator.start(project_id, redis)
        project = await ProjectRepository(session).get(project_id)
        assert project is not None
        return ProjectOut.model_validate(project)


@router.post("/{project_id}/cancel", response_model=ProjectOut)
async def cancel_project(
    project_id: str,
    bus: Annotated[EventBus, Depends(get_bus)],
    db_factory: DbFactory,
    redis: RedisDep,
    _role: OperatorOnly,
) -> ProjectOut:
    """Stop the project and mark it FAILED (FAILED is reachable from every non-terminal state)."""
    async with db_factory() as session:
        project = await ProjectRepository(session).get(project_id)
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
        await runner.request_cancel(project_id, redis)
        if project.state not in (ProjectState.COMPLETED, ProjectState.FAILED):
            project = await ProjectRepository(session, bus=bus).update_state(
                project_id, ProjectState.FAILED
            )
        await emit(
            EventType.ALERT_RAISED,
            session=session,
            bus=bus,
            project_id=project_id,
            payload={"reason": "operator_cancel"},
        )
        await session.commit()
        await bus.dispatch_pending(session)
        await session.refresh(project)
        return ProjectOut.model_validate(project)
