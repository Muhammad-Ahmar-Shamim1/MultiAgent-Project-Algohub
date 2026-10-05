# Contributing to AlgoHub Web Department

Thanks for your interest in contributing.

## Ground rules

This project follows the engineering rules in [AGENTS.md](AGENTS.md) — read it before opening a
PR. In short:

- Work in small, focused changes; explain any file you touch outside the task's scope.
- Type hints everywhere; code must be `ruff` and `mypy` clean.
- Every module ships with tests in the same change; keep coverage at 70%+ (`make test`).
- LLM calls only go through `web_department/llm/router.py`. Workers only act through
  `web_department/workstation/tools.py`.
- Requirements live in [docs/SRS.md](docs/SRS.md); cite `FR-xxx` / `NFR-xxx` / `DR-xxx` / `IR-xxx`
  IDs where relevant instead of re-describing behaviour that's already specified there.
- Every dependency must be open source with an MIT-compatible license, and nothing may require a
  paid API, SaaS or hosting tier to run.

## Getting started

```bash
cp .env.example .env
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
make lint test
docker compose up -d
```

## Adding a new worker role

Add a Blueprint YAML under `web_department/blueprints/` (see docs/SRS.md section 5.1) and, if
needed, a worker implementation under `web_department/agents/`. New roles should not require
changes to the orchestrator (FR-043).

## Pull requests

- Keep PRs scoped to one change.
- Include the commands used to verify the change (lint, tests, manual checks).
- Update README/docs when behaviour changes.
