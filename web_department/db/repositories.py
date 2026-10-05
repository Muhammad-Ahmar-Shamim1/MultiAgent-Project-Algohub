"""Small async repositories, one per aggregate.

Repositories flush but never commit: the caller owns the transaction (``session_scope``).
Events and approvals are append-only, so their repositories expose no update path.
"""

import re
from enum import StrEnum
from typing import Any, ClassVar, Generic, TypeVar

from sqlalchemy import insert, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from web_department.collaboration.events import EventBus, EventType, emit
from web_department.db.base import Base
from web_department.db.models import (
    Agent,
    Approval,
    Blueprint,
    Deployment,
    Event,
    KnowledgeEntry,
    LLMCall,
    Project,
    RequirementSpec,
    Task,
    WarRoom,
    WarRoomMember,
    Workstation,
    task_dependencies,
)
from web_department.db.states import Stateful, transition

_TASK_NUMBER = re.compile(r"^WEB-(\d+)$")

ModelT = TypeVar("ModelT", bound=Base)
StatefulT = TypeVar("StatefulT", Project, Task, Agent)


class Repository(Generic[ModelT]):
    """Create / get / list for one model."""

    model: type[ModelT]
    load_options: ClassVar[tuple[Any, ...]] = ()

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **fields: Any) -> ModelT:
        """Insert a new row and flush so its generated id is available."""
        obj = self.model(**fields)
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def get(self, ident: object) -> ModelT | None:
        """Return the row with primary key ``ident`` or None."""
        return await self.session.get(self.model, ident, options=self.load_options)

    async def list(self, *, limit: int = 100, offset: int = 0, **filters: Any) -> list[ModelT]:
        """Return rows matching column ``filters``, oldest first."""
        primary_key = inspect(self.model).primary_key
        stmt = (
            select(self.model)
            .options(*self.load_options)
            .filter_by(**filters)
            .order_by(*primary_key)
            .limit(limit)
            .offset(offset)
        )
        return list((await self.session.scalars(stmt)).all())


class StatefulRepository(Repository[StatefulT]):
    """Adds validated state changes (with an automatic STATE_CHANGED event) for stateful models."""

    def __init__(self, session: AsyncSession, bus: EventBus | None = None) -> None:
        super().__init__(session)
        self._bus = bus

    def event_scope(self, obj: StatefulT) -> dict[str, str | None]:
        """Ids (``project_id`` and ``task_id``/``agent_id``) to stamp on ``obj``'s events."""
        raise NotImplementedError

    async def update_state(self, ident: object, new_state: StrEnum) -> StatefulT:
        """Lock the row, apply ``transition``, flush and emit STATE_CHANGED in the same transaction.

        Raises InvalidTransition if the change is not allowed. The stream entry is sent when the
        caller commits and dispatches (``session_scope`` does both).
        """
        obj = await self.session.get(self.model, ident, with_for_update=True)
        if obj is None:
            raise LookupError(f"{self.model.__name__} {ident!r} not found")
        previous = obj.state
        stateful: Stateful = obj
        transition(stateful, new_state)
        await self.session.flush()
        await emit(
            EventType.STATE_CHANGED,
            session=self.session,
            bus=self._bus,
            status=new_state.value.lower(),
            payload={
                "object": self.model.__name__.lower(),
                "id": obj.id,
                "from": previous.value,
                "to": new_state.value,
            },
            **self.event_scope(obj),
        )
        return obj


class ProjectRepository(StatefulRepository[Project]):
    """Projects."""

    model = Project

    def event_scope(self, obj: Project) -> dict[str, str | None]:
        """A project's events carry its own id as ``project_id``."""
        return {"project_id": obj.id}


class TaskRepository(StatefulRepository[Task]):
    """Tasks and their dependency edges."""

    model = Task
    load_options = (selectinload(Task.depends_on), selectinload(Task.dependents))

    async def create(self, **fields: Any) -> Task:
        """Insert a task with empty dependency collections so they can be read without a load."""
        fields.setdefault("depends_on", [])
        fields.setdefault("dependents", [])
        return await super().create(**fields)

    def event_scope(self, obj: Task) -> dict[str, str | None]:
        """A task's events carry its project and its own id."""
        return {"project_id": obj.project_id, "task_id": obj.id}

    async def add_dependency(self, task: Task, depends_on: Task) -> None:
        """Record that ``task`` waits for ``depends_on`` (same project only)."""
        if task.project_id != depends_on.project_id:
            raise ValueError("A task can only depend on a task in the same project")
        await self.session.execute(
            insert(task_dependencies).values(task_id=task.id, depends_on_id=depends_on.id)
        )
        await self.session.refresh(task, ["depends_on"])
        await self.session.refresh(depends_on, ["dependents"])

    async def set_retries(self, task_id: str, retries: int) -> Task:
        """Record how many failed attempts ``task_id`` has used so far (FR-012, metrics)."""
        obj = await self.session.get(Task, task_id, with_for_update=True)
        if obj is None:
            raise LookupError(f"Task {task_id!r} not found")
        obj.retries = retries
        await self.session.flush()
        return obj

    async def next_display_id(self, project_id: str) -> str:
        """The next free ``WEB-NNN`` in ``project_id`` (one past its highest)."""
        display_ids = (
            await self.session.scalars(select(Task.display_id).where(Task.project_id == project_id))
        ).all()
        numbers = [int(m[1]) for d in display_ids if (m := _TASK_NUMBER.match(d))]
        return f"WEB-{max(numbers, default=0) + 1:03d}"

    async def assign_agent(self, task_id: str, agent_id: str) -> Task:
        """Record which Agent is running ``task_id`` (task_router.py, FR-036)."""
        obj = await self.session.get(Task, task_id, with_for_update=True)
        if obj is None:
            raise LookupError(f"Task {task_id!r} not found")
        obj.assigned_agent_id = agent_id
        await self.session.flush()
        return obj


