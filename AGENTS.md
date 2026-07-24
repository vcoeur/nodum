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
title + content), the **`vec` projector** (sqlite-vec chunk embeddings,
local in-process fastembed model — migration 0006), **hybrid search**
(`nodum.search`, CLI `search`): BM25 + vector lists fused by reciprocal rank
fusion, then one-hop graph-expansion re-ranking, with a per-signal `signals`
breakdown, **agent policies** (DB-stored per-agent rulesets, design §8.3,
with auto-accept evaluation on the edge write path), the **review/accept
API** (proposal listing with reviewer context, batch accept/reject by id or
filter), **proposed updates** (agent `update_node` stages a `proposed`
version; accept applies it, reject archives it — migration 0005), the
**MCP server** (`nodum.mcp_server`, stdio, read + additive tiers +
`accept`/`reject`; curative tools are never registered), and **assets +
image renditions** (`nodum.assets` — migrations 0007/0008): thin
content-addressed asset registration (a metadata row + an in-database blob +
sha256) and lazily generated, stored, evictable `thumb`/`preview` WebP
renditions (design §5.7), exposed over MCP as `get_asset` (metadata +
rendition image block — never the original).
**Deliberately not built yet** (later phases — do not add): the web UI, the
Phase-4 ingestion pipeline (text extraction, chunking, source/claim
proposals, `ingest_file`/`ingest_url`), `page:<n>` PDF rasters,
`get_download_url`/`request_upload_url`, the internal agent runtime and
consolidation cycle, Markdown Mirror / JSON export. The schema reserves room
for them (`graph_id`, `merge_redirects`, `cycle_id`,
`assets.extracted_text`); each lands as its own append-only migration.

## Architecture

- **`nodum.service`** is the spine and the only writer — validation, the
  `proposed → active → archived` state machine, the event log, versions
  (including `proposed` version updates: agent edits stage, accept applies,
  reject archives), undo, wikilink materialization, agent policies (CRUD +
  auto-accept evaluation on the write path), the review queue (proposal
  listing, batch accept/reject), and the curated graph reads
  (`get_neighborhood`, `traverse`, `find_path`, `get_schema`,
  `diff_versions`, `propose_edges`). Each public function opens its own
  short-lived connection (applying pending migrations idempotently) and
  commits. New behaviour and validation go here first; adapters must not add
  behaviour the service lacks.
- **`nodum.mcp_server`** — the MCP adapter (stdio, official Python SDK
  FastMCP). Registers the design §8.1 read + additive tiers plus
  `accept`/`reject`, each tool a thin delegate to a service/search function;
  one configured `--actor` per server attributes every write. Curative tools
  (`merge_nodes`, `retype`, `supersede_edge`, `bulk_relink`, `consolidate`)
  are **never registered** (§8.2 structural enforcement). Launched by
  `nodum mcp serve`.
- **`nodum.projectors`** — derived-index consumers of the event log. A
  projector registry (`REGISTRY`), per-projector checkpoints in
  `projector_checkpoints`, incremental `run_projectors`, and
  `rebuild_projector` (reset derived state, replay from event 0). The `fts`
  projector maintains `node_fts`; the `vec` projector maintains `chunks` +
  `node_vec` (rebuild = the model-change re-embed path, design D6). The
  service layer never calls projectors — the event log is the only coupling.
  A projector whose requirements are unmet (`vec` without a usable embedding
  provider) reports itself unavailable in `projector status` and its runs
  are no-ops — the backlog waits, nothing crashes.
- **`nodum.embeddings`** — the embedding provider seam (design D10) and
  chunking (design D6). The provider interface is `model_id` + `dimensions`
  + `embed(texts) -> vectors`; the default is a local in-process fastembed
  model (`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`,
  384-dim, multilingual, ONNX/CPU — no daemon, no API key) behind the
  optional `embeddings` extra. A model is never downloaded implicitly: the
  provider resolves only from the local HF cache unless
  `NODUM_EMBED_DOWNLOAD=1` is set (first run fetches it).
  `NODUM_EMBED_MODEL` overrides the model name (a different dimensionality
  needs a new migration — the vec0 table is fixed at 384). Tests inject a
  deterministic hashing fake via `embeddings.set_provider`.
- **`nodum.assets`** — content-addressed binaries and their derived
  renditions (design §5.5/§5.7). **Bytes live in the database, not on the
  filesystem**: `assets` holds metadata, `asset_blobs` holds the bytes under
  the same sha256 key, so the whole system is one file and disaster recovery
  is `DB = everything`. Registration is idempotent sha256 dedup with no
  event-log entry (there is nothing to undo), and streams through
  `Connection.blobopen` so a large file is never held in memory — never
  inline asset bytes into an event payload. Renditions (`thumb` ≤256px WebP
  q75, `preview` ≤1024px WebP q80 with a 300 KB quality-stepping target) are
  keyed by `sha256(asset_hash + ':' + profile)`, generated lazily with Pillow
  on first request, stored as blobs, and evicted by `purge_renditions` (CLI
  `asset purge`) — fully regenerable. Non-image assets are rejected cleanly;
  `page:<n>` rasters are Phase 4. Pillow reads originals through
  `_BlobReader`, which restores the file-style tolerant seeks that
  `sqlite3.Blob` refuses and Pillow's format probing depends on.
- **`nodum.search`** — the query path (design §7). BM25 over the `fts`
  projector's index and vector ANN over the `vec` projector's chunks
  (closest chunk per node wins), fused by reciprocal rank fusion (K=60) with
  `type`/`state`/`created_by`/date filters; optional one-hop graph expansion
  over `active` edges (`--expand`) applies after fusion. Hits carry the
  fused `score` plus a per-signal `signals` breakdown (`bm25` / `vector` /
  `graph`). With no embedding provider the vector signal is skipped —
  search silently degrades to BM25 + graph.
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
  `.venv/` stays gitignored. Python ≥ 3.12. The local embedding model lives
  behind the optional `embeddings` extra (`uv sync --extra embeddings`) —
  tests never need it (they inject a fake provider; one real-model smoke
  test is opt-in via `NODUM_RUN_SLOW=1`).
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
  to land writes in `proposed` instead — unless the agent's stored policy
  auto-accepts the write (`policy set`). An agent `node update` stages a
  `proposed` *version*; `accept <version-id>` applies it, `reject` archives it.
- `--set key=value` is repeatable; values are parsed as JSON with a raw-string
  fallback.
- Surface: `init`, `node create/get/update/list/children`, `edge
  create/list/create-batch`, `accept`/`reject`/`archive <id>`, `undo [seq]`,
  `history <node-id>`, `events`, `types`, `schema <type>`, `search <query>`,
  `traverse`, `find-path`, `diff`, `projector run/status/rebuild`,
  `policy set/get/list`, `review queue/accept/reject/accept-all/reject-all`,
  `asset register/get/list/rendition/purge`, `mcp serve`.
- Asset images reach agents only as renditions: `asset rendition` prints
  metadata + the cache path (never inlines bytes into JSON); the MCP
  `get_asset` tool returns metadata + a WebP image block of the requested
  rendition — originals are never served over MCP (design §5.7).
