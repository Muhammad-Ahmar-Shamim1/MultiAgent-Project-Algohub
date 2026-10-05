"""Security scanners (FR-110): run inside the workstation, normalise results into ``Finding``s.

Each scanner writes JSON to ``artifacts/security/`` (or, for ``npm audit``, which has no output
flag and can exceed the exec output limit, through a ``node -e`` wrapper that writes its stdout
there), and a parser turns that tool's JSON into ``Finding``s. Exit codes are not trusted:
several of these tools exit non-zero precisely when they *find* something, so a scanner counts
as having run when its output file parses, and as ``error`` when it does not.

Severity mapping (to ``info | low | medium | high | critical``):

- semgrep ``extra.severity``: ERROR -> high, WARNING -> medium, INFO -> low (or the rule's own
  ``metadata.impact`` when it is one of ours);
- gitleaks: every leak -> high (a committed secret is exploitable as-is);
- npm audit: its own levels, with ``moderate`` -> medium;
- pip-audit reports no severity, so a vulnerable dependency -> high when a fixed version exists
  (it is known *and* fixable) and medium when none does;
- trivy: its own levels, ``UNKNOWN`` -> info.

``trivy image`` needs a built image; workstations have no Docker socket (NFR-007), so it scans an
image tarball at ``artifacts/image.tar`` when a build step has exported one, and is reported as
``skipped`` otherwise - never silently treated as clean.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus
from web_department.config.permissions import Role
from web_department.db.models import Workstation
from web_department.workstation import tools
from web_department.workstation.manager import WorkstationManager

ROLE = Role.SECURITY
OUT_DIR = "artifacts/security"
IMAGE_TARBALL = "artifacts/image.tar"
SCAN_TIMEOUT_SECONDS = 900

Severity = Literal["info", "low", "medium", "high", "critical"]
SEVERITIES: tuple[Severity, ...] = ("info", "low", "medium", "high", "critical")
ScannerStatus = Literal["ok", "error", "skipped"]


class Finding(BaseModel):
    """One normalised security finding."""

    tool: str
    rule: str
    severity: Severity
    file: str | None = None
    line: int | None = None
    message: str

    @property
    def fingerprint(self) -> str:
        """Stable id for exceptions and fix-task de-duplication: tool, rule, file, line."""
        return fingerprint(self.tool, self.rule, self.file, self.line)


def fingerprint(tool: str, rule: str, file: str | None, line: int | None) -> str:
    """sha256 of ``tool|rule|file|line`` (the identity of a finding, not its wording)."""
    raw = f"{tool}|{rule}|{file or ''}|{line if line is not None else ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ScannerRun(BaseModel):
    """How one scanner went: ``ok`` (output parsed), ``error`` or ``skipped``, and why."""

    tool: str
    status: ScannerStatus
    detail: str = ""
    findings: int = 0


def _level(value: str | None, *, default: Severity = "medium") -> Severity:
    normalized = (value or "").strip().lower()
    aliases: dict[str, Severity] = {"moderate": "medium", "unknown": "info", "warning": "medium"}
    if normalized in SEVERITIES:
        return normalized
    return aliases.get(normalized, default)


# --- parsers (tool JSON -> findings) ---------------------------------------------------------


def parse_semgrep(data: dict[str, Any]) -> list[Finding]:
    """``semgrep --json``: ``results[]`` with ``check_id``, ``path``, ``start.line``, ``extra``."""
    by_semgrep_level: dict[str, Severity] = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}
    findings = []
    for result in data.get("results", []):
        extra = result.get("extra", {})
        impact = str(extra.get("metadata", {}).get("impact", "")).lower()
        severity = (
            impact
            if impact in SEVERITIES
            else by_semgrep_level.get(str(extra.get("severity", "")).upper(), "medium")
        )
        findings.append(
            Finding(
                tool="semgrep",
                rule=str(result.get("check_id", "unknown")),
                severity=severity,
                file=result.get("path"),
                line=result.get("start", {}).get("line"),
                message=str(extra.get("message", "")).strip(),
            )
        )
    return findings


def parse_gitleaks(data: list[dict[str, Any]]) -> list[Finding]:
    """``gitleaks --report-format json`` (run with ``--redact``): a list of leaks."""
    return [
        Finding(
            tool="gitleaks",
            rule=str(leak.get("RuleID", "secret")),
            severity="high",
            file=leak.get("File"),
            line=leak.get("StartLine"),
            message=f"Possible hard-coded secret: {leak.get('Description', 'secret')}",
        )
        for leak in data or []
    ]


def parse_npm_audit(data: dict[str, Any]) -> list[Finding]:
    """``npm audit --json`` (npm 7+): ``vulnerabilities{name: {severity, via, range}}``."""
    findings = []
    for name, vuln in (data.get("vulnerabilities") or {}).items():
        titles = [v.get("title") for v in vuln.get("via", []) if isinstance(v, dict)]
        detail = "; ".join(t for t in titles if t) or "vulnerable via a dependency"
        findings.append(
            Finding(
                tool="npm-audit",
                rule=f"npm:{name}",
                severity=_level(vuln.get("severity")),
                file="frontend/package-lock.json",
                message=f"{name} {vuln.get('range', '')}: {detail}".strip(),
            )
        )
    return findings


def parse_pip_audit(data: dict[str, Any]) -> list[Finding]:
    """``pip-audit -f json``: ``dependencies[{name, version, vulns[{id, fix_versions, ...}]}]``."""
    findings = []
    for dep in data.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            fixes = vuln.get("fix_versions") or []
            fix_note = f"; fixed in {', '.join(fixes)}" if fixes else "; no fixed version yet"
            findings.append(
                Finding(
                    tool="pip-audit",
                    rule=str(vuln.get("id", "unknown")),
                    severity="high" if fixes else "medium",
                    file="backend/pyproject.toml",
                    message=f"{dep.get('name')} {dep.get('version')}: "
                    f"{(vuln.get('description') or '').strip()[:300]}{fix_note}",
                )
            )
    return findings


def parse_trivy(data: dict[str, Any], *, tool: str = "trivy-fs") -> list[Finding]:
    """``trivy --format json``: ``Results[]`` with Vulnerabilities, Misconfigurations, Secrets."""
    findings = []
    for result in data.get("Results") or []:
        target = result.get("Target")
        for vuln in result.get("Vulnerabilities") or []:
            findings.append(
                Finding(
                    tool=tool,
                    rule=str(vuln.get("VulnerabilityID", "unknown")),
                    severity=_level(vuln.get("Severity"), default="info"),
                    file=target,
                    message=f"{vuln.get('PkgName')} {vuln.get('InstalledVersion', '')}: "
                    f"{vuln.get('Title') or ''}".strip(),
                )
            )
        for misconfig in result.get("Misconfigurations") or []:
            findings.append(
                Finding(
                    tool=tool,
                    rule=str(misconfig.get("ID", "unknown")),
                    severity=_level(misconfig.get("Severity"), default="info"),
                    file=target,
                    line=(misconfig.get("CauseMetadata") or {}).get("StartLine"),
                    message=str(misconfig.get("Message") or misconfig.get("Title") or ""),
                )
            )
        for secret in result.get("Secrets") or []:
            findings.append(
                Finding(
                    tool=tool,
                    rule=str(secret.get("RuleID", "secret")),
                    severity=_level(secret.get("Severity"), default="high"),
                    file=target,
                    line=secret.get("StartLine"),
                    message=f"Possible hard-coded secret: {secret.get('Title', 'secret')}",
                )
            )
    return findings


# --- running ---------------------------------------------------------------------------------

_NPM_AUDIT_SCRIPT = (
    "const r = require('child_process').spawnSync('npm', ['audit', '--json'],"
    " {cwd: 'frontend', encoding: 'utf8', maxBuffer: 1e8});"
    f" require('fs').writeFileSync('{OUT_DIR}/npm-audit.json', r.stdout || '');"
    " process.exit(r.stdout ? 0 : 1);"
)


@dataclass(frozen=True)
class Scanner:
    """One scanner: the command, the JSON file it leaves, and that file's parser."""

    tool: str
    command: tuple[str, ...]
    output: str
    parse: Callable[[Any], list[Finding]]
    requires_file: str | None = None


