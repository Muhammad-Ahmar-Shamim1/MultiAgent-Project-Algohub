"""quality_gate.evaluate and the escalation ladder's order (FR-101, FR-103)."""

import pytest

from web_department.db.models import Task
from web_department.qa.failure_triage import (
    EscalationLevel,
    _task_from_git_output,
    escalation_for,
    implementing_task,
)
from web_department.qa.quality_gate import evaluate
from web_department.qa.test_runner import Failure, QAReport


def test_gate_passes_with_no_failures_and_every_criterion_passing() -> None:
    report = QAReport(passed=3, criteria_coverage={"F-01.1": "pass", "F-02.1": "pass"})

    assert evaluate(report).passed is True


def test_gate_fails_on_any_failure_even_with_full_coverage() -> None:
    report = QAReport(
        passed=3,
        failed=1,
        failures=[Failure(test="lint: ruff", message="E501")],
        criteria_coverage={"F-01.1": "pass"},
    )

    result = evaluate(report)

    assert result.passed is False
    assert result.reasons == ["1 failure(s): lint: ruff"]


def test_gate_fails_on_a_missing_or_failing_criterion_and_names_them() -> None:
    report = QAReport(passed=1, criteria_coverage={"F-01.1": "missing", "F-02.1": "fail"})

    result = evaluate(report)

    assert result.passed is False
    assert "criteria with no test: ['F-01.1']" in result.reasons
    assert "criteria with a failing test: ['F-02.1']" in result.reasons


def test_gate_fails_a_report_with_no_criteria_at_all() -> None:
    assert evaluate(QAReport(passed=5)).passed is False


@pytest.mark.parametrize(
    ("cycle", "level"),
    [
        (1, EscalationLevel.FIX),
        (2, EscalationLevel.FIX),
        (3, EscalationLevel.FIX),
        (4, EscalationLevel.REPLAN),
        (5, EscalationLevel.STRONGER_MODEL),
        (6, EscalationLevel.HUMAN_REVIEW),
        (9, EscalationLevel.HUMAN_REVIEW),
    ],
)
def test_escalation_order_after_three_fix_cycles(cycle: int, level: EscalationLevel) -> None:
    assert escalation_for(cycle, max_fix_cycles=3) is level


def _task(display_id: str, role: str, feature: str | None) -> Task:
    return Task(display_id=display_id, role=role, inputs={"feature": feature} if feature else {})


def test_implementing_task_prefers_api_integration_then_other_roles_then_qa() -> None:
    qa = _task("WEB-020", "qa", "F-02")
    frontend = _task("WEB-012", "frontend", "F-02")
    wiring = _task("WEB-016", "api_integration", "F-02")
    other_feature = _task("WEB-015", "api_integration", "F-01")

    assert implementing_task([qa, frontend, wiring, other_feature], "F-02") is wiring
    assert implementing_task([qa, frontend], "F-02") is frontend
    assert implementing_task([qa], "F-02") is qa
    assert implementing_task([other_feature], "F-02") is None


def test_task_from_git_output_reads_the_commit_summary() -> None:
    task = _task("WEB-007", "backend", None)
    porcelain = (
        "a1b2c3 14 14 1\nauthor AlgoHub Worker\nsummary WEB-007: CRUD API: Product\n"
        "filename app/routers/products.py\n"
    )

    assert _task_from_git_output(porcelain, {"WEB-007": task}, porcelain=True) is task
    assert _task_from_git_output("WEB-007: CRUD API", {"WEB-007": task}, porcelain=False) is task
    assert _task_from_git_output("Initial commit", {"WEB-007": task}, porcelain=False) is None
    assert _task_from_git_output("WEB-999: gone", {"WEB-007": task}, porcelain=False) is None
