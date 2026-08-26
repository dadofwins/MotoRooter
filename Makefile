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

dev: port-check ## Run backend and frontend dev servers together
	$(MAKE) -j2 dev-backend dev-frontend

port-check: ## Fail early and legibly if :8000 is taken, before anything starts.
	@# A prerequisite, not a check inside dev-backend, because `make -j2` runs the two
	@# servers concurrently: the backend's refusal killed the whole target and took the
	@# frontend with it, so the symptom was "the frontend does not work" and the cause
	@# scrolled past interleaved with vite's startup banner. Tim reported exactly that.
	@if ss -ltn 2>/dev/null | grep -q ':8000 ' || lsof -i :8000 >/dev/null 2>&1; then \
		echo ""; \
		echo "  Something is already listening on :8000, so the API will not start —"; \
		echo "  and because both servers start together, the frontend will not either."; \
		echo ""; \
		echo "  Usually a dev server left running in another worktree. It would serve"; \
		echo "  the UI happily while being older than your branch, which is worse than"; \
		echo "  not starting: chat 501s, routes look wrong, nothing says why."; \
		echo ""; \
		echo "      pkill -f 'uvicorn motorooter'   &&   make dev"; \
		echo ""; \
		exit 1; \
	fi

dev-backend: ## API on :8000. Uses real providers if backend/.env has keys, else FakeProvider.
	@# FakeProvider interpolates STRAIGHT LINES between waypoints. That is fine for testing
	@# wiring, and useless for judging whether a route looks right — so prefer real routing
	@# whenever credentials exist, and say which mode is running rather than leaving someone
	@# to wonder why their route ignores the roads.
	@# Refuse to start if something already holds the port. Uvicorn would fail to bind and
	@# exit, but `make dev` runs the frontend in parallel, so the UI comes up and proxies to
	@# whatever IS listening — which, after a day of verifying branches in scratch worktrees,
	@# was a stale offline server drawing straight lines. Tim reported it as broken routing.
	@if ss -ltn 2>/dev/null | grep -q ':8000 ' || lsof -i :8000 >/dev/null 2>&1; then \
		echo "REFUSING: something is already listening on :8000."; \
		echo "  It may be a stale server from another worktree, which will silently serve"; \
		echo "  the frontend and can be in a different mode than you expect."; \
		echo "  Clear it with:  pkill -f 'uvicorn motorooter'"; \
		exit 1; \
	fi
	@cd backend && if grep -qs '^ORS_API_KEY=.\+' .env; then \
		echo "dev-backend: REAL providers (ORS + Google) — routes follow actual roads"; \
		set -a && . ./.env && set +a && \
		MOTOROOTER_OFFLINE=0 MOTOROOTER_TRIPS_EPHEMERAL=1 \
		uv run uvicorn motorooter.app:create_app \
			--factory --reload --port 8000; \
	else \
		echo "dev-backend: OFFLINE (FakeProvider) — routes will be STRAIGHT LINES, no keys found"; \
		MOTOROOTER_OFFLINE=1 uv run uvicorn motorooter.app:create_app \
			--factory --reload --port 8000; \
	fi

dev-backend-offline: ## Force FakeProvider even when keys exist (hermetic, no quota spend)
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
	@# `ruff check` does not enforce formatting, so without this, format drift merges
	@# unnoticed and then reappears as noise in the next person's diff.
	cd backend && uv run ruff format --check .
	cd frontend && npm run lint

fmt: ## Auto-format
	cd backend && uv run ruff format . && uv run ruff check --fix .

typecheck: ## mypy --strict and tsc --noEmit
	cd backend && uv run mypy
	cd frontend && npm run typecheck

PREVIEW_DIR ?= /tmp/motorooter-preview

preview: ## Run an unmerged branch for hands-on testing. make preview BRANCH=fe/map-canvas
	@test -n "$(BRANCH)" || { echo 'usage: make preview BRANCH=fe/map-canvas'; exit 1; }
	@git fetch -q origin
	@rm -rf $(PREVIEW_DIR) && git worktree prune
	@git worktree add -q --detach $(PREVIEW_DIR) origin/$(BRANCH)
	@# Local secrets live outside git, so carry them into the scratch checkout.
	@test -f frontend/.env.local && cp frontend/.env.local $(PREVIEW_DIR)/frontend/ || true
	@test -f backend/.env && cp backend/.env $(PREVIEW_DIR)/backend/ || true
	@echo "=== $(BRANCH) checked out at $(PREVIEW_DIR) ==="
	@cd $(PREVIEW_DIR) && $(MAKE) --no-print-directory install >/dev/null
	@echo "=== starting: API on :8000, UI on :5173 (mode printed below) ==="
	@cd $(PREVIEW_DIR) && $(MAKE) --no-print-directory dev

