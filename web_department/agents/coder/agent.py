"""The Coder: the first worker - a generalist that implements its own tasks (FR-100..FR-104).

Everything about how a task is driven to completion (context building, the plan -> edit -> check
-> retry loop, commit-on-success, block-on-exhaustion) lives in ``agents.base.BaseWorker``; this
class only fixes the role (for the tool layer's permission checks, config/permissions.py) and the
Model Router purpose/confidentiality it calls with.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from web_department.agents.base import BaseWorker
from web_department.collaboration.events import EventBus
from web_department.config.permissions import Role
from web_department.config.settings import get_settings
from web_department.db.models import Agent, Workstation
from web_department.llm.router import Purpose
from web_department.workstation.manager import WorkstationManager


class CoderWorker(BaseWorker):
    """A generalist coder: writes code and tests, commits once its task's checks pass.

    ``role = Role.BACKEND``: of the SRS section 10 roles, the closest fit for a generalist that
    reads/writes code, reads/writes the app DB and runs commands - a dedicated ``coder`` role
    would need its own SRS-approved permission grant, which is out of scope here.
    Uses ``purpose=coding`` (local model first per SRS 11.1). ``confidential`` comes from
    ``Settings.coder_confidential`` (default True, IR-024) rather than being hardcoded: the
    default protects a worker's own project code from ever reaching a shared/free-tier provider,
    and flipping it is a deliberate, per-checkout, environment-level opt-out - never a silent
    code change - for when no private/local provider is available.
    """

    role = Role.BACKEND
    purpose = Purpose.CODING
    system_prompt_name = "worker_developer"

    def __init__(
        self,
        session: AsyncSession,
        agent: Agent,
        blueprint: dict[str, Any],
        workstation: Workstation,
        manager: WorkstationManager,
        bus: EventBus | None = None,
    ) -> None:
        super().__init__(session, agent, blueprint, workstation, manager, bus=bus)
        self.confidential = get_settings().coder_confidential
