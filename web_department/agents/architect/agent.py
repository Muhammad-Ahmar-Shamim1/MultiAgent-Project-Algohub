"""The Architect: turns the WRS into the project's architecture record (FR-033).

Outputs, all three on every run (a re-run rewrites them as a whole, keeping them consistent):

- ``docs/architecture.md`` - components, their responsibilities, and the decision log;
- ``openapi.yaml`` - the API contract for every WRS feature (FR-085's contract, which Backend
  implements and Frontend generates its typed client from);
- ``db/schema.md`` - entities, relations and indexes, which the Database worker turns into models
  and a migration.

What "this task's checks pass" means here (``_run_checks``), instead of BaseWorker's pytest - the
Architect has no RUN_COMMAND permission (SRS section 10), and its outputs are documents, not
code:

1. all three files exist and are non-empty;
2. the Blueprint's evaluation checks pass (``openapi_valid``, qa/checks.py);
3. coverage, so "contracts for all features" is checked rather than asserted: every WRS feature
   id tags at least one operation in ``openapi.yaml``, and every WRS entity has its own heading
   in ``db/schema.md``.

Its WRITE_CODE grant covers "docs and scaffolding" only (SRS section 10, config/permissions.py),
so ``_reject_plan`` refuses any plan that writes anywhere but ``docs/``, ``db/schema.md`` or
``openapi.yaml`` - before anything is written.
"""

import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.agents.base import BaseWorker, CheckFailure, WorkerPlan
from web_department.collaboration.events import EventBus
from web_department.config.permissions import Role
from web_department.config.settings import get_settings
from web_department.db.models import Agent, Task, Workstation
from web_department.llm.prompts import render as render_prompt
from web_department.llm.router import Purpose
from web_department.orchestrator.wrs_schema import WebRequirementsSpec
from web_department.qa.checks import OPENAPI_CONTRACT_PATH
from web_department.workstation import tools
from web_department.workstation.manager import WorkstationManager

ARCHITECTURE_DOC_PATH = "docs/architecture.md"
SCHEMA_DOC_PATH = "db/schema.md"
OUTPUT_PATHS = (ARCHITECTURE_DOC_PATH, OPENAPI_CONTRACT_PATH, SCHEMA_DOC_PATH)

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
_HEADING = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*#*\s*$", re.MULTILINE)


class ArchitectWorker(BaseWorker):
    """Writes ``docs/architecture.md``, ``openapi.yaml`` and ``db/schema.md`` from the WRS.

    ``purpose=planning`` (its Blueprint's first model preference). ``confidential`` follows
    ``Settings.coder_confidential`` like ``CoderWorker``: the architecture record is the
    project's own design, so it gets the same opt-out-only protection as its code (IR-024).
    """

    role = Role.ARCHITECT
    purpose = Purpose.PLANNING
    system_prompt_name = "worker_architect"
    evaluation_checks = ("openapi_valid",)

    def __init__(
        self,
        session: AsyncSession,
        agent: Agent,
        blueprint: dict[str, Any],
        workstation: Workstation,
        manager: WorkstationManager,
        bus: EventBus | None = None,
        fs_guard: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    ) -> None:
        super().__init__(
            session, agent, blueprint, workstation, manager, bus=bus, fs_guard=fs_guard
        )
        self.confidential = get_settings().coder_confidential

    async def _build_initial_messages(self, task: Task) -> list[dict[str, str]]:
        """Context: the template guide, the whole WRS, and the current architecture record.

        Unlike the default builder this sends the *whole* WRS (the record must cover every
        feature, entity and page) and no source-tree listing - the Architect designs against the
        requirements and the template's conventions, not against code other workers wrote.
        """
        wrs = await self._load_wrs(task)
        prompt = render_prompt(
            self.system_prompt_name,
            template_guide=await self._template_guide(),
            task_title=task.title,
            task_inputs="\n".join(f"- {key}: {value}" for key, value in task.inputs.items()),
            acceptance_checks=task.acceptance_checks,
            wrs_json=wrs.model_dump_json(indent=2) if wrs is not None else None,
            output_paths=OUTPUT_PATHS,
            existing_outputs=await self._existing_outputs(),
        )
        return [{"role": "user", "content": prompt}]

    async def _existing_outputs(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for path in OUTPUT_PATHS:
            content = await self._read_optional(path)
            if content:
                found[path] = content
        return found

    def _reject_plan(self, task: Task, plan: WorkerPlan) -> str | None:
        outside = [f.path for f in plan.files if not _in_architect_scope(f.path)]
        if not outside:
            return None
        return (
            f"The Architect may only write under docs/, {SCHEMA_DOC_PATH} or "
            f"{OPENAPI_CONTRACT_PATH}; this plan also writes {outside}. Return the plan again "
            f"without those files - implementation belongs to the other workers."
        )

    async def _run_checks(self, task: Task) -> CheckFailure | None:
        contents = {path: await self._read_optional(path) for path in OUTPUT_PATHS}
        missing = [path for path, content in contents.items() if not (content or "").strip()]
        if missing:
            return _failure("architecture record", f"Missing or empty required file(s): {missing}")

        failure = await self._run_evaluation_checks()
        if failure is not None:
            return failure

        wrs = await self._load_wrs(task)
        if wrs is None:
            return None
        problems = _coverage_problems(
            wrs, contents[OPENAPI_CONTRACT_PATH] or "", contents[SCHEMA_DOC_PATH] or ""
        )
        if problems:
            return _failure("architecture coverage", "\n".join(problems))
        return None

    async def _read_optional(self, path: str) -> str | None:
        try:
            return await tools.read_file(
                self.session, self.workstation, self.role, self.agent.id, path, bus=self.bus
            )
        except OSError:
            return None


def _in_architect_scope(path: str) -> bool:
    normalized = path.replace("\\", "/").removeprefix("./")
    return normalized.startswith("docs/") or normalized in (
        SCHEMA_DOC_PATH,
        OPENAPI_CONTRACT_PATH,
    )


def _failure(command: str, detail: str) -> CheckFailure:
    return CheckFailure(command=command, exit_code=1, stdout=detail, stderr="")


def _coverage_problems(wrs: WebRequirementsSpec, openapi_raw: str, schema_md: str) -> list[str]:
    """Every WRS feature tags an operation; every WRS entity has a heading in the schema doc.

    ``openapi_raw`` has already passed ``openapi_valid``, so it parses to a mapping.
    """
    problems: list[str] = []

    document = yaml.safe_load(openapi_raw)
    tagged: set[str] = set()
    for path_item in (document.get("paths") or {}).values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method in _HTTP_METHODS and isinstance(operation, dict):
                tagged.update(str(tag) for tag in operation.get("tags") or [])
    untagged = [f.id for f in wrs.features if f.id not in tagged]
    if untagged:
        problems.append(
            f"{OPENAPI_CONTRACT_PATH}: no operation is tagged with feature id(s) {untagged} - "
            f"tag each operation with the id of the WRS feature(s) it serves."
        )

    headings = {m["title"].strip("`* ").lower() for m in _HEADING.finditer(schema_md)}
    no_heading = [e.name for e in wrs.entities if e.name.lower() not in headings]
    if no_heading:
        problems.append(
            f"{SCHEMA_DOC_PATH}: no heading for entity(ies) {no_heading} - give each WRS entity "
            f"its own heading (e.g. '## {no_heading[0]}')."
        )
    return problems
