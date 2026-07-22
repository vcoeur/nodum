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
projectors with checkpoint/rebuild mechanics and the first derived index —
an FTS5 full-text index feeding BM25 keyword search. Still to come: vector
search + hybrid fusion, the MCP server, the web UI, assets, and the
consolidation cycle.

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

uv run nodum search "graph theory"            # BM25 keyword search (FTS5)
uv run nodum projector status                 # derived-index checkpoints
uv run nodum projector rebuild fts            # drop + replay from event 0

uv run nodum node list --type note
uv run nodum history <node-id>                # version snapshots
uv run nodum undo                             # reverse the latest event
uv run nodum types                            # the seeded type catalog
```

Run `uv run nodum --help` (or any subcommand with `--help`) for the full
surface.

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
  and are accepted/rejected explicitly.
- **Event log + versions.** Every mutation appends an event (actor, op, full
  before/after JSON payload) and — for nodes — a version snapshot. `undo`
  reverses an event by restoring its `before` state.
- **Derived indexes are projectors.** The event log feeds checkpointed,
  independently rebuildable projectors (`nodum projector run/status/rebuild`).
  The first one, `fts`, maintains an FTS5 index over node title + content (+
  extracted asset text once assets land); `nodum search` serves BM25-ranked
  keyword results from it. Vector and graph-expansion signals slot into the
  same hit shape later.

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
