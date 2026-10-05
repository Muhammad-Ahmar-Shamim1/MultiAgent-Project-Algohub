"""POST /projects/{id}/pause, /resume, /cancel (operator-only, SRS 8.2)."""

from collections.abc import Awaitable
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from redis.asyncio import Redis

from web_department.config.settings import get_settings
from web_department.llm.router import CompletionResult
from web_department.orchestrator import runner
from web_department.orchestrator import web_agent as web_agent_module
from web_department.orchestrator.web_agent import ScopeClassification

ECOMMERCE_PAYLOAD = {
    "title": "Clothing e-commerce",
    "description": "An online clothing store with a cart and checkout.",
    "constraints": "Use the golden-v1 stack.",
    "requester_id": "u-42",
}


@pytest.fixture(autouse=True)
def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUESTER_TOKEN", "req-secret")
    monkeypatch.setenv("OPERATOR_TOKEN", "op-secret")
    get_settings.cache_clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _submitted_project(live_server: str, monkeypatch: pytest.MonkeyPatch) -> str:
    result = CompletionResult(
        content="{}",
        parsed=ScopeClassification(is_web=True, reason="because"),
        provider="test",
        model="test/model",
        cached=False,
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
    )
    monkeypatch.setattr(web_agent_module, "complete", AsyncMock(return_value=result))
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{live_server}/api/v1/projects", json=ECOMMERCE_PAYLOAD, headers=_auth("req-secret")
        )
    project_id: str = response.json()["id"]
    return project_id


async def test_pause_requires_operator_role(
    live_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = await _submitted_project(live_server, monkeypatch)

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{live_server}/api/v1/projects/{project_id}/pause", headers=_auth("req-secret")
        )

    assert response.status_code == 403


async def test_pause_sets_the_control_flag_for_the_runner(
    live_server: str, monkeypatch: pytest.MonkeyPatch, redis_client: Redis
) -> None:
    project_id = await _submitted_project(live_server, monkeypatch)

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{live_server}/api/v1/projects/{project_id}/pause", headers=_auth("op-secret")
        )

    assert response.status_code == 200
    assert response.json()["state"] == "RECEIVED"  # pause never touches ProjectState
    assert await redis_client.get(f"{runner.CONTROL_KEY_PREFIX}{project_id}") == "paused"


async def test_resume_clears_the_flag_and_requeues_the_project(
    live_server: str, monkeypatch: pytest.MonkeyPatch, redis_client: Redis
) -> None:
    project_id = await _submitted_project(live_server, monkeypatch)
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"{live_server}/api/v1/projects/{project_id}/pause", headers=_auth("op-secret")
        )

        response = await client.post(
            f"{live_server}/api/v1/projects/{project_id}/resume", headers=_auth("op-secret")
        )

    assert response.status_code == 200
    assert await redis_client.get(f"{runner.CONTROL_KEY_PREFIX}{project_id}") is None
    queued = await cast(Awaitable[list[str]], redis_client.lrange(runner.PROJECT_QUEUE, 0, -1))
    assert queued.count(project_id) >= 1


async def test_cancel_marks_the_project_failed_and_sets_the_control_flag(
    live_server: str, monkeypatch: pytest.MonkeyPatch, redis_client: Redis
) -> None:
    project_id = await _submitted_project(live_server, monkeypatch)

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{live_server}/api/v1/projects/{project_id}/cancel", headers=_auth("op-secret")
        )

    assert response.status_code == 200
    assert response.json()["state"] == "FAILED"
    assert await redis_client.get(f"{runner.CONTROL_KEY_PREFIX}{project_id}") == "cancelled"


async def test_cancel_of_an_unknown_project_is_404(
    live_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{live_server}/api/v1/projects/prj_missing/cancel", headers=_auth("op-secret")
        )

    assert response.status_code == 404


async def test_pause_of_an_unknown_project_is_404(
    live_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{live_server}/api/v1/projects/prj_missing/pause", headers=_auth("op-secret")
        )

    assert response.status_code == 404
