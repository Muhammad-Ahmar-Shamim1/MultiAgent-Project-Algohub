"""Run a project's independent tasks in parallel (FR-034..FR-037).

Replaces the sequential loop in ``orchestrator/graph.py``'s ``execute_tasks_node``.

``run()`` works in rounds:

1. ``_advance_dependency_states`` computes the READY set: a CREATED or DEPENDENCY_WAITING task
   becomes READY once every dependency is COMPLETED, otherwise DEPENDENCY_WAITING. A state is only
   written when it actually changes, so a task is never re-announced ("emit once").
2. Every READY task runs as its own asyncio task. Two limits apply: a Redis-backed semaphore
   (``Settings.max_parallel_tasks``, machine-wide - FR-034), and a budget check on the task's
   purpose. If the router's daily budget for that purpose is used up, the task waits and retries
   after a backoff instead of failing (FR-035).
3. Each task: picks an Agent (``task_router``), gets its own git branch ``task/<display_id>``, runs
   the worker, then merges its branch into ``BASE_BRANCH`` (the integrator step). Only the
   file-touching part of each attempt is serialized on the shared workspace (``_FsGuard``), so
   LLM calls of different tasks overlap while only one task's files are on disk at a time.
   A merge conflict moves the task to FIX_REQUIRED instead of COMPLETED.
4. Dependents of a task that did not complete simply never become READY - that is the "pause
   dependants" behaviour. ``on_blocked`` is the replan hook; it is called once per task that ends
   BLOCKED or conflicted. Its default is None (no replanner exists yet).

A FIX_REQUIRED task is re-run at most once per ``run()`` call, so a fix that keeps conflicting
cannot loop forever (AGENTS.md rule 8). The loop ends when a round finds nothing READY.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import cast

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web_department.agents.base import BaseWorker
from web_department.agents.registry import build as build_worker_registry
from web_department.blueprints.registry import BlueprintRegistry
from web_department.collaboration.events import EventBus, EventType, emit
from web_department.config.permissions import Role
from web_department.config.settings import get_settings
from web_department.db.models import Task, Workstation
from web_department.db.repositories import AgentRepository, TaskRepository, WorkstationRepository
from web_department.db.states import AgentState, TaskState
from web_department.llm.router import has_remaining_budget
from web_department.logging import get_logger
from web_department.orchestrator import task_router
from web_department.workstation import tools
from web_department.workstation.manager import ExecResult, WorkstationManager

log = get_logger(__name__)

BASE_BRANCH = "main"
SEMAPHORE_KEY = "sched:max_parallel_tasks"
SEMAPHORE_TTL_SECONDS = 3600
DEFAULT_BUDGET_BACKOFF_SECONDS = 30.0
GIT_TIMEOUT_SECONDS = 60.0

_DONE_STATES = frozenset({TaskState.COMPLETED, TaskState.CANCELLED})


def branch_name(task: Task) -> str:
    """The git branch a task's work lives on until it is merged."""
    return f"task/{task.display_id}"


class RedisSemaphore:
    """A soft, machine-wide concurrency cap backed by a Redis counter (FR-034).

    Not an ``asyncio.Semaphore``: the cap is per machine, and more than one runner process could
    share it. Each acquire attempt is one atomic Lua check-and-increment, so the counter can never
    go above the cap. The key has a TTL as a leak guard for a crashed holder.
    """

    _ACQUIRE_SCRIPT = """
    local current = tonumber(redis.call('GET', KEYS[1]) or '0')
    if current < tonumber(ARGV[1]) then
        redis.call('INCR', KEYS[1])
        redis.call('EXPIRE', KEYS[1], ARGV[2])
        return 1
    end
    return 0
    """

    def __init__(self, redis: Redis, key: str, max_count: int, *, poll_seconds: float = 0.1):
        self._redis = redis
        self._key = key
        self._max = max_count
        self._poll_seconds = poll_seconds

    async def acquire(self) -> None:
        while True:
            got = await cast(
                Awaitable[int],
                self._redis.eval(
                    self._ACQUIRE_SCRIPT, 1, self._key, str(self._max), str(SEMAPHORE_TTL_SECONDS)
                ),
            )
            if got:
                return
            await asyncio.sleep(self._poll_seconds)

    async def release(self) -> None:
        await self._redis.decr(self._key)

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.release()


@dataclass
class SchedulerResult:
    """What one ``run()`` call accomplished."""

    completed_task_ids: list[str] = field(default_factory=list)
    blocked_task_ids: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_task_ids)


@dataclass
class _Outcome:
    task_id: str
    completed: bool


