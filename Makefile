PYTHONPATH := $(shell pwd)
SHELL := /usr/bin/bash

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "%-16s %s\n", $$1, $$2}'

# ── Deployed: install runtime deps, then serve the app ────────────────────────
install: ## Install runtime dependencies (uv sync)
	uv sync

run: ## Serve the HTTP API + bundled SPA (the deployed entrypoint)
	uv run nodum serve

# ── Dev: install everything, then run the API + frontend together ─────────────
dev-install: frontend-install ## Install everything for dev (Python dev deps + frontend npm)
	uv sync --all-groups

dev-run: db-up init-db ## Run the API + Vite frontend together (brings up the DB first)
	@test -e frontend/node_modules/.bin/vite \
	  || { echo "frontend deps missing — run 'make dev-install'." >&2; exit 1; }
	@echo "→ API on :8600 · frontend on :5700  (Ctrl-C stops both)"
	@set -m; \
	  uv run nodum serve & api=$$!; \
	  ( cd frontend && npm run dev ) & web=$$!; \
	  trap 'kill -- -$$api -$$web 2>/dev/null' INT TERM; \
	  wait -n; \
	  kill -- -$$api -$$web 2>/dev/null; \
	  wait 2>/dev/null

# ── CLI ───────────────────────────────────────────────────────────────────────
cli: ## Run the nodum CLI (pass args after --, e.g. make cli -- search foo)
	uv run nodum

# ── Database (local PostgreSQL container) ─────────────────────────────────────
db-up: ## Start the local PostgreSQL container (waits until healthy)
	docker compose up -d --wait

db-down: ## Stop the local PostgreSQL container
	docker compose down

db-check: ## Fail fast unless the local PostgreSQL container is running
	@docker compose ps db --status running --quiet | grep -q . \
	  || { echo "nodum DB is not running — run 'make db-up' first." >&2; exit 1; }

init-db: ## Create the schema + seed the default kind catalog
	uv run nodum init-db

# ── Frontend (React + Vite SPA in frontend/) ──────────────────────────────────
frontend-install: ## Install the frontend npm deps (npm ci)
	cd frontend && npm ci

frontend-dev: ## Run the Vite dev server (5700), proxying the API to 8600
	cd frontend && npm run dev

frontend-build: ## Build the React SPA into frontend/dist
	cd frontend && npm run build

serve-spa: frontend-build ## Build the SPA, then serve it through FastAPI on 8600
	NODUM_WEB_DIST=$(shell pwd)/frontend/dist uv run nodum serve

# ── Docker image (full app: API + built UI) ───────────────────────────────────
docker-build: ## Build the full-app Docker image (API + built UI)
	docker build -t nodum .

# ── Quality (tests, lint, format) ─────────────────────────────────────────────
test: ## Run pytest (needs the database up)
	uv run pytest

coverage: ## Run pytest with line-coverage report
	uv run pytest --cov=nodum --cov-report=term-missing --cov-report=html

lint: ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

format: ## Ruff auto-fix + format
	uv run ruff check --fix .
	uv run ruff format .

.PHONY: help install run dev-install dev-run cli db-up db-down db-check init-db frontend-install frontend-dev frontend-build serve-spa docker-build test coverage lint format
