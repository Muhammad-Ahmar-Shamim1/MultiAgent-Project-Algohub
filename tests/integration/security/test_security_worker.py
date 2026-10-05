"""SecurityWorker + security gate on a fixture app with a planted secret and an open admin route."""

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.agents.security import agent as security_agent_module
from web_department.agents.security.agent import SecurityReview, SecurityWorker
from web_department.collaboration.events import EventBus
from web_department.db.models import Agent, Event, Project, Task, Workstation
from web_department.db.repositories import (
    AgentRepository,
    ApprovalRepository,
    BlueprintRepository,
    TaskRepository,
    WorkstationRepository,
)
from web_department.db.states import TaskState
from web_department.llm.router import AllProvidersFailedError, CompletionResult, Purpose
from web_department.security.scanners import Finding
from web_department.security.security_gate import EXCEPTION_ACTION
from web_department.workstation.manager import ExecResult

PLANTED_SECRET = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
CONFIG_PY = f'"""App settings."""\n\nAPP_NAME = "shop"\nSTRIPE_SECRET_KEY = "{PLANTED_SECRET}"\n'
ADMIN_PY = """from fastapi import APIRouter

router = APIRouter(prefix="/api/admin")

@router.delete("/users/{user_id}")
async def delete_user(user_id: int) -> dict[str, bool]:
    return {"deleted": True}
"""
PRODUCTS_PY = """from fastapi import APIRouter, Depends

from app.auth import current_user

router = APIRouter(prefix="/api/products")

@router.get("")
async def list_products(user=Depends(current_user)) -> list[str]:
    return []
"""


@pytest.fixture
def fixture_app(tmp_path: Path) -> Path:
    routers = tmp_path / "backend" / "app" / "routers"
    routers.mkdir(parents=True)
    (tmp_path / "backend" / "app" / "config.py").write_text(CONFIG_PY)
    (routers / "admin.py").write_text(ADMIN_PY)
    (routers / "products.py").write_text(PRODUCTS_PY)
    (tmp_path / "backend" / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / "backend" / ".venv" / "lib" / "site.py").write_text("IGNORED_BY_REVIEW = 1")
    return tmp_path


@pytest.fixture
async def ws(session: AsyncSession, project: Project, fixture_app: Path) -> Workstation:
    return await WorkstationRepository(session).create(
        project_id=project.id,
        container_id="fake-container",
        repo_path=str(fixture_app),
        limits={},
        status="running",
    )


@pytest.fixture
async def agent(session: AsyncSession, project: Project) -> Agent:
    await BlueprintRepository(session).create(id="sec-test-bp", version="1", yaml="y", checksum="c")
    return await AgentRepository(session).create(
        display_id="WEB-SECT-001",
        blueprint_id="sec-test-bp",
        blueprint_version="1",
        project_id=project.id,
    )


async def _security_task(session: AsyncSession, project: Project, number: int = 30) -> Task:
    return await TaskRepository(session).create(
        display_id=f"WEB-{number:03d}",
        project_id=project.id,
        title="Security review",
        role="security",
        state=TaskState.ASSIGNED,
        inputs={},
        acceptance_checks=["No High or Critical severity findings remain open"],
    )


def fake_workstation(root: Path, *, broken: frozenset[str] = frozenset()) -> AsyncMock:
    """Stands in for the scanners: gitleaks really scans ``root`` for the planted key pattern;
    the others report nothing. Tools named in ``broken`` crash without writing output.
    """
    out = root / "artifacts" / "security"

    def write(name: str, data: Any) -> None:
        out.mkdir(parents=True, exist_ok=True)
        (out / name).write_text(json.dumps(data))

    async def exec_(_ws: Workstation, cmd: list[str], timeout: float | None = None) -> ExecResult:
        program = cmd[0]
        if program in broken:
            return ExecResult(2, "", f"{program}: crashed", False, False)
        if program == "semgrep":
            write("semgrep.json", {"results": [], "errors": []})
        elif program == "gitleaks":
            leaks = []
            for path in sorted((root / "backend" / "app").rglob("*.py")):
                for number, line in enumerate(path.read_text().splitlines(), start=1):
                    if re.search(r"sk_live_[0-9a-zA-Z]{24}", line):
                        leaks.append(
                            {
                                "RuleID": "stripe-access-token",
                                "Description": "Stripe Access Token",
                                "File": path.relative_to(root).as_posix(),
                                "StartLine": number,
                                "Secret": "REDACTED",  # --redact
                                "Match": "REDACTED",
                            }
                        )
            write("gitleaks.json", leaks)
        elif program == "node":
            write("npm-audit.json", {"vulnerabilities": {}})
        elif program == "pip-audit":
            write("pip-audit.json", {"dependencies": []})
        elif program == "trivy":
            write("trivy-fs.json", {"Results": []})
        return ExecResult(0, "", "", False, False)

    manager = AsyncMock()
    manager.exec.side_effect = exec_
    return manager


