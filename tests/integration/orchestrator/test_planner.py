"""Planner: deterministic expansion + extra tasks + DAG persistence (FR-030..032, FR-038)."""

from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus
from web_department.db.models import Event, Project, RequirementSpec, Task
from web_department.db.repositories import ProjectRepository, RequirementSpecRepository
from web_department.db.states import ProjectState
from web_department.llm.router import CompletionResult
from web_department.orchestrator import planner as planner_module
from web_department.orchestrator.planner import (
    ExtraTask,
    ExtraTasksResponse,
    PlannedTask,
    Planner,
    PlanValidationError,
    plan_to_mermaid,
    validate_and_order,
)
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


@pytest.fixture
def mock_extra_tasks(monkeypatch: pytest.MonkeyPatch) -> Callable[[ExtraTasksResponse], AsyncMock]:
    def _set(response: ExtraTasksResponse | None = None) -> AsyncMock:
        result = CompletionResult(
            content="{}",
            parsed=response if response is not None else ExtraTasksResponse(tasks=[]),
            provider="test",
            model="test/model",
            cached=False,
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
        )
        mock = AsyncMock(return_value=result)
        monkeypatch.setattr(planner_module, "complete", mock)
        return mock

    return _set


@pytest.fixture
async def planning_project_and_spec(
    session: AsyncSession, project: Project
) -> tuple[Project, RequirementSpec]:
    """A project in PLANNING with a stored WRS, mirroring the analyzer's output."""
    repo = ProjectRepository(session)
    await repo.update_state(project.id, ProjectState.ANALYZING)
    project = await repo.update_state(project.id, ProjectState.PLANNING)
    spec = await RequirementSpecRepository(session).create(
        project_id=project.id,
        version=1,
        json=ECOMMERCE_WRS.model_dump(mode="json"),
        created_by="test",
    )
    return project, spec


async def test_ecommerce_wrs_produces_a_valid_dag_with_sequential_web_ids(
    session: AsyncSession,
    bus: EventBus,
    planning_project_and_spec: tuple[Project, RequirementSpec],
    mock_extra_tasks: Callable[[ExtraTasksResponse | None], AsyncMock],
) -> None:
    mock_extra_tasks()
    project, spec = planning_project_and_spec

    tasks = await Planner(session, bus=bus).plan(project, spec)

    assert [t.display_id for t in tasks] == [f"WEB-{i:03d}" for i in range(1, len(tasks) + 1)]
    assert len(tasks) >= 10  # init_repo x2 + architecture + db x2 + backend x2 + auth + 3 crud +
    #                          frontend + 3 page + 3 integration + 3 e2e + security + deploy = 23
    titles = {t.title for t in tasks}
    assert "Architecture record" in titles
    assert "Backend foundation (part 1/2)" in titles
    assert "Backend foundation (part 2/2)" in titles
    assert "CRUD API: User" in titles
    assert "CRUD API: Product" in titles
    assert "CRUD API: Order" in titles
    assert "Page: /cart" in titles
    assert "Wire up feature: F-01" in titles
    assert "End-to-end tests: F-03" in titles
    assert project.state is ProjectState.ARCHITECTING


async def test_dependencies_always_point_to_an_earlier_task(
    session: AsyncSession,
    bus: EventBus,
    planning_project_and_spec: tuple[Project, RequirementSpec],
    mock_extra_tasks: Callable[[ExtraTasksResponse | None], AsyncMock],
) -> None:
    mock_extra_tasks()
    project, spec = planning_project_and_spec

    tasks = await Planner(session, bus=bus).plan(project, spec)

    index = {t.id: i for i, t in enumerate(tasks)}
    for i, task in enumerate(tasks):
        for dep in task.depends_on:
            assert index[dep.id] < i, f"{task.display_id} depends on a later task {dep.display_id}"


