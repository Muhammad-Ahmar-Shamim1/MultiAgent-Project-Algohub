"""POST/GET /api/v1/projects (FR-001..FR-005): auth, scope classification, DB/event effects."""

from unittest.mock import AsyncMock

import httpx
import pytest

from web_department.collaboration.events import EventBus
from web_department.config.settings import get_settings
from web_department.llm.router import CompletionResult
from web_department.orchestrator import requirement_analyzer as analyzer_module
from web_department.orchestrator import web_agent as web_agent_module
from web_department.orchestrator.web_agent import ScopeClassification
from web_department.orchestrator.wrs_schema import Feature, WebRequirementsSpec

WRS_WITH_QUESTIONS = WebRequirementsSpec(
    project_type="web_application",
    domain="ecommerce",
    roles=["customer"],
    features=[
        Feature(id="F-01", name="cart", description="Cart", acceptance=["Cart works"]),
    ],
    open_questions=["Do guests need a checkout without an account?"],
)
COMPLETE_WRS = WRS_WITH_QUESTIONS.model_copy(update={"open_questions": []})

ECOMMERCE_PAYLOAD = {
    "title": "Clothing e-commerce",
    "description": (
        "An online clothing store with a product catalog, cart, checkout and an admin panel."
    ),
    "constraints": "Use the golden-v1 stack.",
    "requester_id": "u-42",
}


def _mock_classification(
    monkeypatch: pytest.MonkeyPatch, is_web: bool, reason: str = "because"
) -> None:
    result = CompletionResult(
        content="{}",
        parsed=ScopeClassification(is_web=is_web, reason=reason),
        provider="test",
        model="test/model",
        cached=False,
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
    )
    monkeypatch.setattr(web_agent_module, "complete", AsyncMock(return_value=result))


@pytest.fixture(autouse=True)
def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUESTER_TOKEN", "req-secret")
    monkeypatch.setenv("APPROVER_TOKEN", "app-secret")
    monkeypatch.setenv("OPERATOR_TOKEN", "op-secret")
    get_settings.cache_clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_valid_ecommerce_request_returns_202_and_a_project_id(
    live_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_classification(monkeypatch, True, "It's an e-commerce web app")

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{live_server}/api/v1/projects", json=ECOMMERCE_PAYLOAD, headers=_auth("req-secret")
        )

    assert response.status_code == 202
    body = response.json()
    assert body["id"].startswith("prj_")
    assert body["title"] == "Clothing e-commerce"
    assert body["requester_id"] == "u-42"
    assert body["state"] == "RECEIVED"
    assert body["stack"] == "golden-v1"


async def test_created_project_is_readable_and_event_is_published(
    live_server: str, monkeypatch: pytest.MonkeyPatch, bus: EventBus
) -> None:
    _mock_classification(monkeypatch, True)

    async with httpx.AsyncClient(timeout=15) as client:
        created = await client.post(
            f"{live_server}/api/v1/projects", json=ECOMMERCE_PAYLOAD, headers=_auth("req-secret")
        )
        project_id = created.json()["id"]

        fetched = await client.get(
            f"{live_server}/api/v1/projects/{project_id}", headers=_auth("app-secret")
        )

    assert fetched.status_code == 200
    assert fetched.json()["id"] == project_id
    streamed = await bus.read(project_id, "0", block_ms=None)
    assert [s.event.event.value for s in streamed] == ["PROJECT_RECEIVED"]
    assert streamed[0].event.payload["description"].startswith("An online clothing store")


async def test_non_web_request_is_rejected_with_a_reason_and_creates_nothing(
    live_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_classification(monkeypatch, False, "This is a mobile-only app, not a web app")

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{live_server}/api/v1/projects", json=ECOMMERCE_PAYLOAD, headers=_auth("req-secret")
        )

    assert response.status_code == 422
    assert "mobile-only" in response.json()["detail"]


