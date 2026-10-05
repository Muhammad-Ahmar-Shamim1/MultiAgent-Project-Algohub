"""Smoke test for the API's liveness endpoint."""

from fastapi.testclient import TestClient

from web_department.api.main import __version__, app

client = TestClient(app)


def test_health_returns_ok_status_and_version() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}
