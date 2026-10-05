"""RequirementAnalyzer: analyze(), clarifications, and the timeout path (FR-020..024)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus
from web_department.config.settings import get_settings
from web_department.db.models import Event, Project
from web_department.db.repositories import EventRepository, RequirementSpecRepository
from web_department.db.states import ProjectState
from web_department.llm.router import CompletionResult
from web_department.orchestrator import requirement_analyzer as analyzer_module
from web_department.orchestrator.requirement_analyzer import RequirementAnalyzer
from web_department.orchestrator.wrs_schema import Feature, WebRequirementsSpec

COMPLETE_WRS = WebRequirementsSpec(
    project_type="web_application",
    domain="todo",
    roles=["user"],
    features=[
        Feature(
            id="F-01",
            name="authentication",
            description="Login",
            acceptance=["User can log in"],
        )
    ],
)

WRS_WITH_QUESTIONS = COMPLETE_WRS.model_copy(
    update={"open_questions": ["Should guests be able to browse without an account?"]}
)


def _result(wrs: WebRequirementsSpec) -> CompletionResult:
    return CompletionResult(
        content="{}",
        parsed=wrs,
        provider="test",
        model="test/model",
        cached=False,
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
    )


@pytest.fixture
def mock_analyzer_llm(monkeypatch: pytest.MonkeyPatch) -> Callable[..., AsyncMock]:
    def _set(*results: WebRequirementsSpec) -> AsyncMock:
        mock = AsyncMock(side_effect=[_result(r) for r in results])
        monkeypatch.setattr(analyzer_module, "complete", mock)
        return mock

    return _set


@pytest.fixture
async def project_with_request(session: AsyncSession, project: Project) -> Project:
    """The shared ``project`` fixture plus the PROJECT_RECEIVED event analyze() reads from."""
    await EventRepository(session).create(
        type="PROJECT_RECEIVED",
        project_id=project.id,
        payload={
            "description": "A todo app where users can sign up and manage tasks.",
            "constraints": "Use PostgreSQL.",
            "attachments": [],
        },
    )
    return project


async def test_analyze_stores_valid_wrs_and_advances_to_planning(
    session: AsyncSession,
    bus: EventBus,
    project_with_request: Project,
    mock_analyzer_llm: Callable[..., AsyncMock],
) -> None:
    mock_analyzer_llm(COMPLETE_WRS)

    spec = await RequirementAnalyzer(session, bus=bus).analyze(project_with_request)

    assert spec.version == 1
    assert spec.json["domain"] == "todo"
    assert project_with_request.state is ProjectState.PLANNING
    assert project_with_request.wrs_version == 1


async def test_analyze_moves_received_project_through_analyzing(
    session: AsyncSession,
    bus: EventBus,
    project_with_request: Project,
    mock_analyzer_llm: Callable[..., AsyncMock],
) -> None:
    assert project_with_request.state is ProjectState.RECEIVED
    mock_analyzer_llm(COMPLETE_WRS)

    await RequirementAnalyzer(session, bus=bus).analyze(project_with_request)

    assert project_with_request.state is ProjectState.PLANNING


async def test_analyze_uses_description_and_constraints_from_project_received(
    session: AsyncSession,
    bus: EventBus,
    project_with_request: Project,
    mock_analyzer_llm: Callable[..., AsyncMock],
) -> None:
    mock = mock_analyzer_llm(COMPLETE_WRS)

    await RequirementAnalyzer(session, bus=bus).analyze(project_with_request)

    (messages,), kwargs = mock.call_args
    prompt = messages[0]["content"]
    assert "sign up and manage tasks" in prompt
    assert "Use PostgreSQL." in prompt
    assert kwargs["json_schema"] is WebRequirementsSpec


async def test_analyze_falls_back_to_title_with_no_project_received_event(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    mock_analyzer_llm: Callable[..., AsyncMock],
) -> None:
    mock = mock_analyzer_llm(COMPLETE_WRS)

    await RequirementAnalyzer(session, bus=bus).analyze(project)

    (messages,), _ = mock.call_args
    assert project.title in messages[0]["content"]


async def test_analyze_with_open_questions_stays_in_analyzing_and_emits_review_required(
    session: AsyncSession,
    bus: EventBus,
    project_with_request: Project,
    mock_analyzer_llm: Callable[..., AsyncMock],
) -> None:
    mock_analyzer_llm(WRS_WITH_QUESTIONS)

    spec = await RequirementAnalyzer(session, bus=bus).analyze(project_with_request)

    assert project_with_request.state is ProjectState.ANALYZING
    assert spec.json["open_questions"] == WRS_WITH_QUESTIONS.open_questions
    events = (
        await session.scalars(
            select(Event).where(
                Event.project_id == project_with_request.id, Event.type == "REVIEW_REQUIRED"
            )
        )
    ).all()
    (event,) = events
    assert event.payload["open_questions"] == WRS_WITH_QUESTIONS.open_questions
    assert event.payload["spec_version"] == 1


async def test_second_analyze_call_creates_version_two(
    session: AsyncSession,
    bus: EventBus,
    project_with_request: Project,
    mock_analyzer_llm: Callable[..., AsyncMock],
) -> None:
    mock_analyzer_llm(WRS_WITH_QUESTIONS, COMPLETE_WRS)
    agent = RequirementAnalyzer(session, bus=bus)

    first = await agent.analyze(project_with_request)
    second = await agent.analyze(project_with_request)

    assert (first.version, second.version) == (1, 2)
    assert project_with_request.wrs_version == 2
    assert project_with_request.state is ProjectState.PLANNING


async def test_apply_clarifications_merges_answers_and_reruns(
    session: AsyncSession,
    bus: EventBus,
    project_with_request: Project,
    mock_analyzer_llm: Callable[..., AsyncMock],
) -> None:
    mock = mock_analyzer_llm(WRS_WITH_QUESTIONS, COMPLETE_WRS)
    agent = RequirementAnalyzer(session, bus=bus)
    await agent.analyze(project_with_request)

    spec = await agent.apply_clarifications(
        project_with_request, {"Should guests browse without an account?": "Yes"}
    )

    assert spec.version == 2
    assert project_with_request.state is ProjectState.PLANNING
    (messages,), _ = mock.call_args
    prompt = messages[0]["content"]
    assert "Should guests browse without an account?" in prompt
    assert "Yes" in prompt
    assert "Previous draft specification (version 1" in prompt


async def test_check_clarification_timeout_returns_none_before_the_deadline(
    session: AsyncSession,
    bus: EventBus,
    project_with_request: Project,
    mock_analyzer_llm: Callable[..., AsyncMock],
) -> None:
    mock_analyzer_llm(WRS_WITH_QUESTIONS)
    agent = RequirementAnalyzer(session, bus=bus)
    await agent.analyze(project_with_request)

    result = await agent.check_clarification_timeout(project_with_request)

    assert result is None
    assert project_with_request.state is ProjectState.ANALYZING


async def test_check_clarification_timeout_advances_the_project_once_expired(
    session: AsyncSession,
    bus: EventBus,
    project_with_request: Project,
    mock_analyzer_llm: Callable[..., AsyncMock],
) -> None:
    mock_analyzer_llm(WRS_WITH_QUESTIONS)
    agent = RequirementAnalyzer(session, bus=bus)
    await agent.analyze(project_with_request)
    spec_repo = RequirementSpecRepository(session)
    latest = await spec_repo.latest_for_project(project_with_request.id)
    assert latest is not None
    timeout_hours = get_settings().analysis_clarification_timeout_hours
    latest.created_at = datetime.now(UTC) - timedelta(hours=timeout_hours + 1)
    await session.flush()

    result = await agent.check_clarification_timeout(project_with_request)

    assert result is not None
    assert result.id == latest.id
    assert project_with_request.state is ProjectState.PLANNING


async def test_check_clarification_timeout_is_a_noop_when_not_analyzing(
    session: AsyncSession, bus: EventBus, project_with_request: Project
) -> None:
    result = await RequirementAnalyzer(session, bus=bus).check_clarification_timeout(
        project_with_request
    )

    assert result is None
    assert project_with_request.state is ProjectState.RECEIVED


async def test_check_clarification_timeout_is_a_noop_with_no_open_questions(
    session: AsyncSession,
    bus: EventBus,
    project_with_request: Project,
    mock_analyzer_llm: Callable[..., AsyncMock],
) -> None:
    mock_analyzer_llm(COMPLETE_WRS)
    agent = RequirementAnalyzer(session, bus=bus)
    await agent.analyze(project_with_request)
    assert project_with_request.state is ProjectState.PLANNING

    result = await agent.check_clarification_timeout(project_with_request)

    assert result is None
