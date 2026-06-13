PYTHONPATH := $(shell pwd)
SHELL := /usr/bin/bash

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "%-12s %s\n", $$1, $$2}'

install: ## Install dependencies into a uv-managed venv
	uv sync

dev-install: ## Install dev dependencies too
	uv sync --all-groups

db-up: ## Start the local PostgreSQL container
	docker compose up -d

db-down: ## Stop the local PostgreSQL container
	docker compose down

db-check: ## Fail fast unless the local PostgreSQL container is running
	@docker compose ps db --status running --quiet | grep -q . \
	  || { echo "nodum DB is not running — run 'make db-up' first." >&2; exit 1; }

init-db: ## Create the schema in the configured database
	uv run nodum init-db

run: ## Run the nodum CLI (pass args after --, e.g. make run -- search foo)
	uv run nodum

serve: ## Run the HTTP API (uvicorn); serves the SPA when NODUM_WEB_DIST is set
	uv run nodum serve

frontend-install: ## Install the React frontend's npm deps (npm ci)
	cd frontend && npm ci

frontend-dev: ## Run the Vite dev server (5700), proxying the API to 8600
	cd frontend && npm run dev

frontend-build: ## Build the React SPA into frontend/dist
	cd frontend && npm run build

dev-web: frontend-build ## Build the SPA then serve it via FastAPI on 8600
	NODUM_WEB_DIST=$(shell pwd)/frontend/dist uv run nodum serve

dev: db-check ## Run the API (:8600) + Vite frontend (:5700); stop both when either exits
	@echo "→ API on :8600 · frontend on :5700  (Ctrl-C stops both)"
	@trap 'kill 0' EXIT INT TERM; \
	  uv run nodum serve & \
	  ( cd frontend && npm run dev ) & \
	  wait -n

docker-build: ## Build the full-app Docker image (API + built UI)
	docker build -t nodum .

test: ## Run pytest
	uv run pytest

coverage: ## Run pytest with line-coverage report
	uv run pytest --cov=nodum --cov-report=term-missing --cov-report=html

lint: ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

format: ## Ruff auto-fix + format
	uv run ruff check --fix .
	uv run ruff format .

.PHONY: help install dev-install db-up db-down db-check init-db run serve frontend-install frontend-dev frontend-build dev-web dev docker-build test coverage lint format
