"""Shared integration fixtures: migrated test DB, rolled-back session, Redis db 15, event bus."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from alembic import command
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tests.integration.db.dbutil import alembic_config, recreate_database, scratch_db_url
from web_department.collaboration.events import EventBus
from web_department.config.settings import get_settings
from web_department.db.base import create_engine
from web_department.db.models import Project
from web_department.db.repositories import ProjectRepository

TEST_REDIS_DB = 15


@pytest.fixture(scope="session")
def test_db_url() -> URL:
    """Create `<db>_test` from scratch and migrate it to head (exercises the real migrations)."""
    url = scratch_db_url("test")
    try:
        recreate_database(url)
    except (OSError, DBAPIError) as exc:
        pytest.fail(
            f"Cannot reach Postgres for integration tests ({exc!r}). "
            "Start it with `docker compose up -d postgres` and check DATABASE_URL.",
            pytrace=False,
        )
    command.upgrade(alembic_config(url), "head")
    return url


@pytest_asyncio.fixture
async def engine(test_db_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(test_db_url.render_as_string(hide_password=False))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction that is always rolled back."""
    async with engine.connect() as connection:
        outer = await connection.begin()
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as db_session:
            yield db_session
        await outer.rollback()


@pytest_asyncio.fixture
async def project(session: AsyncSession) -> Project:
    return await ProjectRepository(session).create(title="Todo app with login", requester_id="u-1")


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """Redis db 15 (reserved for tests), emptied before and after each test."""
    client = Redis.from_url(get_settings().redis_url, db=TEST_REDIS_DB, decode_responses=True)
    try:
        await client.flushdb()
    except (RedisError, OSError) as exc:
        pytest.fail(
            f"Cannot reach Redis for integration tests ({exc!r}). "
            "Start it with `docker compose up -d redis` and check REDIS_URL.",
            pytrace=False,
        )
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def bus(redis_client: Redis, session_factory: async_sessionmaker[AsyncSession]) -> EventBus:
    return EventBus(redis_client, session_factory)


@pytest_asyncio.fixture
async def committed_project_id(session_factory: async_sessionmaker[AsyncSession]) -> str:
    """A project committed to the test DB, for code paths that use their own transaction."""
    async with session_factory() as own:
        created = await ProjectRepository(own).create(title="Committed", requester_id="u-1")
        await own.commit()
        return created.id
