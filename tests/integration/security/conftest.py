"""Point session_scope() (security/approvals.py, security/evidence.py) at the test DB."""

from collections.abc import Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from web_department.db import base


@pytest.fixture(autouse=True)
def _wire_db_to_test_engine(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(base, "get_engine", lambda: engine)
    yield
