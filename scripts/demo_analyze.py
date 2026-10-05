"""Run the Requirement Analyzer on the SRS e-commerce example and print the WRS JSON.

    python scripts/demo_analyze.py

Requires a real, reachable LLM provider (e.g. `docker compose --profile local-llm up -d`, with a
model pulled: `docker compose exec ollama ollama pull qwen3:8b`).
"""

import asyncio
import json

from web_department.collaboration.events import close_event_bus
from web_department.db.base import get_engine, session_scope
from web_department.db.repositories import EventRepository, ProjectRepository
from web_department.llm.router import close_redis
from web_department.orchestrator.requirement_analyzer import RequirementAnalyzer

TITLE = "Clothing e-commerce"
DESCRIPTION = (
    "An e-commerce app for clothing with a product catalog, shopping cart, checkout and an "
    "admin panel. Roles: customer and admin. Customers can register, log in and log out, "
    "browse products, add them to a cart, update quantities and check out. Admins can manage "
    "the product catalog and view orders."
)
CONSTRAINTS = "Use the golden-v1 stack."


async def main() -> None:
    """Create a project, run the analyzer on it, and print the resulting WRS."""
    async with session_scope() as session:
        project = await ProjectRepository(session).create(
            title=TITLE, requester_id="demo", stack="golden-v1"
        )
        await EventRepository(session).create(
            type="PROJECT_RECEIVED",
            project_id=project.id,
            payload={"description": DESCRIPTION, "constraints": CONSTRAINTS, "attachments": []},
        )
        project_id = project.id

    async with session_scope() as session:
        project = await ProjectRepository(session).get(project_id)
        assert project is not None
        spec = await RequirementAnalyzer(session).analyze(project)

    print(f"project_id: {project_id}")
    print(f"spec version: {spec.version}")
    print(json.dumps(spec.json, indent=2))

    await close_redis()
    await close_event_bus()
    await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
