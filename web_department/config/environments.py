"""Per-environment rules (SRS NFR-010: production is more restricted than development)."""

from dataclasses import dataclass
from enum import StrEnum


class Environment(StrEnum):
    """Deployment environment the platform itself is running in."""

    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


@dataclass(frozen=True)
class EnvironmentProfile:
    """Rules that differ between environments."""

    allow_debug: bool
    require_deploy_approval: bool


PROFILES: dict[Environment, EnvironmentProfile] = {
    Environment.DEV: EnvironmentProfile(allow_debug=True, require_deploy_approval=False),
    Environment.STAGING: EnvironmentProfile(allow_debug=True, require_deploy_approval=False),
    Environment.PROD: EnvironmentProfile(allow_debug=False, require_deploy_approval=True),
}


def get_profile(env: Environment) -> EnvironmentProfile:
    """Return the rules for ``env``."""
    return PROFILES[env]
