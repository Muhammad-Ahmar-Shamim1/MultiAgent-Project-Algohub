"""Helpers to build throwaway databases for integration tests (real Postgres + pgvector)."""

import asyncio
from pathlib import Path

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from web_department.config.settings import get_settings

ROOT = Path(__file__).resolve().parents[3]


def scratch_db_url(suffix: str) -> URL:
    """URL of ``<configured database>_<suffix>`` on the configured server."""
    base = make_url(get_settings().database_url)
    return base.set(database=f"{base.database}_{suffix}")


async def _recreate(url: URL) -> None:
    admin = create_async_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        await admin.dispose()


def recreate_database(url: URL) -> None:
    """Drop and recreate the database named in ``url`` (blocking; do not call inside a loop)."""
    asyncio.run(_recreate(url))


def alembic_config(url: URL) -> Config:
    """Alembic config that migrates the database in ``url``."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "web_department" / "db" / "migrations"))
    rendered = url.render_as_string(hide_password=False).replace("%", "%%")
    config.set_main_option("sqlalchemy.url", rendered)
    return config
