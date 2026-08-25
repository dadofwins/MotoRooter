.PHONY: help install dev dev-backend dev-frontend test test-backend test-frontend \
        lint fmt typecheck check build deploy clean

SERVICE  ?= motorooter
REGION   ?= us-west1
PROJECT  ?= $(shell gcloud config get-value project 2>/dev/null)
IMAGE    ?= $(REGION)-docker.pkg.dev/$(PROJECT)/$(SERVICE)/$(SERVICE)

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install backend and frontend dependencies
	cd backend && uv sync
	cd frontend && npm install

dev: ## Run backend and frontend dev servers together
	$(MAKE) -j2 dev-backend dev-frontend

dev-backend: ## API on :8000, backed by FakeProvider so no API keys are needed
	cd backend && MOTOROOTER_OFFLINE=1 uv run uvicorn motorooter.app:create_app \
		--factory --reload --port 8000

dev-frontend: ## Vite dev server on :5173, proxying /api to :8000
	cd frontend && npm run dev

test: test-backend test-frontend ## Run the full suite

test-backend:
	cd backend && uv run pytest

test-frontend:
	cd frontend && npm test

lint: ## Lint both sides
	cd backend && uv run ruff check .
	cd frontend && npm run lint

fmt: ## Auto-format
	cd backend && uv run ruff format . && uv run ruff check --fix .

typecheck: ## mypy --strict and tsc --noEmit
	cd backend && uv run mypy
	cd frontend && npm run typecheck

contract: ## Regenerate shared/openapi.json and the frontend TypeScript types
	cd backend && uv run python scripts/export_openapi.py
	cd frontend && npm run generate:types

contract-check: ## Fail if the committed contract has drifted from the code
	@$(MAKE) --no-print-directory contract
	@git diff --exit-code -- shared/openapi.json frontend/src/api/schema.ts \
		|| { echo ""; \
		     echo "ERROR: the API contract is out of date."; \
		     echo "The backend changed shapes without regenerating. Run 'make contract',"; \
		     echo "commit the result, and flag it — this is a frontend-breaking change."; \
		     exit 1; }

check: lint typecheck contract-check test ## Everything CI runs

build: ## Build the container image
	docker build -f infra/Dockerfile -t $(IMAGE):latest .

deploy: ## Build and deploy to Cloud Run
	gcloud builds submit --config infra/cloudbuild.yaml \
		--substitutions=_SERVICE=$(SERVICE),_REGION=$(REGION)

clean:
	rm -rf backend/.venv backend/.pytest_cache backend/.mypy_cache backend/.ruff_cache
	rm -rf frontend/node_modules frontend/dist

# Single backend test:   cd backend && uv run pytest tests/routing/test_ors.py::TestOrsContract
# Single frontend test:  cd frontend && npx vitest run -t "drag end"
