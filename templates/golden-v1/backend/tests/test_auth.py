"""register / login / me."""

from httpx import AsyncClient


async def test_register_then_login_then_me(client: AsyncClient) -> None:
    register = await client.post(
        "/api/auth/register", json={"email": "a@example.com", "password": "hunter22"}
    )
    assert register.status_code == 201
    assert register.json()["email"] == "a@example.com"

    login = await client.post(
        "/api/auth/login", json={"email": "a@example.com", "password": "hunter22"}
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "a@example.com"


async def test_register_with_a_taken_email_is_rejected(client: AsyncClient) -> None:
    payload = {"email": "dup@example.com", "password": "hunter22"}
    await client.post("/api/auth/register", json=payload)

    response = await client.post("/api/auth/register", json=payload)

    assert response.status_code == 409


async def test_login_with_the_wrong_password_is_rejected(client: AsyncClient) -> None:
    await client.post("/api/auth/register", json={"email": "b@example.com", "password": "hunter22"})

    response = await client.post(
        "/api/auth/login", json={"email": "b@example.com", "password": "wrong-password"}
    )

    assert response.status_code == 401


async def test_me_without_a_token_is_unauthorized(client: AsyncClient) -> None:
    response = await client.get("/api/auth/me")

    assert response.status_code == 401
