"""Workstation tools: permission checks, path safety, policy enforcement, TOOL_CALL events."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus
from web_department.config.permissions import Role
from web_department.db.models import Agent, Approval, Event, Project, Workstation
from web_department.db.repositories import (
    AgentRepository,
    BlueprintRepository,
    WorkstationRepository,
)
from web_department.workstation import tools
from web_department.workstation.manager import ExecResult
from web_department.workstation.policy import ApprovalRequired, PermissionDenied
from web_department.workstation.tools import PathEscapeError, ToolPermissionError


@pytest.fixture
async def ws(session: AsyncSession, project: Project, tmp_path: Path) -> Workstation:
    return await WorkstationRepository(session).create(
        project_id=project.id,
        container_id="fake-container",
        repo_path=str(tmp_path),
        limits={},
        status="running",
    )


@pytest.fixture
async def agent(session: AsyncSession, project: Project) -> Agent:
    await BlueprintRepository(session).create(
        id="workstation-test-bp", version="1", yaml="y", checksum="c"
    )
    return await AgentRepository(session).create(
        display_id="WEB-WS-001",
        blueprint_id="workstation-test-bp",
        blueprint_version="1",
        project_id=project.id,
    )


@pytest.fixture
def fake_manager() -> AsyncMock:
    return AsyncMock()


async def _tool_call_events(session: AsyncSession, ws: Workstation) -> list[Event]:
    result = await session.scalars(
        select(Event).where(Event.project_id == ws.project_id, Event.type == "TOOL_CALL")
    )
    return list(result.all())


# --- read_file / write_file / list_dir / search --------------------------------------------


async def test_write_then_read_file_roundtrips(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent
) -> None:
    await tools.write_file(session, ws, Role.BACKEND, agent.id, "src/app.py", "print(1)", bus=bus)

    content = await tools.read_file(session, ws, Role.BACKEND, agent.id, "src/app.py", bus=bus)

    assert content == "print(1)"
    events = await _tool_call_events(session, ws)
    assert [e.payload["tool"] for e in events] == ["write_file", "read_file"]


async def test_write_file_without_write_code_permission_is_denied(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent
) -> None:
    with pytest.raises(ToolPermissionError, match="write_code"):
        await tools.write_file(session, ws, Role.SECURITY, agent.id, "x.py", "1", bus=bus)


async def test_write_file_allows_write_tests_permission_for_qa(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent
) -> None:
    # QA holds WRITE_TESTS, not WRITE_CODE - it must still be able to write its test files.
    await tools.write_file(session, ws, Role.QA, agent.id, "tests/test_x.py", "x = 1", bus=bus)

    content = await tools.read_file(session, ws, Role.QA, agent.id, "tests/test_x.py", bus=bus)
    assert content == "x = 1"


async def test_read_file_checks_read_code_permission(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every real Role holds READ_CODE, so there is no role to naturally trigger this denial;
    # force the check to fail to confirm read_file actually calls it before touching the file.
    monkeypatch.setattr(tools, "has_permission", lambda role, permission: False)

    with pytest.raises(ToolPermissionError, match="read_code"):
        await tools.read_file(session, ws, Role.BACKEND, agent.id, "x.py", bus=bus)


async def test_write_file_blocks_path_traversal(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent
) -> None:
    with pytest.raises(PathEscapeError):
        await tools.write_file(session, ws, Role.BACKEND, agent.id, "../escape.py", "x", bus=bus)


async def test_read_file_blocks_path_traversal(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent
) -> None:
    with pytest.raises(PathEscapeError):
        await tools.read_file(session, ws, Role.BACKEND, agent.id, "../../etc/passwd", bus=bus)


async def test_list_dir_lists_files_and_suffixes_directories(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent
) -> None:
    Path(ws.repo_path, "a.txt").write_text("x")
    Path(ws.repo_path, "sub").mkdir()

    names = await tools.list_dir(session, ws, Role.FRONTEND, agent.id, bus=bus)

    assert names == ["a.txt", "sub/"]


async def test_search_finds_matches_with_line_numbers(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent
) -> None:
    Path(ws.repo_path, "a.py").write_text("# TODO: fix\nprint(1)\n")
    Path(ws.repo_path, "sub").mkdir()
    Path(ws.repo_path, "sub", "b.py").write_text("x = 1\n# TODO: also here\n")

    matches = await tools.search(session, ws, Role.QA, agent.id, r"TODO", bus=bus)

    found = {(m["path"], m["line"]) for m in matches}
    assert found == {("a.py", 1), (Path("sub", "b.py").as_posix(), 2)}


async def test_search_checks_read_code_permission(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools, "has_permission", lambda role, permission: False)

    with pytest.raises(ToolPermissionError, match="read_code"):
        await tools.search(session, ws, Role.BACKEND, agent.id, "TODO", bus=bus)


# --- run_command: permission / policy enforcement -------------------------------------------


async def test_run_command_without_run_command_permission_is_denied(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    with pytest.raises(ToolPermissionError, match="run_command"):
        await tools.run_command(
            session, ws, Role.ARCHITECT, agent.id, fake_manager, ["git", "status"], bus=bus
        )
    fake_manager.exec.assert_not_called()


async def test_run_command_denied_by_policy_never_execs(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    with pytest.raises(PermissionDenied):
        await tools.run_command(
            session, ws, Role.BACKEND, agent.id, fake_manager, ["docker", "ps"], bus=bus
        )

    fake_manager.exec.assert_not_called()
    (event,) = await _tool_call_events(session, ws)
    assert event.status == "denied"


async def test_run_command_needing_approval_creates_approval_and_event_but_never_execs(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    with pytest.raises(ApprovalRequired):
        await tools.run_command(
            session,
            ws,
            Role.DATABASE,
            agent.id,
            fake_manager,
            ["git", "reset", "--hard"],
            bus=bus,
        )

    fake_manager.exec.assert_not_called()
    approvals = (
        await session.scalars(select(Approval).where(Approval.project_id == ws.project_id))
    ).all()
    (approval,) = approvals
    assert approval.action_type == "run_command"
    assert approval.status == "pending"
    approval_events = (
        await session.scalars(
            select(Event).where(
                Event.project_id == ws.project_id, Event.type == "APPROVAL_REQUESTED"
            )
        )
    ).all()
    assert len(approval_events) == 1
    tool_events = await _tool_call_events(session, ws)
    assert tool_events[-1].status == "approval_required"


async def test_run_command_allowed_execs_and_emits_ok_event(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.return_value = ExecResult(0, "v20.0.0\n", "", False, False)

    result = await tools.run_command(
        session, ws, Role.BACKEND, agent.id, fake_manager, ["node", "-v"], bus=bus
    )

    assert result.exit_code == 0
    fake_manager.exec.assert_awaited_once_with(ws, ["node", "-v"], timeout=None)
    (event,) = await _tool_call_events(session, ws)
    assert event.status == "ok"


async def test_run_command_nonzero_exit_emits_error_status_but_does_not_raise(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.return_value = ExecResult(1, "", "boom", False, False)

    result = await tools.run_command(
        session, ws, Role.BACKEND, agent.id, fake_manager, ["pytest"], bus=bus
    )

    assert result.exit_code == 1
    (event,) = await _tool_call_events(session, ws)
    assert event.status == "error"


async def test_run_tests_delegates_to_run_command_with_pytest(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.return_value = ExecResult(0, "1 passed", "", False, False)

    await tools.run_tests(session, ws, Role.QA, agent.id, fake_manager, path="tests/", bus=bus)

    fake_manager.exec.assert_awaited_once_with(ws, ["pytest", "tests/"], timeout=None)


# --- git_commit ------------------------------------------------------------------------------


async def test_git_commit_stages_then_commits(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.side_effect = [
        ExecResult(0, "", "", False, False),
        ExecResult(0, "[main abc] msg", "", False, False),
    ]

    result = await tools.git_commit(
        session, ws, Role.BACKEND, agent.id, fake_manager, "msg", bus=bus
    )

    assert result.exit_code == 0
    assert fake_manager.exec.await_args_list[0].args == (ws, ["git", "add", "-A"])
    assert fake_manager.exec.await_args_list[1].args == (ws, ["git", "commit", "-m", "msg"])


async def test_git_commit_short_circuits_if_staging_fails(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.return_value = ExecResult(1, "", "fatal", False, False)

    result = await tools.git_commit(
        session, ws, Role.BACKEND, agent.id, fake_manager, "msg", bus=bus
    )

    assert result.exit_code == 1
    assert fake_manager.exec.await_count == 1


async def test_git_commit_without_write_code_permission_is_denied(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    with pytest.raises(ToolPermissionError):
        await tools.git_commit(session, ws, Role.SECURITY, agent.id, fake_manager, "msg", bus=bus)
    fake_manager.exec.assert_not_called()


async def test_git_commit_allows_write_tests_permission_for_qa(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.side_effect = [
        ExecResult(0, "", "", False, False),
        ExecResult(0, "[main abc] msg", "", False, False),
    ]

    result = await tools.git_commit(session, ws, Role.QA, agent.id, fake_manager, "msg", bus=bus)

    assert result.exit_code == 0


# --- browser_screenshot -----------------------------------------------------------------------


async def test_browser_screenshot_returns_the_written_png_bytes(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    async def fake_exec(
        workstation: Workstation, cmd: list[str], timeout: float | None = None
    ) -> ExecResult:
        out_name = cmd[2].split("path='")[1].split("'")[0]
        Path(ws.repo_path, out_name).write_bytes(b"\x89PNG\r\n")
        return ExecResult(0, "", "", False, False)

    fake_manager.exec.side_effect = fake_exec

    data = await tools.browser_screenshot(
        session, ws, Role.QA, agent.id, fake_manager, "http://localhost:3000", bus=bus
    )

    assert data == b"\x89PNG\r\n"
    assert list(Path(ws.repo_path).glob(".algohub_screenshot_*.png")) == []  # cleaned up


async def test_browser_screenshot_without_browser_permission_is_denied(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    with pytest.raises(ToolPermissionError, match="browser"):
        await tools.browser_screenshot(
            session, ws, Role.DATABASE, agent.id, fake_manager, "http://x", bus=bus
        )
    fake_manager.exec.assert_not_called()


async def test_browser_screenshot_raises_on_a_failed_exec(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.return_value = ExecResult(1, "", "playwright crashed", False, False)

    with pytest.raises(RuntimeError, match="playwright crashed"):
        await tools.browser_screenshot(
            session, ws, Role.QA, agent.id, fake_manager, "http://x", bus=bus
        )
