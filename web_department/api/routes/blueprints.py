"""Blueprint endpoints (FR-040..FR-043): list, view, and operator-only update.

The registry loaded at startup (``blueprints/registry.py``) is the live, in-process source these
routes read from - never re-parsed per request. ``PUT`` validates and persists a new *version* of
a blueprint (the YAML file in Git stays the actual source of truth for the next restart, FR-042 -
this only updates the DB's cached copy and the running process's registry) and never overwrites
a version in place.
"""

import hashlib
from typing import Annotated

import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web_department.api.auth import UserRole, require_role
from web_department.api.deps import (
    BlueprintRegistryHolder,
    get_blueprint_registry_holder,
    get_db_factory,
)
from web_department.blueprints.registry import BlueprintRegistry, BlueprintValidationError
from web_department.blueprints.schema import BlueprintSpec, semver_key
from web_department.db.repositories import BlueprintRepository
from web_department.llm.prompts import PromptLibrary

router = APIRouter(prefix="/api/v1/blueprints", tags=["blueprints"])

_prompts = PromptLibrary()

DbFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_db_factory)]
HolderDep = Annotated[BlueprintRegistryHolder, Depends(get_blueprint_registry_holder)]
OperatorOnly = Annotated[UserRole, Depends(require_role(UserRole.OPERATOR))]


def _find(holder: BlueprintRegistryHolder, blueprint_id: str) -> BlueprintSpec:
    for spec in holder.registry.list():
        if spec.id == blueprint_id:
            return spec
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Blueprint not found")


@router.get("", response_model=list[BlueprintSpec])
async def list_blueprints(_role: OperatorOnly, holder: HolderDep) -> list[BlueprintSpec]:
    """Every loaded blueprint, ordered by role."""
    return holder.registry.list()


@router.get("/{blueprint_id}", response_model=BlueprintSpec)
async def get_blueprint(blueprint_id: str, _role: OperatorOnly, holder: HolderDep) -> BlueprintSpec:
    """One blueprint by id."""
    return _find(holder, blueprint_id)


@router.put("/{blueprint_id}", response_model=BlueprintSpec)
async def update_blueprint(
    blueprint_id: str,
    body: BlueprintSpec,
    _role: OperatorOnly,
    holder: HolderDep,
    db_factory: DbFactory,
) -> BlueprintSpec:
    """Replace a blueprint with a new, strictly higher version.

    ``body`` is validated as a full ``BlueprintSpec`` (semver, known tools/checks, permissions
    exactly matching SRS section 10) before this ever runs. This additionally checks the path id
    matches the body, that the referenced system prompt actually exists, and that the version is
    a real bump - never a same-or-lower version, and never a silent in-place overwrite.
    """
    if body.id != blueprint_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "path blueprint_id must match body.id"
        )
    if not _prompts.versions(body.system_prompt_template):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"system_prompt_template {body.system_prompt_template!r} has no matching prompt file",
        )
    current = next((s for s in holder.registry.list() if s.id == blueprint_id), None)
    if current is not None and semver_key(body.version) <= semver_key(current.version):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"new version {body.version!r} must be greater than the current {current.version!r}",
        )

    raw_yaml = yaml.safe_dump(body.model_dump(mode="json"))
    checksum = hashlib.sha256(raw_yaml.encode("utf-8")).hexdigest()
    async with db_factory() as session:
        await BlueprintRepository(session).upsert(
            id=body.id, version=body.version, yaml=raw_yaml, checksum=checksum
        )
        await session.commit()

    remaining = [s for s in holder.registry.list() if s.role != body.role]
    try:
        holder.registry = BlueprintRegistry([*remaining, body])
    except BlueprintValidationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return body
