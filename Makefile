PYTHONPATH := $(shell pwd)

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

init-db: ## Create the schema in the configured database
	uv run nodum init-db

run: ## Run the nodum CLI (pass args after --, e.g. make run -- search foo)
	uv run nodum

serve: ## Run the HTTP API + web view (uvicorn)
	uv run nodum serve

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

.PHONY: help install dev-install db-up db-down init-db run serve test coverage lint format
