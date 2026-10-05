# agents

Worker implementations per role (Architect, Frontend, Backend, Database, API, QA, Security, DevOps, Maintenance).

`base.py` has the shared plan -> edit -> check -> retry loop (`BaseWorker`, FR-100..FR-104) every
worker uses; `coder/` is the first concrete worker, a generalist that implements its own tasks.

`registry.py`'s `build(registry)` maps every `Role` to a `BaseWorker` subclass parameterized by
that role's loaded Blueprint (`role`/`purpose`/`system_prompt_name`). Each runs `BaseWorker`'s
generic loop unless the role has a specialised worker in `SPECIALISED_WORKERS`, which it
subclasses instead. Each class also carries its Blueprint's `evaluation` list as
`evaluation_checks`.

Step 17 specialises the workers one role at a time. A specialised worker overrides
`BaseWorker`'s hooks: `_build_initial_messages` (its own context builder), `_reject_plan` (refuse
a plan before anything is written) and `_run_checks` (what "checks pass" means for the role;
`_run_evaluation_checks()` runs the Blueprint's checks from `qa/checks.py`).

| Role | Worker | Writes | Checks |
|---|---|---|---|
| architect | `architect/agent.py` `ArchitectWorker` (FR-033) | `docs/architecture.md`, `openapi.yaml`, `db/schema.md` only | all three exist; `openapi_valid`; every WRS feature id tags an operation; every WRS entity has a heading in `db/schema.md` |
| qa | `qa/agent.py` `QAWorker` (FR-100, FR-101) | `backend/tests/**`, `frontend/tests/**` only | every acceptance criterion of its feature has a collected pytest/Playwright test tagged with the criterion (e.g. `test_F02_1_...`). Passing is the quality gate's job, not the QA task's |
| security | `security/agent.py` `SecurityWorker` (FR-110..FR-112) | nothing (no write permission) | its own `run()`: scanners + LLM checklist review (always `confidential=True`), then `security/security_gate.py`. REVIEW if the gate passes, BLOCKED (plus fix tasks) if not |
