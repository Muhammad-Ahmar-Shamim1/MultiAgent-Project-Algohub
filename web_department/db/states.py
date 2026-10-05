"""State sets and allowed transitions (SRS 7.2, DR-002).

Project transitions follow the state diagram in SRS 3.1 exactly; FAILED is reachable from every
non-terminal state. The SRS has no diagram for Task or Agent, so those tables are derived from the
requirements that describe their lifecycle: Task from FR-012, FR-037, FR-102 and FR-103; Agent
from FR-051 (Created -> Provisioned -> Available -> Assigned -> Working -> Review -> Available),
FR-052 and FR-053.
"""

from enum import StrEnum
from typing import Any, Protocol


class ProjectState(StrEnum):
    """Lifecycle of a project (SRS 3.1, 7.2)."""

    RECEIVED = "RECEIVED"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    ARCHITECTING = "ARCHITECTING"
    DEVELOPING = "DEVELOPING"
    INTEGRATING = "INTEGRATING"
    TESTING = "TESTING"
    SECURITY_REVIEW = "SECURITY_REVIEW"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    DEPLOYING = "DEPLOYING"
    MONITORING = "MONITORING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class TaskState(StrEnum):
    """Lifecycle of a task (SRS 7.2)."""

    CREATED = "CREATED"
    DEPENDENCY_WAITING = "DEPENDENCY_WAITING"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    REVIEW = "REVIEW"
    FIX_REQUIRED = "FIX_REQUIRED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class AgentState(StrEnum):
    """Lifecycle of a worker (SRS 7.2)."""

    CREATED = "CREATED"
    PROVISIONED = "PROVISIONED"
    AVAILABLE = "AVAILABLE"
    ASSIGNED = "ASSIGNED"
    WORKING = "WORKING"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"
    RETIRED = "RETIRED"


P = ProjectState
T = TaskState
A = AgentState

PROJECT_TRANSITIONS: dict[ProjectState, frozenset[ProjectState]] = {
    P.RECEIVED: frozenset({P.ANALYZING, P.FAILED}),
    P.ANALYZING: frozenset({P.PLANNING, P.FAILED}),
    P.PLANNING: frozenset({P.ARCHITECTING, P.FAILED}),
    P.ARCHITECTING: frozenset({P.DEVELOPING, P.FAILED}),
    P.DEVELOPING: frozenset({P.INTEGRATING, P.BLOCKED, P.FAILED}),
    P.INTEGRATING: frozenset({P.TESTING, P.FAILED}),
    P.TESTING: frozenset({P.DEVELOPING, P.SECURITY_REVIEW, P.FAILED}),
    P.SECURITY_REVIEW: frozenset({P.DEVELOPING, P.AWAITING_APPROVAL, P.FAILED}),
    P.AWAITING_APPROVAL: frozenset({P.DEVELOPING, P.DEPLOYING, P.FAILED}),
    P.DEPLOYING: frozenset({P.MONITORING, P.FAILED}),
    P.MONITORING: frozenset({P.COMPLETED, P.FAILED}),
    P.BLOCKED: frozenset({P.PLANNING, P.FAILED}),
    P.COMPLETED: frozenset(),
    P.FAILED: frozenset(),
}

TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    T.CREATED: frozenset({T.DEPENDENCY_WAITING, T.READY, T.CANCELLED}),
    T.DEPENDENCY_WAITING: frozenset({T.READY, T.CANCELLED}),
    T.READY: frozenset({T.ASSIGNED, T.CANCELLED}),
    T.ASSIGNED: frozenset({T.IN_PROGRESS, T.CANCELLED}),
    T.IN_PROGRESS: frozenset({T.REVIEW, T.BLOCKED, T.CANCELLED}),
    T.REVIEW: frozenset({T.COMPLETED, T.FIX_REQUIRED, T.CANCELLED}),
    T.FIX_REQUIRED: frozenset({T.IN_PROGRESS, T.BLOCKED, T.CANCELLED}),
    T.BLOCKED: frozenset({T.READY, T.CANCELLED}),
    T.COMPLETED: frozenset(),
    T.CANCELLED: frozenset(),
}

AGENT_TRANSITIONS: dict[AgentState, frozenset[AgentState]] = {
    A.CREATED: frozenset({A.PROVISIONED, A.RETIRED}),
    A.PROVISIONED: frozenset({A.AVAILABLE, A.RETIRED}),
    A.AVAILABLE: frozenset({A.ASSIGNED, A.RETIRED}),
    A.ASSIGNED: frozenset({A.WORKING, A.AVAILABLE}),
    A.WORKING: frozenset({A.REVIEW, A.BLOCKED}),
    A.REVIEW: frozenset({A.AVAILABLE, A.WORKING}),
    A.BLOCKED: frozenset({A.AVAILABLE, A.RETIRED}),
    A.RETIRED: frozenset(),
}

ALLOWED_TRANSITIONS: dict[type[StrEnum], dict[Any, frozenset[Any]]] = {
    ProjectState: PROJECT_TRANSITIONS,
    TaskState: TASK_TRANSITIONS,
    AgentState: AGENT_TRANSITIONS,
}


class InvalidTransition(Exception):
    """Raised when a state change is not allowed by ``ALLOWED_TRANSITIONS``."""

    def __init__(self, current: StrEnum, new: StrEnum) -> None:
        super().__init__(f"Invalid {type(new).__name__} transition: {current.value} -> {new.value}")
        self.current = current
        self.new = new


class Stateful(Protocol):
    """Anything with a mutable ``state`` attribute (Project, Task, Agent)."""

    state: Any


def transition(obj: Stateful, new_state: StrEnum) -> None:
    """Move ``obj`` to ``new_state`` or raise ``InvalidTransition`` if not allowed."""
    table = ALLOWED_TRANSITIONS.get(type(new_state))
    if table is None:
        raise TypeError(f"{type(new_state).__name__} is not a known state enum")
    current = obj.state
    if type(current) is not type(new_state):
        raise TypeError(
            f"Cannot move a {type(current).__name__} object to {type(new_state).__name__}"
        )
    if new_state not in table[current]:
        raise InvalidTransition(current, new_state)
    obj.state = new_state
