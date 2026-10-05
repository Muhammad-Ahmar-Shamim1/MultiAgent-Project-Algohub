"""WebRequirementsSpec: field defaults and post-validation (FR-021, FR-023)."""

import pytest
from pydantic import ValidationError

from web_department.orchestrator.wrs_schema import WebRequirementsSpec

VALID = {
    "project_type": "web_application",
    "domain": "ecommerce",
    "roles": ["customer", "admin"],
    "features": [
        {
            "id": "F-01",
            "name": "authentication",
            "description": "Users can sign up and log in.",
            "acceptance": ["User can register, log in and log out"],
        }
    ],
}


def test_minimal_valid_spec_gets_sensible_defaults() -> None:
    spec = WebRequirementsSpec.model_validate(VALID)

    assert spec.pages == []
    assert spec.entities == []
    assert spec.security_level == "standard"
    assert spec.stack == "golden-v1"
    assert spec.deployment_target == "local-docker"
    assert spec.open_questions == []
    assert spec.features[0].priority == "must"


def test_full_spec_matching_the_srs_4_3_example() -> None:
    spec = WebRequirementsSpec.model_validate(
        {
            **VALID,
            "features": [
                {
                    "id": "F-01",
                    "name": "authentication",
                    "description": "Users can sign up and log in.",
                    "priority": "must",
                    "acceptance": ["User can register, log in and log out"],
                },
                {
                    "id": "F-02",
                    "name": "cart",
                    "description": "Users can manage a shopping cart.",
                    "priority": "must",
                    "acceptance": ["Cart total updates when quantity changes"],
                },
            ],
            "pages": [
                {"route": "/cart", "purpose": "View and edit the cart", "roles": ["customer"]}
            ],
            "entities": [
                {"name": "User", "fields": [{"name": "email", "type": "string"}]},
                {"name": "Product", "fields": [{"name": "name", "type": "string"}]},
                {
                    "name": "Order",
                    "fields": [{"name": "total", "type": "decimal"}],
                    "relations": ["belongs to User"],
                },
            ],
            "security_level": "standard",
        }
    )

    assert [f.id for f in spec.features] == ["F-01", "F-02"]
    assert [e.name for e in spec.entities] == ["User", "Product", "Order"]


def test_feature_without_acceptance_is_rejected() -> None:
    payload = {**VALID, "features": [{**VALID["features"][0], "acceptance": []}]}

    with pytest.raises(ValidationError, match="acceptance"):
        WebRequirementsSpec.model_validate(payload)


def test_feature_missing_required_fields_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WebRequirementsSpec.model_validate({**VALID, "features": [{"id": "F-01"}]})


def test_duplicate_entity_names_are_rejected() -> None:
    payload = {
        **VALID,
        "entities": [
            {"name": "User", "fields": [{"name": "email", "type": "string"}]},
            {"name": "User", "fields": [{"name": "id", "type": "string"}]},
        ],
    }

    with pytest.raises(ValidationError, match="Duplicate entity names"):
        WebRequirementsSpec.model_validate(payload)


def test_page_referencing_an_undeclared_role_is_rejected() -> None:
    payload = {**VALID, "pages": [{"route": "/x", "purpose": "y", "roles": ["superadmin"]}]}

    with pytest.raises(ValidationError, match="unknown roles"):
        WebRequirementsSpec.model_validate(payload)


def test_page_role_that_is_declared_is_accepted() -> None:
    spec = WebRequirementsSpec.model_validate(
        {**VALID, "pages": [{"route": "/x", "purpose": "y", "roles": ["admin"]}]}
    )

    assert spec.pages[0].roles == ["admin"]


def test_entity_without_fields_is_rejected() -> None:
    payload = {**VALID, "entities": [{"name": "Empty", "fields": []}]}

    with pytest.raises(ValidationError, match="fields"):
        WebRequirementsSpec.model_validate(payload)


def test_spec_requires_at_least_one_role_and_one_feature() -> None:
    with pytest.raises(ValidationError):
        WebRequirementsSpec.model_validate({**VALID, "roles": []})
    with pytest.raises(ValidationError):
        WebRequirementsSpec.model_validate({**VALID, "features": []})


def test_invalid_security_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WebRequirementsSpec.model_validate({**VALID, "security_level": "maximum"})


def test_unknown_top_level_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WebRequirementsSpec.model_validate({**VALID, "surprise_field": True})


def test_json_schema_exports_without_error() -> None:
    schema = WebRequirementsSpec.model_json_schema()

    assert schema["title"] == "WebRequirementsSpec"
    assert "features" in schema["required"]
