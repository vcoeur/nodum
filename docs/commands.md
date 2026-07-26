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
- `node create` — Create a node (`active` for a human; over MCP, per the
  agent's grant). `--space` is the **write target** — the space the node lands
  in, `main` when absent.
- `node get` — Fetch one node by id (plus its neighborhood when `--depth > 0`).
- `node list` — List nodes in creation order, optionally filtered. `--space`
  narrows to one space (default: every space in scope) and `--include-meta`
  adds the meta space (off by default).
- `node update` — Update a node (applies for a human or an `edit` grant; stages a proposed version on `suggest`).
- `node children` — List a node's children in position order.
- `edge create` — Create a typed, directed edge between two nodes.
- `edge create-batch` — Propose a batch of edges; bad suggestions are reported, not fatal.
- `edge list` — List edges, optionally filtered by incident node, type, or state.
- `types` — Show the full type catalog (node types and edge types).
- `schema <type>` — Show one type's catalog entry, including its JSON schema.

### Traversal and search

- `search <query>` — Hybrid-search node title + content (BM25 + vector,
  RRF-fused). Takes the same two read-side space controls as `node list`:
  `--space` and `--include-meta`.
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
  until `passwd`; argon2id, six characters minimum, and setting one ends that
  human's live sessions). The last enabled human cannot be disabled: with none
  enabled, no surface can mint a principal at all, the CLI's trusted-local path
  included.
- `agent create/list/token-rotate/disable/enable` — Manage agent accounts
  (`create`/`token-rotate` print the show-once token to stderr).
- `grant <agent> <space> <level>` / `revoke <agent> <space>` / `grants [--agent]` —
  Event-logged grant administration, levels `read`/`suggest`/`edit`.
- `space-create` / `space-list` / `space-rename` / `space-archive` — Spaces as
  nodes: a space is a node of builtin type `space` living in the meta space, so
  creating one is a node create, renaming one is a title update, and archiving
  one is a state transition — each event-logged, versioned, and undoable like
  any other write. `space-rename` and `space-archive` take a space id **or**
  name and refuse anything that is not a space. `space-list` reports each
  space's **live node count** (`active` + `proposed`; archived rows are retired,
  not territory) and the **agents granted on it**. These are all human-only.

  Two rules are enforced in the service, so every surface has them:

  - **`main` and `meta` cannot be archived** — by `space-archive` or by the
    generic `archive <id>`. Archiving `main` would hide it from every listing
    while every write that names no space kept landing there (that default
    resolves by id, whatever state the row is in), and archiving `meta` would
    retire the space every other space lives in. Nothing un-archives, so a
    *rename* of either is fine: it moves the title and leaves the id alone.
  - **Two live spaces cannot share a name.** A space reference resolves as
    `id = ? OR title = ?`, so a duplicate would make `--space research` mean
    whichever row SQLite reached first. Names are compared exactly, as the
    lookup does — `Research` and `research` are two spaces. Archiving frees a
    name: an archived space stops resolving, so holding its title would reserve
    it for good.

  A space is used in two independent ways, and they are two controls rather
  than one mode: `--space` on a *read* (`node list`, `search`) narrows the view
  and defaults to every space in scope, while `--space` on a *write*
  (`node create`, `ingest`) targets where the node lands and defaults to `main`
  — reading one space while filing into another is the ordinary case. The read
  filter is a convenience, not a boundary: an agent stays confined to its
  grants underneath it, and a space it holds no grant on does not resolve at
  all, answering exactly as a nonexistent one does.

### Derived indexes

- `projector run` — Apply pending event-log entries to the derived indexes.
- `projector status` — Show every projector's checkpoint, backlog, and derived-store size.
- `projector rebuild <name>` — Drop one projector's derived state and replay the full event log.

### Assets

- `asset register` — Register a file as a content-addressed asset (idempotent dedup by sha256).
- `asset get` — Show one asset's metadata (never its bytes). Takes `--as`.
- `asset list` — List every registered asset. Takes `--as`.
- `asset rendition` — Fetch a rendition, generating and caching it on first
  request. `--profile` takes `thumb` or `preview` for an image, or `page:<n>`
  for a 1-based page of a PDF. Takes `--as`.
- `asset download-url` — Mint a short-lived, single-use URL for an asset's
  original bytes. Takes `--as`.
- `asset upload-url` — Mint a short-lived, single-use URL to PUT one file to
  (`--name`, `--mime`, `--size` required). Takes `--as`.
- `asset purge` — Evict stored renditions (they rebuild on next request).

The reads carry `--as` because they read through the graph — an
`asset_ref` node id is a valid handle, and it resolves only in a space the
caller can read. `register` and `purge` touch the blob store alone.

