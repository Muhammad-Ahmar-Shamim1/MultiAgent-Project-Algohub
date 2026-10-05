"""Worker roles and the permissions they hold (SRS section 10).

The tool layer enforces these on every call. Scoping inside a grant (e.g. the Architect's
WRITE_CODE covering docs/scaffolding only, QA's DB access covering the test DB only) is a
path/target policy for the tool layer, not expressed here.

Reading of the SRS table: "Yes" grants the permission; a named narrow grant ("Docs and
scaffolding", "Tests only", "CI and infra files", "Test DB only") grants the matching permission;
a bare "Limited" grants only the read side (DB_READ) or nothing (SECURITY_REVIEW).
RUN_COMMAND and BROWSER are not in the SRS table and are assigned by tool need (SRS 5.3, 5.1).
"""

from enum import StrEnum
from types import MappingProxyType


class Permission(StrEnum):
    """A capability the tool layer can check before executing a worker's tool call."""

    READ_CODE = "read_code"
    WRITE_CODE = "write_code"
    WRITE_TESTS = "write_tests"
    DB_READ = "db_read"
    DB_WRITE = "db_write"
    DB_MIGRATE = "db_migrate"
    DEPLOY = "deploy"
    SECURITY_REVIEW = "security_review"
    RUN_COMMAND = "run_command"
    BROWSER = "browser"


class Role(StrEnum):
    """Worker roles listed in SRS section 10."""

    ARCHITECT = "architect"
    FRONTEND = "frontend"
    BACKEND = "backend"
    DATABASE = "database"
    API_INTEGRATION = "api_integration"
    QA = "qa"
    SECURITY = "security"
    DEVOPS = "devops"
    MAINTENANCE = "maintenance"


P = Permission

ROLE_PERMISSIONS: MappingProxyType[Role, frozenset[Permission]] = MappingProxyType(
    {
        Role.ARCHITECT: frozenset({P.READ_CODE, P.WRITE_CODE, P.DB_READ}),
        Role.FRONTEND: frozenset({P.READ_CODE, P.WRITE_CODE, P.DB_READ, P.RUN_COMMAND, P.BROWSER}),
        Role.BACKEND: frozenset({P.READ_CODE, P.WRITE_CODE, P.DB_READ, P.DB_WRITE, P.RUN_COMMAND}),
        Role.DATABASE: frozenset(
            {
                P.READ_CODE,
                P.WRITE_CODE,
                P.DB_READ,
                P.DB_WRITE,
                P.DB_MIGRATE,
                P.RUN_COMMAND,
            }
        ),
        Role.API_INTEGRATION: frozenset(
            {P.READ_CODE, P.WRITE_CODE, P.DB_READ, P.RUN_COMMAND, P.BROWSER}
        ),
        Role.QA: frozenset(
            {
                P.READ_CODE,
                P.WRITE_TESTS,
                P.DB_READ,
                P.DB_WRITE,
                P.SECURITY_REVIEW,
                P.RUN_COMMAND,
                P.BROWSER,
            }
        ),
        Role.SECURITY: frozenset({P.READ_CODE, P.DB_READ, P.SECURITY_REVIEW, P.RUN_COMMAND}),
        Role.DEVOPS: frozenset(
            {P.READ_CODE, P.WRITE_CODE, P.DEPLOY, P.SECURITY_REVIEW, P.RUN_COMMAND}
        ),
        Role.MAINTENANCE: frozenset(
            {P.READ_CODE, P.WRITE_CODE, P.DB_READ, P.RUN_COMMAND, P.BROWSER}
        ),
    }
)


def permissions_for(role: Role) -> frozenset[Permission]:
    """Return the permissions granted to ``role``."""
    return ROLE_PERMISSIONS[role]


def has_permission(role: Role, permission: Permission) -> bool:
    """Return True if ``role`` holds ``permission``."""
    return permission in ROLE_PERMISSIONS[role]
