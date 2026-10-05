# AlgoHub Web Department

An open-source (MIT), zero-cost, autonomous multi-agent system that turns a web project request
into a planned, coded, tested, secured, deployed and monitored web app.

Full specification: [docs/SRS.md](docs/SRS.md). Requirement IDs (`FR-xxx`, `NFR-xxx`, `DR-xxx`,
`IR-xxx`) referenced throughout the codebase point back to that document.

## Hard constraints

- **$0 cost**: no paid APIs, SaaS, or hosting required. Local models via Ollama first; free API
  tiers (Groq, Cerebras, Google AI Studio, OpenRouter free, GitHub Models, Mistral) only as
  fallback, routed through LiteLLM.
- Every dependency is open source with an MIT-compatible license.
- Runs on one machine with `docker compose up`.
- Secrets never live in Git and are never sent to `shared` (free cloud) LLM providers.

## Stack

**Platform**: Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2 (async), Alembic, PostgreSQL 16 +
pgvector, Redis 7 (Streams + queue), LangGraph (orchestrator), LiteLLM (model router), OpenHands
Agent SDK (worker execution), Docker SDK (workstations), pytest, ruff, mypy. Dashboard: Next.js +
TypeScript + Tailwind + shadcn/ui.

**Generated-app golden stack (v1)**: Next.js + TypeScript (frontend), FastAPI + SQLAlchemy
(backend), PostgreSQL, pytest + Vitest + Playwright, Docker.

## Repository layout

```
web_department/
  api/            intake + dashboard REST API
  config/         settings, permissions, environments
  orchestrator/   web_agent, orchestrator graph, analyzer, planner, router, scheduler, state
  blueprints/     YAML worker definitions + schema
  agents/         worker implementations per role
  workstation/    docker manager, tool layer, command policy
  llm/            model router, prompts, cache, usage
  knowledge/      global, web, projects, memory
  collaboration/  events, war room
  qa/  security/  deployment/  monitoring/
  db/             models, migrations, repositories
templates/golden-v1/   app template the workers build on
dashboard/              Next.js app
tests/                  unit + integration
docs/                   SRS.md, ADRs, guides
```

## Getting started

```bash
cp .env.example .env              # fill in local values
python -m venv .venv && source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -e ".[dev]"
make lint test                    # ruff + mypy + pytest (coverage)
make up                           # docker compose up -d: postgres, redis, api
curl localhost:8000/health        # -> {"status":"ok","version":"0.1.0"}
make logs                         # tail service logs
make down                         # stop everything
```

To also run a local Ollama container for free, self-hosted LLM inference:

```bash
docker compose --profile local-llm up -d
```

## Events

Every important action is an event (SRS 7.3): stored in the append-only `events` table and streamed
to Redis (`events:{project_id}`, `events:all`). Modules publish with
`emit(EventType.X, project_id=..., session=session)`; project/task/agent state changes emit
`STATE_CHANGED` automatically. Watch a project live over Server-Sent Events:

```bash
curl -N localhost:8000/api/v1/projects/<project_id>/events                # new events only
curl -N "localhost:8000/api/v1/projects/<project_id>/events?from_id=0"    # replay, then live
python scripts/demo_events.py --create                                    # make a demo project
python scripts/demo_events.py <project_id>                                # emit 3 events
```

Reconnecting clients resume automatically with the standard `Last-Event-ID` header.

## LLM Router

Every LLM call in this codebase goes through `web_department/llm/router.py` (AGENTS.md rule 6) -
never import a provider SDK directly elsewhere. Providers are declared in
[providers.yaml](web_department/llm/providers.yaml) and picked per call by `purpose`
(`planning` | `coding` | `review` | `embedding`) following the SRS 11.1 routing table: local
Ollama first for coding, the strongest free API model first for planning/review, falling back to
local; `confidential=True` calls never reach a `shared` (free cloud) provider (IR-024). Responses
are cached in Redis, daily usage per provider is budgeted and enforced, and every call is logged
as an `LLMCall` row (FR-153).

```python
from web_department.llm.router import Purpose, complete

result = await complete([{"role": "user", "content": "..."}], purpose=Purpose.CODING)
print(result.provider, result.content)
```

Check the router end to end:

```bash
python -m web_department.llm.router --ping   # prints which provider answered
```

To exercise the free-tier fallback, stop Ollama, uncomment one shared provider's block in
`providers.yaml`, set its API key in `.env`, then run `--ping` again.

## Intake API

`POST /api/v1/projects` accepts a project request (FR-001), classifies whether it's in the Web
department's scope via the LLM router, and either rejects it with a reason (FR-002) or creates the
`Project`, emits `PROJECT_RECEIVED` and hands off to the orchestrator (FR-003). Every endpoint
needs a bearer token mapped to a role (`requester` | `approver` | `operator`, SRS 2.3) via
`REQUESTER_TOKEN` / `APPROVER_TOKEN` / `OPERATOR_TOKEN` in `.env`.

