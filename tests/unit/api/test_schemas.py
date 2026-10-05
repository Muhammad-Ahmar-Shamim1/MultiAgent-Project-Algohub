"""ProjectRequest field validation at the API boundary (FR-001)."""

import pytest
from pydantic import ValidationError

from web_department.api.schemas import ProjectRequest

VALID = {"title": "Todo app", "description": "A todo app with login.", "requester_id": "u-1"}


def test_minimal_valid_request() -> None:
    request = ProjectRequest.model_validate(VALID)

    assert request.title == "Todo app"
    assert request.constraints is None
    assert request.attachments is None


def test_constraints_and_attachments_are_optional_but_accepted() -> None:
    request = ProjectRequest.model_validate(
        {**VALID, "constraints": "Must use PostgreSQL", "attachments": ["https://x/spec.pdf"]}
    )

    assert request.constraints == "Must use PostgreSQL"
    assert request.attachments == ["https://x/spec.pdf"]


@pytest.mark.parametrize("field", ["title", "description", "requester_id"])
def test_required_fields_reject_empty_string(field: str) -> None:
    with pytest.raises(ValidationError):
        ProjectRequest.model_validate({**VALID, field: ""})


@pytest.mark.parametrize("field", ["title", "description", "requester_id"])
def test_required_fields_cannot_be_missing(field: str) -> None:
    payload = {k: v for k, v in VALID.items() if k != field}
    with pytest.raises(ValidationError):
        ProjectRequest.model_validate(payload)


def test_title_cannot_exceed_the_project_column_width() -> None:
    with pytest.raises(ValidationError):
        ProjectRequest.model_validate({**VALID, "title": "x" * 301})


def test_requester_id_cannot_exceed_the_project_column_width() -> None:
    with pytest.raises(ValidationError):
        ProjectRequest.model_validate({**VALID, "requester_id": "x" * 129})


def test_constraints_has_a_max_length() -> None:
    with pytest.raises(ValidationError):
        ProjectRequest.model_validate({**VALID, "constraints": "x" * 5001})
