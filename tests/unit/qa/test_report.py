"""qa/test_runner.py's pure parts: criteria ids/tags, JUnit parsing, coverage, the report."""

import xml.etree.ElementTree as ET

import pytest

from web_department.orchestrator.wrs_schema import Feature, WebRequirementsSpec
from web_department.qa.test_runner import (
    Criterion,
    Failure,
    TestCase,
    build_report,
    coverage,
    criteria_for_test,
    criteria_from_wrs,
    criterion_tag,
    parse_junit,
    to_markdown,
)

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
CRITERIA = criteria_from_wrs(WRS)

PYTEST_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4" failures="1" errors="1" skipped="1" time="1.5">
  <testcase classname="tests.test_auth" name="test_F01_user_can_log_in" time="0.2"/>
  <testcase classname="tests.test_cart.TestCart" name="test_F02_1_total_updates" time="0.3">
    <failure message="assert 10 == 12">tests/test_cart.py:14: AssertionError</failure>
  </testcase>
  <testcase classname="tests.test_cart" name="test_F02_2_empty" time="0.1">
    <error message="fixture db not found">E   fixture 'db' not found</error>
  </testcase>
  <testcase classname="tests.test_misc" name="test_slow" time="0"><skipped/></testcase>
</testsuite></testsuites>
"""

PLAYWRIGHT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites id="" name="" tests="1" failures="0" skipped="0" errors="0" time="2.1">
<testsuite name="cart.spec.ts" timestamp="2026-10-03T00:00:00" hostname="chromium" tests="1"
  failures="0" skipped="0" time="2.1" errors="0">
<testcase name="F02_2 empty cart shows a message" classname="cart.spec.ts" time="2.1">
</testcase>
</testsuite>
</testsuites>
"""


def test_criterion_ids_are_feature_id_dot_index_in_wrs_order() -> None:
    assert [c.id for c in CRITERIA] == ["F-01.1", "F-02.1", "F-02.2"]
    assert [c.id for c in criteria_from_wrs(WRS, "F-02")] == ["F-02.1", "F-02.2"]
    assert criterion_tag(CRITERIA[1]) == "F02_1"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("test_F02_1_total_updates", ["F-02.1"]),
        ("F02_2 empty cart shows a message", ["F-02.2"]),
        ("test_F01_user_can_log_in", ["F-01.1"]),  # one criterion: index optional
        ("test_F02_total_updates", []),  # two criteria: index required
        ("test_F2_1[case-a]", ["F-02.1"]),  # numbers compare numerically; params ok
        ("test_F01_1_and_F02_2_together", ["F-01.1", "F-02.2"]),
        ("test_F09_1_unknown_feature", []),
        ("test_cart_v2_layout", []),
    ],
)
def test_criteria_for_test_reads_the_tag_from_the_name(name: str, expected: list[str]) -> None:
    assert [c.id for c in criteria_for_test(name, CRITERIA)] == expected


def test_parse_junit_reads_pytest_outcomes_messages_and_derives_files() -> None:
    cases = parse_junit(PYTEST_XML, file_prefix="backend/")

    assert [(c.name, c.outcome) for c in cases] == [
        ("test_F01_user_can_log_in", "passed"),
        ("test_F02_1_total_updates", "failed"),
        ("test_F02_2_empty", "failed"),  # <error> counts as a failure
        ("test_slow", "skipped"),
    ]
    assert cases[1].file == "backend/tests/test_cart.py"  # class name stripped
    assert cases[1].message.startswith("assert 10 == 12")
    assert "fixture 'db' not found" in cases[2].message
    assert cases[1].full_name == "tests.test_cart.TestCart::test_F02_1_total_updates"


def test_parse_junit_uses_the_suite_file_for_playwright() -> None:
    cases = parse_junit(PLAYWRIGHT_XML, file_prefix="frontend/tests/e2e/")

    assert len(cases) == 1
    assert cases[0].outcome == "passed"
    assert cases[0].duration == 2.1


def test_parse_junit_accepts_a_bare_testsuite_root() -> None:
    xml = '<testsuite name="vitest"><testcase name="renders" classname="a.test.ts"/></testsuite>'

    assert [c.name for c in parse_junit(xml)] == ["renders"]


def test_parse_junit_rejects_malformed_xml() -> None:
    with pytest.raises(ET.ParseError):
        parse_junit("<testsuites><testsuite>")


def _case(name: str, outcome: str) -> TestCase:
    return TestCase(name, "", None, outcome, "", 0.0)  # type: ignore[arg-type]


def test_coverage_is_fail_over_pass_over_missing() -> None:
    cases = [
        _case("test_F01_ok", "passed"),
        _case("test_F02_1_a", "passed"),
        _case("test_F02_1_b", "failed"),  # one failing test makes the criterion fail
        _case("test_F02_2_skipped", "skipped"),  # skipped-only is missing
    ]

    assert coverage(CRITERIA, cases) == {"F-01.1": "pass", "F-02.1": "fail", "F-02.2": "missing"}


def test_build_report_counts_and_combines_test_and_stage_failures() -> None:
    cases = parse_junit(PYTEST_XML, file_prefix="backend/") + parse_junit(PLAYWRIGHT_XML)
    lint = Failure(test="lint: ruff check", message="E501 line too long")

    report = build_report(cases, [lint], CRITERIA, duration=3.14159)

    assert (report.passed, report.failed, report.skipped) == (2, 3, 1)
    assert report.duration == 3.142
    assert [f.test for f in report.failures][-1] == "lint: ruff check"
    assert report.failures[0].file == "backend/tests/test_cart.py"
    assert report.criteria_coverage == {"F-01.1": "pass", "F-02.1": "fail", "F-02.2": "fail"}


def test_markdown_summary_lists_verdict_criteria_and_suspects() -> None:
    report = build_report(
        [_case("test_F01_ok", "passed")],
        [Failure(test="lint: ruff", message="E501", suspected_task_id="tsk_1")],
        CRITERIA,
        duration=1.0,
    )

    markdown = to_markdown(report, CRITERIA, {"tsk_1": "WEB-007: Backend foundation"})

    assert markdown.startswith("# QA report - FAIL")
    assert "| F-02.2 | missing | Empty cart shows a message |" in markdown
    assert "- Suspected task: WEB-007: Backend foundation" in markdown


def test_markdown_summary_passes_only_when_clean_and_fully_covered() -> None:
    one = [Criterion("F-01.1", "F-01", 1, "User can log in")]
    report = build_report([_case("test_F01_ok", "passed")], [], one, duration=1.0)

    assert to_markdown(report, one, {}).startswith("# QA report - PASS")
