"""Run the Planner on the SRS e-commerce example and print the task table + Mermaid graph.

    python scripts/demo_plan.py

Requires a real, reachable LLM provider for the "extra tasks" call (see scripts/demo_analyze.py).
"""

import asyncio

from web_department.collaboration.events import close_event_bus
from web_department.db.base import get_engine, session_scope
from web_department.db.repositories import ProjectRepository, RequirementSpecRepository
from web_department.db.states import ProjectState
from web_department.llm.router import close_redis
from web_department.orchestrator.planner import Planner, plan_to_mermaid
from web_department.orchestrator.wrs_schema import (
    Entity,
    EntityField,
    Feature,
    Page,
    WebRequirementsSpec,
)

ECOMMERCE_WRS = WebRequirementsSpec(
    project_type="web_application",
    domain="ecommerce",
    roles=["customer", "admin"],
    features=[
        Feature(
            id="F-01",
            name="authentication",
            description="Users sign up and log in.",
            acceptance=["User can register, log in and log out"],
        ),
        Feature(
            id="F-02",
            name="cart",
            description="Users manage a shopping cart.",
            acceptance=["Cart total updates when quantity changes"],
        ),
        Feature(
            id="F-03",
            name="checkout",
            description="Users check out their cart.",
            acceptance=["Order is created after successful payment"],
        ),
    ],
    pages=[
        Page(route="/products", purpose="Browse the catalog", roles=["customer"]),
        Page(route="/cart", purpose="View and edit the cart", roles=["customer"]),
        Page(route="/admin/products", purpose="Manage products", roles=["admin"]),
    ],
    entities=[
        Entity(name="User", fields=[EntityField(name="email", type="string")]),
        Entity(name="Product", fields=[EntityField(name="name", type="string")]),
        Entity(
            name="Order",
            fields=[EntityField(name="total", type="decimal")],
            relations=["belongs to User"],
        ),
    ],
)


async def main() -> None:
    """Create a project + WRS, plan it, and print the task table and Mermaid graph."""
    async with session_scope() as session:
        repo = ProjectRepository(session)
        project = await repo.create(title="Clothing e-commerce", requester_id="demo")
        await repo.update_state(project.id, ProjectState.ANALYZING)
        project = await repo.update_state(project.id, ProjectState.PLANNING)
        spec = await RequirementSpecRepository(session).create(
            project_id=project.id,
            version=1,
            json=ECOMMERCE_WRS.model_dump(mode="json"),
            created_by="demo",
        )
        project_id, spec_id = project.id, spec.id

    async with session_scope() as session:
        project = await ProjectRepository(session).get(project_id)
        spec = await RequirementSpecRepository(session).get(spec_id)
        assert project is not None and spec is not None
        tasks = await Planner(session).plan(project, spec)

    print(f"project_id: {project_id}")
    print(f"{len(tasks)} tasks\n")
    print(f"{'ID':<10} {'ROLE':<16} {'DEPENDS ON':<30} TITLE")
    for task in tasks:
        deps = ", ".join(d.display_id for d in task.depends_on) or "-"
        print(f"{task.display_id:<10} {task.role:<16} {deps:<30} {task.title}")

    print("\n--- Mermaid ---\n")
    print(plan_to_mermaid(tasks))

    await close_redis()
    await close_event_bus()
    await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
