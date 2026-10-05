"""Load + validate every blueprints/*.yaml file at startup (FR-041..FR-043).

``api/main.py``'s lifespan calls ``load_and_sync`` once at process start; if any file fails
schema validation, it raises and the process refuses to start ("invalid Blueprints shall not be
instantiated" - FR-041) rather than serving with a partially-broken worker roster.
"""

import hashlib
from pathlib import Path

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.blueprints.schema import BlueprintSpec
from web_department.config.permissions import Role
from web_department.db.repositories import BlueprintRepository
from web_department.llm.prompts import PromptLibrary

BLUEPRINTS_DIR = Path(__file__).parent
_prompts = PromptLibrary()


class BlueprintValidationError(Exception):
    """Raised when a ``blueprints/*.yaml`` file fails schema validation (FR-041)."""


def _checksum(raw_yaml: str) -> str:
    return hashlib.sha256(raw_yaml.encode("utf-8")).hexdigest()


def load_all(directory: Path = BLUEPRINTS_DIR) -> dict[str, tuple[BlueprintSpec, str, str]]:
    """Parse and validate every ``*.yaml`` in ``directory``.

    Raises ``BlueprintValidationError`` naming the first bad file, rather than partially loading.
    Returns ``{blueprint_id: (spec, raw_yaml, sha256_checksum)}``.
    """
    found: dict[str, tuple[BlueprintSpec, str, str]] = {}
    for path in sorted(directory.glob("*.yaml")):
        raw = path.read_text(encoding="utf-8")
        try:
            spec = BlueprintSpec.model_validate(yaml.safe_load(raw))
            if not _prompts.versions(spec.system_prompt_template):
                raise ValueError(
                    f"system_prompt_template {spec.system_prompt_template!r} has no "
                    f"llm/prompts/{spec.system_prompt_template}_v*.jinja2 file"
                )
        except Exception as exc:
            raise BlueprintValidationError(f"{path.name}: {exc}") from exc
        if spec.id in found:
            raise BlueprintValidationError(f"{path.name}: duplicate blueprint id {spec.id!r}")
        found[spec.id] = (spec, raw, _checksum(raw))
    return found


class BlueprintRegistry:
    """The loaded, validated set of blueprints, keyed by role."""

    def __init__(self, specs: list[BlueprintSpec]) -> None:
        self._by_role: dict[Role, BlueprintSpec] = {}
        for spec in specs:
            if spec.role in self._by_role:
                raise BlueprintValidationError(
                    f"two blueprints both claim role {spec.role.value!r}: "
                    f"{self._by_role[spec.role].id!r} and {spec.id!r}"
                )
            self._by_role[spec.role] = spec

    def get(self, role: Role) -> BlueprintSpec:
        """Return the loaded Blueprint for ``role``, or raise ``LookupError``."""
        try:
            return self._by_role[role]
        except KeyError:
            raise LookupError(f"No blueprint loaded for role {role.value!r}") from None

    def list(self) -> list[BlueprintSpec]:
        """All loaded blueprints, ordered by role name."""
        return sorted(self._by_role.values(), key=lambda s: s.role.value)


async def load_and_sync(
    session: AsyncSession, directory: Path = BLUEPRINTS_DIR
) -> BlueprintRegistry:
    """Load+validate every blueprint file, upsert its DB row, and return the registry.

    Call once at startup (``api/main.py``'s lifespan). Raises ``BlueprintValidationError`` (and
    upserts nothing) if any file is invalid.
    """
    found = load_all(directory)
    repo = BlueprintRepository(session)
    for spec, raw, checksum in found.values():
        await repo.upsert(id=spec.id, version=spec.version, yaml=raw, checksum=checksum)
    return BlueprintRegistry([spec for spec, _raw, _checksum in found.values()])
