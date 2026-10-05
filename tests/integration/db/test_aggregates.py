"""Remaining aggregates, schema conventions (DR-004) and session handling."""

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from web_department import db  # noqa: F401
from web_department.db import base
from web_department.db.models import Blueprint, Project
from web_department.db.repositories import (
    AgentRepository,
    BlueprintRepository,
    DeploymentRepository,
    KnowledgeEntryRepository,
    LLMCallRepository,
    ProjectRepository,
    RequirementSpecRepository,
    TaskRepository,
    WorkstationRepository,
)
from web_department.db.states import AgentState, InvalidTransition

PROJECT_OWNED = [
    "requirement_specs",
    "tasks",
    "workstations",
    "events",
    "approvals",
    "deployments",
    "agents",
    "knowledge_entries",
    "llm_calls",
]


async def _blueprint(session: AsyncSession) -> Blueprint:
    return await BlueprintRepository(session).create(
        id="frontend_developer", version="1.0.0", yaml="id: frontend_developer", checksum="abc123"
    )


async def test_blueprints_are_keyed_by_id_and_version(session: AsyncSession) -> None:
    repo = BlueprintRepository(session)
    v1 = await _blueprint(session)
    v2 = await repo.create(id="frontend_developer", version="1.1.0", yaml="v2", checksum="def")

    assert await repo.get_version("frontend_developer", "1.0.0") is v1
    assert await repo.get_version("frontend_developer", "1.1.0") is v2
    assert await repo.get_version("frontend_developer", "9.9.9") is None
    assert v1.created_at is not None


async def test_agent_lifecycle_and_blueprint_reference(session: AsyncSession) -> None:
    await _blueprint(session)
    repo = AgentRepository(session)

    agent = await repo.create(
        display_id="WEB-FE-001", blueprint_id="frontend_developer", blueprint_version="1.0.0"
    )
    for state in (AgentState.PROVISIONED, AgentState.AVAILABLE, AgentState.ASSIGNED):
        await repo.update_state(agent.id, state)

    assert agent.id.startswith("agt_")
    assert agent.project_id is None
    assert agent.state is AgentState.ASSIGNED
    with pytest.raises(InvalidTransition):
        await repo.update_state(agent.id, AgentState.RETIRED)


async def test_agent_requires_an_existing_blueprint_version(session: AsyncSession) -> None:
    await _blueprint(session)

    with pytest.raises(IntegrityError):
        await AgentRepository(session).create(
            display_id="WEB-FE-002", blueprint_id="frontend_developer", blueprint_version="9.9.9"
        )


async def test_task_can_be_assigned_to_an_agent(session: AsyncSession, project: Project) -> None:
    await _blueprint(session)
    agent = await AgentRepository(session).create(
        display_id="WEB-FE-001",
        blueprint_id="frontend_developer",
        blueprint_version="1.0.0",
        project_id=project.id,
    )
    task = await TaskRepository(session).create(
        display_id="WEB-001",
        project_id=project.id,
        title="Login page",
        role="frontend",
        assigned_agent_id=agent.id,
    )

    assert task.assigned_agent_id == agent.id


async def test_requirement_specs_are_versioned_per_project(
    session: AsyncSession, project: Project
) -> None:
    repo = RequirementSpecRepository(session)
    await repo.create(
        project_id=project.id, version=1, json={"domain": "todo"}, created_by="analyzer"
    )
    v2 = await repo.create(
        project_id=project.id,
        version=2,
        json={"domain": "todo", "roles": ["user"]},
        created_by="u-1",
    )

    latest = await repo.latest_for_project(project.id)

    assert latest is v2
    assert latest.json == {"domain": "todo", "roles": ["user"]}
    assert await repo.latest_for_project("prj_missing") is None
    await session.commit()
    with pytest.raises(IntegrityError):
        await repo.create(project_id=project.id, version=2, json={}, created_by="x")


