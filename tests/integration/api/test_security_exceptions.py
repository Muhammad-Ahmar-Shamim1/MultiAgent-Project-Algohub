"""POST /api/v1/security/exceptions (FR-113): approver-only, recorded on the audit log."""

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web_department.config.settings import get_settings
from web_department.db.models import Approval, Event
from web_department.security.scanners import fingerprint
from web_department.security.security_gate import EXCEPTION_ACTION, approved_exceptions


@pytest.fixture(autouse=True)
def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUESTER_TOKEN", "req-secret")
    monkeypatch.setenv("APPROVER_TOKEN", "appr-secret")
    get_settings.cache_clear()


def _body(project_id: str) -> dict[str, object]:
    return {
        "project_id": project_id,
        "tool": "gitleaks",
        "rule": "stripe-access-token",
        "file": "backend/app/config.py",
        "line": 4,
        "justification": "Stripe test-mode key, rotated before launch.",
        "approver_id": "approver-7",
    }


async def _post(live_server: str, body: dict[str, object], token: str | None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=15) as client:
        return await client.post(
            f"{live_server}/api/v1/security/exceptions", json=body, headers=headers
        )


async def test_an_approver_records_an_exception_on_the_audit_log(
    live_server: str,
    committed_project_id: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await _post(live_server, _body(committed_project_id), "appr-secret")

    assert response.status_code == 201
    body = response.json()
    expected = fingerprint("gitleaks", "stripe-access-token", "backend/app/config.py", 4)
    assert body["fingerprint"] == expected
    assert body["approver_id"] == "approver-7"
    async with session_factory() as session:
        approval = await session.get(Approval, body["id"])
        assert approval is not None
        assert approval.action_type == EXCEPTION_ACTION
        assert approval.status == "approved"
        assert approval.comment == "Stripe test-mode key, rotated before launch."
        events = (
            await session.scalars(
                select(Event).where(
                    Event.project_id == committed_project_id, Event.type == "APPROVED"
                )
            )
        ).all()
        assert [e.payload["fingerprint"] for e in events] == [expected]
        # ...and the security gate now treats that finding as accepted.
        assert await approved_exceptions(session, committed_project_id) == {expected}


async def test_a_requester_cannot_grant_an_exception(
    live_server: str, committed_project_id: str
) -> None:
    response = await _post(live_server, _body(committed_project_id), "req-secret")

    assert response.status_code == 403


async def test_an_exception_needs_a_token(live_server: str, committed_project_id: str) -> None:
    assert (await _post(live_server, _body(committed_project_id), None)).status_code == 401


async def test_an_exception_for_an_unknown_project_is_404(live_server: str) -> None:
    response = await _post(live_server, _body("prj_doesnotexist"), "appr-secret")

    assert response.status_code == 404


async def test_an_exception_needs_a_real_justification(
    live_server: str, committed_project_id: str
) -> None:
    body = {**_body(committed_project_id), "justification": "ok"}

    assert (await _post(live_server, body, "appr-secret")).status_code == 422
