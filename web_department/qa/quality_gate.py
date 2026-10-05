"""QA quality gate (FR-101, FR-104): a build passes only with zero failures AND every WRS
acceptance criterion covered by at least one test that passed.
"""

from dataclasses import dataclass, field

from web_department.qa.test_runner import QAReport


@dataclass(frozen=True)
class GateResult:
    """The gate's verdict and, when it fails, every reason why."""

    passed: bool
    reasons: list[str] = field(default_factory=list)


def evaluate(report: QAReport) -> GateResult:
    """Pass iff the report has no failures and every criterion's coverage is ``pass``.

    A report with no criteria at all fails: "every criterion covered" can't be shown for a WRS
    the report was not built against, and an empty coverage map is far more likely a wiring bug
    than a project with nothing to accept.
    """
    reasons: list[str] = []
    if report.failures:
        reasons.append(
            f"{len(report.failures)} failure(s): " + ", ".join(f.test for f in report.failures[:10])
        )
    if not report.criteria_coverage:
        reasons.append("no acceptance criteria in the report")
    missing = sorted(c for c, s in report.criteria_coverage.items() if s == "missing")
    failing = sorted(c for c, s in report.criteria_coverage.items() if s == "fail")
    if missing:
        reasons.append(f"criteria with no test: {missing}")
    if failing:
        reasons.append(f"criteria with a failing test: {failing}")
    return GateResult(passed=not reasons, reasons=reasons)
