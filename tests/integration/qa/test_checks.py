"""Evaluation checks registry (FR-043): each check's command shape and pass/fail interpretation."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.blueprints.schema import VALID_EVALUATION_CHECKS
from web_department.collaboration.events import EventBus
from web_department.config.permissions import Role
from web_department.db.models import Agent, Project, Workstation
from web_department.db.repositories import (
    AgentRepository,
    BlueprintRepository,
    WorkstationRepository,
)
from web_department.qa import checks
from web_department.workstation.manager import ExecResult


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
        id="checks-test-bp", version="1", yaml="y", checksum="c"
    )
    return await AgentRepository(session).create(
        display_id="WEB-QA-001",
        blueprint_id="checks-test-bp",
        blueprint_version="1",
        project_id=project.id,
    )


@pytest.fixture
def fake_manager() -> AsyncMock:
    return AsyncMock()


def _ok(stdout: str = "") -> ExecResult:
    return ExecResult(0, stdout, "", False, False)


def _fail(exit_code: int = 1, stderr: str = "boom") -> ExecResult:
    return ExecResult(exit_code, "", stderr, False, False)


def test_checks_registry_matches_the_schema_s_valid_evaluation_checks() -> None:
    assert set(checks.CHECKS) == VALID_EVALUATION_CHECKS


async def test_lint_pass_runs_all_four_commands_and_passes_when_all_succeed(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.side_effect = [_ok(), _ok(), _ok(), _ok()]

    result = await checks.lint_pass(session, ws, Role.QA, agent.id, fake_manager, bus=bus)

    assert result == checks.CheckResult("lint_pass", True, "all checks passed")
    assert fake_manager.exec.await_count == 4


async def test_lint_pass_fails_and_names_the_failing_command_when_one_check_fails(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.side_effect = [_ok(), _fail(stderr="type error"), _ok(), _ok()]

    result = await checks.lint_pass(session, ws, Role.QA, agent.id, fake_manager, bus=bus)

    assert result.passed is False
    assert "mypy" in result.detail
    assert "type error" in result.detail


async def test_unit_tests_pass_runs_backend_and_frontend_suites(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.side_effect = [_ok(), _ok()]

    result = await checks.unit_tests_pass(session, ws, Role.QA, agent.id, fake_manager, bus=bus)

    assert result.passed is True
    first_cmd, second_cmd = (call.args[1] for call in fake_manager.exec.await_args_list)
    assert first_cmd[0].endswith("pytest")
    assert second_cmd[:2] == ["npm", "--prefix"]


async def test_e2e_pass_runs_the_playwright_suite(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.return_value = _ok()

    result = await checks.e2e_pass(session, ws, Role.QA, agent.id, fake_manager, bus=bus)

    assert result.passed is True
    command = fake_manager.exec.await_args_list[0].args[1]
    assert command == ["npm", "--prefix", "frontend", "run", "e2e"]


async def test_e2e_pass_fails_when_playwright_fails(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.return_value = _fail(stderr="1 test failed")

    result = await checks.e2e_pass(session, ws, Role.QA, agent.id, fake_manager, bus=bus)

    assert result.passed is False
    assert "1 test failed" in result.detail


async def test_screenshot_attached_true_when_a_screenshot_file_exists(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    shots = Path(ws.repo_path, "qa", "screenshots")
    shots.mkdir(parents=True)
    (shots / "home.png").write_bytes(b"\x89PNG")

    result = await checks.screenshot_attached(session, ws, Role.QA, agent.id, fake_manager, bus=bus)

    assert result.passed is True


async def test_screenshot_attached_false_when_the_directory_is_missing(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    result = await checks.screenshot_attached(session, ws, Role.QA, agent.id, fake_manager, bus=bus)

    assert result.passed is False


_VALID_OPENAPI = """openapi: 3.1.0
info: {title: Shop, version: 1.0.0}
paths:
  /api/products:
    get:
      responses:
        '200': {description: ok}
