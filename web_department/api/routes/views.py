"""Read-only views behind the dashboard (FR-160..FR-163): overview tiles and filterable tables.

Every route here is GET and open to any authenticated role; none changes state. Lists are newest
first and capped by ``limit`` (default 200, at most 1000).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from web_department.api.auth import UserRole, get_current_role, require_role
from web_department.api.deps import (
    BlueprintRegistryHolder,
    get_blueprint_registry_holder,
    get_db_factory,
)
from web_department.api.schemas import (
    AgentOut,
    AttentionItem,
    DeploymentOut,
    EventOut,
    KnowledgeOut,
    OverviewOut,
    TaskListItemOut,
    WhoAmIOut,
    WorkstationOut,
)
from web_department.db.models import (
    Agent,
    Approval,
    Deployment,
    Event,
    KnowledgeEntry,
    Project,
    Task,
    Workstation,
)
from web_department.db.repositories import TaskRepository
from web_department.db.states import AgentState, ProjectState, TaskState
from web_department.security.approvals import PENDING

router = APIRouter(prefix="/api/v1", tags=["dashboard"])

DbFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_db_factory)]
AnyRole = Annotated[
    UserRole, Depends(require_role(UserRole.REQUESTER, UserRole.APPROVER, UserRole.OPERATOR))
]
Limit = Annotated[int, Query(ge=1, le=1000)]

ALERT_EVENTS = ("ALERT_RAISED", "SECURITY_BLOCKED", "TEST_FAILED", "ROLLED_BACK")
_FINISHED_PROJECTS = (ProjectState.COMPLETED, ProjectState.FAILED)
_PENDING_TASKS = (TaskState.CREATED, TaskState.DEPENDENCY_WAITING, TaskState.READY)
_ACTIVE_TASKS = (TaskState.ASSIGNED, TaskState.IN_PROGRESS, TaskState.REVIEW)
_ATTENTION_LIMIT = 50


def event_out(event: Event) -> EventOut:
    """An ``Event`` row in the API's SRS 7.3 shape."""
    return EventOut(
        event_id=event.id,
        event=event.type,
        project_id=event.project_id,
        task_id=event.task_id,
        agent_id=event.agent_id,
        status=event.status,
        requires_review=event.requires_review,
        payload=event.payload,
        timestamp=event.timestamp,
    )


def pending_approvals_query() -> Any:
    """Approval *requests* with no decision row yet."""
    decision = aliased(Approval)
    return select(Approval).where(
        Approval.status == PENDING,
        Approval.decides_id.is_(None),
        ~exists().where(decision.decides_id == Approval.id),
    )


async def _counts_by(session: AsyncSession, stmt: Any) -> dict[Any, int]:
    """``{key: count}`` from a ``SELECT key, count(*) ... GROUP BY key``."""
    return {key: int(count) for key, count in (await session.execute(stmt)).all()}


