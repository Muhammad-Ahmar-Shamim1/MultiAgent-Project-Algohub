"""QAWorker (FR-100, FR-101): criterion-tagged tests, collected (not passed) as its own check."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.agents import base as base_module
from web_department.agents.base import FileEdit, WorkerPlan
from web_department.agents.qa.agent import QAWorker, playwright_names, pytest_names
from web_department.collaboration.events import EventBus
from web_department.config.permissions import Role
from web_department.db.models import Agent, Project, Task, Workstation
from web_department.db.repositories import RequirementSpecRepository, TaskRepository
from web_department.db.states import TaskState
from web_department.llm.router import CompletionResult
from web_department.orchestrator.wrs_schema import Feature, WebRequirementsSpec
from web_department.workstation.manager import ExecResult

WRS = WebRequirementsSpec(
    project_type="web_application",
    domain="ecommerce",
    roles=["customer"],
    features=[
        Feature(id="F-01", name="auth", description="Log in", acceptance=["User can log in"]),
        Feature(
            id="F-02",
            name="cart",
            description="Cart",
            acceptance=["Cart total updates", "Empty cart shows a message"],
        ),
    ],
)

PYTEST_COLLECT = """tests/test_cart.py::test_F02_1_total_updates
tests/test_cart.py::TestCart::test_unrelated[case-a]

2 tests collected in 0.01s
"""
PLAYWRIGHT_LIST = """Listing tests:
  [chromium] › cart.spec.ts:3:5 › F02_2 empty cart shows a message
