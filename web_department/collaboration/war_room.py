"""The War Room (FR-080, FR-081): a project's team, task board, event feed and artifacts.

- ``open()`` forms the team: one Agent per role the plan needs (``required_roles``), created
  through the Agent Factory, and records it as a ``WarRoom`` with its members.
- ``view()`` is what the dashboard/API shows (FR-162): members, the task board grouped by
  state, the latest events and the artifacts.
- ``request_from()`` is the only way one role asks another for something (FR-081): it creates a
  task for that role - optionally a dependency of the asking task - and emits REVIEW_REQUIRED.
  There is no free-form chat channel; everything a worker says to another is a task record or an
  event, both of which are audited.
- ``close()`` retires the team, copies the artifacts out of the workspace into
  ``Settings.war_room_archive_dir`` (``WorkstationManager.destroy()`` deletes the workspace) and
  archives the room, keeping its artifact manifest.

Design notes:

- Members are created AVAILABLE (``agent_factory.create``), not ASSIGNED via
  ``agent_factory.acquire``: the Task Router (``task_router.choose_agent`` -> ``acquire``) only
  reuses AVAILABLE agents, so a team recruited as ASSIGNED would sit idle while the router made
  a second set of agents beside it. A role that already has a live agent in the project reuses
  it rather than adding another.
- "Release" at close means retire (``agent_factory.retire``): agents are scoped to one project
  (FR-050), so one returned to AVAILABLE after its project ended could never be picked again.
  A team member still WORKING blocks close (``WarRoomBusy``) - its task must finish or block
  first, the same rule ``agent_factory.retire`` applies.
- The platform reads the workspace's artifact files directly (as ``graph.copy_template_node``
  does), not through ``workstation/tools.py``: that layer enforces *worker* permissions
  (AGENTS.md rule 7), and the War Room is platform code, not a worker. Only files inside the
  workspace are read - a symlink pointing outside it is skipped.
"""

import asyncio
import hashlib
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.blueprints.registry import BlueprintRegistry
from web_department.collaboration.events import EventBus, EventType, emit
from web_department.config.permissions import Role
from web_department.config.settings import get_settings
from web_department.db.models import Agent, Event, Task, WarRoom, WarRoomMember
from web_department.db.repositories import (
    AgentRepository,
    RequirementSpecRepository,
    TaskRepository,
    WarRoomRepository,
    WorkstationRepository,
)
from web_department.db.states import AgentState, TaskState
from web_department.logging import get_logger
from web_department.orchestrator import agent_factory
from web_department.orchestrator.wrs_schema import WebRequirementsSpec

log = get_logger(__name__)

# (kind, glob relative to the workspace root). Screenshots follow qa/checks.py's
# screenshot_attached convention; reports/ is where QA/Security reports are expected to land.
ARTIFACT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("architecture", "docs/architecture.md"),
    ("openapi", "openapi.yaml"),
    ("schema", "db/schema.md"),
    ("screenshot", "qa/screenshots/*.png"),
    ("report", "reports/**/*"),
    ("report", "artifacts/qa-report.*"),
    ("report", "artifacts/security-report.*"),
)

# Substrings of an entity field name that mark it as sensitive data (FR-080: "security if
# auth/sensitive data").
_SENSITIVE_FIELD_MARKERS = (
    "password",
    "token",
    "secret",
    "card",
    "payment",
    "iban",
    "ssn",
    "email",
    "phone",
    "address",
    "birth",
)
_AUTH_WORDS = re.compile(r"\b(auth\w*|log ?in|sign ?(in|up)|register\w*|password)\b", re.I)


class NoPlan(Exception):
    """Raised by ``open()`` when the project has no tasks yet - the team is formed from the plan."""


class WarRoomNotOpen(Exception):
    """Raised when an operation needs the project's open War Room and there is none."""


class WarRoomBusy(Exception):
    """Raised by ``close()`` while a team member is still WORKING."""


@dataclass(frozen=True)
class Member:
    """One team member as the War Room shows it."""

    agent_id: str
    display_id: str
    role: str
    state: AgentState


