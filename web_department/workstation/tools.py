"""Tool functions a worker calls inside its workstation (FR-062, FR-081).

Every tool: (1) checks the calling role holds the ``Permission`` it needs, (2) for filesystem
tools, confirms the resolved path stays inside the workstation's ``/workspace``, (3) for
``run_command``, applies the allow/deny policy, and (4) emits a ``TOOL_CALL`` event.

File I/O (``read_file``, ``write_file``, ``list_dir``, ``search``) reads/writes the host side of
the workstation's bind mount directly - no need to exec into the container for that. Everything
that needs the container's own toolchain (``run_command``, ``run_tests``, ``git_commit``,
``browser_screenshot``) goes through ``WorkstationManager.exec``.
"""

import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus, EventType, emit
from web_department.config.permissions import Permission, Role, has_permission
from web_department.db.models import Workstation
from web_department.db.repositories import ApprovalRepository
from web_department.workstation.manager import ExecResult, WorkstationManager
from web_department.workstation.policy import (
    ApprovalRequired,
    PermissionDenied,
    PolicyDecision,
    evaluate_command,
)

MAX_SEARCH_MATCHES = 500


class ToolPermissionError(Exception):
    """Raised when the calling role lacks the permission a tool requires (FR-062)."""


class PathEscapeError(Exception):
    """Raised when a requested path would resolve outside the workstation's /workspace."""


