"""GET /api/v1/projects/{id}/war-room (FR-080, FR-162)."""

from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web_department.blueprints.registry import BLUEPRINTS_DIR, BlueprintRegistry, load_all
from web_department.collaboration import war_room
from web_department.config.settings import get_settings
from web_department.db.repositories import (
    BlueprintRepository,
    ProjectRepository,
    TaskRepository,
    WorkstationRepository,
)
from web_department.orchestrator import agent_factory


@pytest.fixture(autouse=True)
def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUESTER_TOKEN", "req-secret")
    get_settings.cache_clear()


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer req-secret"}


@pytest.fixture
def distinct_agent_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent display ids no other test file uses (``WEB-WRAPIFE-001``, not ``WEB-FE-001``).

    This file commits its rows (the live server reads them through its own connection), and
    committed agents/blueprints stay in the session-wide test DB - other files assert exact ids
    like ``WEB-FE-001`` and create the real ``frontend_developer`` blueprint themselves.
    """
    for role, code in list(agent_factory.ROLE_CODE.items()):
        monkeypatch.setitem(agent_factory.ROLE_CODE, role, f"WRAPI{code}")


async def _registry(session: AsyncSession) -> BlueprintRegistry:
    """The shipped blueprints under distinct ids, upserted (see ``distinct_agent_ids``)."""
    specs = [
        spec.model_copy(update={"id": f"warroom-api-{spec.id}"})
        for spec, _raw, _checksum in load_all(BLUEPRINTS_DIR).values()
    ]
    repo = BlueprintRepository(session)
    for spec in specs:
        await repo.upsert(id=spec.id, version=spec.version, yaml="test", checksum="test")
    return BlueprintRegistry(specs)


async def _project_with_war_room(
    session_factory: async_sessionmaker[AsyncSession], workspace: Path
) -> str:
    """A committed project with two tasks, a workstation and an open war room."""
    async with session_factory() as session:
        registry = await _registry(session)
        project = await ProjectRepository(session).create(title="War room", requester_id="u-1")
        tasks = TaskRepository(session)
        for number, role in ((1, "backend"), (2, "frontend")):
            await tasks.create(
                display_id=f"WEB-{number:03d}",
                project_id=project.id,
                title=f"{role} task",
                role=role,
                inputs={},
                acceptance_checks=["works"],
            )
        await WorkstationRepository(session).create(
            project_id=project.id,
            container_id="fake",
            repo_path=str(workspace),
            limits={},
            status="running",
        )
        await war_room.open(session, registry, project.id)
        await session.commit()
        return project.id


@pytest.mark.usefixtures("distinct_agent_ids")
async def test_get_war_room_returns_team_board_events_and_artifacts(
    live_server: str, session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    (tmp_path / "openapi.yaml").write_text("openapi: 3.1.0")
    project_id = await _project_with_war_room(session_factory, tmp_path)

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{live_server}/api/v1/projects/{project_id}/war-room", headers=_auth()
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "open"
    assert body["required_roles"] == ["frontend", "backend"]
    assert [m["role"] for m in body["members"]] == ["frontend", "backend"]
    assert all(m["state"] == "AVAILABLE" for m in body["members"])
    assert [t["display_id"] for t in body["task_board"]["CREATED"]] == ["WEB-001", "WEB-002"]
    assert body["task_board"]["COMPLETED"] == []
    assert body["events"][0]["event"] == "WAR_ROOM_OPENED"
    assert body["artifacts"] == [
        {"path": "openapi.yaml", "kind": "openapi", "size_bytes": 14, "sha256": None}
    ]


async def test_get_war_room_is_404_when_the_project_has_none(
    live_server: str, committed_project_id: str
) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{live_server}/api/v1/projects/{committed_project_id}/war-room", headers=_auth()
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Project has no war room"


async def test_get_war_room_is_404_for_an_unknown_project(live_server: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{live_server}/api/v1/projects/prj_doesnotexist/war-room", headers=_auth()
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Project not found"


async def test_get_war_room_requires_a_token(live_server: str, committed_project_id: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{live_server}/api/v1/projects/{committed_project_id}/war-room"
        )

    assert response.status_code == 401