preview-clean: ## Remove the preview worktree
	@rm -rf $(PREVIEW_DIR) && git worktree prune && echo "removed $(PREVIEW_DIR)"

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
	@# _TAG from git, because $$SHORT_SHA is empty for a directory submit and docker rejects
	@# an empty tag. Falls back to the cloudbuild default if this is not a git checkout.
	gcloud builds submit --config infra/cloudbuild.yaml \
		--substitutions=_SERVICE=$(SERVICE),_REGION=$(REGION),_TAG=$(shell git rev-parse --short HEAD)

ROLE   ?= $(shell scripts/mail whoami)
BRANCH ?= $(shell git rev-parse --abbrev-ref HEAD)

handoff: export MSG_BODY = $(MSG)
handoff: ## Verify, push, ask for review. MSG="one line", or pipe the body on stdin.
	@# Prefer stdin for anything with backticks. MSG is expanded by *your* shell before make
	@# sees it, so `foo` inside it runs as a command substitution and is replaced by nothing —
	@# a handoff went out with every identifier silently deleted, and the env-var fix here
	@# cannot help because the damage happens at the call site. A quoted heredoc cannot be
	@# mangled that way:
	@#
	@#     make handoff <<'EOF'
	@#     ... body with `identifiers` ...
	@#     EOF
	@#
	@# One shell block, because `body` has to survive to the last line.
	@set -e; \
	body="$$MSG_BODY"; \
	if [ -z "$$body" ] && [ ! -t 0 ]; then body=$$(cat); fi; \
	test -n "$$body" || { echo 'usage: make handoff MSG="..." — or pipe the body on stdin'; exit 1; }; \
	test "$(BRANCH)" != "main" || { echo 'refusing: handoff runs from a feature branch, not main'; exit 1; }; \
	test -z "$$(git status --porcelain)" || { echo 'refusing: commit your work first (git status is dirty)'; exit 1; }; \
	git fetch -q origin main; \
	git merge-base --is-ancestor origin/main HEAD || { \
		echo 'refusing: your branch is behind origin/main.'; \
		echo 'Rebase first so the review diff is against current main:'; \
		echo '    git rebase origin/main'; \
		exit 1; }; \
	$(MAKE) --no-print-directory check </dev/null; \
	git push -u origin $(BRANCH); \
	printf '%s\n' "$$body" | scripts/mail send integrator "review request: $(BRANCH)"

handoff-blocked: export MSG_BODY = $(MSG)
handoff-blocked: ## Push a branch that CANNOT pass check alone, for the integrator to resolve.
	@test -n "$$MSG_BODY" || { echo 'usage: make handoff-blocked MSG="what fails and why you cannot fix it"'; exit 1; }
	@test "$(BRANCH)" != "main" || { echo 'refusing: not from main'; exit 1; }
	@test -z "$$(git status --porcelain)" || { echo 'refusing: commit your work first'; exit 1; }
	@# Deliberately skips `make check`. The gate exists to stop broken work being handed off,
	@# but a change that is correct on your side and breaks the other side's build is a real
	@# and recurring case — a signed-off contract change is the obvious one. Blocking it just
	@# means the work sits unpushed and invisible.
	@git fetch -q origin
	@git push -u origin $(BRANCH)
	@printf 'BLOCKED — needs integrator resolution.\n\n%s\n' "$$MSG_BODY" \
		| scripts/mail send integrator "BLOCKED: $(BRANCH)"
	@echo "pushed $(BRANCH) and flagged it as blocked. Start something else."

mail-watch: ## Stream new messages for this role. Point the Monitor tool at this.
	@scripts/mail watch $(ROLE)

mail-read: ## Print and archive unread messages for this role
	@scripts/mail read $(ROLE)

mail-peek: ## Print unread messages without archiving them
	@scripts/mail peek $(ROLE)

clean:
	rm -rf backend/.venv backend/.pytest_cache backend/.mypy_cache backend/.ruff_cache
	rm -rf frontend/node_modules frontend/dist

# Single backend test:   cd backend && uv run pytest tests/routing/test_ors.py::TestOrsContract
# Single frontend test:  cd frontend && npx vitest run -t "drag end"
