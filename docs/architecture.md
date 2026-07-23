# Architecture

One SQLite file is the only source of truth and the only write path goes
through the service layer. The CLI and the MCP server are thin adapters;
derived stores (FTS, chunk embeddings, renditions) are projectors fed by the
event log or lazily generated, and later phases add more adapters (HTTP)
without touching the core.

```mermaid
flowchart LR
    cli["nodum.cli (Typer)"] --> svc["nodum.service (deterministic, LLM-free)"]
    mcp["nodum.mcp_server (FastMCP, stdio)"] --> svc
    cli --> qry["nodum.search (hybrid: BM25 + vector, RRF)"]
    mcp --> qry
    cli --> ast["nodum.assets (CAS + renditions)"]
    mcp --> ast
    svc --> db[("SQLite (WAL): types · nodes · edge_types · edges · versions · events · assets · renditions · merge_redirects")]
    svc --> mig["nodum.migrations (append-only)"]
    ast --> db
    ast --> cas[("assets/ CAS dir (originals) · renditions/ cache (derived)")]
    db -- "events (append-only)" --> prj["nodum.projectors (checkpoints · run · rebuild)"]
    prj --> fts[("node_fts (FTS5, derived)")]
    prj --> vec[("chunks + node_vec (sqlite-vec, derived)")]
    emb["nodum.embeddings (provider seam, fastembed local)"] --> prj
    qry --> fts
    qry --> vec
    qry --> emb
    qry --> prj
    style cli fill:#e6f0ff,color:#000
    style mcp fill:#e6f0ff,color:#000
    style svc fill:#fff3cd,color:#000
    style db fill:#d9f2d9,color:#000
    style mig fill:#ffe6cc,color:#000
    style prj fill:#f3e6ff,color:#000
    style fts fill:#d9f2d9,color:#000
    style vec fill:#d9f2d9,color:#000
    style emb fill:#ffe6cc,color:#000
    style qry fill:#e6f0ff,color:#000
    style ast fill:#e6f0ff,color:#000
    style cas fill:#d9f2d9,color:#000
```

## Module map

