"""FastAPI dependencies; tests replace these through ``app.dependency_overrides``."""

from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web_department.blueprints.registry import BlueprintRegistry
from web_department.collaboration.events import EventBus, get_event_bus
from web_department.config.settings import get_settings
from web_department.db.base import get_session_factory

_default_redis: Redis | None = None


@dataclass
class BlueprintRegistryHolder:
    """Mutable box around the process-wide ``BlueprintRegistry``.

    A plain module global would work for reads, but ``PUT /api/v1/blueprints/{id}`` needs to
    swap in a new registry and have every route (and every test override, which returns this
    same holder each call) see the change immediately - a fresh immutable ``BlueprintRegistry``
    wouldn't be visible to a dependency override that already captured the old one.
    """

    registry: BlueprintRegistry


_blueprint_registry_holder: BlueprintRegistryHolder | None = None


def init_blueprint_registry(registry: BlueprintRegistry) -> BlueprintRegistryHolder:
    """Called once by ``api/main.py``'s lifespan after startup's ``load_and_sync`` (FR-041)."""
    global _blueprint_registry_holder
    _blueprint_registry_holder = BlueprintRegistryHolder(registry)
    return _blueprint_registry_holder


def get_blueprint_registry_holder() -> BlueprintRegistryHolder:
    """The process-wide blueprint registry holder; tests override this dependency directly."""
    if _blueprint_registry_holder is None:
        raise RuntimeError("Blueprint registry not loaded yet - has the app's lifespan run?")
    return _blueprint_registry_holder


def get_bus() -> EventBus:
    """The process-wide event bus."""
    return get_event_bus()


def get_db_factory() -> async_sessionmaker[AsyncSession]:
    """Factory for short-lived sessions (never hold one open while streaming)."""
    return get_session_factory()


def get_redis() -> Redis:
    """The Redis client routes/projects.py's pause/resume/cancel/submit use to reach the runner
    queue - a real, overridable dependency (not runner.py's own default-client fallback) so tests
    can point it at their own isolated Redis db instead of silently touching db 0.
    """
    global _default_redis
    if _default_redis is None:
        _default_redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _default_redis