"""
_SWAGGER_2 = """swagger: '2.0'
info: {title: t, version: '1'}
paths: {}
"""
_NO_INFO = """openapi: 3.1.0
paths: {}
"""


async def test_openapi_valid_passes_for_a_valid_contract_without_running_any_command(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    fake_manager: AsyncMock,
    tmp_path: Path,
) -> None:
    (tmp_path / "openapi.yaml").write_text(_VALID_OPENAPI)

    # Role.ARCHITECT has no RUN_COMMAND (SRS section 10) - the check must still work for it.
    result = await checks.openapi_valid(
        session, ws, Role.ARCHITECT, agent.id, fake_manager, bus=bus
    )

    assert result.passed is True
    fake_manager.exec.assert_not_awaited()


@pytest.mark.parametrize(
    ("content", "expected_detail"),
    [
        ("openapi: [unclosed", "not valid YAML"),
        (_SWAGGER_2, "OpenAPI 3.x"),
        (_NO_INFO, "'info' is a required property"),
    ],
)
async def test_openapi_valid_fails_with_a_one_line_reason_for_a_bad_contract(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    fake_manager: AsyncMock,
    tmp_path: Path,
    content: str,
    expected_detail: str,
) -> None:
    (tmp_path / "openapi.yaml").write_text(content)

    result = await checks.openapi_valid(
        session, ws, Role.ARCHITECT, agent.id, fake_manager, bus=bus
    )

    assert result.passed is False
    assert expected_detail in result.detail


async def test_openapi_valid_keeps_only_the_first_line_of_a_schema_violation(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    fake_manager: AsyncMock,
    tmp_path: Path,
) -> None:
    # The validator appends a multi-kilobyte schema dump; only its first line is fed back.
    (tmp_path / "openapi.yaml").write_text(_NO_INFO)

    result = await checks.openapi_valid(
        session, ws, Role.ARCHITECT, agent.id, fake_manager, bus=bus
    )

    assert result.detail == "openapi.yaml is invalid: 'info' is a required property"


async def test_openapi_valid_fails_when_the_contract_file_is_missing(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    result = await checks.openapi_valid(
        session, ws, Role.ARCHITECT, agent.id, fake_manager, bus=bus
    )

    assert result == checks.CheckResult("openapi_valid", False, "openapi.yaml does not exist")


async def test_migration_reversible_passes_when_downgrade_then_upgrade_succeeds(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.return_value = _ok("migration_reversible: ok")

    result = await checks.migration_reversible(
        session, ws, Role.QA, agent.id, fake_manager, bus=bus
    )

    assert result.passed is True


async def test_migration_reversible_fails_when_downgrade_raises(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.return_value = _fail(stderr="Can't locate revision")

    result = await checks.migration_reversible(
        session, ws, Role.QA, agent.id, fake_manager, bus=bus
    )

    assert result.passed is False
    assert "Can't locate revision" in result.detail


async def test_no_secrets_passes_when_grep_finds_no_match(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.return_value = ExecResult(1, "", "", False, False)  # grep: no match

    result = await checks.no_secrets(session, ws, Role.QA, agent.id, fake_manager, bus=bus)

    assert result.passed is True


async def test_no_secrets_fails_when_grep_finds_a_match(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.return_value = ExecResult(
        0, "backend/app/config.py:3:AKIAABCDEFGHIJKLMNOP", "", False, False
    )

    result = await checks.no_secrets(session, ws, Role.QA, agent.id, fake_manager, bus=bus)

    assert result.passed is False
    assert "AKIA" in result.detail


async def test_no_secrets_fails_when_the_scan_itself_errors(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, fake_manager: AsyncMock
) -> None:
    fake_manager.exec.return_value = ExecResult(
        2, "", "grep: backend/app: No such file", False, False
    )

    result = await checks.no_secrets(session, ws, Role.QA, agent.id, fake_manager, bus=bus)

    assert result.passed is False
    assert "No such file" in result.detail