@dataclass
class _Context:
    session_factory: async_sessionmaker[AsyncSession]
    registry: BlueprintRegistry
    project_id: str
    workers: dict[Role, type[BaseWorker]]
    semaphore: RedisSemaphore
    lock: asyncio.Lock
    bus: EventBus | None
    on_blocked: Callable[[Task], Awaitable[None]] | None
    budget_backoff_seconds: float


async def _get_workstation(session: AsyncSession, project_id: str) -> Workstation:
    existing = await WorkstationRepository(session).list(project_id=project_id)
    if not existing:
        raise LookupError(f"No workstation provisioned yet for project {project_id!r}")
    return existing[-1]


async def _advance_dependency_states(session: AsyncSession, project_id: str) -> list[Task]:
    """Promote CREATED/DEPENDENCY_WAITING tasks by their dependencies; return the READY ones."""
    repo = TaskRepository(session)
    tasks = await repo.list(project_id=project_id, limit=1000)
    ready: list[Task] = []
    for task in tasks:
        deps_done = all(dep.state is TaskState.COMPLETED for dep in task.depends_on)
        if task.state is TaskState.CREATED:
            task = await repo.update_state(
                task.id, TaskState.READY if deps_done else TaskState.DEPENDENCY_WAITING
            )
        elif task.state is TaskState.DEPENDENCY_WAITING and deps_done:
            task = await repo.update_state(task.id, TaskState.READY)
        if task.state is TaskState.READY:
            ready.append(task)
    return ready


class _FsGuard:
    """Serializes one attempt's file phase on the shared workspace, after checking out its branch.

    The branch is checked out again on every entry: the lock is released between a task's own
    attempts, so another task may have switched branches in the meantime.
    """

    def __init__(
        self,
        lock: asyncio.Lock,
        session: AsyncSession,
        ws: Workstation,
        role: Role,
        agent_id: str,
        manager: WorkstationManager,
        branch: str,
    ) -> None:
        self._lock = lock
        self._session = session
        self._ws = ws
        self._role = role
        self._agent_id = agent_id
        self._manager = manager
        self._branch = branch

    async def __aenter__(self) -> None:
        await self._lock.acquire()
        await _git(
            self._session,
            self._ws,
            self._role,
            self._agent_id,
            self._manager,
            ["git", "checkout", self._branch],
        )

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._lock.release()


async def _git(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str,
    manager: WorkstationManager,
    argv: list[str],
) -> ExecResult:
    return await tools.run_command(
        session, ws, role, agent_id, manager, argv, timeout=GIT_TIMEOUT_SECONDS
    )


async def _create_branch(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str,
    manager: WorkstationManager,
    branch: str,
    lock: asyncio.Lock,
) -> None:
    """Create ``branch`` off ``BASE_BRANCH`` and leave the workspace on it."""
    async with lock:
        await _git(session, ws, role, agent_id, manager, ["git", "checkout", BASE_BRANCH])
        await _git(session, ws, role, agent_id, manager, ["git", "checkout", "-b", branch])


async def _merge_branch(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str,
    manager: WorkstationManager,
    branch: str,
    lock: asyncio.Lock,
) -> bool:
    """Merge ``branch`` into ``BASE_BRANCH``. Returns True on conflict (merge already aborted)."""
    async with lock:
        await _git(session, ws, role, agent_id, manager, ["git", "checkout", BASE_BRANCH])
        merged = await _git(
            session,
            ws,
            role,
            agent_id,
            manager,
            ["git", "merge", "--no-ff", branch, "-m", f"merge {branch}"],
        )
        if merged.exit_code == 0:
            return False
        await _git(session, ws, role, agent_id, manager, ["git", "merge", "--abort"])
        return True


