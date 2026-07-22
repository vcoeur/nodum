PYTHONPATH := $(shell pwd)
SHELL := /usr/bin/bash

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "%-16s %s\n", $$1, $$2}'

install: ## Install runtime dependencies (uv sync)
	uv sync

dev-install: ## Install everything for dev (uv sync --all-groups)
	uv sync --all-groups

cli: ## Run the nodum CLI (pass args after --, e.g. make cli -- node list)
	uv run nodum

init-db: ## Create the database and apply migrations
	uv run nodum init

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

.PHONY: help install dev-install cli init-db test coverage lint format
