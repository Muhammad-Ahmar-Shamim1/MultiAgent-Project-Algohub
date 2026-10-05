"""FastAPI application entry point for the intake + dashboard API."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from web_department.api.deps import init_blueprint_registry
from web_department.api.events import router as events_router
from web_department.api.routes.approvals import router as approvals_router
from web_department.api.routes.blueprints import router as blueprints_router
from web_department.api.routes.projects import router as projects_router
from web_department.api.routes.security import router as security_router
from web_department.api.routes.views import router as views_router
from web_department.blueprints.registry import BlueprintValidationError, load_and_sync
from web_department.collaboration.events import close_event_bus
from web_department.config.settings import get_settings
from web_department.db.base import session_scope
from web_department.logging import configure_logging, get_logger

__version__ = "0.1.0"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Load and validate settings and blueprints at startup so bad config fails fast (FR-041)."""
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = get_logger(__name__)
    try:
        async with session_scope() as session:
            registry = await load_and_sync(session)
    except BlueprintValidationError:
        logger.error("blueprint_validation_failed")
        raise
    init_blueprint_registry(registry)
    logger.info(
        "api_started",
        env=settings.env.value,
        version=__version__,
        blueprints=len(registry.list()),
    )
    yield
    await close_event_bus()


app = FastAPI(title="AlgoHub Web Department API", version=__version__, lifespan=lifespan)
app.include_router(events_router)
app.include_router(projects_router)
app.include_router(blueprints_router)
app.include_router(security_router)
app.include_router(approvals_router)
app.include_router(views_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check used by Docker healthchecks and CI."""
    return {"status": "ok", "version": __version__}
