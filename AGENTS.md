# AGENTS.md — nodum

Agent-facing instructions for working in this repository. Read this before
editing anything here.

## What this repo is

`nodum` is a **DB-native knowledge graph**: a typed graph of Markdown-content
nodes and typed edges in one SQLite file (WAL mode), behind a deterministic,
LLM-free service layer. Every mutation is validated, state-machine-checked,
logged in an append-only event log with full before/after payloads, versioned
(nodes), and reversible (`undo`). `[[wikilinks]]` in content are materialized
as `mentions` edges on write. The Typer CLI is a thin adapter emitting exactly
one JSON object per command.

Phase 1 (core) landed; Phase 2 (agent-native) is underway. Built so far in
Phase 2: **event-log projectors** (`nodum.projectors`) with per-projector
checkpoints and rebuild mechanics, the **`fts` projector** (FTS5 over node
title + content), and **BM25 keyword search** (`nodum.search`, CLI `search`).
**Deliberately not built yet** (later phases — do not add): sqlite-vec /
hybrid fusion, the MCP server, the web UI, assets/CAS/renditions/ingestion,
the internal agent runtime and consolidation cycle, Markdown Mirror / JSON
export. The schema reserves room for them (`graph_id`, `merge_redirects`,
`cycle_id`); each lands as its own append-only migration.

## Architecture

- **`nodum.service`** is the spine and the only writer — validation, the
  `proposed → active → archived` state machine, the event log, versions, undo,
  wikilink materialization. Each public function opens its own short-lived
  connection (applying pending migrations idempotently) and commits. New
  behaviour and validation go here first; adapters must not add behaviour the
  service lacks.
- **`nodum.projectors`** — derived-index consumers of the event log. A
  projector registry (`REGISTRY`), per-projector checkpoints in
  `projector_checkpoints`, incremental `run_projectors`, and
  `rebuild_projector` (reset derived state, replay from event 0). The service
  layer never calls projectors — the event log is the only coupling, and no
  LLM exists anywhere in this path (design Constraint 4).
- **`nodum.search`** — the query path. BM25 keyword search over the `fts`
  projector's index (catching the projector up first), returning hits with a
  fused `score` plus a per-signal `signals` breakdown so vector + graph
  expansion (design §7 RRF fusion) slot in without reshaping the API.
- **`nodum.db`** — connection management (WAL, foreign keys), `NODUM_DB`
  resolution, the migration runner.
- **`nodum.migrations`** — the append-only migration list. Never edit a shipped
  migration; append a new one.
- **`nodum.models`** — the pydantic I/O schema shared by every surface.
- **`nodum.cli`** (Typer) — each command calls one service function and prints
  the result as a single JSON object on stdout; human/error messages go to
  stderr with exit code 1. No `--json` flag.

See `docs/architecture.md` for the design-section → module mapping and the
Phase-1 decision log.

## Workflow rules

- **uv for everything.** `uv sync --all-groups` (or `make dev-install`), `uv
  run nodum …`, `uv run pytest`. Never raw `pip`/`venv`. Commit `uv.lock`;
  `.venv/` stays gitignored. Python ≥ 3.12.
- **`make format` after every code change** (ruff check --fix + format); CI
  runs `make lint` and `make test` on Python 3.12 and 3.13.
- **Tests**: `make test` (pytest, rooted at `tests/`). Every test runs against
  a fresh temp SQLite file via the `fresh_db` fixture — no external services.
- **Version** comes from the git tag (`vX.Y.Z`) via hatch-vcs at build time;
  never bump a version in code.
- **Docstrings on public APIs**: one-line summary plus args/returns where
  applicable. Comment the *why*, not the *what*. Don't annotate code you
  didn't change.
- **Keep adapters thin.** When you add or change a service operation, expose it
  through the CLI in the same change, and update `README.md`,
  `docs/architecture.md`, and this file in the same commit.
- **Line length 100**; ruff rules `E, F, I, UP, B, SIM`.

## CLI contract (for agents driving the CLI)

- Every command prints **one JSON object** on stdout and nothing else on the
  success path — parse stdout directly.
- DB path resolution: `--db` flag → `NODUM_DB` env var →
  `~/.local/share/nodum/nodum.db`.
- Writes default to actor `human` (state `active`); pass `--actor agent:<name>`
  to land writes in `proposed` instead.
- `--set key=value` is repeatable; values are parsed as JSON with a raw-string
  fallback.
- Surface: `init`, `node create/get/update/list/children`, `edge create/list`,
  `accept`/`reject`/`archive <id>`, `undo [seq]`, `history <node-id>`,
  `events`, `types`, `search <query>`, `projector run/status/rebuild`.
