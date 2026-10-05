"""Evaluation checks registry (FR-043).

A Blueprint's ``evaluation`` list names checks from here by name - see
``blueprints.schema.VALID_EVALUATION_CHECKS``, which this module's ``CHECKS`` mapping must stay
in sync with (enforced by a test). Every check goes through ``workstation/tools.py`` like any
other worker action - never against the host or Docker directly - and returns a real, run result
(AGENTS.md rule "never mark a task done by assertion"). Most run a command inside the
workstation; ``openapi_valid`` instead reads the contract file through the tool layer and
validates it as data in-process (``yaml.safe_load`` + openapi-spec-validator, nothing from the
workspace is executed), so roles without RUN_COMMAND - the Architect, SRS section 10 - can use it.

Paths assume the golden-v1 template layout: a FastAPI app at ``backend/app`` (venv at
``backend/.venv``) and a Next.js app at ``frontend``. Commands never rely on a shell ``cd`` -
the workstation's command policy (workstation/policy.py) only allows a fixed set of program
names, ``cd`` is not one of them - so anything that needs a different working directory does it
with ``os.chdir`` inside a ``python -c`` script instead, or with npm's ``--prefix``/``exec``.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml
from openapi_spec_validator import validate as validate_openapi
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus
from web_department.config.permissions import Role
from web_department.db.models import Workstation
from web_department.workstation import tools
from web_department.workstation.manager import ExecResult, WorkstationManager

_BACKEND_PY = "backend/.venv/bin/python"
_BACKEND_RUFF = "backend/.venv/bin/ruff"
_BACKEND_MYPY = "backend/.venv/bin/mypy"
_BACKEND_PYTEST = "backend/.venv/bin/pytest"

# The project's API contract (FR-033, FR-085): written by the Architect, implemented by Backend,
# consumed by Frontend's generated client.
OPENAPI_CONTRACT_PATH = "openapi.yaml"

_MIGRATION_SCRIPT = (
    "import os\n"
    "os.chdir('backend')\n"
    "from alembic.config import Config\n"
    "from alembic import command\n"
    "cfg = Config('alembic.ini')\n"
    "command.downgrade(cfg, '-1')\n"
    "command.upgrade(cfg, 'head')\n"
    "print('migration_reversible: ok')\n"
)

# Common secret-shaped patterns; deliberately conservative (NFR/IR: never commit a real secret).
_SECRET_PATTERN = (
    r"(AKIA[0-9A-Z]{16}"
    r"|-----BEGIN (RSA|EC|OPENSSH|PGP) PRIVATE KEY-----"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,})"
)


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one evaluation check."""

    name: str
    passed: bool
    detail: str


async def _run(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str | None,
    manager: WorkstationManager,
    command: list[str],
    *,
    bus: EventBus | None = None,
    timeout: float | None = None,
) -> ExecResult:
    return await tools.run_command(
        session, ws, role, agent_id, manager, command, timeout=timeout, bus=bus
    )


def _ok(name: str, results: list[tuple[list[str], ExecResult]]) -> CheckResult:
    failed = [(cmd, r) for cmd, r in results if r.exit_code != 0]
    if not failed:
        return CheckResult(name, True, "all checks passed")
    lines = [f"{' '.join(cmd)} (exit {r.exit_code}): {r.stderr or r.stdout}" for cmd, r in failed]
    return CheckResult(name, False, "; ".join(lines))


async def lint_pass(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str | None,
    manager: WorkstationManager,
    *,
    bus: EventBus | None = None,
) -> CheckResult:
    """Backend ruff + mypy, frontend eslint + tsc, all clean."""
    commands = [
        [_BACKEND_RUFF, "check", "backend/app", "backend/tests"],
        [_BACKEND_MYPY, "backend/app"],
        ["npm", "--prefix", "frontend", "run", "lint"],
        ["npm", "--prefix", "frontend", "exec", "--", "tsc", "--noEmit"],
    ]
    results = [
        (cmd, await _run(session, ws, role, agent_id, manager, cmd, bus=bus)) for cmd in commands
    ]
    return _ok("lint_pass", results)


async def unit_tests_pass(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str | None,
    manager: WorkstationManager,
    *,
    bus: EventBus | None = None,
) -> CheckResult:
    """Backend pytest and frontend Vitest both pass."""
    commands = [
        [_BACKEND_PYTEST, "backend/tests"],
        ["npm", "--prefix", "frontend", "run", "test:unit"],
    ]
    results = [
        (cmd, await _run(session, ws, role, agent_id, manager, cmd, bus=bus)) for cmd in commands
    ]
    return _ok("unit_tests_pass", results)