```bash
curl -X POST localhost:8000/api/v1/projects \
  -H "Authorization: Bearer $REQUESTER_TOKEN" -H "Content-Type: application/json" \
  -d '{"title": "Todo app", "description": "A todo app with login.", "requester_id": "u-1"}'
# -> 202 {"id": "prj_...", "state": "RECEIVED", ...}

curl localhost:8000/api/v1/projects/<id> -H "Authorization: Bearer $REQUESTER_TOKEN"
curl localhost:8000/api/v1/projects/<id>/tasks -H "Authorization: Bearer $REQUESTER_TOKEN"
curl localhost:8000/api/v1/projects/<id>/war-room -H "Authorization: Bearer $REQUESTER_TOKEN"
```

`GET /projects/{id}/war-room` (FR-080) returns the project's team (one agent per role the plan
needs), its task board grouped by state, the latest 50 events and its artifacts
(`docs/architecture.md`, `openapi.yaml`, `db/schema.md`, `qa/screenshots/*.png`, `reports/**`).
404 until `collaboration/war_room.open()` has formed the team. Once the room is closed, its
artifacts have been copied to `$WAR_ROOM_ARCHIVE_DIR` and the response shows that manifest.

## Approvals

Before deploying, the pipeline pauses at `AWAITING_APPROVAL` (FR-130..FR-132). It records a
`production_deploy` approval request with an evidence bundle: the git diff summary, the QA and
security reports, screenshots and the deployment plan. If `APPROVAL_WEBHOOK_URL` is set, a
Discord or Slack incoming webhook is notified.

```bash
curl localhost:8000/api/v1/approvals?pending=true -H "Authorization: Bearer $REQUESTER_TOKEN"
curl localhost:8000/api/v1/approvals/<id> -H "Authorization: Bearer $REQUESTER_TOKEN"   # evidence
curl -X POST localhost:8000/api/v1/approvals/<id>   -H "Authorization: Bearer $APPROVER_TOKEN" -H "Content-Type: application/json"   -d '{"decision": "reject", "comment": "Checkout needs a CSRF token", "approver_id": "you"}'
```

- **Approve:** the runner resumes the paused run and deploys.
- **Reject** (a comment is required): the project returns to DEVELOPING with a new fix task
  carrying the comment. After the fix it comes back for a fresh approval.
- Only the approver token may decide; requests and decisions are append-only.

## Security exceptions

A High/Critical security finding blocks release until it is fixed or an Approver accepts it
(FR-113):

```bash
curl -X POST localhost:8000/api/v1/security/exceptions   -H "Authorization: Bearer $APPROVER_TOKEN" -H "Content-Type: application/json"   -d '{"project_id": "prj_...", "tool": "gitleaks", "rule": "stripe-access-token",
       "file": "backend/app/config.py", "line": 4, "approver_id": "you",
       "justification": "Test-mode key, rotated before launch."}'
```

Copy `tool`/`rule`/`file`/`line` from the project's `artifacts/security-report.json`. The
exception is stored as an append-only approval plus an `APPROVED` event.

## Workstations

Each project's workers act inside an isolated Docker workstation (FR-060..065, NFR-007): its own
bridge network, a container built from the `algohub/workstation:golden-v1` image (Python, Node 20
+ pnpm, git, Playwright/Chromium, the Postgres client) running as the non-root `agent` user, a
bind mount at `/workspace` for exactly that project's directory and nothing else - no Docker
socket, no other host path - plus a Postgres sidecar on the same network for the generated app.

```bash
make build-workstation-image   # builds algohub/workstation:golden-v1 (slow: Node + Chromium)
```

Workers only touch a workstation through `web_department/workstation/tools.py`
(`read_file`/`write_file`/`list_dir`/`search`/`run_command`/`run_tests`/`git_commit`/
`browser_screenshot`), never `manager.py` directly. Every tool call checks the caller's role
permissions, keeps paths inside `/workspace`, and emits a `TOOL_CALL` event. `run_command` applies
the allow/deny policy in `workstation/policy.py`: an allow-listed program runs; a hard-denied one
(`sudo`, `docker`, `rm -rf /`, `chmod 777`, `DROP DATABASE`, `curl | sh`, ...) raises
`PermissionDenied`; a destructive-but-legitimate one (`git reset --hard`, `rm -r somedir`,
`DROP TABLE`, ...) raises `ApprovalRequired` and records a pending `Approval` instead of running.