async def test_post_without_auth_is_401(live_server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_classification(monkeypatch, True)

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(f"{live_server}/api/v1/projects", json=ECOMMERCE_PAYLOAD)

    assert response.status_code == 401


async def test_post_with_approver_token_is_forbidden(
    live_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_classification(monkeypatch, True)

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{live_server}/api/v1/projects", json=ECOMMERCE_PAYLOAD, headers=_auth("app-secret")
        )

    assert response.status_code == 403


async def test_operator_token_can_also_submit(
    live_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_classification(monkeypatch, True)

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{live_server}/api/v1/projects", json=ECOMMERCE_PAYLOAD, headers=_auth("op-secret")
        )

    assert response.status_code == 202


async def test_invalid_body_is_422(live_server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_classification(monkeypatch, True)

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{live_server}/api/v1/projects",
            json={"title": "", "description": "x", "requester_id": "u"},
            headers=_auth("req-secret"),
        )

    assert response.status_code == 422


async def test_get_unknown_project_is_404(live_server: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{live_server}/api/v1/projects/prj_missing", headers=_auth("req-secret")
        )

    assert response.status_code == 404


async def test_get_project_requires_auth(live_server: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(f"{live_server}/api/v1/projects/prj_missing")

    assert response.status_code == 401


async def test_new_project_has_an_empty_task_list(
    live_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_classification(monkeypatch, True)

    async with httpx.AsyncClient(timeout=15) as client:
        created = await client.post(
            f"{live_server}/api/v1/projects", json=ECOMMERCE_PAYLOAD, headers=_auth("req-secret")
        )
        project_id = created.json()["id"]

        response = await client.get(
            f"{live_server}/api/v1/projects/{project_id}/tasks", headers=_auth("op-secret")
        )

    assert response.status_code == 200
    assert response.json() == []


def _mock_wrs(monkeypatch: pytest.MonkeyPatch, *specs: WebRequirementsSpec) -> AsyncMock:
    results = [
        CompletionResult(
            content="{}",
            parsed=spec,
            provider="test",
            model="test/model",
            cached=False,
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
        )
        for spec in specs
    ]
    mock = AsyncMock(side_effect=results)
    monkeypatch.setattr(analyzer_module, "complete", mock)
    return mock


async def test_clarifications_merge_answers_and_advance_the_project(
    live_server: str, monkeypatch: pytest.MonkeyPatch, bus: EventBus
) -> None:
    _mock_classification(monkeypatch, True)

    async with httpx.AsyncClient(timeout=15) as client:
        created = await client.post(
            f"{live_server}/api/v1/projects", json=ECOMMERCE_PAYLOAD, headers=_auth("req-secret")
        )
        project_id = created.json()["id"]

        # apply_clarifications() -> analyze() makes exactly one LLM call; it returns a WRS with
        # no open_questions, so the project should go straight to PLANNING.
        _mock_wrs(monkeypatch, COMPLETE_WRS)

        response = await client.post(
            f"{live_server}/api/v1/projects/{project_id}/clarifications",
            json={"answers": {"Do guests need a checkout without an account?": "No"}},
            headers=_auth("req-secret"),
        )

    assert response.status_code == 200
    assert response.json()["state"] == "PLANNING"
    assert response.json()["wrs_version"] == 1


async def test_clarifications_that_still_leave_open_questions_stay_in_analyzing(
    live_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_classification(monkeypatch, True)

    async with httpx.AsyncClient(timeout=15) as client:
        created = await client.post(
            f"{live_server}/api/v1/projects", json=ECOMMERCE_PAYLOAD, headers=_auth("req-secret")
        )
        project_id = created.json()["id"]

        _mock_wrs(monkeypatch, WRS_WITH_QUESTIONS)

        response = await client.post(
            f"{live_server}/api/v1/projects/{project_id}/clarifications",
            json={"answers": {"Do guests need a checkout without an account?": "Partially"}},
            headers=_auth("req-secret"),
        )

    assert response.status_code == 200
    assert response.json()["state"] == "ANALYZING"


async def test_clarifications_for_unknown_project_is_404(
    live_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{live_server}/api/v1/projects/prj_missing/clarifications",
            json={"answers": {"q": "a"}},
            headers=_auth("req-secret"),
        )

    assert response.status_code == 404


async def test_clarifications_require_auth(live_server: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{live_server}/api/v1/projects/prj_missing/clarifications",
            json={"answers": {"q": "a"}},
        )

    assert response.status_code == 401


async def test_tasks_of_unknown_project_is_404(live_server: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{live_server}/api/v1/projects/prj_missing/tasks", headers=_auth("req-secret")
        )

    assert response.status_code == 404
