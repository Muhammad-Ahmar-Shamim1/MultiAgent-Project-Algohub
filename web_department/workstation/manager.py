"""Docker-backed worker workstations: one isolated container + network per project (FR-060..065).

Each workstation is a container built from the golden-v1 image, on its own bridge network with a
Postgres sidecar for the generated app, with a bind mount at ``/workspace`` for exactly that
project's directory and nothing else - no Docker socket, no other host path (NFR-007).

Docker-py is synchronous; every call into it runs off the event loop via ``asyncio.to_thread``.
"""

import asyncio
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker
from docker import DockerClient
from docker.errors import NotFound
from sqlalchemy.ext.asyncio import AsyncSession

from web_department.collaboration.events import EventBus
from web_department.config.settings import get_settings
from web_department.db.models import Workstation
from web_department.db.repositories import WorkstationRepository
from web_department.logging import get_logger

log = get_logger(__name__)

GOLDEN_IMAGE = "algohub/workstation:golden-v1"
SIDECAR_DB_IMAGE = "postgres:16-alpine"
WORKSPACE_MOUNT = "/workspace"
EXEC_OUTPUT_LIMIT = 20_000
SIDECAR_DB_USER = "app"
SIDECAR_DB_PASSWORD = "workspace"  # noqa: S105  local, per-project, non-internet-facing sidecar
SIDECAR_DB_NAME = "app"


class WorkstationTimeout(Exception):
    """Raised when a command does not finish within its allotted time (FR-053)."""


@dataclass(frozen=True)
class ExecResult:
    """The outcome of one ``exec`` call, with output truncated to ``EXEC_OUTPUT_LIMIT`` chars."""

    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


def _project_names(project_id: str) -> tuple[str, str, str]:
    """(network name, workstation container name, sidecar db container name) for a project."""
    base = f"algohub-ws-{project_id}"
    return base, base, f"{base}-db"


def _truncate(data: bytes) -> tuple[str, bool]:
    text = data.decode("utf-8", errors="replace")
    if len(text) <= EXEC_OUTPUT_LIMIT:
        return text, False
    return text[:EXEC_OUTPUT_LIMIT], True


