"""blueprints/registry.py: startup load+validate of blueprints/*.yaml (FR-041..FR-043)."""

from pathlib import Path

import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.blueprints.registry import (
    BLUEPRINTS_DIR,
    BlueprintRegistry,
    BlueprintValidationError,
    load_all,
    load_and_sync,
)
from web_department.blueprints.schema import BlueprintSpec
from web_department.config.permissions import Role, permissions_for
from web_department.db.repositories import BlueprintRepository

SHIPPED_BLUEPRINT_IDS = {
    "web_architect",
    "frontend_developer",
    "backend_developer",
    "database_developer",
    "api_integration",
    "qa_engineer",
    "security_engineer",
    "devops_engineer",
    "maintenance",
}


def _yaml_for(role: Role, **overrides: object) -> str:
    spec: dict[str, object] = {
        "id": overrides.pop("id", f"{role.value}_test"),
        "version": "1.0.0",
        "role": role.value,
        "responsibilities": ["do the thing"],
        "skills": ["testing"],
        "system_prompt_template": "worker_developer",
        "tools": ["read_file"],
        "models": ["coding"],
        "memory_scope": "project",
        "permissions": sorted(p.value for p in permissions_for(role)),
        "workflow": ["do it"],
        "autonomy": "autonomous",
        "budget": {"max_llm_calls_per_task": 40, "max_retries": 3, "timeout_min": 30},
    }
    spec.update(overrides)
    return yaml.safe_dump(spec)


def test_load_all_loads_every_shipped_blueprint() -> None:
    found = load_all(BLUEPRINTS_DIR)

    assert set(found) == SHIPPED_BLUEPRINT_IDS
    for spec, raw, checksum in found.values():
        assert isinstance(spec, BlueprintSpec)
        assert raw
        assert len(checksum) == 64


def test_load_all_rejects_a_syntactically_invalid_blueprint(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text(_yaml_for(Role.BACKEND, version="not-semver"))

    with pytest.raises(BlueprintValidationError, match="bad.yaml"):
        load_all(tmp_path)


def test_load_all_rejects_a_blueprint_with_permissions_not_matching_its_role(
    tmp_path: Path,
) -> None:
    (tmp_path / "bad.yaml").write_text(_yaml_for(Role.BACKEND, permissions=["deploy"]))

    with pytest.raises(BlueprintValidationError, match="SRS section 10"):
        load_all(tmp_path)


def test_load_all_rejects_a_missing_prompt_template(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text(
        _yaml_for(Role.BACKEND, system_prompt_template="does_not_exist")
    )

    with pytest.raises(BlueprintValidationError, match="does_not_exist"):
        load_all(tmp_path)


def test_load_all_rejects_duplicate_blueprint_ids(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(_yaml_for(Role.BACKEND, id="dup"))
    (tmp_path / "b.yaml").write_text(_yaml_for(Role.FRONTEND, id="dup"))

    with pytest.raises(BlueprintValidationError, match="duplicate blueprint id"):
        load_all(tmp_path)


def test_registry_get_returns_the_blueprint_for_a_role() -> None:
    found = load_all(BLUEPRINTS_DIR)
    registry = BlueprintRegistry([spec for spec, _raw, _checksum in found.values()])

    assert registry.get(Role.BACKEND).id == "backend_developer"


def test_registry_get_raises_for_a_role_with_no_blueprint() -> None:
    registry = BlueprintRegistry([])

    with pytest.raises(LookupError):
        registry.get(Role.BACKEND)


def test_registry_list_returns_all_nine_sorted_by_role() -> None:
    found = load_all(BLUEPRINTS_DIR)
    registry = BlueprintRegistry([spec for spec, _raw, _checksum in found.values()])

    listed = registry.list()

    assert len(listed) == 9
    assert [s.role.value for s in listed] == sorted(s.role.value for s in listed)


def test_registry_rejects_two_blueprints_claiming_the_same_role(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(_yaml_for(Role.BACKEND, id="a"))
    (tmp_path / "b.yaml").write_text(_yaml_for(Role.BACKEND, id="b"))
    found = load_all(tmp_path)

    with pytest.raises(BlueprintValidationError, match="both claim role"):
        BlueprintRegistry([spec for spec, _raw, _checksum in found.values()])


async def test_load_and_sync_upserts_every_shipped_blueprint_into_the_db(
    session: AsyncSession,
) -> None:
    registry = await load_and_sync(session, BLUEPRINTS_DIR)

    assert {s.id for s in registry.list()} == SHIPPED_BLUEPRINT_IDS
    repo = BlueprintRepository(session)
    row = await repo.get_version("backend_developer", "1.0.0")
    assert row is not None
    assert len(row.checksum) == 64


async def test_load_and_sync_refuses_and_upserts_nothing_when_one_file_is_invalid(
    session: AsyncSession, tmp_path: Path
) -> None:
    (tmp_path / "bad.yaml").write_text(_yaml_for(Role.BACKEND, version="not-semver"))

    with pytest.raises(BlueprintValidationError):
        await load_and_sync(session, tmp_path)

    repo = BlueprintRepository(session)
    assert await repo.get_version("backend_test", "1.0.0") is None
