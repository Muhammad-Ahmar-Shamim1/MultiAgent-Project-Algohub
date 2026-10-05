.PHONY: up down logs lint test test-e2e fmt migrate downgrade build-workstation-image

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

lint:
	ruff check .
	mypy web_department

test:
	coverage run -m pytest
	coverage report

# Slow: needs `docker compose up -d postgres redis`, the golden-v1 image built, and a real LLM
# provider reachable (Ollama running locally, or a free-tier API key set in .env).
test-e2e:
	pytest -m slow tests/e2e -v

fmt:
	ruff format .
	ruff check --fix .

migrate:
	alembic upgrade head

downgrade:
	alembic downgrade -1

build-workstation-image:
	docker build -t algohub/workstation:golden-v1 -f web_department/workstation/images/Dockerfile.golden web_department/workstation/images
