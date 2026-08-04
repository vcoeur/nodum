PYTHONPATH := $(shell pwd)
SHELL := /usr/bin/bash
WEB_DIR := web
WEB_BUNDLE := nodum/_web

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

test: ## Run pytest (with the `pdf` extra, as CI does)
	uv run --extra pdf pytest

coverage: ## Run pytest with line-coverage report
	uv run pytest --cov=nodum --cov-report=term-missing --cov-report=html

lint: typecheck ## Ruff lint + format check + pyright typecheck
	uv run ruff check .
	uv run ruff format --check .

typecheck: ## Pyright type check over nodum/ (the 25 package modules)
	uv run pyright nodum

format: ## Ruff auto-fix + format
	uv run ruff check --fix .
	uv run ruff format .

# --- Frontend (web/) -------------------------------------------------------
# Node is a build-time dependency only: the wheel ships the built bundle and the
# runtime is pure Python. Run web-install once, then web-build before packaging.

web-install: ## Install the frontend dependencies (npm ci in web/)
	cd $(WEB_DIR) && npm ci

web-build: ## Type-check and build the UI bundle into nodum/_web/
	cd $(WEB_DIR) && npm run build

web-dev: ## Run the Vite dev server (proxies /api and /healthz to nodum serve)
	cd $(WEB_DIR) && npm run dev

web-typecheck: ## Type-check the frontend without building
	cd $(WEB_DIR) && npm run typecheck

# Vitest over the pure modules in web/src. The run is pinned to a non-UTC
# timezone (vitest.config.ts) because the zone-less-timestamp bug src/lib/time.ts
# fixes is invisible in UTC — which is what CI runs in.
web-test: ## Run the frontend unit tests (vitest)
	cd $(WEB_DIR) && npm test

web-clean: ## Drop the built bundle (nodum serve falls back to the placeholder)
	rm -rf $(WEB_BUNDLE)

.PHONY: help install dev-install cli init-db test coverage lint typecheck format \
	web-install web-build web-dev web-typecheck web-test web-clean
