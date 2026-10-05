"""Planner's pure logic: template loading, the split rule, DAG validation, Mermaid rendering."""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from web_department.orchestrator.planner import (
    DEFAULT_TEMPLATES_PATH,
    MAX_FILES_PER_TASK,
    MAX_LINES_PER_TASK,
    PlannedTask,
    PlanValidationError,
    load_templates,
    plan_to_mermaid,
    split_count,
    validate_and_order,
)


def test_load_templates_loads_the_golden_v1_library() -> None:
    templates = load_templates(DEFAULT_TEMPLATES_PATH)

    ids = [t.id for t in templates]
    assert ids == [
        "init_repo",
        "architecture",
        "db_schema",
        "db_migration",
        "backend_foundation",
        "auth_api",
        "crud_api",
        "frontend_foundation",
        "page",
        "api_integration",
        "tests_e2e",
        "security_review",
        "deploy",
    ]
    by_id = {t.id: t for t in templates}
    assert by_id["crud_api"].parameterized_by == "entities"
    assert by_id["page"].parameterized_by == "pages"
    assert by_id["api_integration"].parameterized_by == "features"
    assert by_id["init_repo"].parameterized_by is None
    assert by_id["deploy"].depends_on == ["security_review"]
    assert by_id["architecture"].role == "architect"
    assert by_id["db_schema"].depends_on == ["architecture"]


@pytest.mark.parametrize(
    ("est_files", "est_lines", "expected"),
    [
        (1, 1, 1),
        (MAX_FILES_PER_TASK, MAX_LINES_PER_TASK, 1),
        (MAX_FILES_PER_TASK + 1, 1, 2),
        (1, MAX_LINES_PER_TASK + 1, 2),
        (MAX_FILES_PER_TASK * 3, 1, 3),
        (1, MAX_LINES_PER_TASK * 3, 3),
    ],
)
def test_split_count_thresholds(est_files: int, est_lines: int, expected: int) -> None:
    assert split_count(est_files, est_lines) == expected


def test_validate_and_order_returns_a_topological_order() -> None:
    planned = {
        "a": PlannedTask(key="a", title="a", role="backend"),
        "b": PlannedTask(key="b", title="b", role="backend", depends_on={"a"}),
        "c": PlannedTask(key="c", title="c", role="backend", depends_on={"a", "b"}),
    }

    order = validate_and_order(planned)

    assert order.index("a") < order.index("b") < order.index("c")
    assert set(order) == {"a", "b", "c"}


def test_validate_and_order_rejects_a_cycle() -> None:
    planned = {
        "a": PlannedTask(key="a", title="a", role="backend", depends_on={"b"}),
        "b": PlannedTask(key="b", title="b", role="backend", depends_on={"a"}),
    }

    with pytest.raises(PlanValidationError, match="cycle"):
        validate_and_order(planned)


def test_validate_and_order_rejects_a_missing_dependency() -> None:
    planned = {"a": PlannedTask(key="a", title="a", role="backend", depends_on={"ghost"})}

    with pytest.raises(PlanValidationError, match="missing task"):
        validate_and_order(planned)


def test_validate_and_order_of_an_empty_plan_is_empty() -> None:
    assert validate_and_order({}) == []


@dataclass
class _FakeTask:
    display_id: str
    title: str
    depends_on: list["_FakeTask"]


def test_plan_to_mermaid_renders_nodes_and_edges() -> None:
    a = _FakeTask(display_id="WEB-001", title="Init repo", depends_on=[])
    b = _FakeTask(display_id="WEB-002", title="DB schema", depends_on=[a])

    graph = plan_to_mermaid([a, b])  # type: ignore[list-item]

    assert graph.splitlines()[0] == "flowchart TD"
    assert '    WEB-001["WEB-001: Init repo"]' in graph
    assert '    WEB-002["WEB-002: DB schema"]' in graph
    assert "    WEB-001 --> WEB-002" in graph


def test_plan_to_mermaid_escapes_double_quotes_in_titles() -> None:
    a = SimpleNamespace(display_id="WEB-001", title='Say "hi"', depends_on=[])

    graph = plan_to_mermaid([a])  # type: ignore[list-item]

    node_line = graph.splitlines()[1]
    assert node_line == "    WEB-001[\"WEB-001: Say 'hi'\"]"
    assert node_line.count('"') == 2  # only the two syntactic quotes, none from the title


def test_plan_to_mermaid_of_an_empty_plan() -> None:
    assert plan_to_mermaid([]) == "flowchart TD"
