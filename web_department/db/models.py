"""ORM models for the core entities in SRS 7.1 (DR-001..DR-004).

Deviations from the literal SRS field lists, all forced by other requirements:
- ``Task.display_id`` / ``Agent.display_id`` hold the human IDs (``WEB-007``, ``WEB-FE-001``);
  every primary key is a prefixed ULID.
- ``LLMCall.project_id`` is added so every project-owned record carries ``project_id`` (DR-004).
- ``Event`` and ``Approval`` have no ``updated_at``: they are append-only (DR-003).
- ``Event`` also stores ``status`` and ``requires_review`` (SRS 7.3) so the audit row is lossless,
  and ``Event.project_id`` is nullable for pool-level events (e.g. an idle agent changing state).
"""

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    event,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Mapper, Session, mapped_column, relationship
from sqlalchemy.orm.session import ORMExecuteState

from web_department.db.base import Base, Entity, IdMixin, TimestampMixin
from web_department.db.states import AgentState, ProjectState, TaskState

EMBEDDING_DIM = 768


class AppendOnlyViolation(Exception):
    """Raised when code tries to UPDATE or DELETE a row of an append-only table."""


class AppendOnlyMixin:
    """Marks a table whose rows may only be inserted (DR-003)."""

    __append_only__ = True


def _state_type(enum_cls: type[Any]) -> Enum:
    return Enum(
        enum_cls,
        native_enum=False,
        length=32,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


def _project_fk(*, nullable: bool = False) -> Mapped[str]:
    return mapped_column(ForeignKey("projects.id"), index=True, nullable=nullable)


class Project(Entity):
    """A web project request moving through the department."""

    __tablename__ = "projects"
    id_prefix = "prj"

    title: Mapped[str] = mapped_column(String(300))
    requester_id: Mapped[str] = mapped_column(String(128))
    state: Mapped[ProjectState] = mapped_column(
        _state_type(ProjectState), default=ProjectState.RECEIVED
    )
    wrs_version: Mapped[int | None] = mapped_column(Integer)
    stack: Mapped[str] = mapped_column(String(64), default="golden-v1")


class RequirementSpec(Entity):
    """A versioned Web Requirements Specification (FR-020, FR-024)."""

    __tablename__ = "requirement_specs"
    __table_args__ = (UniqueConstraint("project_id", "version"),)
    id_prefix = "spc"

    project_id: Mapped[str] = _project_fk()
    version: Mapped[int] = mapped_column(Integer)
    json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(128))


class Blueprint(TimestampMixin, Base):
    """A versioned worker role definition; identified by (id, version), not a ULID (FR-042)."""

    __tablename__ = "blueprints"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    yaml: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))


class Workstation(Entity):
    """An isolated container + repo where a worker acts (FR-060)."""

    __tablename__ = "workstations"
    id_prefix = "wks"

    project_id: Mapped[str] = _project_fk()
    container_id: Mapped[str | None] = mapped_column(String(128))
    repo_path: Mapped[str] = mapped_column(String(500))
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(32))


class Agent(Entity):
    """A worker instantiated from a Blueprint; ``project_id`` is null while in the pool."""

    __tablename__ = "agents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["blueprint_id", "blueprint_version"], ["blueprints.id", "blueprints.version"]
        ),
    )
    id_prefix = "agt"

    display_id: Mapped[str] = mapped_column(String(32), unique=True)
    blueprint_id: Mapped[str] = mapped_column(String(100))
    blueprint_version: Mapped[str] = mapped_column(String(32))
    state: Mapped[AgentState] = mapped_column(_state_type(AgentState), default=AgentState.CREATED)
    project_id: Mapped[str | None] = _project_fk(nullable=True)
    workstation_id: Mapped[str | None] = mapped_column(ForeignKey("workstations.id"))