| Module | Role |
|---|---|
| `nodum.db` | Connection management (WAL, foreign keys), `NODUM_DB` resolution, the migration runner over `schema_migrations`. |
| `nodum.migrations` | The append-only migration list: `0001_core` (core DDL), `0002_seed_builtin_types` (the built-in type catalog), `0003_projector_checkpoints_and_fts` (`projector_checkpoints` + the derived `node_fts` FTS5 table), `0004_policies` (per-agent policy rulesets), `0005_proposed_versions` (`versions.state` — `applied`/`proposed`/`archived`), `0006_vectors` (the derived `chunks` + `node_vec` sqlite-vec tables), `0007_assets_and_renditions` (`assets` metadata + the derived `renditions` cache rows). Shipped entries are never edited; later phases append their own. |
| `nodum.service` | The only writer. Validation, the `proposed → active → archived` state machine, the event log, versions (incl. `proposed` updates: agent edits stage a version; accept applies it as an ordinary `node.update`, reject archives it), undo, wikilink materialization, agent policies (CRUD + auto-accept evaluation on the edge write path), the review queue (proposal listing with reviewer context over nodes/edges/updates, batch accept/reject by id or filter), and the curated graph reads behind the MCP read tier (`get_neighborhood`, `traverse`, `find_path`, `get_schema`, `diff_versions`) plus `propose_edges` batch writes. Each public function opens a short-lived connection, applies pending migrations idempotently, and commits — adapters stay stateless. |
| `nodum.projectors` | Derived-index consumers of the event log. The `Projector` base class (`reset`/`apply`/`count`/`availability`), the `REGISTRY`, per-projector checkpoints (`projector_checkpoints.last_event_seq`), incremental `run_projectors`, `rebuild_projector` (reset + replay from event 0), and `projector_status` (with availability + reason). The `fts` projector maintains `node_fts`; the `vec` projector maintains `chunks` + `node_vec` — re-chunking and re-embedding nodes from event payloads, recording `model_id` per chunk. Deterministic and LLM-free; the service layer never calls in — the event log is the only coupling. An unavailable projector (`vec` without a provider) no-ops and keeps its backlog. |
| `nodum.embeddings` | The embedding provider seam (design D10) + chunking (design D6). Provider interface: `model_id`, `dimensions`, `embed(texts)`. The default `FastembedProvider` runs `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384-dim, multilingual) in-process via ONNX — behind the optional `embeddings` extra, resolved from the local HF cache only (downloads need `NODUM_EMBED_DOWNLOAD=1`; model override via `NODUM_EMBED_MODEL`). Chunking is a fixed 512-word window with ~15% overlap (words approximate tokens — dependency-free). `set_provider`/`reset_provider` are the test seam (a deterministic hashing fake lives in `tests/conftest.py`). |
| `nodum.search` | The query path (design §7). `search()` catches the `fts` and `vec` projectors up with the log, runs BM25 (title boosted 5×) and — when a provider is available — a sqlite-vec KNN over chunk embeddings (closest chunk per node wins), fuses both lists by reciprocal rank fusion (K=60), then optionally expands the fused hits one hop along `active` edges (type weight × confidence). Hits carry the fused `score` plus a per-signal `signals` breakdown (`bm25`/`vector` RRF contributions summing to `score`, `graph` edge weight). Without a provider the vector signal is skipped — graceful degradation to BM25 + graph. |
| `nodum.assets` | Content-addressed binaries + derived image renditions (design §5.5/§5.7). `register_asset` copies a local file into the CAS directory (`assets/<hash[:2]>/<hash>` next to the DB file) and inserts the `assets` metadata row — idempotent sha256 dedup, deliberately no event-log entry (nothing to undo; ingestion-time asset events are Phase 4). `get_rendition` lazily generates `thumb`/`preview` WebP images with Pillow (downscale-only, EXIF-transposed, 300 KB quality-stepping target on `preview`), caches them under `renditions/<id[:2]>/<id>.webp` keyed by `sha256(asset_hash + ':' + profile)`, and serves cache hits thereafter; `purge_renditions` evicts rows + files (all regenerable). Non-image assets and unknown profiles are rejected cleanly; `page:<n>` rasters are Phase 4. |
| `nodum.mcp_server` | The MCP adapter: a FastMCP (official Python SDK) server over stdio, launched by `nodum mcp serve`. Registers the design §8.1 read tier (`get_node`, `get_children`, `search`, `traverse`, `list_types`, `get_schema`, `find_path`, `history`, `diff`, `get_asset`), additive tier (`create_node`, `update_node`, `link`, `propose_edges`), and the `accept`/`reject` review tools — every tool a thin delegate to one service/search/assets function with `readOnlyHint`/`destructiveHint` annotations. `get_asset` enforces the §5.7 binary policy structurally: metadata + a `preview`/`thumb` WebP image block, originals never served. One configured `--actor` per server attributes every write. Curative tools (§8.2) are never registered. |
| `nodum.models` | The pydantic I/O schema shared by every surface (`NodeOut`, `EdgeOut`, `VersionOut`, `EventOut`, `TypeOut`/`EdgeTypeOut`/`TypesOut`, `UndoResult`, `InitResult`, `ProjectorStatus`/`ProjectorRun`, `SearchHit`/`SearchResult`, `PolicyOut`, `ProposalOut`, `BatchTransitionOut`/`TransitionFailure`, `SubgraphOut`, `PathOut`, `DiffOut`, `ProposeEdgesOut`/`ItemFailure`, `AssetOut`, `RenditionOut`, `PurgeResult`). Every adapter serialises `model_dump(mode="json")`. |
| `nodum.cli` | Typer adapter. Each command calls one service function and prints exactly one JSON object on stdout; errors go to stderr with exit code 1. No `--json` flag — JSON is the only format. Adds `search`, `traverse`, `find-path`, `diff`, `schema`, `edge create-batch`, the `projector run/status/rebuild` group, the `policy set/get/list` group, the `review queue/accept/reject/accept-all/reject-all` group, the `asset register/get/list/rendition/purge` group, and `mcp serve`. |

## Design-doc mapping

The system design lives in the project's design document; this maps its
sections to the code:

| Design section | Where it lands |
|---|---|
| §2.3 constraints (single write path, Markdown truth, LLM-free core) | `nodum.service` is the only mutation entry point; `content` is canonical Markdown; no LLM calls anywhere in the package — projectors included (Constraint 4). |
| §4 architecture (service layer, event log, projectors) | `nodum.service` + the `events` table; `nodum.projectors` implements the derived-index consumers with checkpoint/rebuild mechanics. Internal agents are a later phase. |
| §5.1 everything-is-a-node, structure vs. meaning | One `nodes` table with `parent_id` + fractional `position`; typed `edges` for meaning. |
| §5.2 schema | `nodum.migrations` `0001_core` — Phase-1 subset (`types`, `nodes`, `edge_types`, `edges`, `versions`, `events`, `merge_redirects`), all with reserved `graph_id DEFAULT 'main'`; `0003_projector_checkpoints_and_fts` adds `projector_checkpoints` and `node_fts` (with the design's `extracted_text` column, empty until asset extraction lands in Phase 4); `0006_vectors` adds `chunks` + `node_vec` (`chunks.id` is an integer rowid for vec0 keying, and deliberately carries no FK to `nodes` — replaying the log must tolerate nodes whose create was undone); `0007_assets_and_renditions` adds `assets` (metadata; bytes in the CAS dir) and `renditions` (derived cache rows, plus `width`/`height`/`size_bytes` beyond the design's columns). |
| §5.3 built-in types | `nodum.migrations` `0002_seed_builtin_types` (11 node types, 17 edge types with inverses). |
| §5.4 wikilink sugar | `service._materialize_mentions` — parse on write, resolve by id or exact title, create/archive `mentions` edges, skip unresolvable targets. |
| §5.5/§5.7 assets + rendition policy | `nodum.assets` + `get_asset` in `nodum.mcp_server`. `get_asset(id_or_hash, rendition)` accepts an asset hash or an asset-reference node id (resolved via the `asset_hash` prop) and returns metadata + a `preview`/`thumb` WebP image block — MCP never serves originals, and non-image assets get metadata only. `page:<n>` rasters, `get_download_url`/`request_upload_url`, and ingestion are Phase 4. |
| §6 state machine + provenance | `service.transition` (`accept`/`reject`/`archive` over nodes, edges, and proposed versions), actor column on every row, event per transition. Versions carry their own `state` (migration `0005`): `applied` snapshots, `proposed` agent updates, `archived` rejects. |
| §7 retrieval (hybrid fusion) | `nodum.search` — BM25 via FTS5 and vector ANN via sqlite-vec (the `vec` projector, migration `0006`), fused by reciprocal rank fusion (K=60) with per-signal `signals`; one-hop graph expansion over `active` edges (type weight × confidence) applies post-fusion as the `graph` signal. The vector signal degrades gracefully when no embedding provider is available. |
| §15.1 D6 embedding lifecycle | `nodum.embeddings` chunking (512-word window, ~15% overlap — words approximate tokens) + `chunks.model_id` per embedding (migration `0006`) + `projector rebuild vec` as the full-rebuild-on-model-change path (reset + replay re-embeds everything with the new model). |
| §15.1 D10 provider abstraction | `nodum.embeddings.EmbeddingProvider` — `model_id` / `dimensions` / `embed(texts)`. The default is local in-process fastembed (no daemon, no API key, no `agedum` dependency); an API-key provider slots in behind the same interface. |
| §8.1 review/accept API | `service.list_proposals` (filterable by actor, type, kind, age; reviewer context: edge endpoints, node parent, update target) + `accept_proposals`/`reject_proposals` (by id, reject carries a `reason` into each event payload) + `accept_matching`/`reject_matching` (batch by filter — resolves to concrete ids, then one event per id). CLI: the `review` group. |
| §8.1 tool contract (read + additive tiers) | `nodum.mcp_server` over stdio: read tier `get_node`/`get_children`/`search`/`traverse`/`list_types`/`get_schema`/`find_path`/`history`/`diff`/`get_asset`, additive tier `create_node`/`update_node`/`link`/`propose_edges`, plus the `accept`/`reject` "write"-tier tools. All delegate to `nodum.service` / `nodum.search` / `nodum.assets` with tool annotations (`readOnlyHint`, `destructiveHint`). Not yet: `ingest_*`, `get_download_url`, `request_upload_url`, `get_context`, `export`, `schema_propose` (later phases). |
| §8.2 additive vs. curative | Structural: `nodum.mcp_server` never registers the curative tools (`merge_nodes`, `retype`, `supersede_edge`, `bulk_relink`, `consolidate`) — they don't exist on the MCP surface at all; a test asserts the registry stays disjoint. |
| §8.3 agent policies | `policies` table (migration `0004`) + `service.set_policy`/`get_policy`/`list_policies` (CLI `policy` group) + `_auto_accept_rule` on the edge write path. `job` rules are stored for the Phase-5 runtime; only `edge_type` + `auto_accept` rules act on direct writes today. |

## Key decisions (Phase 1)

- **Built-in type ids equal their names** (`page`, `supports`, …) — stable,
  readable, and directly referenceable in wikilinks. Custom types (a later
  runtime feature) get uuid ids like everything else.
- **Op names record the landing state**: a human create logs `node.create`, an
  agent create logs `node.propose` (same for edges).
- **Undo restores the `before` payload exactly.** Reversing a create deletes
  the row — for nodes, along with their versions and incident edges (all
  recorded in the undo event's payload). A node restore re-runs wikilink
  materialization so edges stay consistent with the restored content. Undo
  events are themselves logged and are not reversible; an already-undone event
  cannot be undone twice.
- **Version snapshots** are written for every node mutation (create, update,
  transition, undo-restore) and point at the causing event's `seq`.
- **`props` is not yet validated against `types.schema_json`** — the column and
  catalog field exist, but JSON-Schema enforcement is deferred until a schema
  engine is actually needed.

## Key decisions (Phase 2, so far)

- **Projectors are pure event-log consumers.** The `fts` projector indexes
  from event payloads (`after` rows, `restored`/`deleted` on `undo` events),
  never from the live `nodes` table, so a rebuild from event 0 is exactly an
  incremental replay. All node states are indexed; search filters by state
  (default `active`) at query time.
- **Checkpoints are one row per projector** (`name`, `last_event_seq`,
  `updated_at`). A run applies all events past the checkpoint in one
  transaction — a failure rolls the batch back and replay is deterministic.
- **`node_fts` is a plain FTS5 table** (not external-content/contentless):
  `node_id UNINDEXED` plus `title`, `content`, `extracted_text` per the design
  schema. Updates are delete + re-insert by `node_id`; storage overhead is
  irrelevant at personal-KM scale and correctness rules are trivial.
- **Free-text queries are compiled to safe MATCH expressions**: each
  whitespace-separated token becomes one double-quoted term, ANDed — FTS5
  operators in user input can never break or hijack the query.
- **Scores are higher-is-better RRF contributions.** Raw signal scores
  (`bm25()`'s more-negative-is-better rank, sqlite-vec distances) only order
  their lists; the fused `score` and every `signals` entry are RRF
  contributions (`1/(60 + rank)`), which are comparable across signals by
  construction.
- **Search catches the projector up** before querying, so results always
  reflect the latest committed writes without a manual `projector run`. The
  write path stays free of projector work.
- **Policies are one row per agent**, keyed by the exact actor string
  (`agent:researcher`) — no wildcards, no inheritance. The `rules` JSON array
  keeps the design §8.3 vocabulary (`job`/`edge_type` keys, `auto_accept` /
  `auto_apply` / `always_propose` actions, optional `min_confidence` gate);
  edge types are resolved to ids on write and unknown extra keys pass
  through, so later phases can extend rules without a migration.
- **Only `edge_type` + `auto_accept` rules act on the direct write path**, and
  only on edge creation: any matching rule whose gate passes (no
  `min_confidence` = no gate; a gate requires the edge's confidence ≥ it)
  flips the landing state to `active`. `job` rules govern the internal
  runtime (Phase 5) and are stored unevaluated; node writes have no policy
  keys in the design's vocabulary and stay `proposed` for agents.
- **Auto-accept is attribution, not bypass.** The write remains the agent's
  own event — the op records the landing state (`edge.create`, not
  `edge.propose`) and the payload records the matched rule under
  `policy_rule`, so the log alone explains why an agent write landed active.
- **Policy edits are audited events (`policy.set`) but not undoable.** Undo
  stays scoped to graph events (`node.*`/`edge.*`): its default target skips
  non-graph events, and naming one explicitly is refused. Policy history
  lives in the event payloads (full before/after rulesets).
- **Batch review never aborts on a bad id.** `accept_proposals` /
  `reject_proposals` (and the `*_matching` filter variants, which resolve the
  filter to concrete ids first) transition what they can — one event per id,
  actor and reject `reason` on every event — and report the rest in
  `failed`. A batch is a convenience over single transitions, never a silent
  bulk update.
- **A review `type` filter narrows the kind.** A name known only as a node
  type excludes edges (and vice versa) rather than leaving the other kind
  unfiltered.
- **Agent updates stage as `proposed` versions** (design §8.1, migration
  `0005`). `update_node` with a non-human actor inserts a `versions` row in
  state `proposed` (carrying the full would-be title/content/props — unset
  fields copy the current values) and emits `version.propose`; the node
  itself is untouched. Accepting applies the staged fields to the node as an
  ordinary `node.update` event (payload records `applied_version_id` and
  `proposed_event_seq`) — so the FTS projector re-indexes, undo works
  unchanged, and wikilinks re-materialize with the version's original actor.
  Rejecting flips the version to `archived` (`version.reject`, reason in the
  payload). Human updates keep applying in place; their snapshots are
  `applied`. Review ids are unified: `accept`/`reject` resolve a numeric id
  against `versions` after nodes and edges, so the same batch APIs serve all
  three proposal kinds.
- **Graph reads follow `active` edges only** (`get_neighborhood`, `traverse`,
  `find_path`, search expansion): `proposed` structure is invisible to reads
  until accepted, matching the design §7 default.
- **The MCP server holds one configured actor** (`nodum mcp serve --actor`,
  default `agent:mcp`). Every tool call passes it to the service layer, so
  attribution, the proposed-by-default rule, and policy auto-accept behave
  exactly as CLI `--actor` writes. There is no per-connection handshake
  identity yet — multi-tenant auth belongs with the HTTP API phase.
- **`accept`/`reject` are registered on the MCP server** (the §8.1 "write"
  tier): they cost nothing on top of the review API and let a human-driven
  MCP client work the queue. They carry the server's actor like everything
  else, so an agent accepting its own proposals is possible but fully
  audited — deployments that don't want that simply don't hand agents the
  review tools' ids (or a future per-tool allowlist can drop them).
- **Search `expand` is the interim graph signal**: one hop along `active`
  edges from the BM25 hits, scored `type weight × confidence` (`supports`
  1.0, `relates_to` 0.5, others 0.5; design §7), deduped against direct hits
  and capped at `k`. It ships as the `graph` entry in `signals` so RRF
  fusion replaces it without reshaping the API.
- **Embeddings are local and in-process** (fastembed, ONNX Runtime on CPU):
  no Ollama, no daemon, no API key. The default model is
  `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` — 384
  dimensions, multilingual, ~0.22 GB, Apache-2.0, and in fastembed's
  built-in registry (no custom registration). It lives behind the optional
  `embeddings` extra so the core install stays lean, and behind the
  `EmbeddingProvider` interface (`model_id` / `dimensions` / `embed`) so an
  API-key provider can replace it per design D10.
- **Downloads are never implicit.** The default provider resolves only from
  the local Hugging Face cache (`HF_HUB_OFFLINE` during construction);
  fetching the model needs `NODUM_EMBED_DOWNLOAD=1` once. Anything less
  gives a clean *unavailable* state with the reason in `projector status`,
  and search silently falls back to BM25 + graph — CI and fresh machines
  never touch the network.
- **Chunking approximates tokens with words** (design D6's 512-token window,
  ~15% overlap): whitespace-splitting needs no tokenizer download and is
  close enough for ranking at personal-KM scale. Chunks are cut from
  `title + content`; `chunks.model_id` records the producing model per
  chunk, and `projector rebuild vec` (reset + replay) is the
  full-rebuild-on-model-change path.
- **`chunks` has an integer rowid and no FK to `nodes`.** vec0 rows key on
  integer rowids, so each vector shares its chunk's rowid (the design's TEXT
  chunk id doesn't apply); and the projector replays the *event log*, which
  still contains events for nodes whose create was later undone — a foreign
  key to the live `nodes` table would make that replay fail.
- **Fusion is plain RRF (K=60) over ranked lists.** Each signal contributes
  `1/(60 + rank)`; `signals` carries the per-signal contributions and they
  sum to `score` exactly, so the breakdown explains the ranking. The vector
  ANN list is k-deep with no similarity threshold (RRF wants ranks, not
  cutoffs); chunks aggregate to nodes by closest chunk. Graph expansion runs
  on the fused list, after fusion.
- **Projector availability is first-class.** `Projector.availability()`
  gates runs (an unavailable projector no-ops and keeps its checkpoint — the
  backlog waits) and surfaces in `projector status` as
  `available`/`detail`. Rebuilding an unavailable projector is refused —
  it would empty the store without being able to refill it.
- **Asset bytes live in a CAS directory, not the DB** (`assets/<hash[:2]>/
  <hash>` next to the database file), exactly as design §5.2 writes it —
  disaster recovery is `DB + assets/ = everything`. (Moving bytes into the
  DB is a live, undecided proposal in the design project; the `renditions`
  `path` column and the blob-table shape both keep that door open.)
  Registration is idempotent sha256 dedup with **no event-log entry**:
  content-addressed rows are immutable, so there is no state to transition
  or undo, and re-registering the same bytes returns the existing row.
  Ingestion-time asset events (with the never-inline-blob-bytes discipline)
  land with the Phase-4 pipeline.
- **Renditions are lazily generated, not projected.** Unlike FTS/vectors
  there is no event to consume — a rendition derives from CAS bytes, not
  from graph events — so `get_rendition` builds on first request and caches
  the WebP under `renditions/<id[:2]>/<id>.webp`, keyed by
  `sha256(asset_hash + ':' + profile)` (§5.7). Rows and files are both
  regenerable; `asset purge` is the eviction hatch. Downscaling uses
  `Image.thumbnail` (never upscales), EXIF orientation is applied, and
  `preview`'s ≤300 KB target is met by stepping WebP quality down from q80
  (the smallest encode wins if no step fits).
- **`get_asset` over MCP enforces the §5.7 binary policy structurally.** The
  rendition argument is validated against the `thumb`/`preview` profiles
  before anything is read — `full`/`page:<n>`/anything else is a tool
  error, so no code path can return original bytes. Non-image assets return
  the metadata block alone (`rendition: null`), matching §5.7's
  per-media-type rule (extracted text arrives with Phase-4 ingestion).
