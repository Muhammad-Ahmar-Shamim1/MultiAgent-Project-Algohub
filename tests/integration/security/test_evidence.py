"""security/evidence.build_evidence (FR-131): diff, QA/security reports, screenshots, plan."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web_department.collaboration.events import EventBus, set_event_bus
from web_department.db.repositories import WorkstationRepository
from web_department.security.evidence import build_evidence
from web_department.workstation import tools
from web_department.workstation.manager import ExecResult

GIT = {
    "rev-parse": "c0ffee1234\n",
    "rev-list": "abc0000001\n",
    "diff": " backend/app/main.py | 12 ++++\n 1 file changed, 12 insertions(+)\n",
    "log": "c0ffee1 WEB-012: Wire up feature: F-01\nabc0000 Initial template\n",
}


@pytest_asyncio.fixture(autouse=True)
async def _wire_default_event_bus(bus: EventBus) -> AsyncIterator[None]:
    """build_evidence uses session_scope(), which dispatches through the process-wide bus - point
    it at this test's loop (same reason as tests/integration/orchestrator/test_graph.py).
    """
    set_event_bus(bus)
    yield
    set_event_bus(None)


@pytest.fixture
def fake_git(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    async def run_command(*args: object, **_: object) -> ExecResult:
        command = args[5]
        assert isinstance(command, list)
        return ExecResult(0, GIT[command[1]], "", False, False)

    mock = AsyncMock(side_effect=run_command)
    monkeypatch.setattr(tools, "run_command", mock)
    return mock


async def test_evidence_bundles_diff_reports_screenshots_and_plan(
    session_factory: async_sessionmaker[AsyncSession],
    committed_project_id: str,
    tmp_path: Path,
    fake_git: AsyncMock,
) -> None:
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "qa-report.json").write_text(json.dumps({"passed": 9, "failed": 0}))
    (tmp_path / "artifacts" / "security-report.json").write_text(json.dumps({"passed": True}))
    (tmp_path / "qa" / "screenshots").mkdir(parents=True)
    (tmp_path / "qa" / "screenshots" / "cart.png").write_bytes(b"\x89PNG")
    (tmp_path / "qa" / "screenshots" / "notes.txt").write_text("not an image")
    async with session_factory() as own:
        await WorkstationRepository(own).create(
            project_id=committed_project_id,
            container_id="fake",
            repo_path=str(tmp_path),
            limits={},
            status="running",
        )
        await own.commit()

    evidence = await build_evidence(committed_project_id)

    assert evidence["diff"] == {
        "base": "abc0000001",
        "head": "c0ffee1234",
        "stat": GIT["diff"].strip(),
        "commits": ["c0ffee1 WEB-012: Wire up feature: F-01", "abc0000 Initial template"],
    }
    assert evidence["qa_report"] == {"passed": 9, "failed": 0}
    assert evidence["security_report"] == {"passed": True}
    assert evidence["screenshots"] == ["qa/screenshots/cart.png"]
    assert evidence["deployment_plan"]["commit"] == "c0ffee1234"
    assert evidence["deployment_plan"]["status"] == "draft"
    diff_call = [c.args[5] for c in fake_git.await_args_list if c.args[5][1] == "diff"][0]
    assert diff_call == ["git", "diff", "--stat", "abc0000001", "HEAD"]


async def test_missing_pieces_become_notes_not_errors(
    committed_project_id: str, fake_git: AsyncMock
) -> None:
    evidence = await build_evidence(committed_project_id)  # no workstation at all

    assert evidence["diff"] is None
    assert evidence["qa_report"] is None
    assert evidence["note"] == "no workstation provisioned - no code to show"
    assert evidence["deployment_plan"]["target"] == "local-docker"
    fake_git.assert_not_awaited()
