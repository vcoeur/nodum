# nodum

A **DB-native knowledge graph**: knowledge is a typed graph of nodes in one
SQLite file — not files with an index on top. Every mutation flows through a
deterministic, LLM-free service layer that validates, enforces a
`proposed → active → archived` state machine, and appends to an event log with
full before/after payloads — so every change is versioned, auditable, and
reversible.

**Phase 1 (core)** landed: schema + migrations, service layer, event log +
versions + undo, Markdown-as-truth content, wikilink materialization, and a
JSON-emitting CLI. **Phase 2 (agent-native)** is underway: event-log
projectors with checkpoint/rebuild mechanics and two derived indexes — an
FTS5 full-text index and a sqlite-vec chunk-embedding index (local
in-process fastembed model, no daemon, no API key) — feeding **hybrid
search** (BM25 + vector fused by reciprocal rank fusion, then graph-expansion
re-ranking), DB-stored **agent policies** with auto-accept on the write path,
the **review/accept API** for the proposal queue, **proposed updates** (agent
edits stage as `proposed` versions), an **MCP server** (stdio) exposing the
read + additive tool tiers, and **content-addressed assets** — binaries and
their lazily generated `thumb`/`preview` renditions stored in the same file
as the graph (agents get renditions, never originals). Still to come: the web
UI, the ingestion pipeline, and the consolidation cycle.

## Quick start

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
make dev-install        # uv sync --all-groups

# Create the database (path: $NODUM_DB, default ~/.local/share/nodum/nodum.db)
uv run nodum init

# Build a small graph — every command prints one JSON object
uv run nodum node create --type concept --title "Graph Theory"
uv run nodum node create --type note --title "My note" \
    --content "Notes on [[Graph Theory]] and its applications."
uv run nodum edge list --type mentions        # the wikilink became an edge

uv run nodum search "graph theory"            # hybrid search (BM25 + vector, RRF-fused)
uv run nodum projector status                 # derived-index checkpoints + availability
uv run nodum projector rebuild fts            # drop + replay from event 0
uv run nodum projector rebuild vec            # the model-change re-embed path

uv run nodum node list --type note
uv run nodum history <node-id>                # version snapshots
uv run nodum undo                             # reverse the latest event
uv run nodum types                            # the seeded type catalog

# Agent writes land in `proposed` and wait in the review queue…
uv run nodum node create --type note --title "Bot draft" --actor agent:researcher
uv run nodum node update <id> --content "bot rewrite" --actor agent:researcher
uv run nodum review queue --created-by agent:researcher   # nodes, edges, updates
uv run nodum review accept-all --created-by agent:researcher
uv run nodum review reject <id> --reason "not convinced"

# …unless a stored policy auto-accepts them (still the agent's own event)
uv run nodum policy set agent:researcher --rule \
    '{"edge_type":"mentions","min_confidence":0.9,"action":"auto_accept"}'
uv run nodum policy list

# Curated graph reads (the MCP read tier's service functions)
uv run nodum traverse <id> --edge-type supports --depth 2
uv run nodum find-path <a> <b>
uv run nodum diff <version-a> <version-b>

# Assets: register a file into the database, derive stored WebP renditions
uv run nodum asset register ./photo.jpg
uv run nodum asset rendition <hash> --profile preview --out preview.webp
uv run nodum asset purge                      # evict the stored renditions

# MCP server (stdio) for external agents — read + additive tiers only,
# every write attributed to --actor and proposed unless policy auto-accepts
uv run nodum mcp serve --actor agent:researcher
```

Run `uv run nodum --help` (or any subcommand with `--help`) for the full
surface.

### Semantic search (optional)

The vector signal runs on a local, in-process embedding model (fastembed,
ONNX on CPU — no daemon, no API key), installed as an extra:

```sh
uv sync --extra embeddings            # adds fastembed
# First run downloads the model (~0.2 GB) — downloads are never implicit:
NODUM_EMBED_DOWNLOAD=1 uv run nodum projector run vec
uv run nodum search "osmosis in plants"   # now fuses BM25 + vector signals
```

Without the extra (or an uncached model) everything still works: the `vec`
projector reports itself unavailable in `projector status` and search
degrades to BM25 + graph expansion. `NODUM_EMBED_MODEL` switches the model
(default: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`,
384-dim, multilingual); a model change is a `projector rebuild vec`.

