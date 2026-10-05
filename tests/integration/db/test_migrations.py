"""The initial migration upgrades, downgrades and re-upgrades cleanly on an empty database."""

import asyncio

from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.db.dbutil import alembic_config, recreate_database, scratch_db_url

EXPECTED_TABLES = {
    "projects",
    "requirement_specs",
    "tasks",
    "task_dependencies",
    "blueprints",
    "agents",
    "workstations",
    "events",
    "approvals",
    "deployments",
    "knowledge_entries",
    "llm_calls",
    "war_rooms",
    "war_room_members",
}


async def _snapshot(url: URL) -> dict[str, set[str]]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tables = await conn.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            extensions = await conn.scalars(text("SELECT extname FROM pg_extension"))
            functions = await conn.scalars(
                text("SELECT proname FROM pg_proc WHERE proname = 'reject_mutation'")
            )
            triggers = await conn.scalars(
                text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
            )
            return {
                "tables": set(tables),
                "extensions": set(extensions),
                "functions": set(functions),
                "triggers": set(triggers),
            }
    finally:
        await engine.dispose()


async def test_upgrade_downgrade_upgrade_roundtrip() -> None:
    url = scratch_db_url("migration")
    config = alembic_config(url)
    await asyncio.to_thread(recreate_database, url)

    await asyncio.to_thread(command.upgrade, config, "head")
    upgraded = await _snapshot(url)

    assert EXPECTED_TABLES <= upgraded["tables"]
    assert "vector" in upgraded["extensions"]
    assert upgraded["functions"] == {"reject_mutation"}
    assert upgraded["triggers"] == {"events_append_only", "approvals_append_only"}

    await asyncio.to_thread(command.downgrade, config, "base")
    downgraded = await _snapshot(url)

    assert downgraded["tables"] == {"alembic_version"}
    assert "vector" not in downgraded["extensions"]
    assert downgraded["functions"] == set()
    assert downgraded["triggers"] == set()

    await asyncio.to_thread(command.upgrade, config, "head")
    assert EXPECTED_TABLES <= (await _snapshot(url))["tables"]