Total: 1 test in 1 file
"""


def _ok(stdout: str = "") -> ExecResult:
    return ExecResult(0, stdout, "", False, False)


def _completion(*paths: str) -> CompletionResult:
    plan = WorkerPlan(
        files=[FileEdit(path=p, content="# test") for p in paths], summary="cart tests"
    )
    return CompletionResult(
        content=plan.model_dump_json(),
        parsed=plan,
        provider="test",
        model="test/model",
        cached=False,
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
    )


@pytest.fixture
def mock_complete(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(base_module, "complete", mock)
    return mock


@pytest.fixture
async def qa_task(session: AsyncSession, project: Project) -> Task:
    await RequirementSpecRepository(session).create(
        project_id=project.id, version=1, json=WRS.model_dump(mode="json"), created_by="test"
    )
    return await TaskRepository(session).create(
        display_id="WEB-QA-T01",
        project_id=project.id,
        title="End-to-end tests: F-02",
        role="qa",
        state=TaskState.ASSIGNED,
        inputs={"feature": "F-02"},
        acceptance_checks=["End-to-end tests for F-02 pass"],
    )


def _worker(
    session: AsyncSession, agent: Agent, ws: Workstation, manager: AsyncMock, bus: EventBus
) -> QAWorker:
    return QAWorker(session, agent, {}, ws, manager, bus=bus)


def test_qa_worker_role() -> None:
    assert QAWorker.role is Role.QA
    assert QAWorker.system_prompt_name == "worker_qa"


def test_collected_test_names_are_parsed_from_pytest_and_playwright() -> None:
    assert pytest_names(PYTEST_COLLECT) == ["test_F02_1_total_updates", "test_unrelated"]
    assert playwright_names(PLAYWRIGHT_LIST) == ["F02_2 empty cart shows a message"]


async def test_passes_once_every_criterion_of_its_feature_has_a_collected_test(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    qa_task: Task,
    fake_manager: AsyncMock,
    mock_complete: AsyncMock,
    tmp_path: Path,
) -> None:
    mock_complete.return_value = _completion(
        "backend/tests/test_cart.py", "frontend/tests/e2e/cart.spec.ts"
    )
    fake_manager.exec.side_effect = [_ok(PYTEST_COLLECT), _ok(PLAYWRIGHT_LIST), _ok(), _ok()]

    result = await _worker(session, agent, ws, fake_manager, bus).run(qa_task)

    assert result.success is True
    assert (tmp_path / "backend" / "tests" / "test_cart.py").is_file()
    commands = [c.args[1] for c in fake_manager.exec.await_args_list]
    assert commands[0] == ["backend/.venv/bin/pytest", "backend/tests", "--collect-only", "-q"]
    assert commands[1] == ["npm", "--prefix", "frontend", "run", "e2e", "--", "--list"]
    assert commands[2] == ["git", "add", "-A"]


async def test_a_criterion_without_a_tagged_test_is_fed_back_with_its_tag(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    qa_task: Task,
    fake_manager: AsyncMock,
    mock_complete: AsyncMock,
) -> None:
    mock_complete.side_effect = [
        _completion("backend/tests/test_cart.py"),
        _completion("backend/tests/test_cart.py", "frontend/tests/e2e/cart.spec.ts"),
    ]
    fake_manager.exec.side_effect = [
        _ok(PYTEST_COLLECT),
        _ok("Listing tests:\nTotal: 0 tests in 0 files\n"),
        _ok(PYTEST_COLLECT),
        _ok(PLAYWRIGHT_LIST),
        _ok(),
        _ok(),
    ]

    result = await _worker(session, agent, ws, fake_manager, bus).run(qa_task)

    assert result.success is True
    assert result.attempts == 2
    feedback = mock_complete.await_args_list[1].args[0][-1]["content"]
    assert "F-02.2 (tag F02_2)" in feedback
    assert "F-02.1" not in feedback.split("No collected test is tagged for:")[-1]


async def test_a_collection_error_fails_the_check_with_its_output(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    qa_task: Task,
    fake_manager: AsyncMock,
    mock_complete: AsyncMock,
) -> None:
    mock_complete.side_effect = [_completion("backend/tests/test_cart.py")] * 2
    fake_manager.exec.side_effect = [
        ExecResult(2, "ImportError while importing test module", "", False, False),
        _ok(PYTEST_COLLECT),
        _ok(PLAYWRIGHT_LIST),
        _ok(),
        _ok(),
    ]

    await _worker(session, agent, ws, fake_manager, bus).run(qa_task)

    feedback = mock_complete.await_args_list[1].args[0][-1]["content"]
    assert "ImportError while importing test module" in feedback


async def test_a_plan_touching_application_code_is_rejected_before_writing(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    qa_task: Task,
    fake_manager: AsyncMock,
    mock_complete: AsyncMock,
    tmp_path: Path,
) -> None:
    mock_complete.side_effect = [
        _completion("backend/tests/test_cart.py", "backend/app/routers/cart.py"),
        _completion("backend/tests/test_cart.py", "frontend/tests/e2e/cart.spec.ts"),
    ]
    fake_manager.exec.side_effect = [_ok(PYTEST_COLLECT), _ok(PLAYWRIGHT_LIST), _ok(), _ok()]

    result = await _worker(session, agent, ws, fake_manager, bus).run(qa_task)

    assert result.success is True
    assert not (tmp_path / "backend" / "app" / "routers" / "cart.py").exists()
    feedback = mock_complete.await_args_list[1].args[0][-1]["content"]
    assert "backend/app/routers/cart.py" in feedback


async def test_context_lists_the_task_s_criteria_with_tags_and_the_api_contract(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    qa_task: Task,
    fake_manager: AsyncMock,
    mock_complete: AsyncMock,
    tmp_path: Path,
) -> None:
    (tmp_path / "openapi.yaml").write_text("openapi: 3.1.0\npaths: {/api/cart: {}}\n")
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "secret_module.py").write_text("x = 1")
    mock_complete.return_value = _completion("backend/tests/test_cart.py")
    fake_manager.exec.side_effect = [_ok(PYTEST_COLLECT), _ok(PLAYWRIGHT_LIST), _ok(), _ok()]

    await _worker(session, agent, ws, fake_manager, bus).run(qa_task)

    prompt = mock_complete.await_args_list[0].args[0][0]["content"]
    assert "F-02.1 (tag F02_1): Cart total updates" in prompt
    assert "F-02.2 (tag F02_2): Empty cart shows a message" in prompt
    assert "F-01.1" not in prompt  # only this task's feature
    assert "/api/cart" in prompt
    assert "secret_module.py" not in prompt
