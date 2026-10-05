"""The Requirement Analyzer: request -> Web Requirements Specification (FR-020..024).

Produces a new ``RequirementSpec`` version each time it runs. A WRS is always complete (the LLM
is instructed to state its own best-guess defaults rather than leave fields empty), so
``open_questions`` is advisory, not a blocker: when present, the project stays in ANALYZING and a
REVIEW_REQUIRED event is emitted so a human can answer via ``apply_clarifications``; if nobody
does within the configured timeout, ``check_clarification_timeout`` accepts the draft as-is and
advances the project, per FR-022's "apply defaults" behaviour.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus, EventType, emit
from web_department.config.settings import get_settings
from web_department.db.models import Project, RequirementSpec
from web_department.db.repositories import (
    EventRepository,
    ProjectRepository,
    RequirementSpecRepository,
)
from web_department.db.states import ProjectState
from web_department.llm.prompts import render as render_prompt
from web_department.llm.router import Purpose, complete
from web_department.logging import get_logger
from web_department.orchestrator.wrs_schema import WebRequirementsSpec

log = get_logger(__name__)


class RequirementAnalyzer:
    """Turns a project's request into a versioned, structured WRS."""

    def __init__(self, session: AsyncSession, bus: EventBus | None = None) -> None:
        self.session = session
        self.bus = bus

    async def analyze(
        self, project: Project, *, extra_context: str | None = None
    ) -> RequirementSpec:
        """Produce a new WRS version for ``project`` (FR-020).

        Moves RECEIVED -> ANALYZING if needed. If the result has ``open_questions``, the project
        stays in ANALYZING and REVIEW_REQUIRED is emitted (FR-022); otherwise it advances to
        PLANNING.
        """
        project_repo = ProjectRepository(self.session, bus=self.bus)
        if project.state is ProjectState.RECEIVED:
            project = await project_repo.update_state(project.id, ProjectState.ANALYZING)

        description, constraints = await self._original_request(project)
        wrs = await self._call_llm(
            project, description=description, constraints=constraints, extra_context=extra_context
        )

        spec_repo = RequirementSpecRepository(self.session)
        previous = await spec_repo.latest_for_project(project.id)
        next_version = (previous.version + 1) if previous else 1
        spec = await spec_repo.create(
            project_id=project.id,
            version=next_version,
            json=wrs.model_dump(mode="json"),
            created_by="requirement_analyzer",
        )
        project.wrs_version = next_version
        await self.session.flush()

        if wrs.open_questions:
            await emit(
                EventType.REVIEW_REQUIRED,
                session=self.session,
                bus=self.bus,
                project_id=project.id,
                payload={"open_questions": wrs.open_questions, "spec_version": next_version},
            )
        else:
            await project_repo.update_state(project.id, ProjectState.PLANNING)

        return spec

    async def apply_clarifications(
        self, project: Project, answers: dict[str, str]
    ) -> RequirementSpec:
        """Merge human answers into context and re-run analysis (FR-022, SRS 8.2 clarifications)."""
        spec_repo = RequirementSpecRepository(self.session)
        previous = await spec_repo.latest_for_project(project.id)
        lines = [f"Q: {question}\nA: {answer}" for question, answer in answers.items()]
        extra_context = "Answers to previous clarification questions:\n" + "\n".join(lines)
        if previous is not None:
            extra_context = (
                f"Previous draft specification (version {previous.version}, for reference):\n"
                f"{previous.json}\n\n{extra_context}"
            )
        return await self.analyze(project, extra_context=extra_context)

    async def check_clarification_timeout(self, project: Project) -> RequirementSpec | None:
        """If ANALYZING has waited past the timeout, accept the current draft and advance it.

        Returns the accepted spec, or None if there was nothing to do.
        """
        if project.state is not ProjectState.ANALYZING:
            return None
        spec_repo = RequirementSpecRepository(self.session)
        latest = await spec_repo.latest_for_project(project.id)
        if latest is None or not latest.json.get("open_questions"):
            return None
        timeout = timedelta(hours=get_settings().analysis_clarification_timeout_hours)
        if datetime.now(UTC) - latest.created_at < timeout:
            return None

        await ProjectRepository(self.session, bus=self.bus).update_state(
            project.id, ProjectState.PLANNING
        )
        log.info(
            "analysis_clarification_timeout_applied",
            project_id=project.id,
            spec_version=latest.version,
        )
        return latest

    async def _original_request(self, project: Project) -> tuple[str, str | None]:
        """Read the description/constraints the Web Agent recorded on PROJECT_RECEIVED."""
        events = await EventRepository(self.session).list(
            project_id=project.id, type="PROJECT_RECEIVED", limit=1
        )
        if not events:
            return project.title, None
        payload = events[0].payload
        return payload.get("description", project.title), payload.get("constraints")

    async def _call_llm(
        self,
        project: Project,
        *,
        description: str,
        constraints: str | None,
        extra_context: str | None,
    ) -> WebRequirementsSpec:
        prompt = render_prompt(
            "requirement_analyzer",
            title=project.title,
            stack=project.stack,
            description=description,
            constraints=constraints,
            extra_context=extra_context,
        )
        result = await complete(
            [{"role": "user", "content": prompt}],
            purpose=Purpose.PLANNING,
            json_schema=WebRequirementsSpec,
        )
        assert isinstance(result.parsed, WebRequirementsSpec)
        return result.parsed