async def _run_one(ctx: _Context, task: Task) -> _Outcome:
    """Run one READY task through agent pick, branch, worker, merge. Each stage has own session."""
    role = Role(task.role)
    worker_class = ctx.workers[role]
    branch = branch_name(task)

    # Stage 1: agent + branch + WORKING state.
    async with ctx.session_factory() as session:
        agent = await task_router.choose_agent(
            session, ctx.registry, task, ctx.project_id, bus=ctx.bus
        )
        ws = await _get_workstation(session, ctx.project_id)
        manager = WorkstationManager(session)
        await _create_branch(session, ws, role, agent.id, manager, branch, ctx.lock)
        await AgentRepository(session, bus=ctx.bus).update_state(agent.id, AgentState.WORKING)
        task_repo = TaskRepository(session, bus=ctx.bus)
        if task.state is TaskState.READY:
            await task_repo.update_state(task.id, TaskState.ASSIGNED)
        elif task.state is TaskState.FIX_REQUIRED:
            await task_repo.update_state(task.id, TaskState.IN_PROGRESS)
        await session.commit()
        agent_id = agent.id

    # Stage 2: the worker itself (its own retry loop; each file phase goes through _FsGuard).
    async with ctx.session_factory() as session:
        worker_agent = await AgentRepository(session).get(agent_id)
        fresh = await TaskRepository(session).get(task.id)
        ws = await _get_workstation(session, ctx.project_id)
        manager = WorkstationManager(session)
        assert worker_agent is not None and fresh is not None
        worker = worker_class(
            session,
            worker_agent,
            {},
            ws,
            manager,
            bus=ctx.bus,
            fs_guard=lambda: _FsGuard(ctx.lock, session, ws, role, agent_id, manager, branch),
        )
        result = await worker.run(fresh)
        await AgentRepository(session, bus=ctx.bus).update_state(
            agent_id, AgentState.REVIEW if result.success else AgentState.BLOCKED
        )
        await session.commit()

    if not result.success:
        await _after_block(ctx, task.id)
        return _Outcome(task.id, completed=False)

    # Stage 3: integrate the branch, then settle task and agent.
    async with ctx.session_factory() as session:
        ws = await _get_workstation(session, ctx.project_id)
        manager = WorkstationManager(session)
        conflict = await _merge_branch(session, ws, role, agent_id, manager, branch, ctx.lock)
        task_repo = TaskRepository(session, bus=ctx.bus)
        if conflict:
            await task_repo.update_state(task.id, TaskState.FIX_REQUIRED)
            await emit(
                EventType.FIX_REQUIRED,
                session=session,
                bus=ctx.bus,
                project_id=ctx.project_id,
                task_id=task.id,
                payload={"reason": "merge_conflict", "branch": branch},
            )
        else:
            await task_repo.update_state(task.id, TaskState.COMPLETED)
        await AgentRepository(session, bus=ctx.bus).update_state(agent_id, AgentState.AVAILABLE)
        await session.commit()

    if conflict:
        await _after_block(ctx, task.id)
        return _Outcome(task.id, completed=False)
    return _Outcome(task.id, completed=True)


async def _after_block(ctx: _Context, task_id: str) -> None:
    if ctx.on_blocked is None:
        return
    async with ctx.session_factory() as session:
        blocked = await TaskRepository(session).get(task_id)
        assert blocked is not None
    await ctx.on_blocked(blocked)


async def _dispatch(ctx: _Context, task: Task) -> _Outcome:
    """Wait for budget (backoff, no failure), take a machine-wide slot, then run the task."""
    worker_class = ctx.workers[Role(task.role)]
    while not await has_remaining_budget(
        worker_class.purpose, confidential=worker_class.confidential
    ):
        log.info("scheduler_budget_backoff", task_id=task.id, purpose=worker_class.purpose)
        await asyncio.sleep(ctx.budget_backoff_seconds)
    async with ctx.semaphore:
        return await _run_one(ctx, task)


async def run(
    project_id: str,
    registry: BlueprintRegistry,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    *,
    bus: EventBus | None = None,
    on_blocked: Callable[[Task], Awaitable[None]] | None = None,
    max_parallel: int | None = None,
    budget_backoff_seconds: float = DEFAULT_BUDGET_BACKOFF_SECONDS,
) -> SchedulerResult:
    """Run every not-yet-finished task of ``project_id`` as far as its dependencies allow."""
    ctx = _Context(
        session_factory=session_factory,
        registry=registry,
        project_id=project_id,
        workers=build_worker_registry(registry),
        semaphore=RedisSemaphore(
            redis, SEMAPHORE_KEY, max_parallel or get_settings().max_parallel_tasks
        ),
        lock=asyncio.Lock(),
        bus=bus,
        on_blocked=on_blocked,
        budget_backoff_seconds=budget_backoff_seconds,
    )
    result = SchedulerResult()
    fix_attempted: set[str] = set()

    while True:
        async with session_factory() as session:
            ready = await _advance_dependency_states(session, project_id)
            all_tasks = await TaskRepository(session).list(project_id=project_id, limit=1000)
            await session.commit()

        # A FIX_REQUIRED task is re-run once; a fix that conflicts again stays FIX_REQUIRED.
        for task in all_tasks:
            if task.state is TaskState.FIX_REQUIRED and task.id not in fix_attempted:
                fix_attempted.add(task.id)
                ready.append(task)

        if not ready:
            unfinished = [t.id for t in all_tasks if t.state not in _DONE_STATES]
            result.blocked_task_ids = sorted(set(result.blocked_task_ids) | set(unfinished))
            return result

        outcomes = await asyncio.gather(*(_dispatch(ctx, t) for t in ready))
        for outcome in outcomes:
            if outcome.completed:
                result.completed_task_ids.append(outcome.task_id)
            else:
                result.blocked_task_ids.append(outcome.task_id)
