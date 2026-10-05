# AGENTS.md — AlgoHub Web Department

## What we are building
AlgoHub Web Department: an open-source (MIT), zero-cost, autonomous multi-agent system that turns a
web project request into a planned, coded, tested, secured, deployed and monitored web app.
Full spec: docs/SRS.md. Requirement IDs (FR-xxx, NFR-xxx, DR-xxx, IR-xxx) refer to it.

## Hard constraints
- $0 cost: no paid APIs, SaaS or hosting required. Local models via Ollama first; free API tiers
  (Groq, Cerebras, OpenRouter free, GitHub Models) only as fallback via LiteLLM.
- Every dependency must be open source with an MIT-compatible license.
- Runs on one machine with `docker compose up`.
- Secrets never in Git and never sent to `shared` (free cloud) LLM providers.

## Platform stack
Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2 (async), Alembic, PostgreSQL 16 + pgvector,
Redis 7 (Streams + queue), LangGraph (orchestrator), LiteLLM (model router), OpenHands Agent SDK
(worker execution), Docker SDK (workstations), pytest, ruff, mypy. Dashboard: Next.js + TypeScript
+ Tailwind + shadcn/ui.

## Generated-app golden stack (v1)
Next.js + TypeScript (frontend), FastAPI + SQLAlchemy (backend), PostgreSQL, pytest + Vitest +
Playwright, Docker.

## Repository layout
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
dashboard/             Next.js app
tests/                 unit + integration
docs/                  SRS.md, ADRs, guides

## Engineering rules
1. Work in small steps. Show a plan (files to add/change) before writing code.
2. Only touch files the task needs. Explain any extra change.
3. Type hints everywhere; ruff + mypy clean; docstrings on public functions.
4. Every module ships with pytest tests in the same change. Target 70%+ coverage.
5. All state changes emit an event (collaboration/events.py).
6. LLM calls ONLY through llm/router.py. Never import a provider SDK directly.
7. Workers act ONLY through workstation/tools.py, which enforces permissions.
8. Bounded retries everywhere (default 3). No infinite loops.
9. Config from environment via config/settings.py; provide .env.example entries.
10. If a requirement is unclear, ask. Do not invent product behaviour.

## Definition of done for any step
Code + tests written, `make lint test` passes, README/docs updated if behaviour changed,
and you list the commands I should run to verify.
