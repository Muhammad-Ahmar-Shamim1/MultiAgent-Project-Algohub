"""Bearer-token auth: role resolution and require_role (SRS 2.3)."""

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from web_department.api.auth import UserRole, get_current_role, require_role
from web_department.config.settings import get_settings


@pytest.fixture(autouse=True)
def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUESTER_TOKEN", "req-secret")
    monkeypatch.setenv("APPROVER_TOKEN", "app-secret")
    monkeypatch.setenv("OPERATOR_TOKEN", "")  # blank means disabled
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(role: Annotated[UserRole, Depends(get_current_role)]) -> dict[str, str]:
        return {"role": role.value}

    @app.get("/operators-only")
    async def operators_only(
        role: Annotated[UserRole, Depends(require_role(UserRole.OPERATOR))],
    ) -> dict[str, str]:
        return {"role": role.value}

    @app.get("/requester-or-approver")
    async def requester_or_approver(
        role: Annotated[UserRole, Depends(require_role(UserRole.REQUESTER, UserRole.APPROVER))],
    ) -> dict[str, str]:
        return {"role": role.value}

    return TestClient(app)


def test_valid_requester_token_resolves_to_requester_role(client: TestClient) -> None:
    response = client.get("/whoami", headers={"Authorization": "Bearer req-secret"})

    assert response.status_code == 200
    assert response.json() == {"role": "requester"}


def test_valid_approver_token_resolves_to_approver_role(client: TestClient) -> None:
    response = client.get("/whoami", headers={"Authorization": "Bearer app-secret"})

    assert response.json() == {"role": "approver"}


def test_missing_token_is_401(client: TestClient) -> None:
    response = client.get("/whoami")

    assert response.status_code == 401


def test_invalid_token_is_401(client: TestClient) -> None:
    response = client.get("/whoami", headers={"Authorization": "Bearer nope"})

    assert response.status_code == 401


def test_disabled_role_token_never_matches(client: TestClient) -> None:
    response = client.get("/whoami", headers={"Authorization": "Bearer "})

    assert response.status_code == 401


def test_require_role_allows_the_matching_role(client: TestClient) -> None:
    response = client.get("/operators-only", headers={"Authorization": "Bearer req-secret"})

    assert response.status_code == 403


def test_require_role_rejects_a_role_not_in_the_allow_list(client: TestClient) -> None:
    response = client.get("/requester-or-approver", headers={"Authorization": "Bearer req-secret"})

    assert response.status_code == 200
    assert response.json() == {"role": "requester"}


def test_require_role_still_401s_before_checking_the_role(client: TestClient) -> None:
    response = client.get("/operators-only", headers={"Authorization": "Bearer garbage"})

    assert response.status_code == 401