```python
from web_department.workstation.manager import WorkstationManager
from web_department.workstation import tools
from web_department.config.permissions import Role

manager = WorkstationManager(session, bus=bus)
ws = await manager.create(project.id)
result = await tools.run_command(session, ws, Role.BACKEND, agent_id, manager, ["node", "-v"])
await manager.destroy(ws)
```

## Templates

`templates/golden-v1/` is the starting app every generated project is built from: Next.js (App
Router) + TypeScript + Tailwind on the frontend, FastAPI + SQLAlchemy 2 (async) + Alembic + JWT
auth on the backend, Postgres for storage, `docker compose` to run all three. It ships empty of
business logic on purpose - workers extend it, they don't rebuild it. `templates/golden-v1/
TEMPLATE_GUIDE.md` is the contract every worker reads first: where models, routers, pages and
components go, naming rules, and the `make install|dev|test|lint|e2e` commands.

## Workers

`agents/base.py`'s `BaseWorker` is the plan -> edit -> check -> retry loop every worker role
shares (FR-100..FR-104): it builds a bounded context (`TEMPLATE_GUIDE.md`, the task's title,
inputs and acceptance checks, a shallow relevant-file listing, a WRS excerpt if one applies -
never the whole repo), asks the Model Router for the files to write, writes them and runs the
task's checks via `workstation/tools.py`, and on failure feeds the exact check output back for
another attempt - up to `Settings.max_task_retries` (default 3), bounded overall by
`Settings.max_llm_calls_per_task` (default 40). On success it commits as `"<display_id>: <title>"`,
moves the task to `REVIEW` and emits `TASK_COMPLETED`; on exhaustion it moves the task to
`BLOCKED` and emits `TEST_FAILED` - a task is never marked done without its checks passing.

`agents/coder/agent.py`'s `CoderWorker` is the first concrete worker: a generalist that reads and
writes code and tests. It calls the router with `purpose=coding` (local model first, SRS 11.1) and
`confidential=True` (IR-024: a worker's own project code never goes to a shared/free-tier
provider).

```python
from web_department.agents.coder.agent import CoderWorker
from web_department.workstation.manager import WorkstationManager

