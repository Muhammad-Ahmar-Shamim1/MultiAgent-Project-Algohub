"""WorkstationManager: isolation guarantees, exec/truncate/timeout, teardown (FR-060..065)."""

import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from docker.errors import NotFound
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus
from web_department.config.settings import get_settings
from web_department.db.models import Project
from web_department.workstation.manager import (
    EXEC_OUTPUT_LIMIT,
    WORKSPACE_MOUNT,
    ExecResult,
    WorkstationManager,
    WorkstationTimeout,
)


def _fake_client() -> MagicMock:
    client = MagicMock()
    client.containers.run.side_effect = [
        MagicMock(id="db-container-id"),
        MagicMock(id="main-container-id"),
    ]
    return client


@pytest.fixture
def fake_client() -> MagicMock:
    return _fake_client()


@pytest.fixture(autouse=True)
def _isolated_workspace_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKSTATION_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()


async def test_create_provisions_an_isolated_network_and_container(
    session: AsyncSession, bus: EventBus, project: Project, fake_client: MagicMock
) -> None:
    manager = WorkstationManager(session, bus=bus, client=fake_client)

    ws = await manager.create(project.id)

    fake_client.networks.create.assert_called_once()
    network_args, network_kwargs = fake_client.networks.create.call_args
    assert network_args[0] == f"algohub-ws-{project.id}"

    assert fake_client.containers.run.call_count == 2
    main_call = fake_client.containers.run.call_args_list[1]
    main_kwargs = main_call.kwargs

    # Exactly one bind mount, at /workspace, and nothing else - in particular no docker.sock.
    assert list(main_kwargs["volumes"].values()) == [{"bind": WORKSPACE_MOUNT, "mode": "rw"}]
    assert not any("docker.sock" in str(v) for v in main_kwargs["volumes"])
    assert main_kwargs["user"] == "agent"
    assert main_kwargs["cap_drop"] == ["ALL"]
    assert "privileged" not in main_kwargs
    assert main_kwargs["mem_limit"] == get_settings().workstation_mem
    assert main_kwargs["nano_cpus"] == int(get_settings().workstation_cpu * 1_000_000_000)

    assert ws.container_id == "main-container-id"
    assert ws.status == "running"
    assert ws.limits["db_container_id"] == "db-container-id"
    assert ws.limits["network"] == f"algohub-ws-{project.id}"


async def test_create_persists_the_workstation_row(
    session: AsyncSession, bus: EventBus, project: Project, fake_client: MagicMock
) -> None:
    manager = WorkstationManager(session, bus=bus, client=fake_client)

    ws = await manager.create(project.id)

    assert ws.project_id == project.id
    assert ws.repo_path.endswith(project.id)


async def test_create_tears_down_partial_resources_on_failure(
    session: AsyncSession, bus: EventBus, project: Project
) -> None:
    client = _fake_client()
    client.containers.run.side_effect = [MagicMock(id="db-container-id"), RuntimeError("boom")]
    manager = WorkstationManager(session, bus=bus, client=client)

    with pytest.raises(RuntimeError, match="boom"):
        await manager.create(project.id)

    removed_names = {c.args[0] for c in client.containers.get.call_args_list}
    assert removed_names == {f"algohub-ws-{project.id}", f"algohub-ws-{project.id}-db"}
    client.networks.get.assert_called_once_with(f"algohub-ws-{project.id}")


async def test_exec_returns_exit_code_stdout_and_stderr_separately(
    session: AsyncSession, bus: EventBus, project: Project, fake_client: MagicMock
) -> None:
    manager = WorkstationManager(session, bus=bus, client=fake_client)
    ws = await manager.create(project.id)
    fake_client.api.exec_create.return_value = {"Id": "exec-1"}
    fake_client.api.exec_start.return_value = (b"hello\n", b"warning\n")
    fake_client.api.exec_inspect.return_value = {"ExitCode": 0}

    result = await manager.exec(ws, ["node", "-v"])

    fake_client.api.exec_create.assert_called_once()
    create_args, create_kwargs = fake_client.api.exec_create.call_args
    assert create_args[0] == "main-container-id"
    assert create_args[1] == ["node", "-v"]
    assert create_kwargs["user"] == "agent"
    assert create_kwargs["workdir"] == WORKSPACE_MOUNT
    assert result == ExecResult(0, "hello\n", "warning\n", False, False)


