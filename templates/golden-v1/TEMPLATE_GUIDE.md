# golden-v1 template guide

Read this before touching this codebase. It is the contract every worker (and every task's
acceptance check) assumes. If a task needs to deviate from it, that is itself something to flag,
not something to do silently.

## Stack

Next.js (App Router) + TypeScript + Tailwind CSS on the frontend, FastAPI + SQLAlchemy 2 (async)
+ Alembic on the backend, PostgreSQL for storage. `docker compose up` runs all three.

## Layout and naming

```
backend/
  app/
    main.py           FastAPI() instance, lifespan, router registration - nothing else
    config.py          Settings (pydantic-settings), loaded from environment only
    db.py               async engine, session factory, Base
    models/             one file per entity, snake_case module = snake_case(EntityName)
      user.py
    routers/             one file per resource, prefixed with that resource's plural name
      health.py
      auth.py
    auth/                 password hashing + JWT issuing/verification, schemas
      security.py
      schemas.py
  migrations/            Alembic; one migration per schema change, never edited after merge
  tests/                  pytest; test_<module>.py mirrors the module it tests

frontend/
  src/
    app/                  Next.js App Router: one folder per route, kebab-case
      layout.tsx
      page.tsx
      globals.css
    components/           PascalCase.tsx, one component per file, no page-specific logic
    lib/                   api-client.ts and other framework-agnostic helpers
  tests/
    unit/                  Vitest, *.test.ts(x), mirrors src/ structure
    e2e/                    Playwright, *.spec.ts, one file per user journey
```

**New entity ("Order", say):** `backend/app/models/order.py` (SQLAlchemy model) ->
`alembic revision --autogenerate -m "add order"` -> `backend/app/routers/orders.py` (CRUD
endpoints, prefix `/api/orders`) -> register the router in `main.py` -> a matching
`backend/tests/test_orders.py`.

**New page:** `frontend/src/app/<route>/page.tsx`. Data fetching goes through
`frontend/src/lib/api-client.ts`, never a raw `fetch()` scattered in a component.

## Rules

1. **Settings only from environment.** Backend: `app/config.py`'s `Settings` class. Frontend: only
   `NEXT_PUBLIC_*` variables are readable in the browser; anything else stays server-only. Never
   hardcode a URL, secret or port. `NEXT_PUBLIC_*` values are inlined into the client bundle at
   *build* time, not read at container start - `frontend/Dockerfile` takes `NEXT_PUBLIC_API_URL`
   as a build arg, and `docker-compose.yml` passes it under `build.args`, not `environment`.
2. **All backend routes go through `app/main.py`'s router registration** - no route defined
   directly on the `FastAPI()` instance outside `routers/`.
3. **Every model change has a migration.** Never hand-edit the database schema outside Alembic.
4. **Every new router or page ships with a test in the same change** - a pytest test for a router,
   a Vitest test for a component/page, and update the Playwright smoke test if it changes the
   primary user journey.
5. **Auth:** the `auth/` module is the only place that hashes passwords or issues/verifies JWTs.
   Routers depend on `auth.security.get_current_user`, they never touch a token directly.
6. **No secrets in source control.** `.env` is gitignored; `.env.example` documents every
   variable a fresh checkout needs.
7. **Keep it boring.** This template starts empty of business logic on purpose - it is the
   scaffold every generated project builds on, not an example app to extend in place.

## Commands

```bash
make install   # frontend + backend dependencies
make dev       # docker compose up: postgres + backend + frontend, hot reload
make test      # backend pytest + frontend vitest
make lint      # ruff + mypy (backend), eslint + tsc (frontend)
make e2e       # playwright, against a running `make dev` stack
```
