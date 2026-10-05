"""war_room.required_roles / needs_security: which team a plan needs (FR-080)."""

from web_department.collaboration.war_room import needs_security, required_roles
from web_department.config.permissions import Role
from web_department.orchestrator.wrs_schema import (
    Entity,
    EntityField,
    Feature,
    Page,
    WebRequirementsSpec,
)


def _wrs(**overrides: object) -> WebRequirementsSpec:
    fields: dict[str, object] = {
        "project_type": "web_application",
        "domain": "blog",
        "roles": ["reader"],
        "features": [Feature(id="F-01", name="posts", description="List posts", acceptance=["x"])],
        "pages": [Page(route="/", purpose="Home")],
        "entities": [Entity(name="Post", fields=[EntityField(name="title", type="string")])],
        "deployment_target": "",
    }
    fields.update(overrides)
    return WebRequirementsSpec.model_validate(fields)


def test_roles_are_the_plan_s_task_roles_in_role_order() -> None:
    roles = required_roles(["frontend", "architect", "frontend", "backend"], _wrs())

    assert roles == [Role.ARCHITECT, Role.FRONTEND, Role.BACKEND]


def test_a_public_project_with_no_sensitive_data_does_not_need_security() -> None:
    assert needs_security(_wrs()) is False
    assert Role.SECURITY not in required_roles(["backend"], _wrs())


def test_role_gated_pages_mean_auth_so_security_joins() -> None:
    wrs = _wrs(
        roles=["reader", "admin"], pages=[Page(route="/admin", purpose="Admin", roles=["admin"])]
    )

    assert Role.SECURITY in required_roles(["backend"], wrs)


def test_an_auth_feature_means_security_joins() -> None:
    wrs = _wrs(
        features=[Feature(id="F-01", name="Login", description="Users sign in", acceptance=["x"])]
    )

    assert needs_security(wrs) is True


def test_a_sensitive_entity_field_means_security_joins() -> None:
    wrs = _wrs(
        entities=[Entity(name="User", fields=[EntityField(name="password_hash", type="string")])]
    )

    assert needs_security(wrs) is True


def test_security_level_high_means_security_joins() -> None:
    assert needs_security(_wrs(security_level="high")) is True


def test_a_deployment_target_means_devops_joins() -> None:
    assert Role.DEVOPS in required_roles(["backend"], _wrs(deployment_target="local-docker"))
    assert Role.DEVOPS not in required_roles(["backend"], _wrs())


def test_without_a_wrs_only_task_roles_count() -> None:
    assert required_roles(["qa"], None) == [Role.QA]
