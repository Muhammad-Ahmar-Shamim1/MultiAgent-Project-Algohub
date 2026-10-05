"""BlueprintSpec validation (FR-040): semver, tool/check whitelists, SRS-exact permissions."""

from typing import Any

import pytest
from pydantic import ValidationError

from web_department.blueprints.schema import BlueprintSpec
from web_department.config.permissions import Permission, Role, permissions_for


def _spec(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "backend_developer",
        "version": "1.0.0",
        "role": "backend",
        "responsibilities": ["Implement backend tasks"],
        "skills": ["FastAPI"],
        "system_prompt_template": "worker_developer",
        "tools": ["read_file", "write_file"],
        "models": ["coding"],
        "memory_scope": "project",
        "permissions": sorted(p.value for p in permissions_for(Role.BACKEND)),
        "workflow": ["read task", "write code", "run tests", "commit"],
        "events_emitted": ["TASK_STARTED", "TASK_COMPLETED"],
        "evaluation": ["lint_pass"],
        "autonomy": "autonomous",
        "budget": {"max_llm_calls_per_task": 40, "max_retries": 3, "timeout_min": 30},
    }
    base.update(overrides)
    return base


def test_a_well_formed_spec_validates() -> None:
    spec = BlueprintSpec.model_validate(_spec())

    assert spec.role is Role.BACKEND
    assert set(spec.permissions) == permissions_for(Role.BACKEND)


@pytest.mark.parametrize("bad_version", ["1.0", "v1.0.0", "1.0.0.0", "latest", ""])
def test_a_non_semver_version_is_rejected(bad_version: str) -> None:
    with pytest.raises(ValidationError, match="semver"):
        BlueprintSpec.model_validate(_spec(version=bad_version))


@pytest.mark.parametrize("good_version", ["0.1.0", "1.0.0", "2.3.4-rc.1", "1.0.0+build.5"])
def test_a_valid_semver_version_is_accepted(good_version: str) -> None:
    BlueprintSpec.model_validate(_spec(version=good_version))


def test_an_unknown_tool_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown tool"):
        BlueprintSpec.model_validate(_spec(tools=["read_file", "delete_everything"]))


def test_an_unknown_evaluation_check_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown evaluation check"):
        BlueprintSpec.model_validate(_spec(evaluation=["lint_pass", "vibes_pass"]))


def test_permissions_must_exactly_match_the_role_s_srs_section_10_set() -> None:
    with pytest.raises(ValidationError, match="must exactly match SRS section 10"):
        BlueprintSpec.model_validate(_spec(permissions=[Permission.DEPLOY.value]))


def test_permissions_missing_one_entry_from_the_role_s_set_is_rejected() -> None:
    granted = sorted(p.value for p in permissions_for(Role.BACKEND))[:-1]
    with pytest.raises(ValidationError, match="must exactly match SRS section 10"):
        BlueprintSpec.model_validate(_spec(permissions=granted))


def test_an_unrecognized_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        BlueprintSpec.model_validate(_spec(unexpected_field="nope"))


def test_an_empty_responsibilities_list_is_rejected() -> None:
    with pytest.raises(ValidationError):
        BlueprintSpec.model_validate(_spec(responsibilities=[]))


def test_budget_retries_above_ten_is_rejected() -> None:
    with pytest.raises(ValidationError):
        BlueprintSpec.model_validate(
            _spec(budget={"max_llm_calls_per_task": 40, "max_retries": 11, "timeout_min": 30})
        )
