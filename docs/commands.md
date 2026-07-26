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

**Attribution** — the CLI is human-only and every command that touches the
graph requires `--as human:<id>` (or the bare id) — reads included, since
reads are grant-scoped like writes. A human's write lands `active`. Agents
write over
MCP, never the CLI, and land per their grants (`suggest` → `proposed`, `edit` →
`active`).

**Human-only operations** — `accept`, `reject`, `archive`, `undo`, every
`review` subcommand, and all account/grant administration (`human`, `agent`,
`grant`, `revoke`, `space-*` commands) require a human principal. Review
(`accept`/`reject`/`archive`) can also be exercised by an agent holding `edit`
on the item's space — over the service API, not the CLI; `undo` stays
human-only.

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
- `node create` — Create a node (`active` for a human; over MCP, per the agent's grant).
- `node get` — Fetch one node by id (plus its neighborhood when `--depth > 0`).
- `node list` — List nodes in creation order, optionally filtered.
- `node update` — Update a node (applies for a human or an `edit` grant; stages a proposed version on `suggest`).
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

### Review queue

- `review queue` — List pending proposals with reviewer context.
- `review accept` / `review reject` — Act on proposals by id; bad ids are reported, not fatal.
- `review accept-all` / `review reject-all` — Act on every proposal matching the filters.

### Accounts, grants, and spaces

- `human create/list/passwd/disable/enable` — Manage human accounts (passwordless
  until `passwd`; argon2id).
- `agent create/list/token-rotate/disable/enable` — Manage agent accounts
  (`create`/`token-rotate` print the show-once token to stderr).
- `grant <agent> <space> <level>` / `revoke <agent> <space>` / `grants [--agent]` —
  Event-logged grant administration, levels `read`/`suggest`/`edit`.
- `space-create` / `space-list` / `space-archive` — Spaces as nodes. These are
  all human-only.

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
- `mcp serve` — Launch the MCP server on stdio (read + additive tiers); the
  agent token comes from `NODUM_AGENT_TOKEN`.

## Serving

Every `/api` route but `POST /api/login` needs a valid session — log in with
a human name and password (`nodum human passwd` sets one). A non-loopback
bind is allowed: login, not the bind, is the boundary, and the session cookie
gains `Secure` there. `serve` prints the database path on stderr and
translates a port already in use into the contract's exit 1.

Account and grant administration is on the API as well: `GET /api/me` returns
the session's human, and `/api/humans`, `/api/agents` and `/api/grants`
mirror the CLI's `human`/`agent`/`grant`/`revoke`/`grants` commands — the
show-once agent token comes back in the create/token-rotate response body.

```sh
nodum serve [--host 127.0.0.1] [--port 8600] [--allow-host NAME] [--db PATH]
```
