"""Release 1 exit criteria (SRS): submit a real todo-app request to a *running* AlgoHub stack
and prove the whole pipeline actually works, unattended, end to end.

    make test-e2e
    # or: pytest -m slow tests/e2e -v

Requires a running stack (`docker compose up -d`, or `uvicorn` + `python -m
web_department.orchestrator.runner` on the host) and a real, reachable LLM provider (Ollama
local-first, or a free-tier key in `.env`) - this is deliberately NOT mocked anywhere. Skips with
a clear reason if the API isn't reachable, rather than failing, since it is opt-in and excluded
from the default `pytest` run (see `pyproject.toml`'s `slow` marker).

What "passes" means here (all four, matching the STEP 13 exit criteria):
  1. the project reaches ProjectState.COMPLETED
  2. the generated repo's own tests (`make test`) pass when re-run independently
  3. the generated app's `/health` endpoint responds once started for real
  4. no human touched the generated code - only the platform (prompts/templates/task sizing)
     may be edited in response to a failure; see docs/r1-findings.md.
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from web_department.db.base import session_scope
from web_department.db.models import LLMCall, Task
from web_department.db.repositories import TaskRepository, WorkstationRepository
from web_department.db.states import ProjectState

pytestmark = pytest.mark.slow

API_BASE_URL = os.environ.get("E2E_API_BASE_URL", "http://localhost:8000")
REQUESTER_TOKEN = os.environ.get("REQUESTER_TOKEN", "")
# A local model doing full-stack codegen across ~10-15 tasks, each possibly retrying, is slow -
# default to a generous hour. Override with E2E_TIMEOUT_SECONDS for a faster/slower provider.
TIMEOUT_SECONDS = int(os.environ.get("E2E_TIMEOUT_SECONDS", 60 * 60))
POLL_INTERVAL_SECONDS = 5
HEALTH_TIMEOUT_SECONDS = 60

TODO_REQUEST = {
    "title": "Todo app",
    "description": (
        "Build a todo web app. Users can register, log in, create, edit, complete and delete "
        "their own todos."
    ),
    "constraints": "Use the golden-v1 stack.",
    "requester_id": "e2e-r1",
}

METRICS_PATH = Path(__file__).resolve().parent.parent.parent / "docs" / "r1-metrics.json"


@dataclass
class RunMetrics:
    """What STEP 13.3 asks to record."""

    project_id: str
    total_seconds: float
    final_state: str
    calls_per_provider: dict[str, int] = field(default_factory=dict)
    cache_hit_rate: float = 0.0
    retries_per_task: dict[str, int] = field(default_factory=dict)

    def write(self, path: Path = METRICS_PATH) -> None:
        path.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {REQUESTER_TOKEN}"}


def _require_live_api() -> None:
    try:
        response = httpx.get(f"{API_BASE_URL}/health", timeout=5)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(
            f"AlgoHub API is not reachable at {API_BASE_URL} ({exc!r}) - start it with "
            "`docker compose up -d` (or uvicorn + the runner) before running the e2e suite."
        )
    if not REQUESTER_TOKEN:
        pytest.skip(
            "REQUESTER_TOKEN is not set - the e2e suite needs the same token the "
            "running API was started with."
        )


async def _collect_metrics(project_id: str, total_seconds: float, final_state: str) -> RunMetrics:
    """FR-153 already traces every LLM call; this just aggregates it for the run's report."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(LLMCall.provider, LLMCall.cached, func.count())
                .join(Task, Task.id == LLMCall.task_id)
                .where(Task.project_id == project_id)
                .group_by(LLMCall.provider, LLMCall.cached)
            )
        ).all()
        calls_per_provider: dict[str, int] = {}
        cached_calls = 0
        total_calls = 0
        for provider, cached, count in rows:
            calls_per_provider[provider] = calls_per_provider.get(provider, 0) + count
            total_calls += count
            if cached:
                cached_calls += count

        tasks = await TaskRepository(session).list(project_id=project_id, limit=1000)
        retries_per_task = {t.display_id: t.retries for t in tasks}

    return RunMetrics(
        project_id=project_id,
        total_seconds=total_seconds,
        final_state=final_state,
        calls_per_provider=calls_per_provider,
        cache_hit_rate=(cached_calls / total_calls) if total_calls else 0.0,
        retries_per_task=retries_per_task,
    )


async def _workstation_repo_path(project_id: str) -> str:
    async with session_scope() as session:
        stations = await WorkstationRepository(session).list(project_id=project_id)
    if not stations:
        raise AssertionError(f"No workstation was ever provisioned for {project_id!r}")
    return stations[-1].repo_path


def _run_generated_tests(repo_path: str) -> subprocess.CompletedProcess[str]:
    """Independently re-verify "generated repo tests pass" - not just trusting the graph's own
    `test` node, which only proves the pipeline *believed* it passed."""
    return subprocess.run(
        ["make", "test"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _start_app_and_check_health(repo_path: str) -> httpx.Response:
    """`docker compose up` the generated app for real, on the host (not inside a workstation -
    workstations deliberately have no Docker socket, NFR-007), and hit its health endpoint."""
    env_file = Path(repo_path) / ".env"
    if not env_file.exists():
        example = Path(repo_path) / ".env.example"
        if example.exists():
            env_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    subprocess.run(
        ["docker", "compose", "up", "-d", "--build"], cwd=repo_path, check=True, timeout=900
    )
    try:
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                response = httpx.get("http://localhost:8000/health", timeout=5)
                if response.status_code == 200:
                    return response
            except httpx.HTTPError as exc:
                last_error = exc
            time.sleep(2)
        raise AssertionError(f"Generated app's /health never responded: {last_error!r}")
    finally:
        subprocess.run(["docker", "compose", "down", "-v"], cwd=repo_path, timeout=120)


async def test_r1_todo_app_builds_tests_green_and_runs() -> None:
    """Submit the SRS R1 todo-app request and prove it comes out the other end working."""
    _require_live_api()
    start = time.monotonic()

    with httpx.Client(timeout=15) as client:
        submitted = client.post(
            f"{API_BASE_URL}/api/v1/projects", json=TODO_REQUEST, headers=_auth()
        )
        submitted.raise_for_status()
        project_id = submitted.json()["id"]

        final_state = ProjectState.RECEIVED.value
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            got = client.get(f"{API_BASE_URL}/api/v1/projects/{project_id}", headers=_auth())
            got.raise_for_status()
            final_state = got.json()["state"]
            if final_state in (ProjectState.COMPLETED.value, ProjectState.FAILED.value):
                break
            if final_state == ProjectState.BLOCKED.value:
                break
            time.sleep(POLL_INTERVAL_SECONDS)

    total_seconds = time.monotonic() - start
    metrics = await _collect_metrics(project_id, total_seconds, final_state)
    metrics.write()

    assert final_state == ProjectState.COMPLETED.value, (
        f"Project {project_id} ended in {final_state} after {total_seconds:.0f}s, not COMPLETED. "
        f"See {METRICS_PATH} for what ran; write the root cause in docs/r1-findings.md and fix "
        "the platform (prompts/templates/task sizing), not the generated app by hand."
    )

    repo_path = await _workstation_repo_path(project_id)

    tests_result = _run_generated_tests(repo_path)
    assert tests_result.returncode == 0, (
        f"`make test` failed in the generated app:\n{tests_result.stdout}\n{tests_result.stderr}"
    )

    health_response = _start_app_and_check_health(repo_path)
    assert health_response.status_code == 200
