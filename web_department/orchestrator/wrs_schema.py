"""The Web Requirements Specification schema (FR-020, FR-021).

This is the structured output the Requirement Analyzer produces from a natural-language project
request. It is also the JSON Schema handed to the Model Router as ``json_schema`` (IR-020), so the
LLM's response is validated and, if invalid, auto-repaired against these exact rules.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Feature(BaseModel):
    """One feature of the product (FR-021, FR-023: every feature needs ≥1 acceptance criterion)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str
    priority: Literal["must", "should", "could"] = "must"
    acceptance: list[str] = Field(min_length=1)


class Page(BaseModel):
    """One page / screen and which roles may see it."""

    model_config = ConfigDict(extra="forbid")

    route: str
    purpose: str
    roles: list[str] = Field(default_factory=list)


class EntityField(BaseModel):
    """One field of a data entity."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    required: bool = True


class Entity(BaseModel):
    """One data entity (e.g. "User", "Order")."""

    model_config = ConfigDict(extra="forbid")

    name: str
    fields: list[EntityField] = Field(min_length=1)
    relations: list[str] = Field(default_factory=list)


class WebRequirementsSpec(BaseModel):
    """The full WRS (FR-020, FR-021).

    Post-validation, beyond per-field rules: entity names are unique, and every role a page
    references is declared in ``roles`` (FR-023 requires each feature's own acceptance list,
    already enforced by ``Feature.acceptance``).
    """

    model_config = ConfigDict(extra="forbid")

    project_type: str
    domain: str
    roles: list[str] = Field(min_length=1)
    features: list[Feature] = Field(min_length=1)
    pages: list[Page] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    api_needs: list[str] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)
    security_level: Literal["standard", "high"] = "standard"
    non_functional: dict[str, str] = Field(default_factory=dict)
    deployment_target: str = "local-docker"
    stack: str = "golden-v1"
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_cross_references(self) -> Self:
        names = [e.name for e in self.entities]
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"Duplicate entity names: {dupes}")

        known_roles = set(self.roles)
        for page in self.pages:
            unknown = sorted(set(page.roles) - known_roles)
            if unknown:
                raise ValueError(f"Page {page.route!r} references unknown roles: {unknown}")
        return self
