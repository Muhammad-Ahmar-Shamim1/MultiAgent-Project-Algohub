"""The QA worker (FR-100, FR-101): writes at least one test per WRS acceptance criterion.

Each test is tagged with its criterion in the test's name (``test_F02_1_cart_total_updates``,
see ``qa/test_runner.py`` for the convention) - that tag is how the QA report computes criteria
coverage and how failure triage finds the feature a failing test belongs to.

A task covers its feature's criteria (``inputs.feature``, as the Planner's ``tests_e2e`` tasks
set it) or, without one, every criterion in the WRS.

What "this task's checks pass" means here (``_run_checks``): every one of those criteria has at
least one tagged test that pytest or Playwright *collects* - not that the tests pass. QA writes
tests and reports bugs; it never fixes application code (SRS section 10: tests only), so a test
that correctly fails against a buggy app is a finished QA task, and the failure is the quality
gate's (``qa/quality_gate.py``) and triage's (``qa/failure_triage.py``) to handle. That is also
why this worker does not run its Blueprint's ``unit_tests_pass``/``e2e_pass`` evaluation checks
as its own task checks - those belong to the gate.

``_reject_plan`` refuses any file outside ``backend/tests/`` and ``frontend/tests/``.
"""

import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from web_department.agents.base import BaseWorker, CheckFailure, WorkerPlan
from web_department.collaboration.events import EventBus
from web_department.config.permissions import Role
from web_department.config.settings import get_settings
from web_department.db.models import Agent, Task, Workstation
from web_department.llm.prompts import render as render_prompt
from web_department.llm.router import Purpose
from web_department.qa.checks import OPENAPI_CONTRACT_PATH
from web_department.qa.test_runner import (
    Criterion,
    criteria_for_test,
    criteria_from_wrs,
    criterion_tag,
)
from web_department.workstation import tools
from web_department.workstation.manager import WorkstationManager

TEST_ROOTS = ("backend/tests/", "frontend/tests/")
_TEST_DIRS = ("backend/tests", "frontend/tests/unit", "frontend/tests/e2e")
_PYTEST = "backend/.venv/bin/pytest"
_PYTEST_NO_TESTS = 5
_COLLECT_TIMEOUT_SECONDS = 300
_OPENAPI_LIMIT = 12_000
# Playwright `--list` lines look like "  [chromium] › home.spec.ts:3:5 › F02_1 cart total".
_PLAYWRIGHT_LIST_LINE = re.compile(r"›\s*(?P<title>[^›]+?)\s*$")


class QAWorker(BaseWorker):
    """Writes criterion-tagged pytest/Playwright tests for its task's acceptance criteria."""

    role = Role.QA
    purpose = Purpose.CODING
    system_prompt_name = "worker_qa"

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
        # The tests and the API contract it reads are the project's own code (IR-024), like
        # CoderWorker's and ArchitectWorker's.
        self.confidential = get_settings().coder_confidential

    async def _criteria(self, task: Task) -> list[Criterion]:
        wrs = await self._load_wrs(task)
        if wrs is None:
            return []
        feature_id = task.inputs.get("feature")
        return criteria_from_wrs(wrs, str(feature_id) if feature_id else None)

    async def _build_initial_messages(self, task: Task) -> list[dict[str, str]]:
        """Context: template guide, the criteria with their tags, pages, the API contract and
        the existing test files - not the application source tree.
        """
        wrs = await self._load_wrs(task)
        criteria = await self._criteria(task)
        openapi = await self._read_optional(OPENAPI_CONTRACT_PATH)
        prompt = render_prompt(
            self.system_prompt_name,
            template_guide=await self._template_guide(),
            task_title=task.title,
            task_inputs="\n".join(f"- {key}: {value}" for key, value in task.inputs.items()),
            criteria=[{"id": c.id, "tag": criterion_tag(c), "text": c.text} for c in criteria],
            pages=[p.model_dump() for p in wrs.pages] if wrs is not None else [],
            openapi=openapi[:_OPENAPI_LIMIT] if openapi else None,
            test_files=await self._test_files(),
        )
        return [{"role": "user", "content": prompt}]

    async def _test_files(self) -> str:
        lines: list[str] = []
        for directory in _TEST_DIRS:
            try:
                names = await tools.list_dir(
                    self.session,
                    self.workstation,
                    self.role,
                    self.agent.id,
                    directory,
                    bus=self.bus,
                )
            except OSError:
                continue
            lines.append(f"{directory}/:")
            lines.extend(f"  {name}" for name in names)
        return "\n".join(lines)

    def _reject_plan(self, task: Task, plan: WorkerPlan) -> str | None:
        outside = [
            f.path
            for f in plan.files
            if not f.path.replace("\\", "/").removeprefix("./").startswith(TEST_ROOTS)
        ]
        if not outside:
            return None
        return (
            f"QA may only write test files under {list(TEST_ROOTS)}; this plan also writes "
            f"{outside}. Report application bugs through failing tests - never edit the app."
        )

    async def _run_checks(self, task: Task) -> CheckFailure | None:
        criteria = await self._criteria(task)
        names: list[str] = []

        pytest_result = await tools.run_command(
            self.session,
            self.workstation,
            self.role,
            self.agent.id,
            self.manager,
            [_PYTEST, "backend/tests", "--collect-only", "-q"],
            timeout=_COLLECT_TIMEOUT_SECONDS,
            bus=self.bus,
        )
        if pytest_result.exit_code not in (0, _PYTEST_NO_TESTS):
            return CheckFailure(
                command="pytest --collect-only",
                exit_code=pytest_result.exit_code,
                stdout=pytest_result.stdout,
                stderr=pytest_result.stderr,
            )
        names += pytest_names(pytest_result.stdout)

        playwright_result = await tools.run_command(
            self.session,
            self.workstation,
            self.role,
            self.agent.id,
            self.manager,
            ["npm", "--prefix", "frontend", "run", "e2e", "--", "--list"],
            timeout=_COLLECT_TIMEOUT_SECONDS,
            bus=self.bus,
        )
        if playwright_result.exit_code != 0:
            return CheckFailure(
                command="playwright test --list",
                exit_code=playwright_result.exit_code,
                stdout=playwright_result.stdout,
                stderr=playwright_result.stderr,
            )
        names += playwright_names(playwright_result.stdout)

        covered = {c.id for name in names for c in criteria_for_test(name, criteria)}
        missing = [c for c in criteria if c.id not in covered]
        if not missing:
            return None
        return CheckFailure(
            command="criteria coverage",
            exit_code=1,
            stdout="No collected test is tagged for: "
            + "; ".join(f"{c.id} (tag {criterion_tag(c)}): {c.text}" for c in missing),
            stderr="",
        )

    async def _read_optional(self, path: str) -> str | None:
        try:
            return await tools.read_file(
                self.session, self.workstation, self.role, self.agent.id, path, bus=self.bus
            )
        except OSError:
            return None


def pytest_names(collect_output: str) -> list[str]:
    """Test function names from ``pytest --collect-only -q`` (``path::[Class::]name[params]``)."""
    return [
        line.strip().rsplit("::", 1)[-1].split("[", 1)[0]
        for line in collect_output.splitlines()
        if "::" in line
    ]


def playwright_names(list_output: str) -> list[str]:
    """Test titles from ``playwright test --list``."""
    return [
        m["title"] for line in list_output.splitlines() if (m := _PLAYWRIGHT_LIST_LINE.search(line))
    ]
