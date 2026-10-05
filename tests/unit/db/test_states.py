"""State enums and transition tables against SRS 3.1 and 7.2."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pytest

from web_department.db.states import (
    AGENT_TRANSITIONS,
    ALLOWED_TRANSITIONS,
    PROJECT_TRANSITIONS,
    TASK_TRANSITIONS,
    AgentState,
    InvalidTransition,
    ProjectState,
    TaskState,
    transition,
)

P = ProjectState
T = TaskState
A = AgentState


@dataclass
class Thing:
    state: Any


def test_state_sets_match_srs_7_2() -> None:
    assert {s.value for s in ProjectState} == {
        "RECEIVED", "ANALYZING", "PLANNING", "ARCHITECTING", "DEVELOPING", "INTEGRATING",
        "TESTING", "SECURITY_REVIEW", "AWAITING_APPROVAL", "DEPLOYING", "MONITORING",
        "COMPLETED", "FAILED", "BLOCKED",
    }  # fmt: skip
    assert {s.value for s in TaskState} == {
        "CREATED", "DEPENDENCY_WAITING", "READY", "ASSIGNED", "IN_PROGRESS", "REVIEW",
        "FIX_REQUIRED", "COMPLETED", "BLOCKED", "CANCELLED",
    }  # fmt: skip
    assert {s.value for s in AgentState} == {
        "CREATED", "PROVISIONED", "AVAILABLE", "ASSIGNED", "WORKING", "REVIEW", "BLOCKED",
        "RETIRED",
    }  # fmt: skip


@pytest.mark.parametrize("enum_cls", [ProjectState, TaskState, AgentState])
def test_every_state_has_a_transition_entry_and_targets_are_valid(enum_cls: type[StrEnum]) -> None:
    table = ALLOWED_TRANSITIONS[enum_cls]

    assert set(table) == set(enum_cls)
    for targets in table.values():
        assert targets <= set(enum_cls)


SRS_3_1_EDGES = [
    (P.RECEIVED, P.ANALYZING),
    (P.ANALYZING, P.PLANNING),
    (P.PLANNING, P.ARCHITECTING),
    (P.ARCHITECTING, P.DEVELOPING),
    (P.DEVELOPING, P.INTEGRATING),
    (P.INTEGRATING, P.TESTING),
    (P.TESTING, P.DEVELOPING),
    (P.TESTING, P.SECURITY_REVIEW),
    (P.SECURITY_REVIEW, P.DEVELOPING),
    (P.SECURITY_REVIEW, P.AWAITING_APPROVAL),
    (P.AWAITING_APPROVAL, P.DEVELOPING),
    (P.AWAITING_APPROVAL, P.DEPLOYING),
    (P.DEPLOYING, P.MONITORING),
    (P.MONITORING, P.COMPLETED),
    (P.DEVELOPING, P.BLOCKED),
    (P.BLOCKED, P.PLANNING),
]


def test_project_transitions_are_exactly_the_srs_3_1_diagram_plus_failed() -> None:
    edges = {
        (src, dst) for src, dsts in PROJECT_TRANSITIONS.items() for dst in dsts if dst != P.FAILED
    }

    assert edges == set(SRS_3_1_EDGES)


def test_failed_is_reachable_from_every_non_terminal_project_state() -> None:
    terminal = {P.COMPLETED, P.FAILED}

    for state in set(P) - terminal:
        assert P.FAILED in PROJECT_TRANSITIONS[state]
    for state in terminal:
        assert PROJECT_TRANSITIONS[state] == frozenset()


@pytest.mark.parametrize(("src", "dst"), SRS_3_1_EDGES)
def test_valid_project_transition_updates_state(src: ProjectState, dst: ProjectState) -> None:
    project = Thing(state=src)

    transition(project, dst)

    assert project.state is dst


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (P.RECEIVED, P.DEVELOPING),
        (P.RECEIVED, P.RECEIVED),
        (P.TESTING, P.DEPLOYING),
        (P.SECURITY_REVIEW, P.DEPLOYING),
        (P.COMPLETED, P.DEVELOPING),
        (P.FAILED, P.RECEIVED),
        (P.MONITORING, P.DEVELOPING),
    ],
)
def test_invalid_project_transition_raises_and_keeps_state(
    src: ProjectState, dst: ProjectState
) -> None:
    project = Thing(state=src)

    with pytest.raises(InvalidTransition) as exc_info:
        transition(project, dst)

    assert project.state is src
    assert exc_info.value.current is src
    assert exc_info.value.new is dst
    assert f"{src.value} -> {dst.value}" in str(exc_info.value)


def test_agent_lifecycle_follows_fr_051() -> None:
    agent = Thing(state=A.CREATED)

    for step in (A.PROVISIONED, A.AVAILABLE, A.ASSIGNED, A.WORKING, A.REVIEW, A.AVAILABLE):
        transition(agent, step)

    assert agent.state is A.AVAILABLE


def test_retired_agent_is_terminal_and_working_agent_cannot_skip_review() -> None:
    assert AGENT_TRANSITIONS[A.RETIRED] == frozenset()
    with pytest.raises(InvalidTransition):
        transition(Thing(state=A.WORKING), A.AVAILABLE)


def test_task_happy_path_and_fix_loop() -> None:
    task = Thing(state=T.CREATED)

    for step in (
        T.DEPENDENCY_WAITING,
        T.READY,
        T.ASSIGNED,
        T.IN_PROGRESS,
        T.REVIEW,
        T.FIX_REQUIRED,
        T.IN_PROGRESS,
        T.REVIEW,
        T.COMPLETED,
    ):
        transition(task, step)

    assert task.state is T.COMPLETED


def test_task_cannot_complete_without_review() -> None:
    with pytest.raises(InvalidTransition):
        transition(Thing(state=T.IN_PROGRESS), T.COMPLETED)


def test_task_terminal_states_and_cancellation() -> None:
    assert TASK_TRANSITIONS[T.COMPLETED] == frozenset()
    assert TASK_TRANSITIONS[T.CANCELLED] == frozenset()
    for state in set(T) - {T.COMPLETED, T.CANCELLED}:
        assert T.CANCELLED in TASK_TRANSITIONS[state]


def test_exhausted_retries_block_the_task() -> None:
    assert T.BLOCKED in TASK_TRANSITIONS[T.FIX_REQUIRED]


def test_transition_rejects_mismatched_enum_types() -> None:
    with pytest.raises(TypeError, match="Cannot move a TaskState object to ProjectState"):
        transition(Thing(state=T.CREATED), P.ANALYZING)


def test_transition_rejects_non_state_values() -> None:
    with pytest.raises(TypeError, match="not a known state enum"):
        transition(Thing(state=P.RECEIVED), "ANALYZING")  # type: ignore[arg-type]