async def _count(session: AsyncSession, stmt: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


@router.get("/whoami", response_model=WhoAmIOut)
async def whoami(role: Annotated[UserRole, Depends(get_current_role)]) -> WhoAmIOut:
    """The caller's role."""
    return WhoAmIOut(role=role.value)


@router.get("/overview", response_model=OverviewOut)
async def overview(db_factory: DbFactory, _role: AnyRole) -> OverviewOut:
    """Tile counts (FR-160) plus every blocked/failed/awaiting item (FR-163)."""
    async with db_factory() as session:
        agent_states: dict[AgentState, int] = await _counts_by(
            session,
            select(Agent.state, func.count())
            .where(Agent.state != AgentState.RETIRED)
            .group_by(Agent.state),
        )
        task_states: dict[TaskState, int] = await _counts_by(
            session, select(Task.state, func.count()).group_by(Task.state)
        )
        project_states: dict[ProjectState, int] = await _counts_by(
            session, select(Project.state, func.count()).group_by(Project.state)
        )
        pending_approvals = list(
            (
                await session.scalars(
                    pending_approvals_query().order_by(Approval.id.desc()).limit(_ATTENTION_LIMIT)
                )
            ).all()
        )
        approvals_pending = await _count(session, pending_approvals_query())
        attention_tasks = (
            await session.scalars(
                select(Task)
                .where(Task.state.in_((TaskState.BLOCKED, TaskState.FIX_REQUIRED)))
                .order_by(Task.updated_at.desc())
                .limit(_ATTENTION_LIMIT)
            )
        ).all()
        attention_projects = (
            await session.scalars(
                select(Project)
                .where(Project.state.in_((ProjectState.BLOCKED, ProjectState.FAILED)))
                .order_by(Project.updated_at.desc())
                .limit(_ATTENTION_LIMIT)
            )
        ).all()
        deployments = int(await session.scalar(select(func.count()).select_from(Deployment)) or 0)

    attention = [
        AttentionItem(
            kind="approval_pending",
            id=a.id,
            label=f"{a.action_type}: {a.summary}",
            project_id=a.project_id,
        )
        for a in pending_approvals
    ]
    attention += [
        AttentionItem(
            kind="task_blocked" if t.state is TaskState.BLOCKED else "task_failed",
            id=t.id,
            label=f"{t.display_id}: {t.title}",
            project_id=t.project_id,
        )
        for t in attention_tasks
    ]
    attention += [
        AttentionItem(
            kind=f"project_{p.state.value.lower()}",
            id=p.id,
            label=p.title,
            project_id=p.id,
        )
        for p in attention_projects
    ]
    return OverviewOut(
        agents_total=sum(agent_states.values()),
        agents_available=agent_states.get(AgentState.AVAILABLE, 0),
        agents_working=agent_states.get(AgentState.WORKING, 0),
        agents_blocked=agent_states.get(AgentState.BLOCKED, 0),
        projects_active=sum(
            n for state, n in project_states.items() if state not in _FINISHED_PROJECTS
        ),
        projects_blocked=project_states.get(ProjectState.BLOCKED, 0),
        tasks_pending=sum(task_states.get(s, 0) for s in _PENDING_TASKS),
        tasks_in_progress=sum(task_states.get(s, 0) for s in _ACTIVE_TASKS),
        tasks_failed=task_states.get(TaskState.FIX_REQUIRED, 0),
        tasks_blocked=task_states.get(TaskState.BLOCKED, 0),
        deployments=deployments,
        approvals_pending=approvals_pending,
        attention=attention,
    )


@router.get("/agents", response_model=list[AgentOut])
async def list_agents(
    db_factory: DbFactory,
    holder: Annotated[BlueprintRegistryHolder, Depends(get_blueprint_registry_holder)],
    _role: AnyRole,
    state: AgentState | None = None,
    project_id: str | None = None,
    role: str | None = None,
    limit: Limit = 200,
) -> list[AgentOut]:
    """Workers, filterable by state, project and role."""
    roles = {spec.id: spec.role.value for spec in holder.registry.list()}
    async with db_factory() as session:
        stmt = select(Agent)
        if state is not None:
            stmt = stmt.where(Agent.state == state)
        if project_id is not None:
            stmt = stmt.where(Agent.project_id == project_id)
        if role is not None:
            stmt = stmt.where(Agent.blueprint_id.in_([b for b, r in roles.items() if r == role]))
        rows = (await session.scalars(stmt.order_by(Agent.updated_at.desc()).limit(limit))).all()
    return [
        AgentOut(
            id=a.id,
            display_id=a.display_id,
            role=roles.get(a.blueprint_id),
            blueprint_id=a.blueprint_id,
            blueprint_version=a.blueprint_version,
            state=a.state,
            project_id=a.project_id,
            updated_at=a.updated_at,
        )
        for a in rows
    ]


@router.get("/tasks", response_model=list[TaskListItemOut])
async def list_tasks(
    db_factory: DbFactory,
    _role: AnyRole,
    state: TaskState | None = None,
    role: str | None = None,
    project_id: str | None = None,
    limit: Limit = 200,
) -> list[TaskListItemOut]:
    """Tasks across every project, filterable by state, role and project."""
    async with db_factory() as session:
        stmt = select(Task).options(*TaskRepository.load_options)
        if state is not None:
            stmt = stmt.where(Task.state == state)
        if role is not None:
            stmt = stmt.where(Task.role == role)
        if project_id is not None:
            stmt = stmt.where(Task.project_id == project_id)
        rows = (await session.scalars(stmt.order_by(Task.updated_at.desc()).limit(limit))).all()
        return [
            TaskListItemOut(
                id=t.id,
                display_id=t.display_id,
                title=t.title,
                role=t.role,
                state=t.state,
                retries=t.retries,
                assigned_agent_id=t.assigned_agent_id,
                depends_on=[d.id for d in t.depends_on],
                project_id=t.project_id,
                updated_at=t.updated_at,
            )
            for t in rows
        ]


@router.get("/workstations", response_model=list[WorkstationOut])
async def list_workstations(
    db_factory: DbFactory,
    _role: AnyRole,
    status: str | None = None,
    project_id: str | None = None,
    limit: Limit = 200,
) -> list[WorkstationOut]:
    """Workstations, filterable by status and project."""
    async with db_factory() as session:
        stmt = select(Workstation)
        if status is not None:
            stmt = stmt.where(Workstation.status == status)
        if project_id is not None:
            stmt = stmt.where(Workstation.project_id == project_id)
        rows = (
            await session.scalars(stmt.order_by(Workstation.created_at.desc()).limit(limit))
        ).all()
        return [WorkstationOut.model_validate(w) for w in rows]


@router.get("/knowledge", response_model=list[KnowledgeOut])
async def list_knowledge(
    db_factory: DbFactory,
    _role: AnyRole,
    layer: str | None = None,
    project_id: str | None = None,
    limit: Limit = 200,
) -> list[KnowledgeOut]:
    """Knowledge entries, filterable by layer and project (content shortened)."""
    async with db_factory() as session:
        stmt = select(KnowledgeEntry)
        if layer is not None:
            stmt = stmt.where(KnowledgeEntry.layer == layer)
        if project_id is not None:
            stmt = stmt.where(KnowledgeEntry.project_id == project_id)
        rows = (
            await session.scalars(stmt.order_by(KnowledgeEntry.updated_at.desc()).limit(limit))
        ).all()
        return [
            KnowledgeOut(
                id=k.id,
                layer=k.layer,
                project_id=k.project_id,
                source=k.source,
                version=k.version,
                excerpt=k.content[:300],
                updated_at=k.updated_at,
            )
            for k in rows
        ]


@router.get("/deployments", response_model=list[DeploymentOut])
async def list_deployments(
    db_factory: DbFactory,
    _role: AnyRole,
    status: str | None = None,
    project_id: str | None = None,
    limit: Limit = 200,
) -> list[DeploymentOut]:
    """Deployments, filterable by status and project."""
    async with db_factory() as session:
        stmt = select(Deployment)
        if status is not None:
            stmt = stmt.where(Deployment.status == status)
        if project_id is not None:
            stmt = stmt.where(Deployment.project_id == project_id)
        rows = (
            await session.scalars(stmt.order_by(Deployment.created_at.desc()).limit(limit))
        ).all()
        return [DeploymentOut.model_validate(d) for d in rows]


@router.get("/monitoring/alerts", response_model=list[EventOut])
async def list_alerts(
    db_factory: DbFactory,
    _role: AnyRole,
    project_id: str | None = None,
    event: Annotated[str | None, Query(description=f"one of {', '.join(ALERT_EVENTS)}")] = None,
    limit: Limit = 200,
) -> list[EventOut]:
    """Alert-type events (alerts, security blocks, failed tests, rollbacks), newest first."""
    types = (event,) if event in ALERT_EVENTS else ALERT_EVENTS
    async with db_factory() as session:
        stmt = select(Event).where(Event.type.in_(types))
        if project_id is not None:
            stmt = stmt.where(Event.project_id == project_id)
        rows = (await session.scalars(stmt.order_by(Event.timestamp.desc()).limit(limit))).all()
        return [event_out(e) for e in rows]
