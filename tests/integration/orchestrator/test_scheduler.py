"""Parallel scheduler (FR-034..FR-037): concurrency, budget backoff, merge conflict -> fix task.

Git (branch create/merge) and the LLM-driven worker run are faked; everything else - the DAG
state machine, Agent/Task/Workstation rows, the Redis semaphore - is real.
"""

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web_department.agents.base import BaseWorker, TaskResult
from web_department.blueprints.registry import BLUEPRINTS_DIR, BlueprintRegistry, load_and_sync
from web_department.config.permissions import Role
from web_department.db.models import Task
from web_department.db.repositories import AgentRepository, TaskRepository, WorkstationRepository
from web_department.db.states import AgentState, TaskState
from web_department.orchestrator import scheduler, task_router


@pytest.fixture
async def setup(
    session_factory: async_sessionmaker[AsyncSession],
    committed_project_id: str,
    tmp_path: Path,
) -> tuple[str, BlueprintRegistry]:
    async with session_factory() as s:
        registry = await load_and_sync(s, BLUEPRINTS_DIR)
        await WorkstationRepository(s).create(
            project_id=committed_project_id,
            container_id="fake-container",
            repo_path=str(tmp_path),
            limits={},
            status="running",
        )
        await s.commit()
    return committed_project_id, registry


async def _add_tasks(
    session_factory: async_sessionmaker[AsyncSession], project_id: str, spec: dict[str, list[str]]
) -> dict[str, str]:
    """Create tasks named by key; ``spec`` maps a task key to its dependency keys."""
    async with session_factory() as s:
        repo = TaskRepository(s)
        made: dict[str, Task] = {}
        for index, key in enumerate(spec, start=1):
            made[key] = await repo.create(
                display_id=f"WEB-{index:03d}",
                project_id=project_id,
                title=f"task {key}",
                role=Role.BACKEND.value,
            )
        for key, deps in spec.items():
            for dep in deps:
                await repo.add_dependency(made[key], made[dep])
        await s.commit()
        return {key: task.id for key, task in made.items()}


async def _task_states(
    session_factory: async_sessionmaker[AsyncSession], ids: dict[str, str]
) -> dict[str, TaskState]:
    async with session_factory() as s:
        return {
            key: (await TaskRepository(s).get(task_id)).state  # type: ignore[union-attr]
            for key, task_id in ids.items()
        }


def _fake_agent_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each task gets its own fresh, already-ASSIGNED Agent row (choose_agent's output)."""

    async def _choose(
        session: AsyncSession,
        registry: BlueprintRegistry,
        task: Task,
        project_id: str,
        *,
        bus: Any = None,
    ) -> Any:
        spec = registry.get(Role(task.role))
        return await AgentRepository(session).create(
            display_id=f"WEB-BE-{uuid.uuid4().hex[:8].upper()}",
            blueprint_id=spec.id,
            blueprint_version=spec.version,
            project_id=project_id,
            state=AgentState.ASSIGNED,
        )

    monkeypatch.setattr(task_router, "choose_agent", _choose)


def _fake_git(monkeypatch: pytest.MonkeyPatch, conflicts: dict[str, int]) -> list[str]:
    """Branch creation is a no-op; merging conflicts ``conflicts[branch]`` times, then succeeds."""
    merged: list[str] = []

    async def _create(*args: Any, **kwargs: Any) -> None:
        return None

    async def _merge(
        session: Any, ws: Any, role: Any, agent_id: Any, manager: Any, branch: str, lock: Any
    ) -> bool:
        merged.append(branch)
        if conflicts.get(branch, 0) > 0:
            conflicts[branch] -= 1
            return True
        return False

    monkeypatch.setattr(scheduler, "_create_branch", _create)
    monkeypatch.setattr(scheduler, "_merge_branch", _merge)
    return merged


def _fake_worker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    delay: float = 0.0,
    fail_keys: frozenset[str] = frozenset(),
    tracker: dict[str, int] | None = None,
) -> None:
    """Stands in for the LLM-driven BaseWorker.run: moves the task to REVIEW (or BLOCKED)."""
    active = {"now": 0}

    async def _fake_run(self: BaseWorker, task: Task) -> TaskResult:
        active["now"] += 1
        if tracker is not None:
            tracker["max"] = max(tracker.get("max", 0), active["now"])
        try:
            await asyncio.sleep(delay)
            repo = TaskRepository(self.session)
            if task.state is TaskState.ASSIGNED:
                await repo.update_state(task.id, TaskState.IN_PROGRESS)
            ok = task.title.removeprefix("task ") not in fail_keys
            await repo.update_state(task.id, TaskState.REVIEW if ok else TaskState.BLOCKED)
            return TaskResult(ok, [], ok, "fake", 1)
        finally:
            active["now"] -= 1

    monkeypatch.setattr(BaseWorker, "run", _fake_run)


