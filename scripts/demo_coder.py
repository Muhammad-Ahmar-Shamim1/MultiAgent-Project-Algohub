"""Give the Coder worker one real task on a copy of templates/golden-v1 and print the result.

    python scripts/demo_coder.py

Requires Docker (a real workstation is provisioned from algohub/workstation:golden-v1) and a
real, reachable LLM provider (Ollama local-first, falling back per providers.yaml). Installs the
template backend's dependencies inside the workstation first (the equivalent of `make install`),
since the worker's own job is writing code and running checks, not provisioning the app.
"""

import asyncio
import shutil
from pathlib import Path

from web_department.agents.coder.agent import CoderWorker
from web_department.collaboration.events import close_event_bus
from web_department.config.permissions import Role
from web_department.db.base import get_engine, session_scope
from web_department.db.repositories import (
    AgentRepository,
    BlueprintRepository,
    ProjectRepository,
    TaskRepository,
)
from web_department.db.states import TaskState
from web_department.llm.router import close_redis
from web_department.workstation import tools
from web_department.workstation.manager import WorkstationManager

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "golden-v1"
TASK_TITLE = "Add GET /api/ping returning {pong: true} with a test"


async def main() -> None:
    """Provision a workstation from the template, hand the Coder one task, print the outcome."""
    async with session_scope() as session:
        project = await ProjectRepository(session).create(title="Coder demo", requester_id="demo")
        await BlueprintRepository(session).create(
            id="demo-coder-v1", version="1", yaml="role: coder", checksum="demo"
        )
        agent = await AgentRepository(session).create(
            display_id="WEB-DEMO-001",
            blueprint_id="demo-coder-v1",
            blueprint_version="1",
            project_id=project.id,
        )

        manager = WorkstationManager(session, bus=None)
        ws = await manager.create(project.id)
        print(f"workstation created: {ws.id} ({ws.repo_path})")

        print("copying templates/golden-v1 into the workspace...")
        shutil.copytree(TEMPLATE_DIR, ws.repo_path, dirs_exist_ok=True)

        print("installing backend dependencies inside the workstation (this takes a while)...")
        install_result = await tools.run_command(
            session,
            ws,
            Role.BACKEND,
            agent.id,
            manager,
            ["pip", "install", "-e", "backend[dev]"],
            timeout=600,
        )
        print(f"install exit_code={install_result.exit_code}")
        if install_result.exit_code != 0:
            print(install_result.stdout[-2000:])
            print(install_result.stderr[-2000:])
            await manager.destroy(ws)
            raise SystemExit("backend install failed; see output above")

        task = await TaskRepository(session).create(
            display_id="WEB-DEMO-001",
            project_id=project.id,
            title=TASK_TITLE,
            role="backend",
            state=TaskState.ASSIGNED,
            inputs={"test_path": "backend/tests"},
            acceptance_checks=[
                'GET /api/ping returns 200 with JSON body {"pong": true}',
                "A test for the new endpoint exists and passes",
            ],
        )

        worker = CoderWorker(session, agent, {}, ws, manager, bus=None)
        result = await worker.run(task)

        print("\n--- result ---")
        print(f"success:       {result.success}")
        print(f"tests_passed:  {result.tests_passed}")
        print(f"attempts:      {result.attempts}")
        print(f"files_changed: {result.files_changed}")
        print(f"summary:       {result.summary}")

        await manager.destroy(ws)
        print("\nworkstation destroyed")

    await close_redis()
    await close_event_bus()
    await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
