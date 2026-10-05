.PHONY: install dev test lint e2e

install:
	python3 -m venv backend/.venv
	backend/.venv/bin/pip install --upgrade pip
	backend/.venv/bin/pip install -e "./backend[dev]"
	cd frontend && npm install

dev:
	docker compose up --build

test:
	backend/.venv/bin/pytest backend/tests
	cd frontend && npm run test:unit

lint:
	backend/.venv/bin/ruff check backend/app backend/tests
	backend/.venv/bin/mypy backend/app
	cd frontend && npm run lint
	cd frontend && npx tsc --noEmit

e2e:
	cd frontend && npx playwright install --with-deps chromium
	cd frontend && npm run e2e