@dataclass(frozen=True)
class Artifact:
    """One artifact file; ``archived_path``/``sha256`` are set once the room is closed."""

    path: str
    kind: str
    size_bytes: int
    sha256: str | None = None
    archived_path: str | None = None


@dataclass(frozen=True)
class WarRoomView:
    """Everything the War Room exposes (FR-080, FR-162)."""

    war_room: WarRoom
    members: list[Member]
    task_board: dict[TaskState, list[Task]]
    events: list[Event]
    artifacts: list[Artifact]


def needs_security(wrs: WebRequirementsSpec) -> bool:
    """The project has authentication or holds sensitive data (or says it is high-security)."""
    if wrs.security_level == "high":
        return True
    if any(page.roles for page in wrs.pages):
        return True
    if any(_AUTH_WORDS.search(f"{f.name} {f.description}") for f in wrs.features):
        return True
    return any(
        marker in field.name.lower()
        for entity in wrs.entities
        for field in entity.fields
        for marker in _SENSITIVE_FIELD_MARKERS
    )


def required_roles(task_roles: Iterable[str], wrs: WebRequirementsSpec | None) -> list[Role]:
    """The team a plan needs: every role a task is assigned to, plus Security if the project has
    auth or sensitive data and DevOps if it deploys anywhere - in ``Role`` declaration order.
    """
    roles = {Role(role) for role in task_roles}
    if wrs is not None:
        if needs_security(wrs):
            roles.add(Role.SECURITY)
        if wrs.deployment_target:
            roles.add(Role.DEVOPS)
    return [role for role in Role if role in roles]


async def open(
    session: AsyncSession,
    registry: BlueprintRegistry,
    project_id: str,
    *,
    bus: EventBus | None = None,
) -> WarRoom:
    """Form the project's team from its plan and open its War Room (FR-080).

    Idempotent: returns the already-open room unchanged. Raises ``NoPlan`` if the project has
    no tasks yet, and ``agent_factory.NoWorkstation`` if it has no workstation to attach agents to.
    """
    repo = WarRoomRepository(session)
    existing = await repo.open_for_project(project_id)
    if existing is not None:
        return existing

    task_roles = (
        await session.scalars(select(Task.role).where(Task.project_id == project_id).distinct())
    ).all()
    if not task_roles:
        raise NoPlan(f"project {project_id!r} has no plan yet")
    spec = await RequirementSpecRepository(session).latest_for_project(project_id)
    wrs = WebRequirementsSpec.model_validate(spec.json) if spec is not None else None
    roles = required_roles(task_roles, wrs)

    war_room = await repo.create(
        project_id=project_id, status="open", required_roles=[r.value for r in roles], artifacts=[]
    )
    display_ids: list[str] = []
    for role in roles:
        agent = await _recruit(session, registry, project_id, role, bus=bus)
        await repo.add_member(war_room, agent, role.value)
        display_ids.append(agent.display_id)

    await emit(
        EventType.WAR_ROOM_OPENED,
        session=session,
        bus=bus,
        project_id=project_id,
        payload={
            "war_room_id": war_room.id,
            "required_roles": war_room.required_roles,
            "members": display_ids,
        },
    )
    log.info("war_room_opened", project_id=project_id, roles=war_room.required_roles)
    return war_room


async def _recruit(
    session: AsyncSession,
    registry: BlueprintRegistry,
    project_id: str,
    role: Role,
    *,
    bus: EventBus | None,
) -> Agent:
    blueprint_id = registry.get(role).id
    for agent in await AgentRepository(session).list(project_id=project_id, limit=1000):
        if agent.blueprint_id == blueprint_id and agent.state is not AgentState.RETIRED:
            return agent
    return await agent_factory.create(session, registry, role, project_id, bus=bus)


async def _members_in_role_order(
    session: AsyncSession, war_room: WarRoom
) -> list[tuple[WarRoomMember, Agent]]:
    # Members recruited in one transaction share joined_at (Postgres now() is per-transaction),
    # so order by the room's own role list - the order open()/request_from() recruited them in.
    position = {role: index for index, role in enumerate(war_room.required_roles)}
    members = await WarRoomRepository(session).members(war_room.id)
    return sorted(members, key=lambda pair: position.get(pair[0].role, len(position)))


