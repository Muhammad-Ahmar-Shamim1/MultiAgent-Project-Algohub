"""A real uvicorn server in a thread (Starlette's test client cannot stream endless responses)."""

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from redis.asyncio import Redis
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import TEST_REDIS_DB
from web_department.api.deps import (
    BlueprintRegistryHolder,
    get_blueprint_registry_holder,
    get_bus,
    get_db_factory,
    get_redis,
)
from web_department.api.main import app
from web_department.blueprints.registry import BLUEPRINTS_DIR, BlueprintRegistry, load_all
from web_department.collaboration.events import EventBus
from web_department.config.settings import get_settings
from web_department.db.base import create_engine


@pytest.fixture
def live_server(test_db_url: URL, redis_client: Redis) -> Iterator[str]:
    """Serve the app on a free port, wired to the test DB and Redis db 15; yield its base URL."""
    resources: dict[str, Any] = {}

    def build() -> None:
        if "factory" not in resources:
            engine = create_engine(test_db_url.render_as_string(hide_password=False))
            factory = async_sessionmaker(engine, expire_on_commit=False)
            redis = Redis.from_url(
                get_settings().redis_url, db=TEST_REDIS_DB, decode_responses=True
            )
            resources.update(factory=factory, bus=EventBus(redis, factory), redis=redis)

    def bus_override() -> EventBus:
        build()
        return resources["bus"]  # type: ignore[no-any-return]

    def factory_override() -> async_sessionmaker[AsyncSession]:
        build()
        return resources["factory"]  # type: ignore[no-any-return]

    def redis_override() -> Redis:
        build()
        return resources["redis"]  # type: ignore[no-any-return]

    def blueprint_holder_override() -> BlueprintRegistryHolder:
        # lifespan="off" below means main.py's own startup sync never runs, so nothing ever
        # calls init_blueprint_registry() - without this override, any blueprints/* route would
        # hit get_blueprint_registry_holder()'s "not loaded yet" RuntimeError. One holder per
        # live_server call (cached in `resources`) so a PUT persists for that test's later GETs.
        if "blueprint_holder" not in resources:
            found = load_all(BLUEPRINTS_DIR)
            resources["blueprint_holder"] = BlueprintRegistryHolder(
                BlueprintRegistry([spec for spec, _raw, _checksum in found.values()])
            )
        return resources["blueprint_holder"]  # type: ignore[no-any-return]

    app.dependency_overrides[get_bus] = bus_override
    app.dependency_overrides[get_db_factory] = factory_override
    # Without this, routes/projects.py's real orchestrator.start()/runner.request_*() calls
    # would silently enqueue/write control keys against Redis db 0 - the real one, not the test's
    # isolated TEST_REDIS_DB - since api/deps.py's own get_redis() defaults there.
    app.dependency_overrides[get_redis] = redis_override
    app.dependency_overrides[get_blueprint_registry_holder] = blueprint_holder_override

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        app.dependency_overrides.clear()
