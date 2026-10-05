"""GET/PUT /api/v1/blueprints (operator-only, FR-040..FR-043)."""

import httpx
import pytest

from web_department.config.settings import get_settings


@pytest.fixture(autouse=True)
def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUESTER_TOKEN", "req-secret")
    monkeypatch.setenv("OPERATOR_TOKEN", "op-secret")
    get_settings.cache_clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_list_blueprints_requires_operator_role(live_server: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(f"{live_server}/api/v1/blueprints", headers=_auth("req-secret"))

    assert response.status_code == 403


async def test_list_blueprints_returns_all_nine_shipped_blueprints(live_server: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(f"{live_server}/api/v1/blueprints", headers=_auth("op-secret"))

    assert response.status_code == 200
    ids = {b["id"] for b in response.json()}
    assert ids == {
        "web_architect",
        "frontend_developer",
        "backend_developer",
        "database_developer",
        "api_integration",
        "qa_engineer",
        "security_engineer",
        "devops_engineer",
        "maintenance",
    }


async def test_get_blueprint_by_id(live_server: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{live_server}/api/v1/blueprints/backend_developer", headers=_auth("op-secret")
        )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "backend_developer"
    assert body["role"] == "backend"


async def test_get_blueprint_404s_for_an_unknown_id(live_server: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{live_server}/api/v1/blueprints/does-not-exist", headers=_auth("op-secret")
        )

    assert response.status_code == 404


async def _get_backend_blueprint(client: httpx.AsyncClient, live_server: str) -> dict:  # type: ignore[type-arg]
    response = await client.get(
        f"{live_server}/api/v1/blueprints/backend_developer", headers=_auth("op-secret")
    )
    body: dict = response.json()  # type: ignore[type-arg]
    return body


async def test_put_bumps_the_version_and_the_change_is_visible_on_a_later_get(
    live_server: str,
) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        current = await _get_backend_blueprint(client, live_server)
        current["version"] = "1.0.1"
        current["responsibilities"] = [*current["responsibilities"], "A new responsibility"]

        put_response = await client.put(
            f"{live_server}/api/v1/blueprints/backend_developer",
            json=current,
            headers=_auth("op-secret"),
        )
        get_response = await client.get(
            f"{live_server}/api/v1/blueprints/backend_developer", headers=_auth("op-secret")
        )

    assert put_response.status_code == 200
    assert put_response.json()["version"] == "1.0.1"
    assert get_response.json()["version"] == "1.0.1"
    assert "A new responsibility" in get_response.json()["responsibilities"]


async def test_put_rejects_a_version_that_does_not_bump(live_server: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        current = await _get_backend_blueprint(client, live_server)

        response = await client.put(
            f"{live_server}/api/v1/blueprints/backend_developer",
            json=current,
            headers=_auth("op-secret"),
        )

    assert response.status_code == 409


async def test_put_rejects_a_path_id_body_id_mismatch(live_server: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        current = await _get_backend_blueprint(client, live_server)
        current["version"] = "1.0.1"

        response = await client.put(
            f"{live_server}/api/v1/blueprints/database_developer",
            json=current,
            headers=_auth("op-secret"),
        )

    assert response.status_code == 422


async def test_put_rejects_permissions_not_matching_the_role(live_server: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        current = await _get_backend_blueprint(client, live_server)
        current["version"] = "1.0.1"
        current["permissions"] = ["deploy"]

        response = await client.put(
            f"{live_server}/api/v1/blueprints/backend_developer",
            json=current,
            headers=_auth("op-secret"),
        )

    assert response.status_code == 422


async def test_put_requires_operator_role(live_server: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        current = await _get_backend_blueprint(client, live_server)
        current["version"] = "1.0.1"

        response = await client.put(
            f"{live_server}/api/v1/blueprints/backend_developer",
            json=current,
            headers=_auth("req-secret"),
        )

    assert response.status_code == 403
