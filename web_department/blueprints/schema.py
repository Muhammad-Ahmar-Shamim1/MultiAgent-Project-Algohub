"""Blueprint schema: the contract every worker role is defined by (FR-040..FR-043).

A Blueprint YAML file is validated against ``BlueprintSpec`` at load time (FR-041) - an invalid
file refuses the whole department to start (``registry.py``). The DB ``Blueprint`` row
(``db/models.py``: ``id``, ``version``, ``yaml``, ``checksum``) is a checksummed cache of the
file for other tables to foreign-key against (``Agent.blueprint_id``/``blueprint_version``) - the
YAML file in Git is the source of truth (FR-042).
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from web_department.collaboration.events import EventType
from web_department.config.permissions import Permission, Role, permissions_for
from web_department.llm.router import Purpose

_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*)?"
    r"(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?$"
)

# workstation/tools.py's actual tool functions - the only things a Blueprint may grant.
VALID_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "list_dir",
        "search",
        "run_command",
        "run_tests",
        "git_commit",
        "browser_screenshot",
    }
)

# qa/checks.py's evaluation check registry - the only names a Blueprint's `evaluation` may cite.
VALID_EVALUATION_CHECKS = frozenset(
    {
        "lint_pass",
        "unit_tests_pass",
        "e2e_pass",
        "screenshot_attached",
        "openapi_valid",
        "migration_reversible",
        "no_secrets",
    }
)


def semver_key(version: str) -> tuple[int, int, int]:
    """(major, minor, patch) as an orderable tuple; pre-release/build metadata is not ordered.

    Used by the blueprints API's PUT endpoint to enforce "PUT bumps version" (FR-042) - the new
    version must sort strictly after the one it replaces.
    """
    match = _SEMVER.match(version)
    if not match:
        raise ValueError(f"not a semver version: {version!r}")
    return (int(match[1]), int(match[2]), int(match[3]))


class Budget(BaseModel):
    """Per-task rate-limit budget (AGENTS.md rule 8: bounded retries, no infinite loops)."""

    model_config = ConfigDict(extra="forbid")

    max_llm_calls_per_task: int = Field(gt=0)
    max_retries: int = Field(ge=0, le=10)
    timeout_min: int = Field(gt=0)


class BlueprintSpec(BaseModel):
    """The validated contents of one ``blueprints/*.yaml`` file (FR-040)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    version: str
    role: Role
    responsibilities: list[str] = Field(min_length=1)
    skills: list[str] = Field(min_length=1)
    # A name in llm/prompts/ (rendered via llm.prompts.render), not inline text - registry.py
    # checks the file actually exists, same "invalid Blueprint refuses to start" guarantee.
    system_prompt_template: str
    tools: list[str] = Field(min_length=1)
    # Which Model Router purposes (llm/router.py) this role calls, in preference order - not a
    # literal model name list: the router (not the worker) picks the actual provider/model per
    # purpose (SRS 11.1), so "model preference" here means "purpose preference".
    models: list[Purpose] = Field(min_length=1)
    memory_scope: Literal["none", "project", "role"]
    permissions: list[Permission] = Field(min_length=1)
    workflow: list[str] = Field(min_length=1)
    events_emitted: list[EventType] = Field(default_factory=list)
    evaluation: list[str] = Field(default_factory=list)
    autonomy: Literal["supervised", "autonomous"]
    budget: Budget

    @model_validator(mode="after")
    def _version_is_semver(self) -> "BlueprintSpec":
        if not _SEMVER.match(self.version):
            raise ValueError(f"version must be semver (X.Y.Z), got {self.version!r}")
        return self

    @model_validator(mode="after")
    def _tools_are_known(self) -> "BlueprintSpec":
        unknown = sorted(set(self.tools) - VALID_TOOLS)
        if unknown:
            raise ValueError(f"unknown tool(s) {unknown}; know: {sorted(VALID_TOOLS)}")
        return self

    @model_validator(mode="after")
    def _evaluation_checks_are_known(self) -> "BlueprintSpec":
        unknown = sorted(set(self.evaluation) - VALID_EVALUATION_CHECKS)
        if unknown:
            raise ValueError(
                f"unknown evaluation check(s) {unknown}; know: {sorted(VALID_EVALUATION_CHECKS)}"
            )
        return self

    @model_validator(mode="after")
    def _permissions_match_srs_section_10(self) -> "BlueprintSpec":
        """ "Permissions must match SRS section 10 exactly" - not just valid Permission values,
        but exactly the set config/permissions.py already derived from that table for this role.
        """
        expected = permissions_for(self.role)
        got = set(self.permissions)
        if got != expected:
            raise ValueError(
                f"permissions for role {self.role.value!r} must exactly match SRS section 10 "
                f"(config.permissions.ROLE_PERMISSIONS): expected "
                f"{sorted(p.value for p in expected)}, got {sorted(p.value for p in got)}"
            )
        return self