def _review_of(prompt: str) -> SecurityReview:
    """A reviewer that flags the admin route only if the prompt really shows it unprotected."""
    findings: list[dict[str, Any]] = []
    admin = prompt.split("--- backend/app/routers/admin.py ---", 1)
    if len(admin) == 2 and "Depends(" not in admin[1].split("---", 1)[0]:
        findings.append(
            {
                "checklist_item": "authz",
                "severity": "high",
                "file": "backend/app/routers/admin.py",
                "line": 5,
                "message": "DELETE /api/admin/users/{user_id} has no server-side auth check.",
            }
        )
    return SecurityReview.model_validate({"findings": findings})


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    async def complete(messages: list[dict[str, str]], **kwargs: Any) -> CompletionResult:
        review = _review_of(messages[0]["content"])
        return CompletionResult(
            content=review.model_dump_json(),
            parsed=review,
            provider="test",
            model="test/model",
            cached=False,
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
        )

    mock = AsyncMock(side_effect=complete)
    monkeypatch.setattr(security_agent_module, "complete", mock)
    return mock


async def _events(session: AsyncSession, project: Project, type_: str) -> list[Event]:
    stmt = select(Event).where(Event.project_id == project.id, Event.type == type_)
    return list((await session.scalars(stmt)).all())


async def test_planted_secret_and_open_admin_route_block_the_gate_with_two_findings(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    agent: Agent,
    fixture_app: Path,
    fake_llm: AsyncMock,
) -> None:
    task = await _security_task(session, project)
    worker = SecurityWorker(session, agent, {}, ws, fake_workstation(fixture_app), bus=bus)

    result = await worker.run(task)

    assert result.success is False
    assert (await TaskRepository(session).get(task.id)).state is TaskState.BLOCKED  # type: ignore[union-attr]

    report = json.loads((fixture_app / "artifacts" / "security-report.json").read_text())
    assert report["passed"] is False
    blocking = [(f["tool"], f["rule"], f["file"], f["line"]) for f in report["blocking"]]
    assert blocking == [
        ("gitleaks", "stripe-access-token", "backend/app/config.py", 4),
        ("llm-review", "checklist:authz", "backend/app/routers/admin.py", 5),
    ]
    statuses = {s["tool"]: s["status"] for s in report["scanners"]}
    assert statuses == {
        "semgrep": "ok",
        "gitleaks": "ok",
        "npm-audit": "ok",
        "pip-audit": "ok",
        "trivy-fs": "ok",
        "trivy-image": "skipped",  # no exported image in the workstation
        "llm-review": "ok",
    }

    # One READY fix task per blocking finding, for the role that owns the file.
    fixes = [await TaskRepository(session).get(i) for i in report["fix_task_ids"]]
    assert [(t.role, t.state) for t in fixes if t] == [
        ("backend", TaskState.READY),
        ("backend", TaskState.READY),
    ]
    assert fixes[1] is not None and "no server-side auth" in fixes[1].inputs["fix_context"]

    blocked = await _events(session, project, "SECURITY_BLOCKED")
    assert len(blocked) == 1 and blocked[0].requires_review is True

    # The review ran on the private provider only, and the secret never reached the reports.
    assert fake_llm.await_args_list[0].kwargs["confidential"] is True
    assert fake_llm.await_args_list[0].kwargs["purpose"] is Purpose.REVIEW
    for name in ("security-report.json", "security-report.md"):
        assert PLANTED_SECRET not in (fixture_app / "artifacts" / name).read_text()


