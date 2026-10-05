"""security/scanners.py parsers and security_gate.evaluate (FR-110, FR-112)."""

import pytest

from web_department.config.permissions import Role
from web_department.security.scanners import (
    Finding,
    ScannerRun,
    fingerprint,
    parse_gitleaks,
    parse_npm_audit,
    parse_pip_audit,
    parse_semgrep,
    parse_trivy,
)
from web_department.security.security_gate import evaluate, owner_role, to_markdown
from web_department.workstation.policy import PolicyDecision, evaluate_command


def test_semgrep_severity_maps_error_warning_info() -> None:
    data = {
        "results": [
            {
                "check_id": "python.lang.security.audit.sqli",
                "path": "backend/app/db.py",
                "start": {"line": 12},
                "extra": {"severity": "ERROR", "message": "SQL built from input", "metadata": {}},
            },
            {
                "check_id": "ts.react.dangerous-html",
                "path": "frontend/src/page.tsx",
                "start": {"line": 4},
                "extra": {"severity": "WARNING", "message": "dangerouslySetInnerHTML"},
            },
            {
                "check_id": "x",
                "path": "a.py",
                "start": {"line": 1},
                "extra": {"severity": "INFO", "message": "m", "metadata": {"impact": "CRITICAL"}},
            },
        ]
    }

    findings = parse_semgrep(data)

    assert [(f.severity, f.file, f.line) for f in findings] == [
        ("high", "backend/app/db.py", 12),
        ("medium", "frontend/src/page.tsx", 4),
        ("critical", "a.py", 1),  # the rule's own impact wins over semgrep's INFO
    ]


def test_gitleaks_every_leak_is_high_and_never_carries_the_secret() -> None:
    data = [
        {
            "RuleID": "stripe-access-token",
            "Description": "Stripe Access Token",
            "File": "backend/app/config.py",
            "StartLine": 7,
            "Secret": "REDACTED",
            "Match": "STRIPE_KEY = REDACTED",
        }
    ]

    [finding] = parse_gitleaks(data)

    assert finding == Finding(
        tool="gitleaks",
        rule="stripe-access-token",
        severity="high",
        file="backend/app/config.py",
        line=7,
        message="Possible hard-coded secret: Stripe Access Token",
    )


def test_npm_audit_maps_moderate_to_medium() -> None:
    data = {
        "vulnerabilities": {
            "next": {
                "severity": "critical",
                "range": "<14.2.25",
                "via": [{"title": "Auth bypass"}],
            },
            "nanoid": {"severity": "moderate", "range": "<3.3.8", "via": ["postcss"]},
        }
    }

    findings = parse_npm_audit(data)

    assert [(f.rule, f.severity) for f in findings] == [
        ("npm:next", "critical"),
        ("npm:nanoid", "medium"),
    ]
    assert findings[0].message == "next <14.2.25: Auth bypass"


def test_pip_audit_is_high_when_fixable_and_medium_when_not() -> None:
    data = {
        "dependencies": [
            {
                "name": "jinja2",
                "version": "3.1.2",
                "vulns": [
                    {"id": "GHSA-1", "fix_versions": ["3.1.5"], "description": "sandbox escape"},
                    {"id": "GHSA-2", "fix_versions": [], "description": "no fix"},
                ],
            },
            {"name": "fastapi", "version": "0.115.0", "vulns": []},
        ]
    }

    findings = parse_pip_audit(data)

    assert [(f.rule, f.severity) for f in findings] == [("GHSA-1", "high"), ("GHSA-2", "medium")]
    assert "fixed in 3.1.5" in findings[0].message


def test_trivy_reads_vulnerabilities_misconfigurations_and_secrets() -> None:
    data = {
        "Results": [
            {
                "Target": "frontend/package-lock.json",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2025-1",
                        "PkgName": "axios",
                        "InstalledVersion": "1.6.0",
                        "Severity": "HIGH",
                        "Title": "SSRF",
                    }
                ],
            },
            {
                "Target": "backend/Dockerfile",
                "Misconfigurations": [
                    {
                        "ID": "DS002",
                        "Severity": "UNKNOWN",
                        "Message": "Image runs as root",
                        "CauseMetadata": {"StartLine": 1},
                    }
                ],
                "Secrets": [{"RuleID": "aws-key", "Severity": "CRITICAL", "Title": "AWS key"}],
            },
            {"Target": "empty", "Vulnerabilities": None},
        ]
    }

    findings = parse_trivy(data)

    assert [(f.rule, f.severity, f.file, f.line) for f in findings] == [
        ("CVE-2025-1", "high", "frontend/package-lock.json", None),
        ("DS002", "info", "backend/Dockerfile", 1),
        ("aws-key", "critical", "backend/Dockerfile", None),
    ]