A `page:<n>` raster is an ordinary rendition: same lazy generation, same cache,
same eviction by `asset purge`. It needs the `pdf` extra, which it names rather
than failing at import time.

The two capability-URL commands are the escape hatch for a host that shares no
filesystem with the graph. Both print the token **once** — only its sha256 is
stored — and log both the mint and the later redemption. `--ttl` is bounded
(1 second to 1 hour, 5 minutes by default). An `upload-url` whose `--sha256`
this graph already holds answers with the existing `asset` and **no** `grant`:
the bytes are here, so no bytes move. The URLs resolve against `nodum serve`,
which has to be running; set `NODUM_PUBLIC_URL` when that server is not on the
default address.

### Ingestion

- `ingest file <path>…` — Ingest local files: register the bytes, extract text,
  describe, propose. Takes `--as`.
- `ingest url <url>` — Fetch an `http`/`https` URL into the blob store and
  ingest it exactly like a local file. Takes `--as`.
- `ingest handlers` — List every extraction handler, its MIME families, and
  whether it can run. No `--as`, and no database.

Each document becomes an asset, an `asset_ref` node describing those bytes in
one space, a `source` node whose content is the extracted text, a
`derived_from` edge between them, and one `block` child per page of text. Every
write goes through the ordinary service API, so the subgraph lands in the state
the writer's grant earns.

**`ingest file` takes one or more paths, and a directory argument ingests the
files directly inside it** (`--recursive` walks deeper). Dot-names and anything
that is not a regular file are skipped, and the rest are ingested in sorted
order, so the same folder ingests the same way twice. One path naming a *file*
prints that ingestion as a single JSON object; anything else — several paths,
or a directory — is a batch and prints `{"ingestions": [...], "count": n}`.
`--name` and `--title` describe one document and are refused for a batch;
`--space` applies to all of it.

**A batch never loses its successes.** Each file is ingested on its own; one
that fails prints its reason and then `skipped <path>` on stderr, and the batch
carries on. Every file that landed is in the envelope on stdout. **The exit
code is 1 if any file failed**, so a non-zero exit from `ingest file` means
"read stderr for what is missing", not "nothing happened". Re-running is safe:
ingestion is idempotent per `(hash, space)`, so what already landed comes back
with `created: false` instead of being duplicated.

`ingest url` fetches `http`/`https` only, once, with a timeout and a size
ceiling, and refuses a redirect that leaves those two schemes. It does *not*
block loopback or private ranges — nodum is itself a loopback service — so
granting ingestion grants the server's network position.

`ingest handlers` is the answer to "my PDF produced no text": it reports every
handler with its MIME families, whether it is `available`, and — when it is not
— a `detail` naming the extra to install. Plain text, Markdown, JSON, and HTML
are handled by the standard library and always work; PDF text (`pdf`), image
OCR (`ocr`, which also needs the `tesseract` binary on PATH) and audio
transcription (`audio`) are optional extras. A missing handler is never fatal:
the asset is still registered and still described, and the result says plainly
that no text came out.

### Servers

- `serve` — Serve the human web UI and its JSON API.
- `mcp serve` — Launch the MCP server on stdio (read + additive tiers); the
  agent token comes from `NODUM_AGENT_TOKEN`.

## Serving

Every `/api` route but `POST /api/login` needs a valid session — log in with
a human name and password (`nodum human passwd` sets one). A non-loopback
bind is allowed: login, not the bind, is the boundary, and the session cookie
gains `Secure` there — `serve` warns on stderr that it speaks plain HTTP, so
put TLS in front of it or the password crosses the network in the clear. `serve` prints the database path on stderr and
translates a port already in use into the contract's exit 1.

Account and grant administration is on the API as well: `GET /api/me` returns
the session's human, and `/api/humans`, `/api/agents` and `/api/grants`
mirror the CLI's `human`/`agent`/`grant`/`revoke`/`grants` commands — the
show-once agent token comes back in the create/token-rotate response body.
Spaces mirror their commands the same way: `GET /api/nodes` and
`GET /api/search` take `?space=` and `?include_meta=`, `POST /api/nodes` takes
`space` in the body, and the lifecycle is `POST /api/spaces`,
`POST /api/spaces/{id}/rename` and `POST /api/spaces/{id}/archive`, with
`GET /api/spaces` returning exactly what `nodum space-list` prints.
`POST /api/ingest` mirrors `nodum ingest`, taking exactly one of `path` and
`url`. The two capability-URL redemption routes — `GET /api/download/{token}`
and `PUT /api/uploads/{token}` — are the only `/api` routes outside the session
gate: the single-use token in the path *is* the authorisation, so there is no
ambient cookie for a cross-origin page to ride.

```sh
nodum serve [--host 127.0.0.1] [--port 8600] [--allow-host NAME] [--db PATH]
```