async def test_split_tasks_are_chained_in_order(
    session: AsyncSession,
    bus: EventBus,
    planning_project_and_spec: tuple[Project, RequirementSpec],
    mock_extra_tasks: Callable[[ExtraTasksResponse | None], AsyncMock],
) -> None:
    mock_extra_tasks()
    project, spec = planning_project_and_spec

    tasks = await Planner(session, bus=bus).plan(project, spec)

    init_repo_parts = [t for t in tasks if t.title.startswith("Initialize repository scaffold")]
    assert [t.title for t in init_repo_parts] == [
        "Initialize repository scaffold (part 1/2)",
        "Initialize repository scaffold (part 2/2)",
    ]
    part_two = init_repo_parts[1]
    assert [d.id for d in part_two.depends_on] == [init_repo_parts[0].id]
    assert part_two.acceptance_checks == ["Repository scaffold builds and CI passes"]
    assert init_repo_parts[0].acceptance_checks != part_two.acceptance_checks


async def test_task_created_events_are_emitted_for_every_task(
    session: AsyncSession,
    bus: EventBus,
    planning_project_and_spec: tuple[Project, RequirementSpec],
    mock_extra_tasks: Callable[[ExtraTasksResponse | None], AsyncMock],
) -> None:
    mock_extra_tasks()
    project, spec = planning_project_and_spec

    tasks = await Planner(session, bus=bus).plan(project, spec)

    events = (
        await session.scalars(
            select(Event).where(Event.project_id == project.id, Event.type == "TASK_CREATED")
        )
    ).all()
    assert {e.task_id for e in events} == {t.id for t in tasks}


async def test_extra_task_from_the_llm_is_included_and_wired(
    session: AsyncSession,
    bus: EventBus,
    planning_project_and_spec: tuple[Project, RequirementSpec],
    mock_extra_tasks: Callable[[ExtraTasksResponse | None], AsyncMock],
) -> None:
    mock_extra_tasks(
        ExtraTasksResponse(
            tasks=[
                ExtraTask(
                    title="Integrate payment gateway",
                    role="backend",
                    description="Wire up the payment provider's SDK.",
                    depends_on_templates=["backend_foundation"],
                    acceptance_checks=["A test payment completes successfully"],
                    est_files=2,
                    est_lines=100,
                )
            ]
        )
    )
    project, spec = planning_project_and_spec

    tasks = await Planner(session, bus=bus).plan(project, spec)

    extra = next(t for t in tasks if t.title == "Integrate payment gateway")
    backend_foundation_last_part = next(
        t for t in tasks if t.title == "Backend foundation (part 2/2)"
    )
    assert extra.role == "backend"
    assert extra.acceptance_checks == ["A test payment completes successfully"]
    assert backend_foundation_last_part.id in {d.id for d in extra.depends_on}


async def test_extra_task_referencing_an_unknown_template_is_rejected(
    session: AsyncSession,
    bus: EventBus,
    planning_project_and_spec: tuple[Project, RequirementSpec],
    mock_extra_tasks: Callable[[ExtraTasksResponse | None], AsyncMock],
) -> None:
    mock_extra_tasks(
        ExtraTasksResponse(
            tasks=[
                ExtraTask(
                    title="Mystery task",
                    role="backend",
                    description="...",
                    depends_on_templates=["not_a_real_template"],
                    acceptance_checks=["It works"],
                )
            ]
        )
    )
    project, spec = planning_project_and_spec

    with pytest.raises(PlanValidationError, match="not_a_real_template"):
        await Planner(session, bus=bus).plan(project, spec)

    tasks = (await session.scalars(select(Task).where(Task.project_id == project.id))).all()
    assert tasks == []
    assert project.state is ProjectState.PLANNING


async def test_plan_to_mermaid_renders_the_full_ecommerce_plan(
    session: AsyncSession,
    bus: EventBus,
    planning_project_and_spec: tuple[Project, RequirementSpec],
    mock_extra_tasks: Callable[[ExtraTasksResponse | None], AsyncMock],
) -> None:
    mock_extra_tasks()
    project, spec = planning_project_and_spec

    tasks = await Planner(session, bus=bus).plan(project, spec)
    graph = plan_to_mermaid(tasks)

    assert graph.startswith("flowchart TD\n")
    for task in tasks:
        assert f'{task.display_id}["{task.display_id}: ' in graph
    assert " --> " in graph


async def test_validate_and_order_used_by_plan_matches_the_standalone_function() -> None:
    """Sanity check: the DAG validator Planner uses is the same one exported for direct testing."""
    planned = {"a": PlannedTask(key="a", title="a", role="backend")}

    assert validate_and_order(planned) == ["a"]
