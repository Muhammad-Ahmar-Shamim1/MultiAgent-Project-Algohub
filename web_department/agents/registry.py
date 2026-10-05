"""Role -> worker class (FR-053-adjacent; the Agent Factory step).

``BaseWorker`` reads ``role``/``purpose``/``system_prompt_name``/``evaluation_checks`` off the
*class*, so a single shared class can't serve nine different roles at once - this builds one
small subclass per role, parameterized by that role's loaded Blueprint. Roles with a specialised
worker (step 17: its own context builder and checks, e.g. ``ArchitectWorker``) subclass that;
the rest still subclass ``BaseWorker`` and run its generic loop unchanged.
"""

from web_department.agents.architect.agent import ArchitectWorker
from web_department.agents.base import BaseWorker
from web_department.agents.qa.agent import QAWorker
from web_department.agents.security.agent import SecurityWorker
from web_department.blueprints.registry import BlueprintRegistry
from web_department.blueprints.schema import BlueprintSpec
from web_department.config.permissions import Role
from web_department.llm.router import Purpose

SPECIALISED_WORKERS: dict[Role, type[BaseWorker]] = {
    Role.ARCHITECT: ArchitectWorker,
    Role.QA: QAWorker,
    Role.SECURITY: SecurityWorker,
}


def _worker_class_for(spec: BlueprintSpec) -> type[BaseWorker]:
    class_name = "".join(part.title() for part in spec.role.value.split("_")) + "Worker"
    purpose = spec.models[0] if spec.models else Purpose.CODING
    return type(
        class_name,
        (SPECIALISED_WORKERS.get(spec.role, BaseWorker),),
        {
            "role": spec.role,
            "purpose": purpose,
            "system_prompt_name": spec.system_prompt_template,
            "evaluation_checks": tuple(spec.evaluation),
        },
    )


def build(registry: BlueprintRegistry) -> dict[Role, type[BaseWorker]]:
    """One worker class per loaded blueprint, keyed by role."""
    return {spec.role: _worker_class_for(spec) for spec in registry.list()}