async def test_review_context_has_app_code_with_line_numbers_but_not_dependencies(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    agent: Agent,
    fixture_app: Path,
    fake_llm: AsyncMock,
) -> None:
    task = await _security_task(session, project)

    await SecurityWorker(session, agent, {}, ws, fake_workstation(fixture_app), bus=bus).run(task)

    prompt = fake_llm.await_args_list[0].args[0][0]["content"]
    assert "--- backend/app/routers/products.py ---" in prompt
    assert '   5 | @router.delete("/users/{user_id}")' in prompt
    assert "IGNORED_BY_REVIEW" not in prompt
    assert "authz: Server-side authorization" in prompt


async def test_approved_exceptions_let_the_gate_pass(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    agent: Agent,
    fixture_app: Path,
    fake_llm: AsyncMock,
) -> None:
    for finding in (
        Finding(
            tool="gitleaks",
            rule="stripe-access-token",
            severity="high",
            file="backend/app/config.py",
            line=4,
            message="",
        ),
        Finding(
            tool="llm-review",
            rule="checklist:authz",
            severity="high",
            file="backend/app/routers/admin.py",
            line=5,
            message="",
        ),
    ):
        await ApprovalRepository(session).create(
            project_id=project.id,
            action_type=EXCEPTION_ACTION,
            summary=json.dumps({"fingerprint": finding.fingerprint}),
            status="approved",
            approver_id="approver-1",
            comment="test-only key; admin route is behind the VPN",
        )
    task = await _security_task(session, project)

    result = await SecurityWorker(
        session, agent, {}, ws, fake_workstation(fixture_app), bus=bus
    ).run(task)

    assert result.success is True
    assert (await TaskRepository(session).get(task.id)).state is TaskState.REVIEW  # type: ignore[union-attr]
    report = json.loads((fixture_app / "artifacts" / "security-report.json").read_text())
    assert len(report["excepted"]) == 2
    assert report["fix_task_ids"] == []


async def test_a_rerun_reuses_the_open_fix_tasks(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    agent: Agent,
    fixture_app: Path,
    fake_llm: AsyncMock,
) -> None:
    first = await _security_task(session, project, 30)
    await SecurityWorker(session, agent, {}, ws, fake_workstation(fixture_app), bus=bus).run(first)
    first_ids = json.loads((fixture_app / "artifacts" / "security-report.json").read_text())[
        "fix_task_ids"
    ]
    second = await _security_task(session, project, 40)

    await SecurityWorker(session, agent, {}, ws, fake_workstation(fixture_app), bus=bus).run(second)

    second_ids = json.loads((fixture_app / "artifacts" / "security-report.json").read_text())[
        "fix_task_ids"
    ]
    assert second_ids == first_ids


async def test_a_scanner_that_crashes_blocks_the_gate(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    agent: Agent,
    fixture_app: Path,
    fake_llm: AsyncMock,
) -> None:
    (fixture_app / "backend" / "app" / "config.py").write_text('APP_NAME = "shop"\n')
    (fixture_app / "backend" / "app" / "routers" / "admin.py").unlink()
    task = await _security_task(session, project)
    manager = fake_workstation(fixture_app, broken=frozenset({"semgrep"}))

    result = await SecurityWorker(session, agent, {}, ws, manager, bus=bus).run(task)

    assert result.success is False
    assert "scanner(s) failed to run: ['semgrep']" in result.summary


async def test_an_unavailable_reviewer_model_blocks_rather_than_passing(
    session: AsyncSession,
    bus: EventBus,
    project: Project,
    ws: Workstation,
    agent: Agent,
    fixture_app: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (fixture_app / "backend" / "app" / "config.py").write_text('APP_NAME = "shop"\n')
    monkeypatch.setattr(
        security_agent_module,
        "complete",
        AsyncMock(side_effect=AllProvidersFailedError("no private provider is up")),
    )
    task = await _security_task(session, project)

    result = await SecurityWorker(
        session, agent, {}, ws, fake_workstation(fixture_app), bus=bus
    ).run(task)

    assert result.success is False
    assert "llm-review" in result.summary