async def view(session: AsyncSession, project_id: str, *, event_limit: int = 50) -> WarRoomView:
    """The project's open War Room (else its latest archived one), ready to display.

    Raises ``LookupError`` if the project has never had one.
    """
    war_room = await WarRoomRepository(session).latest_for_project(project_id)
    if war_room is None:
        raise LookupError(f"project {project_id!r} has no war room")

    members = [
        Member(agent_id=agent.id, display_id=agent.display_id, role=member.role, state=agent.state)
        for member, agent in await _members_in_role_order(session, war_room)
    ]

    tasks = (
        await session.scalars(
            select(Task)
            .options(*TaskRepository.load_options)
            .where(Task.project_id == project_id)
            .order_by(Task.display_id)
        )
    ).all()
    task_board: dict[TaskState, list[Task]] = {state: [] for state in TaskState}
    for task in tasks:
        task_board[task.state].append(task)

    events = list(
        (
            await session.scalars(
                select(Event)
                .where(Event.project_id == project_id)
                .order_by(Event.timestamp.desc(), Event.id.desc())
                .limit(event_limit)
            )
        ).all()
    )

    if war_room.status == "open":
        artifacts = await _scan_artifacts(session, project_id)
    else:
        artifacts = [Artifact(**item) for item in war_room.artifacts]
    return WarRoomView(war_room, members, task_board, events, artifacts)


async def request_from(
    session: AsyncSession,
    registry: BlueprintRegistry,
    project_id: str,
    requester_task_id: str,
    role: Role,
    title: str,
    *,
    acceptance_checks: list[str],
    inputs: dict[str, Any] | None = None,
    blocking: bool = True,
    bus: EventBus | None = None,
) -> Task:
    """Ask ``role`` for something, the only way workers talk to each other (FR-081).

    Creates a READY task for ``role`` (recruiting a member for it if the team has none), makes
    the requesting task depend on it when ``blocking``, and emits REVIEW_REQUIRED naming both.
    """
    war_room = await WarRoomRepository(session).open_for_project(project_id)
    if war_room is None:
        raise WarRoomNotOpen(f"project {project_id!r} has no open war room")
    task_repo = TaskRepository(session, bus=bus)
    requester = await task_repo.get(requester_task_id)
    if requester is None or requester.project_id != project_id:
        raise LookupError(f"task {requester_task_id!r} not found in project {project_id!r}")

    if role.value not in war_room.required_roles:
        agent = await _recruit(session, registry, project_id, role, bus=bus)
        await WarRoomRepository(session).add_member(war_room, agent, role.value)
        war_room.required_roles = [*war_room.required_roles, role.value]
        await session.flush()

    task = await task_repo.create(
        display_id=await task_repo.next_display_id(project_id),
        project_id=project_id,
        title=title,
        role=role.value,
        inputs={**(inputs or {}), "requested_by": requester.display_id},
        outputs={},
        acceptance_checks=acceptance_checks,
    )
    await emit(
        EventType.TASK_CREATED,
        session=session,
        bus=bus,
        project_id=project_id,
        task_id=task.id,
        payload={"display_id": task.display_id, "title": task.title, "role": task.role},
    )
    task = await task_repo.update_state(task.id, TaskState.READY)
    if blocking:
        await task_repo.add_dependency(requester, task)

    await emit(
        EventType.REVIEW_REQUIRED,
        session=session,
        bus=bus,
        project_id=project_id,
        task_id=task.id,
        requires_review=True,
        payload={
            "requested_role": role.value,
            "requested_by_task": requester.display_id,
            "request_task": task.display_id,
            "title": title,
            "blocking": blocking,
        },
    )
    return task


