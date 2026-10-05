"""The Planner: WebRequirementsSpec -> a validated task DAG (FR-030..FR-032, FR-038).

Tasks come from two sources: a deterministic expansion of ``task_templates/golden_v1.yaml``
against the WRS's entities/pages/features (FR-031), plus one LLM call asking only for whatever
those templates don't cover (e.g. third-party integrations). The combined plan is validated as a
DAG (FR-032) and persisted with IDs assigned in topological order (FR-030).

Known limitation: the WRS has no explicit link between a page/entity and the feature it serves,
so ``api_integration`` depends on *every* page and crud_api instance, not just the relevant ones.
"""

import graphlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus, EventType, emit
from web_department.config.permissions import Role
from web_department.db.models import Project, RequirementSpec, Task
from web_department.db.repositories import ProjectRepository, TaskRepository
from web_department.db.states import ProjectState
from web_department.llm.prompts import render as render_prompt
from web_department.llm.router import Purpose, complete
from web_department.orchestrator.wrs_schema import WebRequirementsSpec

DEFAULT_TEMPLATES_PATH = Path(__file__).parent / "task_templates" / "golden_v1.yaml"

MAX_FILES_PER_TASK = 5
MAX_LINES_PER_TASK = 300


class PlanValidationError(Exception):
    """Raised when a generated plan has a cycle or a missing dependency (FR-032)."""


class TaskTemplate(BaseModel):
    """One entry from golden_v1.yaml."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    role: Role
    parameterized_by: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    acceptance_checks: list[str] = Field(default_factory=list)
    est_files: int = Field(default=1, ge=1)
    est_lines: int = Field(default=1, ge=1)


class ExtraTask(BaseModel):
    """One LLM-proposed task not covered by the template library."""

    model_config = ConfigDict(extra="forbid")

    title: str
    role: Role
    description: str
    depends_on_templates: list[str] = Field(default_factory=list)
    acceptance_checks: list[str] = Field(min_length=1)
    est_files: int = Field(default=1, ge=1)
    est_lines: int = Field(default=1, ge=1)


class ExtraTasksResponse(BaseModel):
    """The LLM's answer to "what's missing from the deterministic plan?"."""

    model_config = ConfigDict(extra="forbid")

    tasks: list[ExtraTask] = Field(default_factory=list)


@dataclass
class PlannedTask:
    """One not-yet-persisted task in the plan being built."""

    key: str
    title: str
    role: str
    depends_on: set[str] = field(default_factory=set)
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    acceptance_checks: list[str] = field(default_factory=list)