def test_fingerprint_identifies_tool_rule_file_and_line() -> None:
    finding = Finding(tool="t", rule="r", severity="high", file="f.py", line=3, message="a")
    reworded = finding.model_copy(update={"message": "b", "severity": "low"})

    assert finding.fingerprint == fingerprint("t", "r", "f.py", 3) == reworded.fingerprint
    assert finding.fingerprint != fingerprint("t", "r", "f.py", 4)


def _finding(severity: str, rule: str = "r", file: str = "backend/app/x.py") -> Finding:
    return Finding(tool="t", rule=rule, severity=severity, file=file, line=1, message="m")  # type: ignore[arg-type]


def test_gate_blocks_on_high_or_critical_and_warns_on_medium() -> None:
    findings = [
        _finding("critical", "a"),
        _finding("high", "b"),
        _finding("medium", "c"),
        _finding("low", "d"),
        _finding("info", "e"),
    ]

    report = evaluate(findings, [], excepted=set())

    assert report.passed is False
    assert [f.rule for f in report.blocking] == ["a", "b"]
    assert [f.rule for f in report.warnings] == ["c"]
    assert [f.rule for f in report.informational] == ["d", "e"]
    assert report.reasons == ["2 high/critical finding(s)"]


def test_gate_passes_with_only_warnings() -> None:
    assert evaluate([_finding("medium")], [], excepted=set()).passed is True


def test_an_approved_exception_stops_a_finding_from_blocking() -> None:
    blocked = _finding("critical")

    report = evaluate([blocked], [], excepted={blocked.fingerprint})

    assert report.passed is True
    assert report.excepted == [blocked]


def test_a_scanner_that_failed_to_run_blocks_but_a_skipped_one_does_not() -> None:
    errored = evaluate([], [ScannerRun(tool="semgrep", status="error")], excepted=set())
    skipped = evaluate([], [ScannerRun(tool="trivy-image", status="skipped")], excepted=set())

    assert errored.passed is False
    assert errored.reasons == ["scanner(s) failed to run: ['semgrep']"]
    assert skipped.passed is True


def test_duplicate_findings_count_once() -> None:
    report = evaluate([_finding("high"), _finding("high")], [], excepted=set())

    assert len(report.blocking) == 1


@pytest.mark.parametrize(
    ("tool", "file", "role"),
    [
        ("semgrep", "backend/app/routers/admin.py", Role.BACKEND),
        ("semgrep", "frontend/src/app/page.tsx", Role.FRONTEND),
        ("npm-audit", "frontend/package-lock.json", Role.FRONTEND),
        ("pip-audit", "backend/pyproject.toml", Role.BACKEND),
        ("trivy-fs", "backend/Dockerfile", Role.DEVOPS),
        ("trivy-fs", "docker-compose.yml", Role.DEVOPS),
        ("trivy-image", "algohub/app (ubuntu 24.04)", Role.DEVOPS),
        ("llm-review", None, Role.BACKEND),
    ],
)
def test_fix_tasks_go_to_the_role_that_owns_the_file(
    tool: str, file: str | None, role: Role
) -> None:
    finding = Finding(tool=tool, rule="r", severity="high", file=file, message="m")

    assert owner_role(finding) is role


def test_markdown_report_names_the_verdict_and_fix_tasks() -> None:
    report = evaluate([_finding("high", "secret")], [ScannerRun(tool="t", status="ok")], set())

    markdown = to_markdown(report, ["WEB-031: Security fix: secret"])

    assert markdown.startswith("# Security report - BLOCKED")
    assert "| high | t | secret | backend/app/x.py:1 | m |" in markdown
    assert "- WEB-031: Security fix: secret" in markdown


@pytest.mark.parametrize("program", ["semgrep scan", "gitleaks detect", "trivy fs .", "pip-audit"])
def test_the_workstation_policy_allows_the_scanners(program: str) -> None:
    assert evaluate_command(program).decision is PolicyDecision.ALLOWED
