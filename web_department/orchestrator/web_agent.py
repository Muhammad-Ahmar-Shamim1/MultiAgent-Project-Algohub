"""The Web Agent: the intake entry point for the Web department (FR-002..FR-004).

Classifies whether a request is in scope, creates the Project record and emits PROJECT_RECEIVED.
Does *not* hand off to the orchestrator itself: ``accept()`` runs inside the caller's
still-uncommitted transaction, and orchestration hand-off (``orchestrator.start()``) is a real
external side effect now (it enqueues to Redis for ``runner.py`` to pick up) - queuing a project
the runner might dequeue before this transaction's own commit is visible to it would be a race.
The caller (``api/routes/projects.py``) hands off after it commits.
"""

import httpx
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.api.schemas import ProjectRequest
from web_department.collaboration.events import EventBus, EventType, emit
from web_department.config.settings import get_settings
from web_department.db.base import get_session_factory
from web_department.db.models import Project
from web_department.db.repositories import ProjectRepository
from web_department.llm.prompts import render as render_prompt
from web_department.llm.router import Purpose, complete
from web_department.logging import get_logger

log = get_logger(__name__)


class ScopeClassification(BaseModel):
    """The LLM's answer to "is this request Web-department work?" (FR-002)."""

    is_web: bool
    reason: str


class NotWebScope(Exception):
    """Raised when a request is classified as out of the Web department's scope (FR-002)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class WebAgent:
    """Accepts project requests on behalf of the Web department."""

    def __init__(self, session: AsyncSession, bus: EventBus | None = None) -> None:
        self.session = session
        self.bus = bus

    async def accept(self, request: ProjectRequest) -> Project:
        """Classify and create ``request`` as a Project, or raise ``NotWebScope`` (FR-002, FR-003).

        The caller must hand off to the orchestrator (``orchestrator.start(project.id)``) itself,
        after its own transaction commits - see the module docstring.
        """
        classification = await self._classify_scope(request)
        if not classification.is_web:
            log.info("project_rejected_out_of_scope", reason=classification.reason)
            raise NotWebScope(classification.reason)

        project = await ProjectRepository(self.session, bus=self.bus).create(
            title=request.title, requester_id=request.requester_id
        )
        await emit(
            EventType.PROJECT_RECEIVED,
            session=self.session,
            bus=self.bus,
            project_id=project.id,
            payload={
                "description": request.description,
                "constraints": request.constraints,
                "attachments": request.attachments or [],
            },
        )
        return project

    async def _classify_scope(self, request: ProjectRequest) -> ScopeClassification:
        prompt = render_prompt(
            "classify_web_scope",
            title=request.title,
            description=request.description,
            constraints=request.constraints,
        )
        result = await complete(
            [{"role": "user", "content": prompt}],
            purpose=Purpose.PLANNING,
            json_schema=ScopeClassification,
        )
        assert isinstance(result.parsed, ScopeClassification)  # complete() parsed our own schema
        return result.parsed

    async def report_status(self, project_id: str) -> None:
        """POST the project's current state to STATUS_WEBHOOK_URL, if configured (FR-004)."""
        settings = get_settings()
        if settings.status_webhook_url is None:
            return
        async with get_session_factory()() as session:
            project = await ProjectRepository(session).get(project_id)
        if project is None:
            return
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                response = await client.post(
                    settings.status_webhook_url,
                    json={"project_id": project.id, "state": project.state.value},
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("status_webhook_failed", project_id=project_id, error=str(exc))
