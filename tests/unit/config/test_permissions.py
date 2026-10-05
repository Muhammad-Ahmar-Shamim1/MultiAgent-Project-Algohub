"""Tests for web_department.config.permissions against SRS section 10."""

import pytest

from web_department.config.permissions import (
    ROLE_PERMISSIONS,
    Permission,
    Role,
    has_permission,
    permissions_for,
)

SRS_SECTION_10_ROLES = {
    "architect",
    "frontend",
    "backend",
    "database",
    "api_integration",
    "qa",
    "security",
    "devops",
    "maintenance",
}


def test_roles_match_srs_section_10() -> None:
    assert {role.value for role in Role} == SRS_SECTION_10_ROLES


def test_role_map_covers_every_role() -> None:
    assert set(ROLE_PERMISSIONS) == set(Role)


def test_permission_enum_has_expected_members() -> None:
    assert {p.name for p in Permission} == {
        "READ_CODE",
        "WRITE_CODE",
        "WRITE_TESTS",
        "DB_READ",
        "DB_WRITE",
        "DB_MIGRATE",
        "DEPLOY",
        "SECURITY_REVIEW",
        "RUN_COMMAND",
        "BROWSER",
    }


@pytest.mark.parametrize("role", list(Role))
def test_every_role_can_read_code(role: Role) -> None:
    assert has_permission(role, Permission.READ_CODE)


def test_only_devops_can_deploy() -> None:
    assert {r for r in Role if has_permission(r, Permission.DEPLOY)} == {Role.DEVOPS}


def test_only_database_role_can_migrate() -> None:
    assert {r for r in Role if has_permission(r, Permission.DB_MIGRATE)} == {Role.DATABASE}


def test_security_review_is_held_by_qa_security_and_devops_only() -> None:
    holders = {r for r in Role if has_permission(r, Permission.SECURITY_REVIEW)}

    assert holders == {Role.QA, Role.SECURITY, Role.DEVOPS}


def test_qa_writes_tests_but_not_application_code() -> None:
    assert has_permission(Role.QA, Permission.WRITE_TESTS)
    assert not has_permission(Role.QA, Permission.WRITE_CODE)


def test_security_role_cannot_write_code_or_deploy() -> None:
    perms = permissions_for(Role.SECURITY)

    assert Permission.WRITE_CODE not in perms
    assert Permission.DEPLOY not in perms


def test_only_backend_and_database_write_to_the_dev_database_among_developers() -> None:
    developers = {Role.FRONTEND, Role.BACKEND, Role.DATABASE, Role.API_INTEGRATION}

    writers = {r for r in developers if has_permission(r, Permission.DB_WRITE)}

    assert writers == {Role.BACKEND, Role.DATABASE}


def test_role_permission_sets_are_immutable() -> None:
    assert isinstance(permissions_for(Role.QA), frozenset)
    with pytest.raises(TypeError):
        ROLE_PERMISSIONS[Role.QA] = frozenset()  # type: ignore[index]
