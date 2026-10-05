"""Command allow/deny policy for the workstation tool layer (FR-062, FR-063, NFR-007).

Every command a worker wants to run is classified before it ever reaches Docker:
- **denied** outright (a hard-deny pattern, or the program isn't on the allow-list) -> the tool
  layer raises ``PermissionDenied`` and nothing runs.
- **needs approval** (destructive but legitimate, e.g. ``git reset --hard``) -> the tool layer
  raises ``ApprovalRequired`` and creates an Approval record instead of running it (FR-130).
- **allowed** -> runs normally.
"""

import re
import shlex
from dataclasses import dataclass
from enum import StrEnum

#: Program names a command's first token may be. Anything else is denied (FR-062).
ALLOWED_PROGRAMS = frozenset(
    {
        "git",
        "npm",
        "pnpm",
        "npx",
        "python",
        "python3",
        "pip",
        "pip3",
        "pytest",
        "uvicorn",
        "node",
        "ls",
        "cat",
        "sed",
        "grep",
        "mkdir",
        "cp",
        "mv",
        "playwright",
        "alembic",
        "ruff",
        "mypy",
        "vitest",
        "tsc",
        "pwd",
        "echo",
        "find",
        "touch",
        "rm",
        "psql",
        # Security scanners (FR-110, security/scanners.py).
        "semgrep",
        "gitleaks",
        "trivy",
        "pip-audit",
    }
)

#: `rm` targets dangerous enough that even `-rf` against them is a hard deny, not just an approval.
_RM_FORBIDDEN_TARGETS = frozenset({"/", "/*", "~", "$HOME", ".", "..", "*"})

# Checked over the raw command string: always denied outright, regardless of the allow-list.
_HARD_DENY_PATTERNS = [
    re.compile(r"\bsudo\b"),
    re.compile(r"\bchmod\s+777\b"),
    re.compile(r"\bdrop\s+database\b", re.IGNORECASE),
    re.compile(r"\bdocker\b"),
    re.compile(r"\b(curl|wget)\b[^|;]*\|\s*(sh|bash)\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\b(shutdown|reboot)\b"),
    re.compile(r">\s*/dev/sd[a-z]"),
]

# Checked over the raw command string: destructive but sometimes legitimate -> held for approval.
_APPROVAL_PATTERNS = [
    re.compile(r"\bgit\s+push\b[^|;]*--force\b"),
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bdrop\s+table\b", re.IGNORECASE),
    re.compile(r"\btruncate\b", re.IGNORECASE),
]


class PolicyDecision(StrEnum):
    """The verdict ``evaluate_command`` reaches for one command string."""

    ALLOWED = "allowed"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True)
class PolicyResult:
    """A decision plus the human-readable reason for it."""

    decision: PolicyDecision
    reason: str


class PermissionDenied(Exception):
    """Raised for a command that is never allowed to run (FR-063)."""


class ApprovalRequired(Exception):
    """Raised for a destructive-but-legitimate command that needs human sign-off (FR-063)."""


def _rm_flags_and_targets(tokens: list[str]) -> tuple[set[str], list[str]]:
    """Return (short flag letters, non-flag arguments) for an `rm` invocation."""
    flags: set[str] = set()
    targets: list[str] = []
    for token in tokens[1:]:
        if token == "--recursive":
            flags.add("r")
        elif token == "--force":
            flags.add("f")
        elif token.startswith("--"):
            continue
        elif token.startswith("-") and len(token) > 1:
            flags.update(token[1:].lower())
        else:
            targets.append(token)
    return flags, targets


def _classify_rm(tokens: list[str]) -> PolicyResult | None:
    """Special-case `rm`: recursive+force against a root-ish path is a hard deny, not approval."""
    if not tokens or tokens[0] != "rm":
        return None
    flags, targets = _rm_flags_and_targets(tokens)
    is_recursive_force = "r" in flags and "f" in flags
    if is_recursive_force and (not targets or any(t in _RM_FORBIDDEN_TARGETS for t in targets)):
        return PolicyResult(PolicyDecision.DENIED, "rm -rf of the root/home directory is denied")
    if "r" in flags:
        return PolicyResult(PolicyDecision.APPROVAL_REQUIRED, "recursive rm needs human approval")
    return None


def evaluate_command(command: str) -> PolicyResult:
    """Classify ``command`` as allowed, denied, or needing approval."""
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return PolicyResult(PolicyDecision.DENIED, f"cannot parse command: {exc}")
    if not tokens:
        return PolicyResult(PolicyDecision.DENIED, "empty command")

    rm_verdict = _classify_rm(tokens)
    if rm_verdict is not None and rm_verdict.decision is PolicyDecision.DENIED:
        return rm_verdict

    for pattern in _HARD_DENY_PATTERNS:
        if pattern.search(command):
            return PolicyResult(
                PolicyDecision.DENIED, f"matches a denied pattern: {pattern.pattern}"
            )

    if rm_verdict is not None:
        return rm_verdict

    for pattern in _APPROVAL_PATTERNS:
        if pattern.search(command):
            return PolicyResult(
                PolicyDecision.APPROVAL_REQUIRED,
                f"destructive command needs human approval: {pattern.pattern}",
            )

    program = tokens[0].rsplit("/", 1)[-1]
    if program not in ALLOWED_PROGRAMS:
        return PolicyResult(PolicyDecision.DENIED, f"{program!r} is not on the allow-list")

    return PolicyResult(PolicyDecision.ALLOWED, "ok")


def check_command(command: str) -> None:
    """Raise ``PermissionDenied`` or ``ApprovalRequired`` unless ``command`` is cleanly allowed."""
    result = evaluate_command(command)
    if result.decision is PolicyDecision.DENIED:
        raise PermissionDenied(result.reason)
    if result.decision is PolicyDecision.APPROVAL_REQUIRED:
        raise ApprovalRequired(result.reason)
