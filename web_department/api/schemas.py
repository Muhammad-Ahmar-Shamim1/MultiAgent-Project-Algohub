"""Request/response schemas for the intake + project API (FR-001, SRS 8.2)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from web_department.db.states import AgentState, ProjectState, TaskState
from web_department.security.approvals import Decision


class ProjectRequest(BaseModel):
    """A web project request (FR-001, FR-005).

    ``description``, ``constraints`` and ``attachments`` have no column on the ``Project`` entity
    (SRS 7.1 defines it as id/title/requester_id/state/wrs_version/stack only) - they are the raw
    material the future Requirement Analyzer turns into a WRS (FR-020), so the Web Agent records
    them on the ``PROJECT_RECEIVED`` event instead of inventing new Project columns.
    """

    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=10_000)
    constraints: str | None = Field(default=None, max_length=5_000)
    requester_id: str = Field(min_length=1, max_length=128)
    attachments: list[str] | None = None


class ProjectOut(BaseModel):
    """A project's public state."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    requester_id: str
    state: ProjectState
    stack: str
    wrs_version: int | None
    created_at: datetime
    updated_at: datetime


class ClarificationAnswers(BaseModel):
    """Human answers to a WRS's ``open_questions`` (SRS 8.2: POST .../clarifications, FR-022).

    Keyed by the exact question text from the latest ``RequirementSpec.open_questions``.
    """

    answers: dict[str, str] = Field(min_length=1)


class TaskOut(BaseModel):
    """A task's public state (SRS 8.2: GET /projects/{id}/tasks - task graph and states)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    display_id: str
    title: str
    role: str
    state: TaskState
    retries: int
    assigned_agent_id: str | None
    depends_on: list[str] = Field(default_factory=list)


class WarRoomMemberOut(BaseModel):
    """One War Room team member (FR-080)."""

    agent_id: str
    display_id: str
    role: str
    state: AgentState


class WarRoomEventOut(BaseModel):
    """One entry of the War Room's event feed (SRS 7.3 shape)."""

    event_id: str
    event: str
    task_id: str | None
    agent_id: str | None
    status: str | None
    requires_review: bool
    payload: dict[str, Any]
    timestamp: datetime


class WarRoomArtifactOut(BaseModel):
    """One artifact; ``sha256`` is set once the room is archived."""

    path: str
    kind: str
    size_bytes: int
    sha256: str | None


class WarRoomOut(BaseModel):
    """GET /projects/{id}/war-room: team, task board by state, event feed, artifacts (FR-080)."""

    id: str
    project_id: str
    status: str
    required_roles: list[str]
    opened_at: datetime
    closed_at: datetime | None
    members: list[WarRoomMemberOut]
    task_board: dict[TaskState, list[TaskOut]]
    events: list[WarRoomEventOut]
    artifacts: list[WarRoomArtifactOut]


class SecurityExceptionRequest(BaseModel):
    """POST /security/exceptions: accept one finding (FR-113). Identify it as the report does."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=32)
    tool: str = Field(min_length=1, max_length=64)
    rule: str = Field(min_length=1, max_length=300)
    file: str | None = Field(default=None, max_length=500)
    line: int | None = Field(default=None, ge=1)
    justification: str = Field(min_length=10, max_length=2000)
    approver_id: str = Field(min_length=1, max_length=128)


class SecurityExceptionOut(BaseModel):
    """A recorded security exception."""

    id: str
    project_id: str
    fingerprint: str
    approver_id: str
    justification: str
    decided_at: datetime


class ApprovalDecisionRequest(BaseModel):
    """POST /approvals/{id}: Approve or Reject with a comment (FR-131).

    A rejection needs a comment - it becomes the fix task's input (FR-132).
    """

    model_config = ConfigDict(extra="forbid")

    decision: Decision
    comment: str | None = Field(default=None, max_length=5_000)
    approver_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _reject_needs_a_comment(self) -> "ApprovalDecisionRequest":
        if self.decision is Decision.REJECT and not (self.comment or "").strip():
            raise ValueError("a rejection needs a comment saying what must change")
        return self


class ApprovalDecisionOut(BaseModel):
    """A recorded decision."""

    approval_id: str
    decision: Decision
    comment: str | None
    approver_id: str


class ApprovalOut(BaseModel):
    """An approval request, its evidence (FR-131) and its decision, if made."""

    id: str
    project_id: str
    action_type: str
    summary: str
    created_at: datetime
    evidence: dict[str, Any] | None
    decision: ApprovalDecisionOut | None


# --- dashboard read models (FR-160..FR-163) ---------------------------------------------------


class WhoAmIOut(BaseModel):
    """The caller's role, so a UI can hide actions the API would refuse anyway."""

    role: str


class AttentionItem(BaseModel):
    """One thing that must stay visible (FR-163): a blocked task, a failed one, an approval..."""

    kind: str
    id: str
    label: str
    project_id: str | None


class OverviewOut(BaseModel):
    """GET /overview: the dashboard's tiles (FR-160) and its never-hidden list (FR-163)."""

    agents_total: int
    agents_available: int
    agents_working: int
    agents_blocked: int
    projects_active: int
    projects_blocked: int
    tasks_pending: int
    tasks_in_progress: int
    tasks_failed: int
    tasks_blocked: int
    deployments: int
    approvals_pending: int
    attention: list[AttentionItem]


class ProjectSummaryOut(ProjectOut):
    """A project in a list."""


class AgentOut(BaseModel):
    """One worker."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    display_id: str
    role: str | None
    blueprint_id: str
    blueprint_version: str
    state: AgentState
    project_id: str | None
    updated_at: datetime


class TaskListItemOut(TaskOut):
    """A task in the cross-project task table."""

    project_id: str
    updated_at: datetime


class WorkstationOut(BaseModel):
    """One project workstation."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    container_id: str | None
    status: str
    limits: dict[str, Any]
    created_at: datetime


class KnowledgeOut(BaseModel):
    """One knowledge entry (content shortened for the table)."""

    id: str
    layer: str
    project_id: str | None
    source: str
    version: int
    excerpt: str
    updated_at: datetime


class DeploymentOut(BaseModel):
    """One deployment."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    version: str
    commit_sha: str
    target: str
    status: str
    health: str | None
    approver_id: str | None
    deployed_at: datetime | None


class EventOut(BaseModel):
    """One stored event (SRS 7.3 shape)."""

    event_id: str
    event: str
    project_id: str | None
    task_id: str | None
    agent_id: str | None
    status: str | None
    requires_review: bool
    payload: dict[str, Any]
    timestamp: datetime


class RequirementsOut(BaseModel):
    """GET /projects/{id}/requirements: the latest WRS and its open clarification questions."""

    version: int
    spec: dict[str, Any]
    open_questions: list[str]


class PlanOut(BaseModel):
    """GET /projects/{id}/plan: the task DAG as Mermaid (FR-162)."""

    mermaid: str


class TimelineEntryOut(BaseModel):
    """One project state change."""

    state: str
    previous: str | None
    timestamp: datetime


class ReportsOut(BaseModel):
    """GET /projects/{id}/reports: the saved QA and security reports, if any."""

    qa: dict[str, Any] | None
    security: dict[str, Any] | None