worker = CoderWorker(session, agent, blueprint_dict, workstation, manager, bus=bus)
result = await worker.run(task)  # TaskResult(success, files_changed, tests_passed, summary, attempts)
```

## Blueprints

Every worker role is defined declaratively, in one `blueprints/*.yaml` file per role
(`web_architect`, `frontend_developer`, `backend_developer`, `database_developer`,
`api_integration`, `qa_engineer`, `security_engineer`, `devops_engineer`, `maintenance`;
FR-040..FR-043) instead of scattered across code: its responsibilities and skills, the
`llm/prompts/` system prompt it renders, which tools and evaluation checks it may use, its
`models` (Model Router *purposes*, in preference order - the router still picks the actual
provider per call, SRS 11.1), `memory_scope`, `autonomy`, and a bounded `budget`
(`max_llm_calls_per_task` / `max_retries` / `timeout_min`, AGENTS.md rule 8).

`blueprints/schema.py`'s `BlueprintSpec` validates every file at startup - semver version, only
known tool/evaluation-check names, and **permissions that must exactly match
`config.permissions.ROLE_PERMISSIONS` for that role** (SRS section 10), so the YAML can never
silently drift from the enforced permission table. `blueprints/registry.py`'s `load_and_sync()`
runs once in `api/main.py`'s lifespan: it validates every file, upserts a checksummed cache row
per blueprint into the DB (the YAML file in Git stays the actual source of truth, FR-042), and
refuses to let the process start at all if any file is invalid (FR-041).

```bash
curl -H "Authorization: Bearer $OPERATOR_TOKEN" http://localhost:8000/api/v1/blueprints
curl -H "Authorization: Bearer $OPERATOR_TOKEN" http://localhost:8000/api/v1/blueprints/backend_developer
```

`PUT /api/v1/blueprints/{id}` (operator-only) publishes a new *version* of a blueprint - it
always requires a semver bump over the current version and re-runs the same validation, and it
never overwrites a version in place.

`qa/checks.py`'s `CHECKS` registry is the seven evaluation checks a blueprint's `evaluation` list
may name (`lint_pass`, `unit_tests_pass`, `e2e_pass`, `screenshot_attached`, `openapi_valid`,
`migration_reversible`, `no_secrets`) - each one runs for real inside the project's workstation
through `workstation/tools.py`, the same as any worker action, and returns a real pass/fail, never
an assumed one.

## Agent Factory

`orchestrator/agent_factory.py` creates and reuses worker `Agent` rows (FR-050..FR-053), driving
them through `db/states.py`'s `AGENT_TRANSITIONS`: `CREATED -> PROVISIONED` (attach the project's
workstation) `-> AVAILABLE -> ASSIGNED -> WORKING -> REVIEW -> AVAILABLE ... -> RETIRED`.

- `create(role, project_id)` - a fresh Agent, `display_id` `WEB-<ROLECODE>-NNN` (`BE`, `FE`, `DB`,
  `API`, `QA`, `SEC`, `DEVOPS`, `ARCH`, `MAINT`), recording the role's current blueprint id and
  version.
- `acquire(role, project_id)` - reuse an `AVAILABLE` agent of that role in the project, else
  `create()` one; raises `RoleCapReached` once the project holds `Settings.max_agents_per_role`
  live (non-`RETIRED`) agents of that role and none are free.
- `release(agent)` - back to `AVAILABLE`; `retire(agent)` - `RETIRED` at project end.
- `watchdog_tick()` - BLOCKs (and emits `ALERT_RAISED`) every agent that has been `WORKING` longer
  than its own blueprint's `budget.timeout_min`; `run_watchdog()` calls it on a timer
  (`Settings.agent_watchdog_interval_seconds`) as a long-running background task.

`agents/registry.py`'s `build(registry)` maps every `Role` to a `BaseWorker` subclass carrying
that role's blueprint (`role`/`purpose`/`system_prompt_name`) - all nine roles run the same
generic plan/edit/check/retry loop for now; per-role specialisation is step 17.

## Orchestration

`orchestrator/graph.py` wires everything above into one LangGraph state machine
(FR-010..FR-014): `analyze -> plan -> provision_workstation -> copy_template -> execute_tasks ->
integrate -> test -> security -> approval -> deploy -> finish`, with `test` looping back to
`execute_tasks` up to 3 times on failure (creating a `FIX_REQUIRED` task each time) before
blocking and escalating. `security`/`approval`/`deploy` are stubs that always pass for now -
`qa/`, `security/` and `deployment/` are still empty scaffolds. Every node maps onto
`db/states.py`'s `PROJECT_TRANSITIONS` table (SRS 3.1) and is checkpointed, so a killed and
restarted runner resumes from the last completed node instead of starting over.

`orchestrator/runner.py` is the background process that drives it:

```bash
python -m web_department.orchestrator.runner
```

It pulls project ids off a Redis queue (`WebAgent.accept()` -> `orchestrator.start()` enqueues,
only after that project's own transaction commits) and runs one project's graph at a time against
the LangGraph Postgres checkpointer, keyed by `thread_id=project_id`. `POST
/api/v1/projects/{id}/pause|resume|cancel` (operator-only) are Redis-level runner control, not a
new `ProjectState` - pause/resume don't change what phase a project is conceptually in, and
cancel moves it straight to `FAILED` (reachable from every non-terminal state).

## Run your first project

*(Screen recording: TODO - link here once one is captured against a real run.)*

```bash
make up                                    # postgres, redis, api
docker compose --profile local-llm up -d   # or set a free-tier key in .env instead
make build-workstation-image               # algohub/workstation:golden-v1 (slow, first time only)
python -m web_department.orchestrator.runner &   # or add it to `make up` once you're set up

curl -X POST localhost:8000/api/v1/projects \
  -H "Authorization: Bearer $REQUESTER_TOKEN" -H "Content-Type: application/json" \
  -d '{"title": "Todo app", "description": "Build a todo web app. Users can register, log in, \
create, edit, complete and delete their own todos.", "requester_id": "u-1"}'
# -> 202 {"id": "prj_...", "state": "RECEIVED", ...}

curl -N localhost:8000/api/v1/projects/<id>/events   # watch it move through every state, live
curl localhost:8000/api/v1/projects/<id>              # or just poll "state" until COMPLETED
```

Once `state` is `COMPLETED`, the generated app is sitting at
`$WORKSTATION_DATA_DIR/<project_id>/` - a full Next.js + FastAPI + Postgres repo, ready to run:

```bash
cd "$WORKSTATION_DATA_DIR/<project_id>"
make test        # the app's own generated tests - the exit criterion is these are all green
cp .env.example .env
docker compose up -d --build
curl localhost:8000/health                 # -> {"status": "ok"}
```

`tests/e2e/test_r1_todo.py` (`make test-e2e`) automates exactly this walkthrough end to end and
is how Release 1 was validated - see `docs/r1-findings.md` for anything it turned up and
`docs/r1-metrics.json` for the run's numbers (LLM calls per provider, retries per task, cache hit
rate, total time).

## Engineering rules

See [AGENTS.md](AGENTS.md) for the full set of rules this project is built under (small steps,
type hints, tests alongside every module, events on state changes, LLM calls only through the
router, workers acting only through the permission-checked tool layer, bounded retries, config via
environment).