async def test_diamond_dag_runs_the_two_middle_tasks_concurrently(
    monkeypatch: pytest.MonkeyPatch,
    setup: tuple[str, BlueprintRegistry],
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    project_id, registry = setup
    ids = await _add_tasks(
        session_factory, project_id, {"A": [], "B": ["A"], "C": ["A"], "D": ["B", "C"]}
    )
    tracker: dict[str, int] = {}
    _fake_worker(monkeypatch, delay=0.3, tracker=tracker)
    _fake_agent_factory(monkeypatch)
    _fake_git(monkeypatch, {})

    result = await scheduler.run(
        project_id, registry, session_factory, redis_client, max_parallel=2
    )

    assert tracker["max"] == 2  # B and C overlapped
    assert sorted(result.completed_task_ids) == sorted(ids.values())
    assert result.blocked_task_ids == []
    assert set((await _task_states(session_factory, ids)).values()) == {TaskState.COMPLETED}


async def test_dependents_of_a_blocked_task_never_start(
    monkeypatch: pytest.MonkeyPatch,
    setup: tuple[str, BlueprintRegistry],
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    project_id, registry = setup
    ids = await _add_tasks(session_factory, project_id, {"A": [], "B": ["A"]})
    _fake_worker(monkeypatch, fail_keys=frozenset({"A"}))
    _fake_agent_factory(monkeypatch)
    _fake_git(monkeypatch, {})

    result = await scheduler.run(project_id, registry, session_factory, redis_client)

    states = await _task_states(session_factory, ids)
    assert states["A"] is TaskState.BLOCKED
    assert states["B"] is TaskState.DEPENDENCY_WAITING
    assert result.blocked_task_ids == sorted([ids["A"], ids["B"]])


async def test_budget_exhaustion_delays_the_task_instead_of_failing_it(
    monkeypatch: pytest.MonkeyPatch,
    setup: tuple[str, BlueprintRegistry],
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    project_id, registry = setup
    ids = await _add_tasks(session_factory, project_id, {"A": []})
    calls = {"n": 0}

    async def _budget(purpose: Any, *, confidential: bool = False, **kwargs: Any) -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # exhausted for the first two checks

    monkeypatch.setattr(scheduler, "has_remaining_budget", _budget)
    _fake_worker(monkeypatch)
    _fake_agent_factory(monkeypatch)
    _fake_git(monkeypatch, {})

    result = await scheduler.run(
        project_id, registry, session_factory, redis_client, budget_backoff_seconds=0.01
    )

    assert calls["n"] >= 3
    assert result.completed_task_ids == [ids["A"]]
    assert (await _task_states(session_factory, ids))["A"] is TaskState.COMPLETED


async def test_merge_conflict_moves_the_task_to_fix_required_and_fix_is_retried(
    monkeypatch: pytest.MonkeyPatch,
    setup: tuple[str, BlueprintRegistry],
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    project_id, registry = setup
    ids = await _add_tasks(session_factory, project_id, {"A": [], "B": ["A"]})
    flagged: list[tuple[str, TaskState]] = []

    async def _on_blocked(task: Task) -> None:
        flagged.append((task.display_id, task.state))

    _fake_worker(monkeypatch)
    _fake_agent_factory(monkeypatch)
    merged = _fake_git(monkeypatch, {"task/WEB-001": 1})

    result = await scheduler.run(
        project_id, registry, session_factory, redis_client, on_blocked=_on_blocked
    )

    assert flagged == [("WEB-001", TaskState.FIX_REQUIRED)]
    assert merged.count("task/WEB-001") == 2  # conflict, then the fix run merged cleanly
    assert sorted(result.completed_task_ids) == sorted(ids.values())
    assert (await _task_states(session_factory, ids))["A"] is TaskState.COMPLETED


async def test_a_fix_that_still_conflicts_stays_fix_required_without_looping(
    monkeypatch: pytest.MonkeyPatch,
    setup: tuple[str, BlueprintRegistry],
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    project_id, registry = setup
    ids = await _add_tasks(session_factory, project_id, {"A": []})
    _fake_worker(monkeypatch)
    _fake_agent_factory(monkeypatch)
    merged = _fake_git(monkeypatch, {"task/WEB-001": 99})

    result = await scheduler.run(project_id, registry, session_factory, redis_client)

    assert merged.count("task/WEB-001") == 2  # original attempt + exactly one fix retry
    assert result.blocked_task_ids == [ids["A"]]
    assert (await _task_states(session_factory, ids))["A"] is TaskState.FIX_REQUIRED