class WarRoom(Entity):
    """A project's collaboration space (FR-080): its team, task board, events and artifacts.

    Not in SRS 7.1's entity list - added for FR-080/FR-162. At most one ``open`` war room per
    project (partial unique index); a closed one is kept as ``archived`` with its artifact
    manifest, since the workstation that held the files is destroyed at project end.
    """

    __tablename__ = "war_rooms"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'archived')", name="status"),
        Index(
            "uq_war_rooms_one_open_per_project",
            "project_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )
    id_prefix = "wrm"

    project_id: Mapped[str] = _project_fk()
    status: Mapped[str] = mapped_column(String(16), default="open")
    required_roles: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # [{path, kind, size_bytes, sha256, archived_path}] - written by close(), empty while open.
    artifacts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WarRoomMember(Base):
    """One agent on a war room's team, with the role it was recruited for."""

    __tablename__ = "war_room_members"

    war_room_id: Mapped[str] = mapped_column(ForeignKey("war_rooms.id"), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    project_id: Mapped[str] = _project_fk()
    role: Mapped[str] = mapped_column(String(64))
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


task_dependencies = Table(
    "task_dependencies",
    Base.metadata,
    Column("task_id", ForeignKey("tasks.id"), primary_key=True),
    Column("depends_on_id", ForeignKey("tasks.id"), primary_key=True, index=True),
    CheckConstraint("task_id <> depends_on_id", name="no_self_dependency"),
)


class Task(Entity):
    """A unit of work in a project's plan DAG (FR-030)."""

    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("project_id", "display_id"),)
    id_prefix = "tsk"

    display_id: Mapped[str] = mapped_column(String(32))
    project_id: Mapped[str] = _project_fk()
    title: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(64))
    state: Mapped[TaskState] = mapped_column(_state_type(TaskState), default=TaskState.CREATED)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    acceptance_checks: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    assigned_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"), index=True)

    depends_on: Mapped[list["Task"]] = relationship(
        secondary=task_dependencies,
        primaryjoin=lambda: Task.id == task_dependencies.c.task_id,
        secondaryjoin=lambda: Task.id == task_dependencies.c.depends_on_id,
        back_populates="dependents",
        lazy="selectin",
    )
    dependents: Mapped[list["Task"]] = relationship(
        secondary=task_dependencies,
        primaryjoin=lambda: Task.id == task_dependencies.c.depends_on_id,
        secondaryjoin=lambda: Task.id == task_dependencies.c.task_id,
        back_populates="depends_on",
        lazy="selectin",
    )


class Event(AppendOnlyMixin, IdMixin, Base):
    """An immutable record of something that happened (SRS 7.3, DR-003)."""

    __tablename__ = "events"
    id_prefix = "evt"

    type: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str | None] = _project_fk(nullable=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"))
    status: Mapped[str | None] = mapped_column(String(32))
    requires_review: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Approval(AppendOnlyMixin, IdMixin, Base):
    """An immutable human-approval record (FR-130..FR-132, DR-003).

    Append-only, so a request and its decision are two rows: the request (``status="pending"``,
    with its ``evidence`` and an optional ``request_key``) and a later decision row
    (``status="approved"|"rejected"``) whose ``decides_id`` points back at it - at most one per
    request. ``request_key`` makes a graph node's request idempotent across LangGraph re-running
    the node on resume (``security/approvals.py``). Rows that are their own decision (a security
    exception granted directly, FR-113) have neither.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        Index(
            "uq_approvals_one_decision_per_request",
            "decides_id",
            unique=True,
            postgresql_where=text("decides_id IS NOT NULL"),
        ),
        Index(
            "uq_approvals_request_key_per_project",
            "project_id",
            "request_key",
            unique=True,
            postgresql_where=text("request_key IS NOT NULL"),
        ),
    )
    id_prefix = "apr"

    project_id: Mapped[str] = _project_fk()
    action_type: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    approver_id: Mapped[str | None] = mapped_column(String(128))
    comment: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decides_id: Mapped[str | None] = mapped_column(ForeignKey("approvals.id"))
    request_key: Mapped[str | None] = mapped_column(String(128))
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Deployment(Entity):
    """A deployment of an approved build (FR-141, FR-142)."""

    __tablename__ = "deployments"
    id_prefix = "dpl"

    project_id: Mapped[str] = _project_fk()
    version: Mapped[str] = mapped_column(String(64))
    commit_sha: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    health: Mapped[str | None] = mapped_column(String(32))
    approver_id: Mapped[str | None] = mapped_column(String(128))
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeEntry(Entity):
    """A versioned knowledge item in one of four layers (FR-070, FR-073).

    ``embedding`` is written as ``list[float]`` and read back as a numpy array.
    """

    __tablename__ = "knowledge_entries"
    id_prefix = "knw"

    layer: Mapped[str] = mapped_column(String(32))
    project_id: Mapped[str | None] = _project_fk(nullable=True)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    source: Mapped[str] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, default=1)


class LLMCall(Entity):
    """A traced LLM call (FR-153)."""

    __tablename__ = "llm_calls"
    id_prefix = "llm"

    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    project_id: Mapped[str | None] = _project_fk(nullable=True)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    tokens_in: Mapped[int] = mapped_column(Integer)
    tokens_out: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32))


def _reject_mutation(mapper: Mapper[Any], _connection: Any, target: Any) -> None:
    raise AppendOnlyViolation(f"{mapper.class_.__name__} rows are append-only (DR-003)")


for _model in (Event, Approval):
    event.listen(_model, "before_update", _reject_mutation)
    event.listen(_model, "before_delete", _reject_mutation)


@event.listens_for(Session, "do_orm_execute")
def _reject_bulk_mutation(state: ORMExecuteState) -> None:
    if not (state.is_update or state.is_delete):
        return
    for mapper in state.all_mappers:
        if getattr(mapper.class_, "__append_only__", False):
            raise AppendOnlyViolation(f"{mapper.class_.__name__} rows are append-only (DR-003)")