SCANNERS: tuple[Scanner, ...] = (
    Scanner(
        "semgrep",
        (
            "semgrep",
            "scan",
            "--config",
            "p/owasp-top-ten",
            "--config",
            "p/python",
            "--config",
            "p/typescript",
            "--json",
            "--output",
            f"{OUT_DIR}/semgrep.json",
            "--metrics=off",
            "backend",
            "frontend/src",
        ),  # fmt: skip
        f"{OUT_DIR}/semgrep.json",
        parse_semgrep,
    ),
    Scanner(
        "gitleaks",
        (
            "gitleaks",
            "detect",
            "--source",
            ".",
            "--redact",
            "--exit-code",
            "0",
            "--report-format",
            "json",
            "--report-path",
            f"{OUT_DIR}/gitleaks.json",
        ),  # fmt: skip
        f"{OUT_DIR}/gitleaks.json",
        parse_gitleaks,
    ),
    Scanner(
        "npm-audit", ("node", "-e", _NPM_AUDIT_SCRIPT), f"{OUT_DIR}/npm-audit.json", parse_npm_audit
    ),  # fmt: skip
    Scanner(
        "pip-audit",
        ("pip-audit", "-f", "json", "-o", f"{OUT_DIR}/pip-audit.json", "backend"),
        f"{OUT_DIR}/pip-audit.json",
        parse_pip_audit,
    ),
    Scanner(
        "trivy-fs",
        (
            "trivy",
            "fs",
            "--format",
            "json",
            "--output",
            f"{OUT_DIR}/trivy-fs.json",
            "--scanners",
            "vuln,misconfig,secret",
            "--skip-dirs",
            "backend/.venv",
            "--skip-dirs",
            "frontend/node_modules",
            ".",
        ),  # fmt: skip
        f"{OUT_DIR}/trivy-fs.json",
        parse_trivy,
    ),
    Scanner(
        "trivy-image",
        (
            "trivy",
            "image",
            "--format",
            "json",
            "--output",
            f"{OUT_DIR}/trivy-image.json",
            "--input",
            IMAGE_TARBALL,
        ),  # fmt: skip
        f"{OUT_DIR}/trivy-image.json",
        lambda data: parse_trivy(data, tool="trivy-image"),
        requires_file=IMAGE_TARBALL,
    ),
)