async def close(session: AsyncSession, project_id: str, *, bus: EventBus | None = None) -> WarRoom:
    """Retire the team, archive the artifacts and the room (FR-080, FR-052).

    Raises ``WarRoomNotOpen`` if there is no open room, ``WarRoomBusy`` (changing nothing) if a
    member is still WORKING.
    """
    repo = WarRoomRepository(session)
    war_room = await repo.open_for_project(project_id)
    if war_room is None:
        raise WarRoomNotOpen(f"project {project_id!r} has no open war room")

    agents = [agent for _member, agent in await _members_in_role_order(session, war_room)]
    busy = [a.display_id for a in agents if a.state is AgentState.WORKING]
    if busy:
        raise WarRoomBusy(f"agents still working: {busy}")

    retired: list[str] = []
    for agent in agents:
        if agent.state is AgentState.RETIRED:
            continue
        if agent.state is AgentState.REVIEW:
            agent = await agent_factory.release(session, agent, bus=bus)
        await agent_factory.retire(session, agent, bus=bus)
        retired.append(agent.display_id)

    archive_dir = Path(get_settings().war_room_archive_dir) / project_id / war_room.id
    artifacts = await _archive_artifacts(session, project_id, archive_dir)

    war_room.status = "archived"
    war_room.closed_at = datetime.now(UTC)
    war_room.artifacts = [vars(a) for a in artifacts]
    await session.flush()

    await emit(
        EventType.WAR_ROOM_CLOSED,
        session=session,
        bus=bus,
        project_id=project_id,
        payload={
            "war_room_id": war_room.id,
            "retired": retired,
            "artifacts": [a.path for a in artifacts],
        },
    )
    log.info("war_room_closed", project_id=project_id, retired=len(retired))
    return war_room


async def artifact_file(session: AsyncSession, project_id: str, path: str) -> Path | None:
    """The file behind artifact ``path`` of ``project_id``, or None if it is not one.

    Only paths that are artifacts (``ARTIFACT_PATTERNS``) resolve - never an arbitrary workspace
    file. An archived room serves its archived copy (the workspace may be gone); otherwise the
    file is looked up in the live workspace.
    """
    room = await WarRoomRepository(session).latest_for_project(project_id)
    if room is not None and room.status == "archived":
        for item in room.artifacts:
            if item["path"] == path and item.get("archived_path"):
                archived = Path(item["archived_path"])
                return archived if archived.is_file() else None
        return None
    root = await _workspace_root(session, project_id)
    if root is None:
        return None
    for _kind, found in await asyncio.to_thread(_find_artifacts, root):
        if found.relative_to(root).as_posix() == path:
            return found
    return None


async def _workspace_root(session: AsyncSession, project_id: str) -> Path | None:
    workstations = await WorkstationRepository(session).list(project_id=project_id)
    return Path(workstations[-1].repo_path) if workstations else None


def _find_artifacts(root: Path) -> list[tuple[str, Path]]:
    if not root.is_dir():
        return []
    resolved_root = root.resolve()
    found: dict[Path, str] = {}
    for kind, pattern in ARTIFACT_PATTERNS:
        for path in sorted(root.glob(pattern)):
            if path.is_file() and path.resolve().is_relative_to(resolved_root):
                found.setdefault(path, kind)
    return [(kind, path) for path, kind in found.items()]


async def _scan_artifacts(session: AsyncSession, project_id: str) -> list[Artifact]:
    root = await _workspace_root(session, project_id)
    if root is None:
        return []

    def scan() -> list[Artifact]:
        return [
            Artifact(
                path=path.relative_to(root).as_posix(), kind=kind, size_bytes=path.stat().st_size
            )
            for kind, path in _find_artifacts(root)
        ]

    return await asyncio.to_thread(scan)


async def _archive_artifacts(
    session: AsyncSession, project_id: str, archive_dir: Path
) -> list[Artifact]:
    root = await _workspace_root(session, project_id)
    if root is None:
        return []

    def copy_all() -> list[Artifact]:
        archived: list[Artifact] = []
        for kind, path in _find_artifacts(root):
            relative = path.relative_to(root).as_posix()
            target = archive_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            archived.append(
                Artifact(
                    path=relative,
                    kind=kind,
                    size_bytes=target.stat().st_size,
                    sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                    archived_path=str(target),
                )
            )
        return archived

    return await asyncio.to_thread(copy_all)