def resolve_workspace_path(repo_path: str, relative_path: str) -> Path:
    """Resolve ``relative_path`` against ``repo_path``; raise if it would escape that directory."""
    base = Path(repo_path).resolve()
    candidate = (base / relative_path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise PathEscapeError(
            f"{relative_path!r} would resolve outside the workstation's /workspace"
        ) from None
    return candidate


def _require(role: Role, permission: Permission) -> None:
    if not has_permission(role, permission):
        raise ToolPermissionError(f"role {role.value!r} lacks permission {permission.value!r}")


def _require_any(role: Role, *permissions: Permission) -> None:
    if not any(has_permission(role, permission) for permission in permissions):
        names = " or ".join(p.value for p in permissions)
        raise ToolPermissionError(f"role {role.value!r} lacks permission {names!r}")


async def _emit_tool_call(
    session: AsyncSession,
    bus: EventBus | None,
    ws: Workstation,
    agent_id: str | None,
    tool: str,
    payload: dict[str, Any],
    *,
    status: str = "ok",
) -> None:
    await emit(
        EventType.TOOL_CALL,
        session=session,
        bus=bus,
        project_id=ws.project_id,
        agent_id=agent_id,
        status=status,
        payload={"tool": tool, **payload},
    )


async def read_file(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str | None,
    path: str,
    *,
    bus: EventBus | None = None,
) -> str:
    """Read a text file from the workstation's workspace (FR-065's "eyes")."""
    _require(role, Permission.READ_CODE)
    full_path = resolve_workspace_path(ws.repo_path, path)
    content = full_path.read_text(encoding="utf-8")
    await _emit_tool_call(session, bus, ws, agent_id, "read_file", {"path": path})
    return content


async def write_file(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str | None,
    path: str,
    content: str,
    *,
    bus: EventBus | None = None,
) -> None:
    """Write a text file into the workstation's workspace (FR-065's "hands").

    Accepts either WRITE_CODE (most roles) or WRITE_TESTS (QA, whose grant is test files only -
    the path/target scoping itself is enforced by the caller's own task, not here).
    """
    _require_any(role, Permission.WRITE_CODE, Permission.WRITE_TESTS)
    full_path = resolve_workspace_path(ws.repo_path, path)
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    await _emit_tool_call(session, bus, ws, agent_id, "write_file", {"path": path})


async def list_dir(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str | None,
    path: str = ".",
    *,
    bus: EventBus | None = None,
) -> list[str]:
    """List one directory's entries (directories suffixed with ``/``)."""
    _require(role, Permission.READ_CODE)
    full_path = resolve_workspace_path(ws.repo_path, path)
    names = sorted(p.name + ("/" if p.is_dir() else "") for p in full_path.iterdir())
    await _emit_tool_call(session, bus, ws, agent_id, "list_dir", {"path": path})
    return names


async def search(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str | None,
    pattern: str,
    *,
    path: str = ".",
    bus: EventBus | None = None,
) -> list[dict[str, Any]]:
    """Regex-search text files under ``path``; returns up to ``MAX_SEARCH_MATCHES`` hits."""
    _require(role, Permission.READ_CODE)
    base = resolve_workspace_path(ws.repo_path, path)
    regex = re.compile(pattern)
    matches: list[dict[str, Any]] = []
    for file in sorted(base.rglob("*")):
        if not file.is_file() or len(matches) >= MAX_SEARCH_MATCHES:
            continue
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(lines, start=1):
            if regex.search(line):
                rel = file.relative_to(base).as_posix()  # POSIX-style regardless of host OS
                matches.append({"path": rel, "line": lineno, "text": line})
                if len(matches) >= MAX_SEARCH_MATCHES:
                    break
    await _emit_tool_call(session, bus, ws, agent_id, "search", {"pattern": pattern, "path": path})
    return matches


async def run_command(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str | None,
    manager: WorkstationManager,
    command: list[str],
    *,
    timeout: float | None = None,
    bus: EventBus | None = None,
) -> ExecResult:
    """Run a shell command inside the workstation (FR-062, FR-063).

    Raises ``PermissionDenied`` for a hard-denied command, or ``ApprovalRequired`` (recording an
    Approval and emitting APPROVAL_REQUESTED - FR-130) for a destructive-but-allowed one; neither
    case runs anything.
    """
    _require(role, Permission.RUN_COMMAND)
    joined = " ".join(command)
    result = evaluate_command(joined)

    if result.decision is PolicyDecision.DENIED:
        await _emit_tool_call(
            session, bus, ws, agent_id, "run_command", {"command": joined}, status="denied"
        )
        raise PermissionDenied(result.reason)

    if result.decision is PolicyDecision.APPROVAL_REQUIRED:
        approval = await ApprovalRepository(session).create(
            project_id=ws.project_id,
            action_type="run_command",
            summary=joined,
            status="pending",
        )
        await emit(
            EventType.APPROVAL_REQUESTED,
            session=session,
            bus=bus,
            project_id=ws.project_id,
            agent_id=agent_id,
            payload={"command": joined, "approval_id": approval.id, "reason": result.reason},
        )
        await _emit_tool_call(
            session,
            bus,
            ws,
            agent_id,
            "run_command",
            {"command": joined, "approval_id": approval.id},
            status="approval_required",
        )
        raise ApprovalRequired(result.reason)

    exec_result = await manager.exec(ws, command, timeout=timeout)
    await _emit_tool_call(
        session,
        bus,
        ws,
        agent_id,
        "run_command",
        {"command": joined, "exit_code": exec_result.exit_code},
        status="ok" if exec_result.exit_code == 0 else "error",
    )
    return exec_result


async def run_tests(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str | None,
    manager: WorkstationManager,
    *,
    path: str = ".",
    bus: EventBus | None = None,
) -> ExecResult:
    """Run pytest under ``path`` inside the workstation."""
    return await run_command(session, ws, role, agent_id, manager, ["pytest", path], bus=bus)


async def git_commit(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str | None,
    manager: WorkstationManager,
    message: str,
    *,
    bus: EventBus | None = None,
) -> ExecResult:
    """Stage everything and commit it inside the workstation."""
    _require_any(role, Permission.WRITE_CODE, Permission.WRITE_TESTS)
    add_result = await manager.exec(ws, ["git", "add", "-A"])
    if add_result.exit_code != 0:
        await _emit_tool_call(
            session, bus, ws, agent_id, "git_commit", {"stage": "add"}, status="error"
        )
        return add_result
    result = await manager.exec(ws, ["git", "commit", "-m", message])
    await _emit_tool_call(
        session,
        bus,
        ws,
        agent_id,
        "git_commit",
        {"message": message},
        status="ok" if result.exit_code == 0 else "error",
    )
    return result


async def browser_screenshot(
    session: AsyncSession,
    ws: Workstation,
    role: Role,
    agent_id: str | None,
    manager: WorkstationManager,
    url: str,
    *,
    bus: EventBus | None = None,
) -> bytes:
    """Screenshot ``url`` with headless Chromium inside the workstation and return the PNG bytes."""
    _require(role, Permission.BROWSER)
    out_name = f".algohub_screenshot_{uuid.uuid4().hex}.png"
    script = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p:\n"
        "    browser = p.chromium.launch()\n"
        "    page = browser.new_page()\n"
        f"    page.goto({url!r})\n"
        f"    page.screenshot(path={out_name!r})\n"
        "    browser.close()\n"
    )
    result = await manager.exec(ws, ["python3", "-c", script], timeout=60)
    if result.exit_code != 0:
        await _emit_tool_call(
            session, bus, ws, agent_id, "browser_screenshot", {"url": url}, status="error"
        )
        raise RuntimeError(f"browser_screenshot failed: {result.stderr}")

    out_path = resolve_workspace_path(ws.repo_path, out_name)
    data = out_path.read_bytes()
    out_path.unlink(missing_ok=True)
    await _emit_tool_call(session, bus, ws, agent_id, "browser_screenshot", {"url": url})
    return data
