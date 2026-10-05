"""Wire the router's Redis client and DB session factory to this test's isolated infrastructure."""

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from web_department.db import base
from web_department.llm import router


@pytest.fixture(autouse=True)
def _wire_router_to_test_infra(
    engine: AsyncEngine, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Point router.get_redis() and its DB logging at the test Redis db and test database."""
    monkeypatch.setattr(base, "get_engine", lambda: engine)
    router.set_redis(redis_client)
    yield
    router.set_redis(None)


@pytest_asyncio.fixture(autouse=True)
async def _clean_llm_calls(engine: AsyncEngine) -> AsyncIterator[None]:
    """Empty ``llm_calls`` before each test.

    ``_log_llm_call`` uses its own ``session_scope()`` transaction (so a logging failure never
    blocks the caller), which really commits - unlike the ``session`` fixture other tests use,
    which always rolls back. Without this, rows from earlier tests in this session-scoped test
    database would leak into later assertions.
    """
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE llm_calls"))
    yield