class AgentRepository(StatefulRepository[Agent]):
    """Workers."""

    model = Agent

    def event_scope(self, obj: Agent) -> dict[str, str | None]:
        """An agent's events carry its own id; ``project_id`` is None while it is in the pool."""
        return {"project_id": obj.project_id, "agent_id": obj.id}


class BlueprintRepository(Repository[Blueprint]):
    """Blueprints, keyed by (id, version)."""

    model = Blueprint

    async def get_version(self, blueprint_id: str, version: str) -> Blueprint | None:
        """Return one specific Blueprint version."""
        return await self.get((blueprint_id, version))

    async def upsert(self, *, id: str, version: str, yaml: str, checksum: str) -> Blueprint:  # noqa: A002
        """Insert (id, version) if new, or refresh its yaml/checksum if the file on disk changed.

        Used by blueprints/registry.py's startup sync - re-running it is always safe (FR-042: the
        YAML file is the source of truth, this just keeps the DB's cached copy honest).
        """
        existing = await self.get_version(id, version)
        if existing is None:
            return await self.create(id=id, version=version, yaml=yaml, checksum=checksum)
        if existing.checksum != checksum:
            existing.yaml = yaml
            existing.checksum = checksum
            await self.session.flush()
        return existing


class RequirementSpecRepository(Repository[RequirementSpec]):
    """Requirement specifications."""

    model = RequirementSpec

    async def latest_for_project(self, project_id: str) -> RequirementSpec | None:
        """Return the highest-versioned spec of a project."""
        stmt = (
            select(RequirementSpec)
            .where(RequirementSpec.project_id == project_id)
            .order_by(RequirementSpec.version.desc())
            .limit(1)
        )
        return (await self.session.scalars(stmt)).first()


class WorkstationRepository(Repository[Workstation]):
    """Workstations."""

    model = Workstation

    async def set_status(self, workstation_id: str, status: str) -> Workstation:
        """Update ``status`` (a free-text field, not a validated state machine)."""
        obj = await self.session.get(Workstation, workstation_id, with_for_update=True)
        if obj is None:
            raise LookupError(f"Workstation {workstation_id!r} not found")
        obj.status = status
        await self.session.flush()
        return obj


class EventRepository(Repository[Event]):
    """Append-only events: create, get and list only."""

    model = Event


class ApprovalRepository(Repository[Approval]):
    """Append-only approval records: create, get and list only."""

    model = Approval


class DeploymentRepository(Repository[Deployment]):
    """Deployments."""

    model = Deployment


class KnowledgeEntryRepository(Repository[KnowledgeEntry]):
    """Knowledge entries."""

    model = KnowledgeEntry


class LLMCallRepository(Repository[LLMCall]):
    """Traced LLM calls."""

    model = LLMCall


class WarRoomRepository(Repository[WarRoom]):
    """War rooms and their members (FR-080)."""

    model = WarRoom

    async def open_for_project(self, project_id: str) -> WarRoom | None:
        """The project's open war room, if any (at most one - a partial unique index)."""
        stmt = select(WarRoom).where(WarRoom.project_id == project_id, WarRoom.status == "open")
        return (await self.session.scalars(stmt)).first()

    async def latest_for_project(self, project_id: str) -> WarRoom | None:
        """The project's open war room, else its most recently created archived one."""
        stmt = (
            select(WarRoom)
            .where(WarRoom.project_id == project_id)
            .order_by((WarRoom.status == "open").desc(), WarRoom.id.desc())
            .limit(1)
        )
        return (await self.session.scalars(stmt)).first()

    async def add_member(self, war_room: WarRoom, agent: Agent, role: str) -> WarRoomMember:
        """Put ``agent`` on ``war_room``'s team as ``role``."""
        member = WarRoomMember(
            war_room_id=war_room.id, agent_id=agent.id, project_id=war_room.project_id, role=role
        )
        self.session.add(member)
        await self.session.flush()
        return member

    async def members(self, war_room_id: str) -> list[tuple[WarRoomMember, Agent]]:
        """Every member with its Agent row, in the order they joined."""
        stmt = (
            select(WarRoomMember, Agent)
            .join(Agent, Agent.id == WarRoomMember.agent_id)
            .where(WarRoomMember.war_room_id == war_room_id)
            .order_by(WarRoomMember.joined_at, Agent.display_id)
        )
        return [(member, agent) for member, agent in (await self.session.execute(stmt)).all()]
