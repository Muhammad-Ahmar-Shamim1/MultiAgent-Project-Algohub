"""Shared fixtures: a workstation on a tmp dir, an agent, and a task ready to be worked."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.db.models import Agent, Project, Task, Workstation
from web_department.db.repositories import (
    AgentRepository,
    BlueprintRepository,
    TaskRepository,
    WorkstationRepository,
)
from web_department.db.states import TaskState


@pytest_asyncio.fixture
async def ws(session: AsyncSession, project: Project, tmp_path: Path) -> Workstation:
    return await WorkstationRepository(session).create(
        project_id=project.id,
        container_id="fake-container",
        repo_path=str(tmp_path),
        limits={},
        status="running",
    )


@pytest_asyncio.fixture
async def agent(session: AsyncSession, project: Project) -> Agent:
    await BlueprintRepository(session).create(
        id="agents-test-bp", version="1", yaml="y", checksum="c"
    )
    return await AgentRepository(session).create(
        display_id="WEB-AG-001",
        blueprint_id="agents-test-bp",
        blueprint_version="1",
        project_id=project.id,
    )


@pytest_asyncio.fixture
async def task(session: AsyncSession, project: Project) -> Task:
    return await TaskRepository(session).create(
        display_id="WEB-001",
        project_id=project.id,
        title="Add GET /api/ping",
        role="backend",
        state=TaskState.ASSIGNED,
        inputs={},
        acceptance_checks=['GET /api/ping returns {"pong": true}'],
    )


@pytest.fixture
def fake_manager() -> AsyncMock:
    return AsyncMock()
