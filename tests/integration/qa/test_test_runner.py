"""qa/test_runner.run_qa / save_report: stages run in the workstation, JUnit -> QAReport."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus
from web_department.db.models import Agent, Project, Workstation
from web_department.db.repositories import (
    AgentRepository,
    BlueprintRepository,
    WorkstationRepository,
)
from web_department.orchestrator.wrs_schema import Feature, WebRequirementsSpec
from web_department.qa.test_runner import (
    REPORT_JSON_PATH,
    REPORT_MD_PATH,
    Failure,
    criteria_from_wrs,
    run_qa,
    save_report,
)
from web_department.workstation.manager import ExecResult

WRS = WebRequirementsSpec(
    project_type="web_application",
    domain="ecommerce",
    roles=["customer"],
    features=[
        Feature(id="F-01", name="auth", description="Log in", acceptance=["User can log in"]),
        Feature(
            id="F-02",
            name="cart",
            description="Cart",
            acceptance=["Cart total updates", "Empty cart shows a message"],
        ),
    ],
)

BACKEND_JUNIT = """<testsuites><testsuite name="pytest">
<testcase classname="tests.test_auth" name="test_F01_login" time="0.1"/>
<testcase classname="tests.test_cart" name="test_F02_1_total" time="0.2">
  <failure message="assert 10 == 12">app/routers/cart.py:42: AssertionError</failure>
</testcase>
</testsuite></testsuites>"""
VITEST_JUNIT = (
    '<testsuites><testsuite name="v"><testcase name="renders" classname="a"/>'
    "</testsuite></testsuites>"
)
E2E_JUNIT = (
    '<testsuites><testsuite name="cart.spec.ts">'
    '<testcase name="F02_2 empty cart message" classname="cart.spec.ts" time="1"/>'
    "</testsuite></testsuites>"
)


@pytest.fixture
async def ws(session: AsyncSession, project: Project, tmp_path: Path) -> Workstation:
    return await WorkstationRepository(session).create(
        project_id=project.id,
        container_id="fake-container",
        repo_path=str(tmp_path),
        limits={},
        status="running",
    )


@pytest.fixture
async def agent(session: AsyncSession, project: Project) -> Agent:
    await BlueprintRepository(session).create(id="qa-run-bp", version="1", yaml="y", checksum="c")
    return await AgentRepository(session).create(
        display_id="WEB-QARUN-001",
        blueprint_id="qa-run-bp",
        blueprint_version="1",
        project_id=project.id,
    )


def _fake_workstation(tmp_path: Path, outcomes: dict[str, tuple[int, str | None]]) -> AsyncMock:
    """A manager whose exec writes each step's JUnit file, keyed by a substring of the command."""
    manager = AsyncMock()

    async def exec_(_ws: Workstation, cmd: list[str], timeout: float | None = None) -> ExecResult:
        joined = " ".join(cmd)
        for key, (exit_code, junit) in outcomes.items():
            if key in joined:
                if junit is not None:
                    target = {
                        "pytest": "unit-backend.xml",
                        "test:unit": "unit-frontend.xml",
                        "run e2e": "e2e.xml",
                    }[key]
                    (tmp_path / "artifacts" / "junit" / target).write_text(junit)
                return ExecResult(exit_code, f"ran {key}", "", False, False)
        return ExecResult(0, "", "", False, False)

    manager.exec.side_effect = exec_
    return manager


async def test_run_qa_runs_every_stage_and_builds_the_report(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, tmp_path: Path
) -> None:
    manager = _fake_workstation(
        tmp_path,
        {
            "ruff": (1, None),  # lint fails: a stage failure without JUnit
            "pytest": (1, BACKEND_JUNIT),
            "test:unit": (0, VITEST_JUNIT),
            "run e2e": (0, E2E_JUNIT),
        },
    )
    suspects: list[str] = []

    async def suspect(failure: Failure) -> str | None:
        suspects.append(failure.test)
        return "tsk_responsible"

    report = await run_qa(session, ws, agent.id, manager, WRS, suspect=suspect, bus=bus)

    commands = [" ".join(c.args[1]) for c in manager.exec.await_args_list]
    assert len(commands) == 7  # lint x2, typecheck x2, unit x2, e2e; no integration/regression
    assert not any(
        c.startswith(
            (
                "backend/.venv/bin/pytest backend/tests/integration",
                "backend/.venv/bin/pytest backend/tests/regression",
            )
        )
        for c in commands
    )
    assert (report.passed, report.failed, report.skipped) == (3, 2, 0)
    assert report.criteria_coverage == {"F-01.1": "pass", "F-02.1": "fail", "F-02.2": "pass"}
    assert [f.test for f in report.failures] == [
        "tests.test_cart::test_F02_1_total",
        "lint: backend/.venv/bin/ruff check backend/app backend/tests",
    ]
    assert report.failures[0].file == "backend/tests/test_cart.py"
    assert all(f.suspected_task_id == "tsk_responsible" for f in report.failures)
    assert len(suspects) == 2


async def test_run_qa_runs_integration_and_regression_when_their_dirs_exist(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, tmp_path: Path
) -> None:
    (tmp_path / "backend" / "tests" / "integration").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "regression").mkdir(parents=True)
    manager = _fake_workstation(tmp_path, {})

    await run_qa(session, ws, agent.id, manager, WRS, bus=bus)

    commands = [" ".join(c.args[1]) for c in manager.exec.await_args_list]
    assert any(c.startswith("backend/.venv/bin/pytest backend/tests/integration") for c in commands)
    assert any(c.startswith("backend/.venv/bin/pytest backend/tests/regression") for c in commands)


async def test_a_crashed_test_step_is_a_failure_and_a_stale_report_is_not_reused(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, tmp_path: Path
) -> None:
    # A previous run's e2e report claimed everything passed; this run's e2e crashes outright.
    (tmp_path / "artifacts" / "junit").mkdir(parents=True)
    (tmp_path / "artifacts" / "junit" / "e2e.xml").write_text(E2E_JUNIT)
    manager = _fake_workstation(tmp_path, {"run e2e": (1, None)})

    report = await run_qa(session, ws, agent.id, manager, WRS, bus=bus)

    assert report.criteria_coverage["F-02.2"] == "missing"
    assert [f.test for f in report.failures] == ["e2e: npm --prefix frontend run e2e"]
    assert "exit 1" in report.failures[0].message


async def test_pytest_collecting_no_tests_is_not_a_failure(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, tmp_path: Path
) -> None:
    manager = _fake_workstation(tmp_path, {"pytest": (5, None)})

    report = await run_qa(session, ws, agent.id, manager, WRS, bus=bus)

    assert report.failures == []
    assert set(report.criteria_coverage.values()) == {"missing"}


async def test_save_report_writes_json_and_markdown_artifacts(
    session: AsyncSession, bus: EventBus, ws: Workstation, agent: Agent, tmp_path: Path
) -> None:
    manager = _fake_workstation(tmp_path, {"pytest": (1, BACKEND_JUNIT)})
    report = await run_qa(session, ws, agent.id, manager, WRS, bus=bus)

    paths = await save_report(session, ws, agent.id, report, criteria_from_wrs(WRS), {}, bus=bus)

    assert paths == (REPORT_JSON_PATH, REPORT_MD_PATH)
    saved = json.loads((tmp_path / REPORT_JSON_PATH).read_text())
    assert saved["criteria_coverage"]["F-02.1"] == "fail"
    assert saved["failures"][0]["test"] == "tests.test_cart::test_F02_1_total"
    assert (tmp_path / REPORT_MD_PATH).read_text().startswith("# QA report - FAIL")
