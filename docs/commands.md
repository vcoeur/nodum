---
title: Commands · nodum
description: The full nodum CLI surface — every command, its purpose, and the conventions every command shares.
---

# Commands

Every command prints **one JSON object** on stdout and nothing else on the
success path, so you can parse stdout directly. There is no `--json` flag —
JSON is the only output format. Human-facing and error messages go to stderr.

```sh
nodum node get <id> | jq .title
```

List-returning commands wrap their rows in a named key plus a `count`:

```json
{ "nodes": [ … ], "count": 2 }
```

Errors are always one line on stderr with exit 1 — never a traceback.

## Conventions

**Database path** — `--db` flag → `NODUM_DB` environment variable →
`~/.local/share/nodum/nodum.db`.

**Actors** — writes default to `--actor human`, which lands them `active`. Pass
`--actor agent:<name>` to land writes as `proposed` instead, unless that
agent's stored policy auto-accepts them.

**Human-only operations** — `accept`, `reject`, `archive`, `undo`, every
`review` subcommand, and `policy set` require `--actor human`. An `agent:*`
actor exits 1. This is not delegable: `policy set` most of all, since a policy
grants auto-accept and an agent setting one would self-grant the direct live
write the human tier withholds.

**Rejections need a reason** — both `reject <id> --reason` and `review reject
… --reason` require it and record it in the reject event's payload.

**`--set key=value`** is repeatable. Values are parsed as JSON with a
raw-string fallback, so `--set year=1815` yields an integer and `--set
venue=Nature` a string.

## Self-description

Two commands work without a database, which makes them safe to run against a
fresh install:

- `nodum --version` — prints `nodum <version>`.
- `nodum schema-dump` — prints the whole command tree as JSON, including
  parameters.

Note `schema-dump` (this CLI's own surface) is a different thing from `schema
<type>` (one node or edge type's catalog entry, read from the database).

## Full surface

Run `nodum schema-dump` for the machine-readable version of this list,
including every parameter.

### Graph

- `init` — Create the database (if needed) and apply pending migrations.
- `node create` — Create a node (active for actor `human`, proposed otherwise).
- `node get` — Fetch one node by id (plus its neighborhood when `--depth > 0`).
- `node list` — List nodes in creation order, optionally filtered.
- `node update` — Update a node (applies for `human`, stages a proposed version otherwise).
- `node children` — List a node's children in position order.
- `edge create` — Create a typed, directed edge between two nodes.
- `edge create-batch` — Propose a batch of edges; bad suggestions are reported, not fatal.
- `edge list` — List edges, optionally filtered by incident node, type, or state.
- `types` — Show the full type catalog (node types and edge types).
- `schema <type>` — Show one type's catalog entry, including its JSON schema.

### Traversal and search

- `search <query>` — Hybrid-search node title + content (BM25 + vector, RRF-fused).
- `traverse` — Walk the subgraph reachable from a node over active edges.
- `subgraph <root-id>` — Bounded, filtered neighborhood of a node — node and edge caps stop the walk.
- `find-path` — Find the shortest path between two nodes over active edges.
- `suggest-links <prefix>` — Suggest wikilink targets by title prefix (case-insensitive).

### History and state

- `events` — Show the most recent event-log entries (newest first).
- `history <node-id>` — Show a node's version history (chronological).
- `diff <a> <b>` — Unified diff between two versions of one node.
- `undo [seq]` — Reverse an event, restoring the prior state from its payload.
- `accept <id>` — Accept a proposed node, edge, or update (proposed → active). Human only.
- `reject <id> --reason` — Reject a proposed node, edge, or update (proposed → archived). Human only.
- `archive <id>` — Archive an active node or edge (active → archived).

### Review queue and policies

- `review queue` — List pending proposals with reviewer context.
- `review accept` / `review reject` — Act on proposals by id; bad ids are reported, not fatal.
- `review accept-all` / `review reject-all` — Act on every proposal matching the filters.
- `policy set` — Create or replace an agent's policy ruleset (audited as `policy.set`). Human only.
- `policy get` / `policy list` — Read stored policies.

!!! warning
    A policy rule's `min_confidence` grades the *agent's own* reported
    confidence, so it is inert unless the rule also sets
    `"trust_self_reported_confidence": true`.

### Derived indexes

- `projector run` — Apply pending event-log entries to the derived indexes.
- `projector status` — Show every projector's checkpoint, backlog, and derived-store size.
- `projector rebuild <name>` — Drop one projector's derived state and replay the full event log.

### Assets

- `asset register` — Register a file as a content-addressed asset (idempotent dedup by sha256).
- `asset get` — Show one asset's metadata (never its bytes).
- `asset list` — List every registered asset.
- `asset rendition` — Fetch an image rendition, generating and caching it on first request.
- `asset purge` — Evict stored renditions (they rebuild on next request).

### Servers

- `serve` — Serve the human web UI and its JSON API.
- `mcp serve --actor agent:<name>` — Launch the MCP server on stdio (read + additive tiers).

## Serving

`serve` refuses a non-loopback bind without `--token` (exit 1), prints the
database path and the `#token=…` UI URL on stderr, and translates a port
already in use into the contract's exit 1.

```sh
nodum serve [--host 127.0.0.1] [--port 8600] [--token TOKEN] [--allow-host NAME] [--db PATH]
```