def load_templates(path: Path = DEFAULT_TEMPLATES_PATH) -> list[TaskTemplate]:
    """Parse golden_v1.yaml into ``TaskTemplate`` objects, in file order."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [TaskTemplate.model_validate(t) for t in data.get("templates") or []]


def split_count(est_files: int, est_lines: int) -> int:
    """How many parts a task of this size must split into (FR-038: >5 files or >300 lines)."""
    by_files = -(-est_files // MAX_FILES_PER_TASK)
    by_lines = -(-est_lines // MAX_LINES_PER_TASK)
    return max(by_files, by_lines, 1)


def _template_items(
    template: TaskTemplate, wrs: WebRequirementsSpec
) -> list[tuple[str, dict[str, Any]]]:
    """Return [(item_key, context)] - one pair per instance this template produces."""
    if template.parameterized_by is None:
        return [("", {})]
    if template.parameterized_by == "entities":
        return [(e.name, {"entity": e.name}) for e in wrs.entities]
    if template.parameterized_by == "pages":
        return [(p.route, {"route": p.route}) for p in wrs.pages]
    if template.parameterized_by == "features":
        return [(f.id, {"feature": f.id}) for f in wrs.features]
    raise PlanValidationError(f"Unknown parameterized_by: {template.parameterized_by!r}")


def _expand_parts(
    key_prefix: str,
    base_title: str,
    role: str,
    item_key: str,
    context: dict[str, Any],
    outputs: list[str],
    acceptance_checks: list[str],
    n: int,
) -> dict[str, PlannedTask]:
    """Build ``n`` sequentially-chained PlannedTask parts for one template/extra-task instance."""
    part_keys = [f"{key_prefix}:{i + 1}" for i in range(n)]
    title = base_title if not item_key else f"{base_title}: {item_key}"
    parts: dict[str, PlannedTask] = {}
    for i, key in enumerate(part_keys):
        is_last = i == n - 1
        part_title = title if n == 1 else f"{title} (part {i + 1}/{n})"
        checks = (
            [c.format(param=item_key) for c in acceptance_checks]
            if is_last
            else [f"Part {i + 1}/{n} of {title} compiles and lints cleanly"]
        )
        parts[key] = PlannedTask(
            key=key,
            title=part_title,
            role=role,
            depends_on={part_keys[i - 1]} if i > 0 else set(),
            inputs=dict(context),
            outputs=outputs if is_last else [],
            acceptance_checks=checks,
        )
    return parts


def validate_and_order(planned: dict[str, PlannedTask]) -> list[str]:
    """Validate the plan is a DAG (FR-032) and return its keys in topological order."""
    all_keys = set(planned)
    for key, item in planned.items():
        missing = item.depends_on - all_keys
        if missing:
            raise PlanValidationError(f"{key!r} depends on missing task(s): {sorted(missing)}")
    sorter = graphlib.TopologicalSorter({k: v.depends_on for k, v in planned.items()})
    try:
        return list(sorter.static_order())
    except graphlib.CycleError as exc:
        raise PlanValidationError(f"Dependency cycle detected: {exc.args[1]}") from exc


def plan_to_mermaid(tasks: list[Task]) -> str:
    """Render a task list (with ``depends_on`` loaded) as a Mermaid flowchart string."""
    lines = ["flowchart TD"]
    for task in tasks:
        label = f"{task.display_id}: {task.title}".replace('"', "'")
        lines.append(f'    {task.display_id}["{label}"]')
    edges = sorted((dep.display_id, task.display_id) for task in tasks for dep in task.depends_on)
    lines.extend(f"    {src} --> {dst}" for src, dst in edges)
    return "\n".join(lines)


class Planner:
    """Turns a project's WRS into a persisted, validated task DAG."""

    def __init__(self, session: AsyncSession, bus: EventBus | None = None) -> None:
        self.session = session
        self.bus = bus

    async def plan(self, project: Project, spec: RequirementSpec) -> list[Task]:
        """Plan ``project`` from ``spec`` (FR-030): expand templates, add extra tasks, persist."""
        wrs = WebRequirementsSpec.model_validate(spec.json)
        templates = load_templates()

        planned, last_parts_by_template = self._expand_templates(templates, wrs)
        planned.update(await self._extra_tasks(templates, wrs, planned, last_parts_by_template))
        order = validate_and_order(planned)

        tasks = await self._persist(project, planned, order)
        await ProjectRepository(self.session, bus=self.bus).update_state(
            project.id, ProjectState.ARCHITECTING
        )
        return tasks

    def _expand_templates(
        self, templates: list[TaskTemplate], wrs: WebRequirementsSpec
    ) -> tuple[dict[str, PlannedTask], dict[str, list[str]]]:
        planned: dict[str, PlannedTask] = {}
        last_parts_by_template: dict[str, list[str]] = {}

        for template in templates:
            for item_key, context in _template_items(template, wrs):
                n = split_count(template.est_files, template.est_lines)
                prefix = f"{template.id}:{item_key}" if item_key else template.id
                parts = _expand_parts(
                    prefix,
                    template.title,
                    template.role.value,
                    item_key,
                    {**context, "template": template.id},
                    template.outputs,
                    template.acceptance_checks,
                    n,
                )
                planned.update(parts)
                last_parts_by_template.setdefault(template.id, []).append(f"{prefix}:{n}")

        for template in templates:
            for item_key, _ in _template_items(template, wrs):
                prefix = f"{template.id}:{item_key}" if item_key else template.id
                first_key = f"{prefix}:1"
                for dep_id in template.depends_on:
                    planned[first_key].depends_on.update(last_parts_by_template.get(dep_id, []))

        return planned, last_parts_by_template

    async def _extra_tasks(
        self,
        templates: list[TaskTemplate],
        wrs: WebRequirementsSpec,
        planned: dict[str, PlannedTask],
        last_parts_by_template: dict[str, list[str]],
    ) -> dict[str, PlannedTask]:
        known_ids = {t.id for t in templates}
        prompt = render_prompt(
            "planner_extra_tasks",
            wrs=wrs.model_dump_json(indent=2),
            already_planned=sorted({p.title for p in planned.values()}),
            template_ids=sorted(known_ids),
        )
        result = await complete(
            [{"role": "user", "content": prompt}],
            purpose=Purpose.PLANNING,
            json_schema=ExtraTasksResponse,
        )
        assert isinstance(result.parsed, ExtraTasksResponse)

        extra: dict[str, PlannedTask] = {}
        for index, item in enumerate(result.parsed.tasks, start=1):
            unknown = set(item.depends_on_templates) - known_ids
            if unknown:
                raise PlanValidationError(
                    f"Extra task {item.title!r} depends on unknown template(s): {sorted(unknown)}"
                )
            n = split_count(item.est_files, item.est_lines)
            parts = _expand_parts(
                f"extra:{index}",
                item.title,
                item.role.value,
                "",
                {"template": "extra", "description": item.description},
                [],
                item.acceptance_checks,
                n,
            )
            first_key = f"extra:{index}:1"
            for dep_id in item.depends_on_templates:
                parts[first_key].depends_on.update(last_parts_by_template.get(dep_id, []))
            extra.update(parts)
        return extra

    async def _persist(
        self, project: Project, planned: dict[str, PlannedTask], order: list[str]
    ) -> list[Task]:
        task_repo = TaskRepository(self.session, bus=self.bus)
        created: dict[str, Task] = {}
        for index, key in enumerate(order, start=1):
            item = planned[key]
            task = await task_repo.create(
                display_id=f"WEB-{index:03d}",
                project_id=project.id,
                title=item.title,
                role=item.role,
                inputs=item.inputs,
                outputs={"produces": item.outputs},
                acceptance_checks=item.acceptance_checks,
            )
            created[key] = task
            await emit(
                EventType.TASK_CREATED,
                session=self.session,
                bus=self.bus,
                project_id=project.id,
                task_id=task.id,
                payload={"display_id": task.display_id, "title": task.title, "role": task.role},
            )
        for key in order:
            for dep_key in planned[key].depends_on:
                await task_repo.add_dependency(created[key], created[dep_key])
        return [created[k] for k in order]