class WorkstationManager:
    """Creates, execs into, lists and destroys per-project Docker workstations."""

    def __init__(
        self,
        session: AsyncSession,
        bus: EventBus | None = None,
        *,
        client: DockerClient | None = None,
    ) -> None:
        self.session = session
        self.bus = bus
        self._client = client

    def _docker(self) -> DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def _workspace_path(self, project_id: str) -> Path:
        return Path(get_settings().workstation_data_dir).resolve() / project_id

    async def create(self, project_id: str) -> Workstation:
        """Provision a network, workstation container and DB sidecar for ``project_id``."""
        workspace_path = self._workspace_path(project_id)
        limits = await asyncio.to_thread(self._create_sync, project_id, workspace_path)

        repo = WorkstationRepository(self.session)
        return await repo.create(
            project_id=project_id,
            container_id=limits["container_id"],
            repo_path=str(workspace_path),
            limits=limits,
            status="running",
        )

    def _create_sync(self, project_id: str, workspace_path: Path) -> dict[str, Any]:
        settings = get_settings()
        client = self._docker()
        network_name, container_name, db_name = _project_names(project_id)

        workspace_path.mkdir(parents=True, exist_ok=True)
        workspace_path.chmod(0o777)

        client.networks.create(network_name, driver="bridge", check_duplicate=True)
        try:
            db_container = client.containers.run(
                SIDECAR_DB_IMAGE,
                name=db_name,
                environment={
                    "POSTGRES_USER": SIDECAR_DB_USER,
                    "POSTGRES_PASSWORD": SIDECAR_DB_PASSWORD,
                    "POSTGRES_DB": SIDECAR_DB_NAME,
                },
                network=network_name,
                detach=True,
                mem_limit="512m",
            )
            db_url = (
                f"postgresql://{SIDECAR_DB_USER}:{SIDECAR_DB_PASSWORD}@{db_name}:5432/"
                f"{SIDECAR_DB_NAME}"
            )
            container = client.containers.run(
                GOLDEN_IMAGE,
                name=container_name,
                command=["sleep", "infinity"],
                detach=True,
                network=network_name,
                volumes={str(workspace_path): {"bind": WORKSPACE_MOUNT, "mode": "rw"}},
                working_dir=WORKSPACE_MOUNT,
                environment={"DATABASE_URL": db_url},
                mem_limit=settings.workstation_mem,
                nano_cpus=int(settings.workstation_cpu * 1_000_000_000),
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                user="agent",
            )
        except Exception:
            self._teardown_sync(network_name, [container_name, db_name])
            raise

        return {
            "cpu": settings.workstation_cpu,
            "mem": settings.workstation_mem,
            "network": network_name,
            "container_id": container.id,
            "db_container_id": db_container.id,
            "db_container_name": db_name,
        }

    async def exec(
        self, ws: Workstation, cmd: list[str], timeout: float | None = None
    ) -> ExecResult:
        """Run ``cmd`` inside ``ws`` as the non-root ``agent`` user, bounded by ``timeout`` seconds.

        On timeout the underlying Docker exec call keeps running server-side (docker-py's exec
        API has no cancel primitive); the caller still gets ``WorkstationTimeout`` promptly so
        bounded-retry logic upstream (AGENTS.md rule 8) is not blocked by a hung command.
        """
        if ws.container_id is None:
            raise ValueError(f"Workstation {ws.id!r} has no container (not created yet?)")
        settings = get_settings()
        effective_timeout = timeout if timeout is not None else settings.task_timeout_min * 60
        try:
            exit_code, stdout, stderr = await asyncio.wait_for(
                asyncio.to_thread(self._exec_sync, ws.container_id, cmd), timeout=effective_timeout
            )
        except TimeoutError as exc:
            raise WorkstationTimeout(
                f"Command timed out after {effective_timeout}s: {' '.join(cmd)}"
            ) from exc

        stdout_text, stdout_truncated = _truncate(stdout)
        stderr_text, stderr_truncated = _truncate(stderr)
        return ExecResult(exit_code, stdout_text, stderr_text, stdout_truncated, stderr_truncated)

    def _exec_sync(self, container_id: str, cmd: list[str]) -> tuple[int, bytes, bytes]:
        client = self._docker()
        api = client.api
        exec_id = api.exec_create(
            container_id, cmd, user="agent", workdir=WORKSPACE_MOUNT, tty=False
        )["Id"]
        stdout, stderr = api.exec_start(exec_id, demux=True)
        exit_code = api.exec_inspect(exec_id)["ExitCode"]
        return exit_code, stdout or b"", stderr or b""

    async def destroy(self, ws: Workstation) -> None:
        """Remove ``ws``'s containers, network and host workspace directory."""
        limits = ws.limits or {}
        container_names = [n for n in (ws.container_id, limits.get("db_container_id")) if n]
        network_name = limits.get("network")
        await asyncio.to_thread(self._teardown_sync, network_name, container_names)
        await asyncio.to_thread(shutil.rmtree, self._workspace_path(ws.project_id), True)
        await WorkstationRepository(self.session).set_status(ws.id, "destroyed")

    def _teardown_sync(self, network_name: str | None, container_ids: list[str]) -> None:
        client = self._docker()
        for container_id in container_ids:
            try:
                client.containers.get(container_id).remove(force=True)
            except NotFound:
                pass
        if network_name:
            try:
                client.networks.get(network_name).remove()
            except NotFound:
                pass

    async def list(self, project_id: str | None = None) -> list[Workstation]:
        """List known workstations, optionally scoped to one project."""
        repo = WorkstationRepository(self.session)
        if project_id is not None:
            return await repo.list(project_id=project_id)
        return await repo.list()

    async def health(self) -> dict[str, Any]:
        """A lightweight liveness check of the Docker daemon itself."""
        start = time.monotonic()
        try:
            client = await asyncio.to_thread(self._docker)
            await asyncio.to_thread(client.ping)
        except Exception as exc:  # noqa: BLE001  daemon reachability is a broad, expected failure mode
            return {"docker_reachable": False, "error": str(exc)}
        return {
            "docker_reachable": True,
            "latency_ms": int((time.monotonic() - start) * 1000),
        }
