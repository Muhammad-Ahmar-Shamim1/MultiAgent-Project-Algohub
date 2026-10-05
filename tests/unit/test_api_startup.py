"""API startup validates configuration (fail fast, SRS NFR-006/ENV rules)."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from web_department.api.main import app
from web_department.config.settings import get_settings


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ENV", "DEBUG"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


def test_startup_succeeds_with_valid_config(capsys: pytest.CaptureFixture[str]) -> None:
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert "api_started" in capsys.readouterr().out


def test_startup_fails_clearly_for_prod_with_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("DEBUG", "true")

    with pytest.raises(ValidationError, match="DEBUG=true is not allowed when ENV=prod"):
        with TestClient(app):
            pass