async def run_scanners(
    session: AsyncSession,
    ws: Workstation,
    agent_id: str | None,
    manager: WorkstationManager,
    *,
    scanners: tuple[Scanner, ...] = SCANNERS,
    bus: EventBus | None = None,
) -> tuple[list[Finding], list[ScannerRun]]:
    """Run every scanner inside ``ws``; return all findings and how each scanner went."""
    await tools.run_command(session, ws, ROLE, agent_id, manager, ["mkdir", "-p", OUT_DIR], bus=bus)
    findings: list[Finding] = []
    runs: list[ScannerRun] = []
    for scanner in scanners:
        if scanner.requires_file and not await _exists(
            session, ws, agent_id, scanner.requires_file, bus
        ):
            runs.append(
                ScannerRun(
                    tool=scanner.tool,
                    status="skipped",
                    detail=f"{scanner.requires_file} not found (no image has been exported)",
                )
            )
            continue
        # Remove a previous run's output so a scanner that fails to write can't reuse it.
        await tools.run_command(
            session, ws, ROLE, agent_id, manager, ["rm", "-f", scanner.output], bus=bus
        )
        result = await tools.run_command(
            session,
            ws,
            ROLE,
            agent_id,
            manager,
            list(scanner.command),
            timeout=SCAN_TIMEOUT_SECONDS,
            bus=bus,
        )
        try:
            raw = await tools.read_file(session, ws, ROLE, agent_id, scanner.output, bus=bus)
            found = scanner.parse(json.loads(raw))
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            detail = f"exit {result.exit_code}: {(result.stderr or result.stdout)[-500:]}"
            runs.append(
                ScannerRun(tool=scanner.tool, status="error", detail=f"{detail} ({exc!r})"[-800:])
            )
            continue
        findings += found
        runs.append(ScannerRun(tool=scanner.tool, status="ok", findings=len(found)))
    return findings, runs


async def _exists(
    session: AsyncSession, ws: Workstation, agent_id: str | None, path: str, bus: EventBus | None
) -> bool:
    directory, _, name = path.rpartition("/")
    try:
        entries = await tools.list_dir(session, ws, ROLE, agent_id, directory or ".", bus=bus)
    except OSError:
        return False
    return name in entries