## How it works

- **Everything is a node.** Pages, blocks, notes, claims, concepts, people,
  sources, tags — one `nodes` table distinguished by `type_id`. Structure
  (document trees) is `parent_id` + fractional `position`; meaning is typed
  `edges` between any two nodes.
- **Markdown is truth.** Node `content` is canonical Markdown. `[[wikilinks]]`
  are parsed on write and materialized as `mentions` edges; deleting the text
  archives the edge. Unresolvable targets are skipped silently.
- **State machine.** Nodes and edges are `proposed`, `active`, or `archived`.
  Human (CLI) writes land `active`; any other actor's writes land `proposed`
  and are accepted/rejected explicitly — individually, in batches, or by
  filter through the review queue (`nodum review …`). Agent *updates* stage
  as `proposed` versions: accepting applies the staged fields to the node
  (an ordinary, undoable `node.update`), rejecting archives the version.
- **Agent policies.** Per-agent rulesets stored in the DB (`nodum policy …`)
  can auto-accept an agent's writes — e.g. "accept `mentions` edges from
  `agent:researcher` with confidence ≥ 0.9". An auto-accepted write is still
  the agent's own event, with the matched rule recorded in the payload.
- **MCP server.** `nodum mcp serve` runs a stdio MCP server (the official
  Python SDK's FastMCP) exposing the design §8.1 read tier (`get_node`,
  `get_children`, `search`, `traverse`, `list_types`, `get_schema`,
  `find_path`, `history`, `diff`, `get_asset`), additive tier (`create_node`,
  `update_node`, `link`, `propose_edges`), and `accept`/`reject`. One
  configured `--actor` per server attributes every write. Curative tools
  (`merge_nodes`, `retype`, …) are **never registered** — structural
  enforcement of §8.2.
- **Assets and renditions.** `asset register` streams a file into the
  database keyed by its sha256 (dedup is free) and records its metadata row.
  `asset rendition` derives small WebP images from it — `thumb` (≤256px) and
  `preview` (≤1024px, ≤300 KB) — lazily on first request, stored alongside,
  evictable with `asset purge` (everything regenerates). Binaries live in the
  database rather than a directory beside it, so **the database file is the
  whole system**: back it up and you have backed up the graph, its history,
  and its binaries. Over MCP, `get_asset` returns metadata plus a rendition
  image block: **LLMs never receive original binaries** (design §5.7).
- **Event log + versions.** Every mutation appends an event (actor, op, full
  before/after JSON payload) and — for nodes — a version snapshot. `undo`
  reverses an event by restoring its `before` state.
- **Derived indexes are projectors.** The event log feeds checkpointed,
  independently rebuildable projectors (`nodum projector run/status/rebuild`).
  `fts` maintains an FTS5 index over node title + content (+ extracted asset
  text once assets land); `vec` maintains sqlite-vec embeddings of
  fixed-window text chunks (512 words, ~15% overlap, `model_id` recorded per
  chunk). `nodum search` fuses both — BM25 and vector lists merged by
  reciprocal rank fusion, then one-hop graph expansion along `active` edges —
  and every hit carries a per-signal `signals` breakdown. With no embedding
  provider, the vector signal drops out and search stays BM25 + graph.

See [docs/architecture.md](docs/architecture.md) for the module map and
[AGENTS.md](AGENTS.md) for contributor/agent workflow rules.

## Development

```sh
make test      # pytest
make lint      # ruff check + format check
make format    # ruff auto-fix + format (run after every code change)
```

The package version is derived from the git tag (`vX.Y.Z`) at build time by
hatch-vcs and is never committed.