async def e2e_pass(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str | None,
    manager: WorkstationManager,
    *,
    bus: EventBus | None = None,
) -> CheckResult:
    """Playwright end-to-end suite passes (browsers are baked into the golden image)."""
    result = await _run(
        session, ws, role, agent_id, manager, ["npm", "--prefix", "frontend", "run", "e2e"], bus=bus
    )
    return _ok("e2e_pass", [(["npm", "run", "e2e"], result)])


async def screenshot_attached(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str | None,
    manager: WorkstationManager,
    *,
    bus: EventBus | None = None,
) -> CheckResult:
    """At least one screenshot exists under qa/screenshots/ (the convention QA tasks write to)."""
    try:
        entries = await tools.list_dir(session, ws, role, agent_id, "qa/screenshots", bus=bus)
    except (OSError, FileNotFoundError):
        return CheckResult("screenshot_attached", False, "qa/screenshots/ does not exist")
    images = [e for e in entries if not e.endswith("/")]
    if not images:
        return CheckResult("screenshot_attached", False, "qa/screenshots/ is empty")
    return CheckResult("screenshot_attached", True, f"{len(images)} screenshot(s) found")


async def openapi_valid(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str | None,
    manager: WorkstationManager,
    *,
    bus: EventBus | None = None,
) -> CheckResult:
    """``openapi.yaml`` exists and is a valid OpenAPI 3.x document."""
    name = "openapi_valid"
    try:
        raw = await tools.read_file(session, ws, role, agent_id, OPENAPI_CONTRACT_PATH, bus=bus)
    except OSError:
        return CheckResult(name, False, f"{OPENAPI_CONTRACT_PATH} does not exist")
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return CheckResult(name, False, f"{OPENAPI_CONTRACT_PATH} is not valid YAML: {exc}")
    if not isinstance(document, dict) or not str(document.get("openapi", "")).startswith("3."):
        return CheckResult(
            name, False, f"{OPENAPI_CONTRACT_PATH} must be an OpenAPI 3.x document ('openapi: 3.x')"
        )
    try:
        validate_openapi(document)
    # The validator raises its own OpenAPIValidationError for schema violations, but also
    # assorted resolver/lookup errors for broken $refs - any of them means "not a valid contract".
    except Exception as exc:
        return CheckResult(name, False, f"{OPENAPI_CONTRACT_PATH} is invalid: {_first_line(exc)}")
    return CheckResult(name, True, f"{OPENAPI_CONTRACT_PATH} is a valid OpenAPI document")


def _first_line(exc: Exception) -> str:
    # openapi-spec-validator's messages append a multi-kilobyte dump of the schema it failed
    # against; the first line ("'info' is a required property") is the useful part.
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


async def migration_reversible(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str | None,
    manager: WorkstationManager,
    *,
    bus: EventBus | None = None,
) -> CheckResult:
    """The latest migration has a working downgrade: down one step, then back up to head."""
    result = await _run(
        session,
        ws,
        role,
        agent_id,
        manager,
        [_BACKEND_PY, "-c", _MIGRATION_SCRIPT],
        bus=bus,
        timeout=120,
    )
    return _ok("migration_reversible", [([_BACKEND_PY, "-c", "..."], result)])


async def no_secrets(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str | None,
    manager: WorkstationManager,
    *,
    bus: EventBus | None = None,
) -> CheckResult:
    """No secret-shaped string (API key, private key header, ...) is committed to the repo."""
    result = await _run(
        session,
        ws,
        role,
        agent_id,
        manager,
        ["grep", "-RIniE", _SECRET_PATTERN, "backend/app", "frontend/src"],
        bus=bus,
    )
    if result.exit_code == 1:
        return CheckResult("no_secrets", True, "no secret-shaped strings found")
    if result.exit_code == 0:
        return CheckResult("no_secrets", False, f"possible secret found: {result.stdout}")
    return CheckResult(
        "no_secrets", False, f"scan failed (exit {result.exit_code}): {result.stderr}"
    )


CHECKS = {
    "lint_pass": lint_pass,
    "unit_tests_pass": unit_tests_pass,
    "e2e_pass": e2e_pass,
    "screenshot_attached": screenshot_attached,
    "openapi_valid": openapi_valid,
    "migration_reversible": migration_reversible,
    "no_secrets": no_secrets,
}
