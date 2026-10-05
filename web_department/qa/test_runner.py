"""QA test runner (FR-100, FR-101): run every check stage inside the workstation, parse the JUnit
XML results into a ``QAReport`` with acceptance-criteria coverage, and save it as artifacts.

Acceptance criteria (FR-101): the WRS gives each feature a list of acceptance strings with no ids
of their own, so a criterion's id is ``<feature id>.<1-based index>`` - ``F-02.1`` is F-02's first
criterion. A test covers a criterion when its name carries the criterion's *tag*: the feature id
without punctuation, then ``_<index>``, e.g. ``test_F02_1_cart_total_updates`` (pytest) or
``"F02_1 cart total updates"`` (Playwright). For a feature with exactly one criterion the index
may be left out - ``test_F02_cart_total_updates`` - matching the convention's original example.

Stages (all through ``workstation/tools.py``, as the QA role): lint, typecheck, unit, integration,
e2e, regression. Test stages write JUnit XML under ``artifacts/junit/``; lint/typecheck have no
JUnit output, so a non-zero exit there becomes a failure carrying the command's output. A test
stage that exits non-zero without reporting any failing test (a crash, a collection error) is a
failure too - "the suite didn't run" never counts as passing. Integration and regression run only
when their directory exists (``backend/tests/integration``, ``backend/tests/regression`` - the
regression convention is where a fix's reproducing test is kept).

JUnit files come from the generated app's own tools; they are parsed with the standard library's
expat-based ElementTree, which never resolves external entities and (Python 3.11+, expat 2.4+)
rejects entity-expansion bombs.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus
from web_department.config.permissions import Role
from web_department.db.models import Workstation
from web_department.orchestrator.wrs_schema import WebRequirementsSpec
from web_department.workstation import tools
from web_department.workstation.manager import WorkstationManager

ROLE = Role.QA
JUNIT_DIR = "artifacts/junit"
REPORT_JSON_PATH = "artifacts/qa-report.json"
REPORT_MD_PATH = "artifacts/qa-report.md"
STAGE_TIMEOUT_SECONDS = 900
_OUTPUT_TAIL = 2000

CoverageStatus = Literal["pass", "fail", "missing"]


# --- criteria --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Criterion:
    """One WRS acceptance criterion, with the id tests are tagged by."""

    id: str
    feature_id: str
    index: int
    text: str


def criteria_from_wrs(wrs: WebRequirementsSpec, feature_id: str | None = None) -> list[Criterion]:
    """Every acceptance criterion in ``wrs`` (or only ``feature_id``'s), in WRS order."""
    return [
        Criterion(id=f"{feature.id}.{index}", feature_id=feature.id, index=index, text=text)
        for feature in wrs.features
        if feature_id is None or feature.id == feature_id
        for index, text in enumerate(feature.acceptance, start=1)
    ]


def criterion_tag(criterion: Criterion) -> str:
    """The token a test name must contain to cover ``criterion``, e.g. ``F02_1``."""
    return f"{_compact(criterion.feature_id)}_{criterion.index}"


def _compact(feature_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", feature_id)


# A tag: letters+digits (the feature id, compacted), optionally "_<index>", starting the name or
# after an underscore/space, and ending the name or followed by a separator ("_", space, ".", ":",
# "-", or a pytest parametrize "[").
_TAG = re.compile(r"(?:^|[_\s])([A-Za-z]+)(\d+)(?:_(\d+))?(?=[_\s.:\-\[]|$)")


def criteria_for_test(test_name: str, criteria: Iterable[Criterion]) -> list[Criterion]:
    """The criteria ``test_name`` is tagged with (feature numbers compare numerically)."""
    by_feature: dict[tuple[str, int], list[Criterion]] = {}
    for criterion in criteria:
        match = re.fullmatch(r"([A-Za-z]+)(\d+)", _compact(criterion.feature_id))
        if match:
            key = (match[1].upper(), int(match[2]))
            by_feature.setdefault(key, []).append(criterion)

    found: list[Criterion] = []
    for match in _TAG.finditer(test_name):
        feature = by_feature.get((match[1].upper(), int(match[2])))
        if not feature:
            continue
        if match[3] is not None:
            found += [c for c in feature if c.index == int(match[3])]
        elif len(feature) == 1:
            found += feature
    return list(dict.fromkeys(found))


# --- report model ----------------------------------------------------------------------------


class Failure(BaseModel):
    """One failing test (or failing non-test stage, e.g. lint)."""

    test: str
    message: str
    file: str | None = None
    suspected_task_id: str | None = None


class QAReport(BaseModel):
    """The result of one QA run (FR-100, FR-101)."""

    passed: int = 0
    failed: int = 0
    skipped: int = 0
    duration: float = 0.0
    failures: list[Failure] = Field(default_factory=list)
    criteria_coverage: dict[str, CoverageStatus] = Field(default_factory=dict)


@dataclass(frozen=True)
class TestCase:
    """One ``<testcase>`` from a JUnit file."""

    __test__ = False  # not a pytest test class, despite the name

    name: str
    classname: str
    file: str | None
    outcome: Literal["passed", "failed", "skipped"]
    message: str
    duration: float

    @property
    def full_name(self) -> str:
        """``classname::name`` (pytest's node id shape), or just the name."""
        return f"{self.classname}::{self.name}" if self.classname else self.name


def parse_junit(xml_text: str, *, file_prefix: str = "") -> list[TestCase]:
    """Every ``<testcase>`` in a JUnit document (``<testsuites>`` or a bare ``<testsuite>``).

    ``file`` is the testcase's own ``file`` attribute, else its suite's (Playwright puts the spec
    file there), else derived from a pytest ``classname`` (``tests.test_cart`` ->
    ``tests/test_cart.py``); ``file_prefix`` (e.g. ``backend/``) makes it workspace-relative.
    Raises ``ET.ParseError`` on malformed XML.
    """
    root = ET.fromstring(xml_text)
    suites = [root] if root.tag == "testsuite" else root.iter("testsuite")
    cases: list[TestCase] = []
    for suite in suites:
        for case in suite.findall("testcase"):
            classname = case.get("classname", "")
            file = case.get("file") or suite.get("file") or _file_from_classname(classname)
            outcome: Literal["passed", "failed", "skipped"] = "passed"
            message = ""
            problem = case.find("failure")
            if problem is None:
                problem = case.find("error")
            if problem is not None:
                outcome = "failed"
                message = (problem.get("message") or "") + "\n" + (problem.text or "")
            elif case.find("skipped") is not None:
                outcome = "skipped"
            cases.append(
                TestCase(
                    name=case.get("name", ""),
                    classname=classname,
                    file=f"{file_prefix}{file}" if file else None,
                    outcome=outcome,
                    message=message.strip(),
                    duration=float(case.get("time") or 0),
                )
            )
    return cases


def _file_from_classname(classname: str) -> str | None:
    if not classname or "." not in classname or classname.startswith("."):
        return None
    module = classname.split("::")[0]
    parts = module.split(".")
    # pytest classnames are the module path, plus the class name for methods in a test class.
    if parts[-1][:1].isupper():
        parts = parts[:-1]
    return "/".join(parts) + ".py"


def coverage(criteria: Iterable[Criterion], cases: Iterable[TestCase]) -> dict[str, CoverageStatus]:
    """Per criterion: ``fail`` if any tagged test failed, ``pass`` if at least one tagged test
    passed and none failed, ``missing`` if no tagged test ran (skipped-only counts as missing).
    """
    criteria = list(criteria)
    outcomes: dict[str, set[str]] = {c.id: set() for c in criteria}
    for case in cases:
        for criterion in criteria_for_test(case.name, criteria):
            outcomes[criterion.id].add(case.outcome)
    result: dict[str, CoverageStatus] = {}
    for criterion_id, seen in outcomes.items():
        if "failed" in seen:
            result[criterion_id] = "fail"
        elif "passed" in seen:
            result[criterion_id] = "pass"
        else:
            result[criterion_id] = "missing"
    return result


def build_report(
    cases: list[TestCase],
    stage_failures: list[Failure],
    criteria: list[Criterion],
    duration: float,
) -> QAReport:
    """Combine parsed test cases and non-test stage failures into one ``QAReport``."""
    failures = [
        Failure(test=c.full_name, message=c.message[-_OUTPUT_TAIL:], file=c.file)
        for c in cases
        if c.outcome == "failed"
    ]
    return QAReport(
        passed=sum(c.outcome == "passed" for c in cases),
        failed=len(failures) + len(stage_failures),
        skipped=sum(c.outcome == "skipped" for c in cases),
        duration=round(duration, 3),
        failures=[*failures, *stage_failures],
        criteria_coverage=coverage(criteria, cases),
    )


# --- running ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One command of a stage, and the JUnit file it writes (workspace-relative), if any."""

    command: list[str]
    junit: str | None = None
    junit_file_prefix: str = ""


@dataclass(frozen=True)
class Stage:
    """A named group of steps; skipped entirely if ``requires_dir`` does not exist."""

    name: str
    steps: tuple[Step, ...]
    requires_dir: str | None = None


_PY = "backend/.venv/bin"
STAGES: tuple[Stage, ...] = (
    Stage(
        "lint",
        (
            Step([f"{_PY}/ruff", "check", "backend/app", "backend/tests"]),
            Step(["npm", "--prefix", "frontend", "run", "lint"]),
        ),
    ),
    Stage(
        "typecheck",
        (
            Step([f"{_PY}/mypy", "backend/app"]),
            Step(["npm", "--prefix", "frontend", "exec", "--", "tsc", "--noEmit"]),
        ),
    ),
    Stage(
        "unit",
        (
            Step(
                [
                    f"{_PY}/pytest",
                    "backend/tests",
                    "--ignore=backend/tests/integration",
                    "--ignore=backend/tests/regression",
                    f"--junitxml={JUNIT_DIR}/unit-backend.xml",
                ],
                junit=f"{JUNIT_DIR}/unit-backend.xml",
                junit_file_prefix="backend/",
            ),
            Step(
                [
                    "npm",
                    "--prefix",
                    "frontend",
                    "run",
                    "test:unit",
                    "--",
                    "--reporter=default",
                    "--reporter=junit",
                    f"--outputFile.junit=../{JUNIT_DIR}/unit-frontend.xml",
                ],
                junit=f"{JUNIT_DIR}/unit-frontend.xml",
                junit_file_prefix="frontend/",
            ),
        ),
    ),
    Stage(
        "integration",
        (
            Step(
                [
                    f"{_PY}/pytest",
                    "backend/tests/integration",
                    f"--junitxml={JUNIT_DIR}/integration.xml",
                ],
                junit=f"{JUNIT_DIR}/integration.xml",
                junit_file_prefix="backend/",
            ),
        ),
        requires_dir="backend/tests/integration",
    ),
    Stage(
        "e2e",
        # The golden-v1 playwright.config.ts writes this file itself (junit reporter).
        (
            Step(
                ["npm", "--prefix", "frontend", "run", "e2e"],
                junit=f"{JUNIT_DIR}/e2e.xml",
                junit_file_prefix="frontend/tests/e2e/",
            ),
        ),
    ),
    Stage(
        "regression",
        (
            Step(
                [
                    f"{_PY}/pytest",
                    "backend/tests/regression",
                    f"--junitxml={JUNIT_DIR}/regression.xml",
                ],
                junit=f"{JUNIT_DIR}/regression.xml",
                junit_file_prefix="backend/",
            ),
        ),
        requires_dir="backend/tests/regression",
    ),
)

# pytest's "no tests were collected" - an empty suite, not a failure.
_PYTEST_NO_TESTS = 5

FailureResolver = Callable[[Failure], Awaitable[str | None]]


async def run_qa(
    session: AsyncSession,
    ws: Workstation,
    agent_id: str | None,
    manager: WorkstationManager,
    wrs: WebRequirementsSpec,
    *,
    suspect: FailureResolver | None = None,
    stages: tuple[Stage, ...] = STAGES,
    bus: EventBus | None = None,
) -> QAReport:
    """Run every stage inside ``ws`` and return the combined report (FR-100, FR-101).

    ``suspect`` (``failure_triage.suspect_task``, bound to this project) fills each failure's
    ``suspected_task_id``; without it they stay None.
    """
    started = time.monotonic()
    cases: list[TestCase] = []
    stage_failures: list[Failure] = []
    for stage in stages:
        if stage.requires_dir and not await _exists(session, ws, agent_id, stage.requires_dir, bus):
            continue
        for step in stage.steps:
            if step.junit:
                # Overwrite any report a previous run left, so a crashed step can't reuse it.
                await tools.write_file(session, ws, ROLE, agent_id, step.junit, "", bus=bus)
            result = await tools.run_command(
                session,
                ws,
                ROLE,
                agent_id,
                manager,
                step.command,
                timeout=STAGE_TIMEOUT_SECONDS,
                bus=bus,
            )
            step_cases = await _read_junit(session, ws, agent_id, step, bus)
            cases += step_cases
            ran_ok = result.exit_code == 0 or (
                result.exit_code == _PYTEST_NO_TESTS and step.command[0].endswith("pytest")
            )
            if not ran_ok and not any(c.outcome == "failed" for c in step_cases):
                stage_failures.append(
                    Failure(
                        test=f"{stage.name}: {' '.join(step.command)}",
                        message=f"exit {result.exit_code}\n{result.stdout}\n{result.stderr}"[
                            -_OUTPUT_TAIL:
                        ],
                    )
                )

    report = build_report(cases, stage_failures, criteria_from_wrs(wrs), time.monotonic() - started)
    if suspect is not None:
        for failure in report.failures:
            failure.suspected_task_id = await suspect(failure)
    return report


async def _exists(
    session: AsyncSession, ws: Workstation, agent_id: str | None, path: str, bus: EventBus | None
) -> bool:
    try:
        await tools.list_dir(session, ws, ROLE, agent_id, path, bus=bus)
    except OSError:
        return False
    return True


async def _read_junit(
    session: AsyncSession, ws: Workstation, agent_id: str | None, step: Step, bus: EventBus | None
) -> list[TestCase]:
    if step.junit is None:
        return []
    try:
        raw = await tools.read_file(session, ws, ROLE, agent_id, step.junit, bus=bus)
    except OSError:
        return []
    if not raw.strip():
        return []
    try:
        return parse_junit(raw, file_prefix=step.junit_file_prefix)
    except ET.ParseError:
        return []


# --- saving ----------------------------------------------------------------------------------


def to_markdown(report: QAReport, criteria: list[Criterion], task_labels: dict[str, str]) -> str:
    """A human-readable summary; ``task_labels`` maps task ids to ``"WEB-007: title"``."""
    verdict = "PASS" if not report.failures and _all_pass(report) else "FAIL"
    lines = [
        f"# QA report - {verdict}",
        "",
        f"- Passed: {report.passed}",
        f"- Failed: {report.failed}",
        f"- Skipped: {report.skipped}",
        f"- Duration: {report.duration:.1f}s",
        "",
        "## Acceptance criteria",
        "",
        "| Criterion | Status | Text |",
        "|---|---|---|",
    ]
    for criterion in criteria:
        status = report.criteria_coverage.get(criterion.id, "missing")
        lines.append(f"| {criterion.id} | {status} | {_cell(criterion.text)} |")
    lines += ["", "## Failures", ""]
    if not report.failures:
        lines.append("None.")
    for failure in report.failures:
        suspect = task_labels.get(failure.suspected_task_id or "", "unknown")
        lines += [
            f"### {failure.test}",
            "",
            f"- File: {failure.file or '-'}",
            f"- Suspected task: {suspect}",
            "",
            "```",
            failure.message.strip()[-_OUTPUT_TAIL:],
            "```",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def _all_pass(report: QAReport) -> bool:
    return all(status == "pass" for status in report.criteria_coverage.values())


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


async def save_report(
    session: AsyncSession,
    ws: Workstation,
    agent_id: str | None,
    report: QAReport,
    criteria: list[Criterion],
    task_labels: dict[str, str],
    *,
    bus: EventBus | None = None,
) -> tuple[str, str]:
    """Write ``artifacts/qa-report.json`` and ``artifacts/qa-report.md``; return both paths."""
    await tools.write_file(
        session,
        ws,
        ROLE,
        agent_id,
        REPORT_JSON_PATH,
        json.dumps(report.model_dump(mode="json"), indent=2) + "\n",
        bus=bus,
    )
    await tools.write_file(
        session,
        ws,
        ROLE,
        agent_id,
        REPORT_MD_PATH,
        to_markdown(report, criteria, task_labels),
        bus=bus,
    )
    return REPORT_JSON_PATH, REPORT_MD_PATH