async def test_workstation_deployment_and_llm_call(session: AsyncSession, project: Project) -> None:
    workstation = await WorkstationRepository(session).create(
        project_id=project.id,
        repo_path="/workspaces/p1",
        limits={"cpu": 2, "mem": "4g"},
        status="provisioning",
    )
    deployment = await DeploymentRepository(session).create(
        project_id=project.id,
        version="1.0.0",
        commit_sha="a" * 40,
        target="local-docker",
        status="pending",
    )
    call = await LLMCallRepository(session).create(
        project_id=project.id,
        provider="ollama",
        model="qwen3-coder",
        tokens_in=100,
        tokens_out=50,
        latency_ms=850,
        status="ok",
    )

    assert workstation.id.startswith("wks_")
    assert workstation.container_id is None
    assert workstation.limits == {"cpu": 2, "mem": "4g"}
    assert deployment.id.startswith("dpl_")
    assert deployment.health is None
    assert call.id.startswith("llm_")
    assert call.cached is False


async def test_knowledge_embedding_roundtrip_768_dims(session: AsyncSession) -> None:
    repo = KnowledgeEntryRepository(session)
    vector = [i / 768 for i in range(768)]
    entry = await repo.create(
        layer="web", content="Use server components", source="docs", embedding=vector
    )
    await session.commit()
    session.expunge_all()

    reloaded = await repo.get(entry.id)

    assert reloaded is not None
    assert reloaded.id.startswith("knw_")
    assert reloaded.project_id is None
    assert reloaded.version == 1
    assert len(reloaded.embedding) == 768
    assert reloaded.embedding[10] == pytest.approx(10 / 768, rel=1e-5)


async def test_knowledge_embedding_is_optional_and_searchable(session: AsyncSession) -> None:
    repo = KnowledgeEntryRepository(session)
    await repo.create(layer="global", content="no vector", source="sop")
    near = await repo.create(layer="web", content="near", source="s", embedding=[1.0] + [0.0] * 767)
    far = await repo.create(layer="web", content="far", source="s", embedding=[0.0] * 767 + [1.0])

    stmt = (
        select(base.Base.metadata.tables["knowledge_entries"].c.id)
        .where(base.Base.metadata.tables["knowledge_entries"].c.embedding.is_not(None))
        .order_by(
            base.Base.metadata.tables["knowledge_entries"].c.embedding.l2_distance(
                [1.0] + [0.0] * 767
            )
        )
    )
    ids = list((await session.scalars(stmt)).all())

    assert ids[:2] == [near.id, far.id]


def _project_columns(sync_conn: Connection) -> dict[str, tuple[bool, bool]]:
    inspector = inspect(sync_conn)
    result: dict[str, tuple[bool, bool]] = {}
    for table in PROJECT_OWNED:
        has_fk = any(
            fk["constrained_columns"] == ["project_id"] and fk["referred_table"] == "projects"
            for fk in inspector.get_foreign_keys(table)
        )
        has_index = any(ix["column_names"] == ["project_id"] for ix in inspector.get_indexes(table))
        result[table] = (has_fk, has_index)
    return result


async def test_every_project_owned_table_has_project_fk_and_index(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        columns = await conn.run_sync(_project_columns)

    assert columns == {table: (True, True) for table in PROJECT_OWNED}


async def test_session_scope_commits_on_success_and_rolls_back_on_error(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(base, "get_engine", lambda: engine)

    async with base.session_scope() as scoped:
        kept = await ProjectRepository(scoped).create(title="kept", requester_id="scope-test")
    with pytest.raises(RuntimeError, match="boom"):
        async with base.session_scope() as scoped:
            dropped = await ProjectRepository(scoped).create(
                title="dropped", requester_id="scope-test"
            )
            raise RuntimeError("boom")

    async with base.session_scope() as check:
        repo = ProjectRepository(check)
        assert await repo.get(kept.id) is not None
        assert await repo.get(dropped.id) is None