async def test_exec_truncates_output_over_the_limit(
    session: AsyncSession, bus: EventBus, project: Project, fake_client: MagicMock
) -> None:
    manager = WorkstationManager(session, bus=bus, client=fake_client)
    ws = await manager.create(project.id)
    big = b"x" * (EXEC_OUTPUT_LIMIT + 500)
    fake_client.api.exec_create.return_value = {"Id": "exec-1"}
    fake_client.api.exec_start.return_value = (big, b"")
    fake_client.api.exec_inspect.return_value = {"ExitCode": 0}

    result = await manager.exec(ws, ["cat", "bigfile"])

    assert len(result.stdout) == EXEC_OUTPUT_LIMIT
    assert result.stdout_truncated is True
    assert result.stderr_truncated is False


async def test_exec_raises_workstation_timeout_for_a_hanging_command(
    session: AsyncSession, bus: EventBus, project: Project, fake_client: MagicMock
) -> None:
    manager = WorkstationManager(session, bus=bus, client=fake_client)
    ws = await manager.create(project.id)

    def _hang(*_args: Any, **_kwargs: Any) -> Any:
        time.sleep(5)
        return {"Id": "exec-1"}

    fake_client.api.exec_create.side_effect = _hang

    start = time.monotonic()
    with pytest.raises(WorkstationTimeout, match="timed out"):
        await manager.exec(ws, ["sleep", "5"], timeout=0.2)
    elapsed = time.monotonic() - start

    assert elapsed < 2  # the caller is unblocked promptly even though the thread keeps running


async def test_exec_on_a_workstation_without_a_container_id_raises(
    session: AsyncSession, bus: EventBus, project: Project, fake_client: MagicMock
) -> None:
    manager = WorkstationManager(session, bus=bus, client=fake_client)
    ws = await manager.create(project.id)
    ws.container_id = None

    with pytest.raises(ValueError, match="no container"):
        await manager.exec(ws, ["true"])


async def test_destroy_removes_containers_network_workspace_and_marks_destroyed(
    session: AsyncSession, bus: EventBus, project: Project, fake_client: MagicMock, tmp_path: Any
) -> None:
    manager = WorkstationManager(session, bus=bus, client=fake_client)
    ws = await manager.create(project.id)
    workspace_dir = tmp_path / project.id
    assert workspace_dir.exists()

    await manager.destroy(ws)

    removed = {c.args[0] for c in fake_client.containers.get.call_args_list}
    assert removed == {"main-container-id", "db-container-id"}
    fake_client.networks.get.assert_called_with(f"algohub-ws-{project.id}")
    assert not workspace_dir.exists()
    assert ws.status == "destroyed"


async def test_destroy_tolerates_already_missing_containers_and_network(
    session: AsyncSession, bus: EventBus, project: Project, fake_client: MagicMock
) -> None:
    manager = WorkstationManager(session, bus=bus, client=fake_client)
    ws = await manager.create(project.id)
    fake_client.containers.get.side_effect = NotFound("gone")
    fake_client.networks.get.side_effect = NotFound("gone")

    await manager.destroy(ws)  # must not raise

    assert ws.status == "destroyed"


async def test_health_reports_reachable(
    session: AsyncSession, bus: EventBus, fake_client: MagicMock
) -> None:
    manager = WorkstationManager(session, bus=bus, client=fake_client)

    result = await manager.health()

    fake_client.ping.assert_called_once()
    assert result["docker_reachable"] is True
    assert isinstance(result["latency_ms"], int)


async def test_health_reports_unreachable_on_error(session: AsyncSession, bus: EventBus) -> None:
    client = _fake_client()
    client.ping.side_effect = ConnectionError("no daemon")
    manager = WorkstationManager(session, bus=bus, client=client)

    result = await manager.health()

    assert result["docker_reachable"] is False
    assert "no daemon" in result["error"]


async def test_list_scopes_to_a_project(
    session: AsyncSession, bus: EventBus, project: Project, fake_client: MagicMock
) -> None:
    manager = WorkstationManager(session, bus=bus, client=fake_client)
    ws = await manager.create(project.id)

    scoped = await manager.list(project.id)
    unscoped = await manager.list()

    assert [w.id for w in scoped] == [ws.id]
    assert ws.id in {w.id for w in unscoped}
