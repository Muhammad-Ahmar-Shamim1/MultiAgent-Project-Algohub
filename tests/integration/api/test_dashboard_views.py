"""Dashboard read endpoints (FR-160..FR-163): overview, tables, project views, artifacts.

The live server reads committed rows, and the session-wide test DB also holds other files'
committed rows - so these assertions are about this file's own rows (filtered by its own
project), never absolute totals.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web_department.collaboration.events import EventType, emit
from web_department.config.settings import get_settings
from web_department.db.repositories import (
    AgentRepository,
    BlueprintRepository,
    DeploymentRepository,
    KnowledgeEntryRepository,
    ProjectRepository,
    RequirementSpecRepository,
    TaskRepository,
    WorkstationRepository,
)
from web_department.db.states import AgentState, ProjectState, TaskState
from web_department.security.approvals import ActionType, create_request


@pytest.fixture(autouse=True)
def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUESTER_TOKEN", "req-secret")
    monkeypatch.setenv("APPROVER_TOKEN", "appr-secret")
    get_settings.cache_clear()


async def _get(live_server: str, path: str, token: str = "req-secret", **params: Any) -> Any:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{live_server}{path}", params=params, headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
async def world(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> dict[str, str]:
    """A committed project with tasks in every interesting state, an agent, a workstation with
    artifacts, a pending approval, a deployment, a knowledge entry and an alert.
    """
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "qa-report.json").write_text(json.dumps({"passed": 4, "failed": 1}))
    (tmp_path / "qa" / "screenshots").mkdir(parents=True)
    (tmp_path / "qa" / "screenshots" / "home.png").write_bytes(b"\x89PNG-fake")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "secret.py").write_text("KEY = 'not an artifact'")

    async with session_factory() as s:
        projects = ProjectRepository(s)
        project = await projects.create(title="Dashboard shop", requester_id="u-1")
        for project_state in (ProjectState.ANALYZING, ProjectState.PLANNING):
            await projects.update_state(project.id, project_state)
        await RequirementSpecRepository(s).create(
            project_id=project.id,
            version=1,
            json={"project_type": "web", "open_questions": ["Which payment provider?"]},
            created_by="test",
        )
        tasks = TaskRepository(s)
        ids: dict[str, str] = {"project": project.id}
        for number, (key, path) in enumerate(
            (
                ("ready", [TaskState.READY]),
                (
                    "blocked",
                    [TaskState.READY, TaskState.ASSIGNED, TaskState.IN_PROGRESS, TaskState.BLOCKED],
                ),
                (
                    "failed",
                    [
                        TaskState.READY,
                        TaskState.ASSIGNED,
                        TaskState.IN_PROGRESS,
                        TaskState.REVIEW,
                        TaskState.FIX_REQUIRED,
                    ],
                ),
            ),  # fmt: skip
            start=1,
        ):
            task = await tasks.create(
                display_id=f"WEB-{number:03d}",
                project_id=project.id,
                title=f"{key} task",
                role="backend" if key != "ready" else "frontend",
                inputs={},
                acceptance_checks=["x"],
            )
            for state in path:
                await tasks.update_state(task.id, state)
            ids[key] = task.id
        await tasks.add_dependency(
            await tasks.get(ids["failed"]),  # type: ignore[arg-type]
            await tasks.get(ids["ready"]),  # type: ignore[arg-type]
        )
        await BlueprintRepository(s).upsert(id="dash-test-bp", version="1", yaml="y", checksum="c")
        agent = await AgentRepository(s).create(
            display_id=f"WEB-DASHT-{project.id[-8:]}",
            blueprint_id="dash-test-bp",
            blueprint_version="1",
            project_id=project.id,
        )
        await AgentRepository(s).update_state(agent.id, AgentState.PROVISIONED)
        await WorkstationRepository(s).create(
            project_id=project.id,
            container_id="c-1",
            repo_path=str(tmp_path),
            limits={"cpu": 2},
            status="running",
        )
        approval = await create_request(
            s, project.id, ActionType.PRODUCTION_DEPLOY, "Deploy v1", {"diff": None}
        )
        await DeploymentRepository(s).create(
            project_id=project.id, version="1", commit_sha="abc", target="local", status="ok"
        )
        await KnowledgeEntryRepository(s).create(
            layer="project", project_id=project.id, content="Use Stripe test mode.", source="t"
        )
        await emit(
            EventType.ALERT_RAISED,
            session=s,
            project_id=project.id,
            payload={"reason": "test_retries_exhausted"},
        )
        await s.commit()
        ids.update(agent=agent.id, approval=approval.id)
        return ids


async def test_whoami_reports_the_caller_s_role(live_server: str) -> None:
    assert await _get(live_server, "/api/v1/whoami", "appr-secret") == {"role": "approver"}


async def test_overview_counts_and_keeps_blocked_failed_and_pending_visible(
    live_server: str, world: dict[str, str]
) -> None:
    overview = await _get(live_server, "/api/v1/overview")

    assert overview["approvals_pending"] >= 1
    assert overview["tasks_blocked"] >= 1
    assert overview["tasks_failed"] >= 1
    assert overview["tasks_pending"] >= 1
    assert overview["projects_active"] >= 1
    assert overview["deployments"] >= 1
    mine = {(a["kind"], a["id"]) for a in overview["attention"]}
    assert ("approval_pending", world["approval"]) in mine
    assert ("task_blocked", world["blocked"]) in mine
    assert ("task_failed", world["failed"]) in mine


async def test_tables_filter_by_project_and_state(live_server: str, world: dict[str, str]) -> None:
    project = world["project"]

    tasks = await _get(live_server, "/api/v1/tasks", project_id=project)
    blocked = await _get(live_server, "/api/v1/tasks", project_id=project, state="BLOCKED")
    frontend = await _get(live_server, "/api/v1/tasks", project_id=project, role="frontend")
    agents = await _get(live_server, "/api/v1/agents", project_id=project, state="PROVISIONED")
    workstations = await _get(live_server, "/api/v1/workstations", project_id=project)
    knowledge = await _get(live_server, "/api/v1/knowledge", project_id=project, layer="project")
    deployments = await _get(live_server, "/api/v1/deployments", project_id=project)
    alerts = await _get(live_server, "/api/v1/monitoring/alerts", project_id=project)
    planning = await _get(live_server, "/api/v1/projects", state="PLANNING")

    assert len(tasks) == 3
    assert [t["id"] for t in blocked] == [world["blocked"]]
    assert [t["id"] for t in frontend] == [world["ready"]]
    assert [a["id"] for a in agents] == [world["agent"]]
    assert workstations[0]["limits"] == {"cpu": 2}
    assert knowledge[0]["excerpt"] == "Use Stripe test mode."
    assert deployments[0]["commit_sha"] == "abc"
    assert alerts[0]["event"] == "ALERT_RAISED"
    assert project in [p["id"] for p in planning]


async def test_project_views_requirements_plan_timeline_and_reports(
    live_server: str, world: dict[str, str]
) -> None:
    base = f"/api/v1/projects/{world['project']}"

    requirements = await _get(live_server, f"{base}/requirements")
    plan = await _get(live_server, f"{base}/plan")
    timeline = await _get(live_server, f"{base}/timeline")
    reports = await _get(live_server, f"{base}/reports")

    assert requirements["open_questions"] == ["Which payment provider?"]
    assert plan["mermaid"].startswith("flowchart TD")
    assert "WEB-001 --> WEB-003" in plan["mermaid"]
    assert [(e["previous"], e["state"]) for e in timeline] == [
        ("RECEIVED", "ANALYZING"),
        ("ANALYZING", "PLANNING"),
    ]
    assert reports == {"qa": {"passed": 4, "failed": 1}, "security": None}


async def test_artifacts_serve_only_artifact_paths(live_server: str, world: dict[str, str]) -> None:
    base = f"{live_server}/api/v1/projects/{world['project']}/artifacts"
    headers = {"Authorization": "Bearer req-secret"}
    async with httpx.AsyncClient(timeout=15) as client:
        screenshot = await client.get(f"{base}/qa/screenshots/home.png", headers=headers)
        not_artifact = await client.get(f"{base}/backend/secret.py", headers=headers)
        escape = await client.get(f"{base}/..%2F..%2Fetc%2Fpasswd", headers=headers)

    assert screenshot.status_code == 200
    assert screenshot.headers["content-type"] == "image/png"
    assert screenshot.content == b"\x89PNG-fake"
    assert not_artifact.status_code == 404
    assert escape.status_code == 404


async def test_requirements_404_before_analysis(
    live_server: str, committed_project_id: str
) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{live_server}/api/v1/projects/{committed_project_id}/requirements",
            headers={"Authorization": "Bearer req-secret"},
        )

    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/api/v1/overview", "/api/v1/tasks", "/api/v1/whoami"])
async def test_dashboard_views_need_a_token(live_server: str, path: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        assert (await client.get(f"{live_server}{path}")).status_code == 401
