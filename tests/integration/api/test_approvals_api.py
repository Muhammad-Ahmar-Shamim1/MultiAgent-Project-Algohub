"""/api/v1/approvals (FR-130..FR-132): view requests with evidence; Approver-only decisions."""

from collections.abc import Mapping
from typing import Any, cast

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web_department.config.settings import get_settings
from web_department.db.models import Approval, Event
from web_department.orchestrator.runner import PROJECT_QUEUE
from web_department.security import approvals
from web_department.security.approvals import ActionType


@pytest.fixture(autouse=True)
def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUESTER_TOKEN", "req-secret")
    monkeypatch.setenv("APPROVER_TOKEN", "appr-secret")
    monkeypatch.setenv("OPERATOR_TOKEN", "op-secret")
    get_settings.cache_clear()


@pytest.fixture
async def request_id(
    session_factory: async_sessionmaker[AsyncSession], committed_project_id: str
) -> str:
    async with session_factory() as own:
        approval = await approvals.create_request(
            own,
            committed_project_id,
            ActionType.PRODUCTION_DEPLOY,
            "Deploy this build",
            {"diff": {"stat": "3 files changed"}, "qa_report": {"passed": 12, "failed": 0}},
        )
        await own.commit()
        return approval.id


async def _post(
    live_server: str, approval_id: str, body: Mapping[str, object], token: str
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15) as client:
        return await client.post(
            f"{live_server}/api/v1/approvals/{approval_id}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )


def _approve(comment: str | None = None) -> dict[str, object]:
    return {"decision": "approve", "comment": comment, "approver_id": "approver-1"}


@pytest.mark.parametrize("token", ["req-secret", "op-secret"])
async def test_only_an_approver_can_decide(live_server: str, request_id: str, token: str) -> None:
    response = await _post(live_server, request_id, _approve(), token)

    assert response.status_code == 403


async def test_approving_records_the_decision_and_requeues_the_project(
    live_server: str,
    request_id: str,
    committed_project_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    response = await _post(live_server, request_id, _approve("looks good"), "appr-secret")

    assert response.status_code == 200
    assert response.json() == {
        "approval_id": request_id,
        "decision": "approve",
        "comment": "looks good",
        "approver_id": "approver-1",
    }
    async with session_factory() as own:
        decision_row = (
            await own.scalars(select(Approval).where(Approval.decides_id == request_id))
        ).one()
        assert decision_row.status == "approved"
        assert decision_row.decided_at is not None
        events = (
            await own.scalars(
                select(Event).where(
                    Event.project_id == committed_project_id, Event.type == "APPROVED"
                )
            )
        ).all()
        assert [e.payload["approval_id"] for e in events] == [request_id]
    queued = await cast(Any, redis_client.lrange(PROJECT_QUEUE, 0, -1))
    assert committed_project_id in queued


async def test_a_rejection_needs_a_comment(live_server: str, request_id: str) -> None:
    body = {"decision": "reject", "approver_id": "approver-1"}

    assert (await _post(live_server, request_id, body, "appr-secret")).status_code == 422


async def test_rejecting_with_a_comment_emits_rejected(
    live_server: str,
    request_id: str,
    committed_project_id: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    body = {"decision": "reject", "comment": "Add rate limiting", "approver_id": "approver-1"}

    response = await _post(live_server, request_id, body, "appr-secret")

    assert response.status_code == 200
    async with session_factory() as own:
        decision = await approvals.decision_for(own, request_id)
        assert decision is not None
        assert decision.decision is approvals.Decision.REJECT
        assert decision.comment == "Add rate limiting"


async def test_a_request_can_only_be_decided_once(live_server: str, request_id: str) -> None:
    assert (await _post(live_server, request_id, _approve(), "appr-secret")).status_code == 200

    second = await _post(live_server, request_id, _approve(), "appr-secret")

    assert second.status_code == 409


async def test_deciding_an_unknown_request_is_404(live_server: str) -> None:
    response = await _post(live_server, "apr_doesnotexist", _approve(), "appr-secret")

    assert response.status_code == 404


async def test_get_shows_the_evidence_and_then_the_decision(
    live_server: str, request_id: str, committed_project_id: str
) -> None:
    headers = {"Authorization": "Bearer req-secret"}
    async with httpx.AsyncClient(timeout=15) as client:
        before = await client.get(f"{live_server}/api/v1/approvals/{request_id}", headers=headers)
        pending = await client.get(
            f"{live_server}/api/v1/approvals",
            params={"project_id": committed_project_id, "pending": "true"},
            headers=headers,
        )
    assert before.status_code == 200
    assert before.json()["evidence"]["qa_report"] == {"passed": 12, "failed": 0}
    assert before.json()["decision"] is None
    assert [a["id"] for a in pending.json()] == [request_id]

    await _post(live_server, request_id, _approve(), "appr-secret")

    async with httpx.AsyncClient(timeout=15) as client:
        after = await client.get(f"{live_server}/api/v1/approvals/{request_id}", headers=headers)
        pending_after = await client.get(
            f"{live_server}/api/v1/approvals",
            params={"project_id": committed_project_id, "pending": "true"},
            headers=headers,
        )
    assert after.json()["decision"]["decision"] == "approve"
    assert pending_after.json() == []
