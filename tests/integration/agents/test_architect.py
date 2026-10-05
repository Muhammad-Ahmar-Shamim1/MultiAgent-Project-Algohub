"""ArchitectWorker (FR-033): its context, its path scope, and its own checks instead of pytest."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.agents import base as base_module
from web_department.agents.architect.agent import OUTPUT_PATHS, ArchitectWorker
from web_department.agents.base import FileEdit, WorkerPlan
from web_department.collaboration.events import EventBus
from web_department.config.permissions import Role
from web_department.config.settings import get_settings
from web_department.db.models import Agent, Project, Task, Workstation
from web_department.db.repositories import RequirementSpecRepository, TaskRepository
from web_department.db.states import TaskState
from web_department.llm.router import CompletionResult, Purpose
from web_department.orchestrator.wrs_schema import (
    Entity,
    EntityField,
    Feature,
    Page,
    WebRequirementsSpec,
)
from web_department.workstation.manager import ExecResult

WRS = WebRequirementsSpec(
    project_type="e-commerce",
    domain="clothing",
    roles=["customer", "admin"],
    features=[
        Feature(id="F-01", name="Catalog", description="Browse products", acceptance=["lists"]),
        Feature(id="F-02", name="Cart", description="Add to cart", acceptance=["adds"]),
    ],
    pages=[Page(route="/products", purpose="Product list")],
    entities=[
        Entity(name="Product", fields=[EntityField(name="title", type="string")]),
        Entity(name="CartItem", fields=[EntityField(name="quantity", type="int")]),
    ],
)

ARCHITECTURE_MD = "# Architecture\n\n## Components\n\n## Decision log\n"
SCHEMA_MD = "# Schema\n\n## Product\n\n| column | type |\n\n## CartItem\n\n| column | type |\n"


def _openapi(tags_per_operation: list[list[str]]) -> str:
    paths = {
        f"/api/op{i}": {
            "get": {
                "operationId": f"op{i}",
                "tags": tags,
                "responses": {"200": {"description": "ok"}},
            }
        }
        for i, tags in enumerate(tags_per_operation)
    }
    return yaml.safe_dump(
        {"openapi": "3.1.0", "info": {"title": "Shop", "version": "1.0.0"}, "paths": paths}
    )


def _plan(openapi: str = _openapi([["F-01"], ["F-02"]]), schema: str = SCHEMA_MD) -> WorkerPlan:
    return WorkerPlan(
        files=[
            FileEdit(path="docs/architecture.md", content=ARCHITECTURE_MD),
            FileEdit(path="openapi.yaml", content=openapi),
            FileEdit(path="db/schema.md", content=schema),
        ],
        summary="Architecture record",
    )


def _completion(plan: WorkerPlan) -> CompletionResult:
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


def _git_ok() -> list[ExecResult]:
    return [ExecResult(0, "", "", False, False), ExecResult(0, "[main abc]", "", False, False)]


@pytest.fixture
def mock_complete(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(base_module, "complete", mock)
    return mock


@pytest.fixture
async def architect_task(session: AsyncSession, project: Project) -> Task:
    await RequirementSpecRepository(session).create(
        project_id=project.id, version=1, json=WRS.model_dump(mode="json"), created_by="test"
    )
    return await TaskRepository(session).create(
        display_id="WEB-ARCH-T01",
        project_id=project.id,
        title="Architecture record",
        role="architect",
        state=TaskState.ASSIGNED,
        inputs={},
        acceptance_checks=["openapi.yaml is a valid OpenAPI document"],
    )


def _worker(
    session: AsyncSession, agent: Agent, ws: Workstation, manager: AsyncMock, bus: EventBus
) -> ArchitectWorker:
    return ArchitectWorker(session, agent, {}, ws, manager, bus=bus)


def test_architect_role_purpose_and_checks() -> None:
    assert ArchitectWorker.role is Role.ARCHITECT
    assert ArchitectWorker.purpose is Purpose.PLANNING
    assert ArchitectWorker.evaluation_checks == ("openapi_valid",)


async def test_writes_the_three_documents_passes_its_checks_and_commits(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    architect_task: Task,
    fake_manager: AsyncMock,
    mock_complete: AsyncMock,
    tmp_path: Path,
) -> None:
    mock_complete.return_value = _completion(_plan())
    fake_manager.exec.side_effect = _git_ok()

    result = await _worker(session, agent, ws, fake_manager, bus).run(architect_task)

    assert result.success is True
    assert sorted(result.files_changed) == sorted(OUTPUT_PATHS)
    for path in OUTPUT_PATHS:
        assert (tmp_path / path).is_file()
    # No pytest: the only container commands are the commit (Architect has no RUN_COMMAND).
    commands = [call.args[1] for call in fake_manager.exec.await_args_list]
    assert commands[0] == ["git", "add", "-A"]
    assert commands[1][:3] == ["git", "commit", "-m"]
    assert len(commands) == 2
    refreshed = await TaskRepository(session).get(architect_task.id)
    assert refreshed is not None
    assert refreshed.state is TaskState.REVIEW


async def test_context_has_the_whole_wrs_and_no_source_tree_listing(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    architect_task: Task,
    fake_manager: AsyncMock,
    mock_complete: AsyncMock,
    tmp_path: Path,
) -> None:
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "secret_module.py").write_text("x = 1")
    mock_complete.return_value = _completion(_plan())
    fake_manager.exec.side_effect = _git_ok()

    await _worker(session, agent, ws, fake_manager, bus).run(architect_task)

    prompt = mock_complete.await_args_list[0].args[0][0]["content"]
    assert "F-01" in prompt and "F-02" in prompt
    assert "CartItem" in prompt and "/products" in prompt
    assert "secret_module.py" not in prompt


async def test_context_includes_the_existing_record_on_a_rerun(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    architect_task: Task,
    fake_manager: AsyncMock,
    mock_complete: AsyncMock,
    tmp_path: Path,
) -> None:
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "schema.md").write_text("## OldEntityFromLastRun\n")
    mock_complete.return_value = _completion(_plan())
    fake_manager.exec.side_effect = _git_ok()

    await _worker(session, agent, ws, fake_manager, bus).run(architect_task)

    prompt = mock_complete.await_args_list[0].args[0][0]["content"]
    assert "--- db/schema.md ---" in prompt
    assert "OldEntityFromLastRun" in prompt


async def test_a_feature_without_an_operation_fails_and_is_fed_back_then_fixed(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    architect_task: Task,
    fake_manager: AsyncMock,
    mock_complete: AsyncMock,
) -> None:
    mock_complete.side_effect = [
        _completion(_plan(openapi=_openapi([["F-01"]]))),
        _completion(_plan()),
    ]
    fake_manager.exec.side_effect = _git_ok()

    result = await _worker(session, agent, ws, fake_manager, bus).run(architect_task)

    assert result.success is True
    assert result.attempts == 2
    retry_messages = mock_complete.await_args_list[1].args[0]
    feedback = retry_messages[-1]["content"]
    assert "feature id(s) ['F-02']" in feedback


async def test_an_entity_without_a_schema_heading_fails_the_coverage_check(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    architect_task: Task,
    fake_manager: AsyncMock,
    mock_complete: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_TASK_RETRIES", "1")
    get_settings.cache_clear()
    try:
        mock_complete.return_value = _completion(_plan(schema="## Product\nonly one entity\n"))

        result = await _worker(session, agent, ws, fake_manager, bus).run(architect_task)
    finally:
        get_settings.cache_clear()

    assert result.success is False
    assert "CartItem" in result.summary
    fake_manager.exec.assert_not_awaited()  # never committed
    refreshed = await TaskRepository(session).get(architect_task.id)
    assert refreshed is not None
    assert refreshed.state is TaskState.BLOCKED


async def test_an_invalid_openapi_document_fails_openapi_valid(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    architect_task: Task,
    fake_manager: AsyncMock,
    mock_complete: AsyncMock,
) -> None:
    broken = yaml.safe_dump({"openapi": "3.1.0", "paths": {}})  # no `info`
    mock_complete.side_effect = [_completion(_plan(openapi=broken)), _completion(_plan())]
    fake_manager.exec.side_effect = _git_ok()

    result = await _worker(session, agent, ws, fake_manager, bus).run(architect_task)

    assert result.attempts == 2
    feedback = mock_complete.await_args_list[1].args[0][-1]["content"]
    assert "openapi_valid" in feedback
    assert "'info' is a required property" in feedback


async def test_a_plan_writing_outside_docs_and_contracts_is_rejected_before_writing(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    architect_task: Task,
    fake_manager: AsyncMock,
    mock_complete: AsyncMock,
    tmp_path: Path,
) -> None:
    overreaching = _plan()
    overreaching.files.append(FileEdit(path="backend/app/main.py", content="hacked"))
    mock_complete.side_effect = [_completion(overreaching), _completion(_plan())]
    fake_manager.exec.side_effect = _git_ok()

    result = await _worker(session, agent, ws, fake_manager, bus).run(architect_task)

    assert result.success is True
    assert not (tmp_path / "backend" / "app" / "main.py").exists()
    feedback = mock_complete.await_args_list[1].args[0][-1]["content"]
    assert "backend/app/main.py" in feedback


async def test_a_missing_document_fails_the_record_check(
    session: AsyncSession,
    bus: EventBus,
    ws: Workstation,
    agent: Agent,
    architect_task: Task,
    fake_manager: AsyncMock,
    mock_complete: AsyncMock,
) -> None:
    incomplete = WorkerPlan(
        files=[FileEdit(path="openapi.yaml", content=_openapi([["F-01"], ["F-02"]]))],
        summary="only the contract",
    )
    mock_complete.side_effect = [_completion(incomplete), _completion(_plan())]
    fake_manager.exec.side_effect = _git_ok()

    result = await _worker(session, agent, ws, fake_manager, bus).run(architect_task)

    assert result.attempts == 2
    feedback = mock_complete.await_args_list[1].args[0][-1]["content"]
    assert "docs/architecture.md" in feedback and "db/schema.md" in feedback
