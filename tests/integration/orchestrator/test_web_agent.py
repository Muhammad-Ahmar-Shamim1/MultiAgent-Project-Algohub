"""WebAgent: scope classification, project creation, PROJECT_RECEIVED, status webhook."""

from collections.abc import Callable
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.api.schemas import ProjectRequest
from web_department.collaboration.events import EventBus
from web_department.config.settings import get_settings
from web_department.db.models import Event, Project
from web_department.llm.router import CompletionResult
from web_department.orchestrator import web_agent as web_agent_module
from web_department.orchestrator.web_agent import NotWebScope, ScopeClassification, WebAgent

E_COMMERCE_REQUEST = ProjectRequest(
    title="Clothing e-commerce",
    description=(
        "An online clothing store with a product catalog, cart, checkout and an admin panel."
    ),
    constraints="Use the golden-v1 stack.",
    requester_id="u-42",
)


def _classification_result(is_web: bool, reason: str) -> CompletionResult:
    return CompletionResult(
        content="{}",
        parsed=ScopeClassification(is_web=is_web, reason=reason),
        provider="test",
        model="test/model",
        cached=False,
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
    )


@pytest.fixture
def mock_classify(monkeypatch: pytest.MonkeyPatch) -> Callable[..., AsyncMock]:
    def _set(is_web: bool, reason: str = "because") -> AsyncMock:
        mock = AsyncMock(return_value=_classification_result(is_web, reason))
        monkeypatch.setattr(web_agent_module, "complete", mock)
        return mock

    return _set


async def test_accept_creates_project_and_emits_project_received(
    session: AsyncSession, bus: EventBus, mock_classify: Callable[..., AsyncMock]
) -> None:
    mock_classify(True, "It's a web app")
    agent = WebAgent(session, bus=bus)

    project = await agent.accept(E_COMMERCE_REQUEST)

    assert project.title == "Clothing e-commerce"
    assert project.requester_id == "u-42"
    assert project.state.value == "RECEIVED"
    events = (await session.scalars(select(Event).where(Event.project_id == project.id))).all()
    (event,) = events
    assert event.type == "PROJECT_RECEIVED"
    assert event.payload["description"].startswith("An online clothing store")
    assert event.payload["constraints"] == "Use the golden-v1 stack."
    assert event.payload["attachments"] == []


async def test_accept_passes_request_fields_to_the_classifier_prompt(
    session: AsyncSession, bus: EventBus, mock_classify: Callable[..., AsyncMock]
) -> None:
    mock = mock_classify(True)

    await WebAgent(session, bus=bus).accept(E_COMMERCE_REQUEST)

    (messages,), kwargs = mock.call_args
    prompt = messages[0]["content"]
    assert "Clothing e-commerce" in prompt
    assert "online clothing store" in prompt
    assert "golden-v1 stack" in prompt
    assert kwargs["json_schema"] is ScopeClassification


async def test_accept_rejects_non_web_requests_without_creating_a_project(
    session: AsyncSession, bus: EventBus, mock_classify: Callable[..., AsyncMock]
) -> None:
    mock_classify(False, "This is a mobile-only app request, not a web app")
    projects_before = len((await session.scalars(select(Project))).all())
    events_before = len((await session.scalars(select(Event))).all())

    with pytest.raises(NotWebScope, match="mobile-only"):
        await WebAgent(session, bus=bus).accept(E_COMMERCE_REQUEST)

    projects_after = (await session.scalars(select(Project))).all()
    events_after = (await session.scalars(select(Event))).all()
    assert len(projects_after) == projects_before
    assert len(events_after) == events_before


async def test_not_web_scope_exception_carries_the_reason() -> None:
    exc = NotWebScope("out of scope")

    assert exc.reason == "out of scope"
    assert str(exc) == "out of scope"


async def test_report_status_does_nothing_when_no_webhook_configured(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, committed_project_id: str
) -> None:
    monkeypatch.delenv("STATUS_WEBHOOK_URL", raising=False)
    get_settings.cache_clear()
    posted = AsyncMock()
    monkeypatch.setattr(httpx.AsyncClient, "post", posted)

    await WebAgent(session).report_status(committed_project_id)

    posted.assert_not_called()


async def test_report_status_posts_project_id_and_state_when_configured(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, committed_project_id: str
) -> None:
    monkeypatch.setenv("STATUS_WEBHOOK_URL", "https://example.test/webhook")
    get_settings.cache_clear()
    posted: dict[str, object] = {}

    async def fake_post(
        self: httpx.AsyncClient, url: str, *, json: dict[str, object] | None = None, **_: object
    ) -> httpx.Response:
        posted["url"] = url
        posted["json"] = json
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    await WebAgent(session).report_status(committed_project_id)

    assert posted["url"] == "https://example.test/webhook"
    assert posted["json"] == {"project_id": committed_project_id, "state": "RECEIVED"}


async def test_report_status_logs_and_swallows_webhook_failures(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, committed_project_id: str
) -> None:
    monkeypatch.setenv("STATUS_WEBHOOK_URL", "https://example.test/webhook")
    get_settings.cache_clear()

    async def failing_post(self: httpx.AsyncClient, url: str, **_: object) -> httpx.Response:
        return httpx.Response(500, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", failing_post)

    await WebAgent(session).report_status(committed_project_id)  # must not raise


async def test_report_status_of_unknown_project_does_not_call_the_webhook(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession
) -> None:
    monkeypatch.setenv("STATUS_WEBHOOK_URL", "https://example.test/webhook")
    get_settings.cache_clear()
    posted = AsyncMock()
    monkeypatch.setattr(httpx.AsyncClient, "post", posted)

    await WebAgent(session).report_status("prj_missing")

    posted.assert_not_called()
