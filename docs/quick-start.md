---
title: Quick start · nodum
description: Build a small nodum graph from the CLI — nodes, wikilinks, search, history, and the agent review queue.
---

# Quick start

Assumes nodum is [installed](install.md) and on your `PATH`.

## Create the database

```sh
nodum init
```

## Build a small graph

Every command prints one JSON object.

```sh
nodum node create --type concept --title "Graph Theory" --as owner
nodum node create --type note --title "My note" \
    --content "Notes on [[Graph Theory]] and its applications." --as owner
```

The wikilink in that content is not decoration — it materialises as a real
edge:

```sh
nodum edge list --type mentions --as owner
```

## Drop in a folder of documents

Point `ingest` at a directory and every file directly inside it is registered,
read, and described. Check first what this install can read:

```sh
nodum ingest handlers
```

Plain text, Markdown, JSON, and HTML always work. PDF text needs the `pdf`
extra, image OCR the `ocr` extra (plus the `tesseract` binary), and audio
transcription the `audio` extra — a handler that cannot run says so, and names
what to install.

```sh
nodum ingest file ~/papers --as owner              # the files directly inside
nodum ingest file ~/papers --recursive --as owner  # and the ones below them
nodum ingest url https://example.com/paper.pdf --as owner
```

Each document becomes an `asset_ref` node for the bytes, a `source` node
holding the extracted text, a `derived_from` edge between them, and one `block`
child per page. A single file prints one JSON object; a folder prints
`{"ingestions": [...], "count": n}`.

A batch never loses its successes: a file that fails prints its reason to
stderr and the rest carry on, so a non-zero exit means "read stderr for what is
missing", not "nothing happened". Running it again is safe — ingestion is
idempotent per (hash, space), and what already landed comes back with
`"created": false` rather than a duplicate.

Missing an extra is not fatal either. The asset is still registered and still
described; the result just reports that no text came out.

## Search it

Everything ingested is searchable immediately — the full extracted text through
the `asset_ref` node, and each page through its own block:

```sh
nodum search "graph theory" --as owner
```

Hybrid search fuses BM25 and vector results by reciprocal rank fusion, then
re-ranks by graph expansion. Without the `embeddings` extra installed this
degrades to BM25 alone rather than failing.

Check what the derived indexes know:

```sh
nodum projector status            # checkpoints, backlog, availability
nodum projector rebuild fts       # drop + replay from event 0
```

## Read the history

Every mutation appends to the event log with full before/after payloads, so
nothing is lost:

```sh
nodum history <node-id> --as owner   # version snapshots
nodum events --as owner              # the log itself, newest first
nodum diff <version-a> <version-b> --as owner
nodum undo --as owner                # reverse the latest event
```

## Let an agent write

Agents do not drive the CLI. Create an agent account and grant it access to a
space, then run the MCP server:

```sh
nodum agent create researcher --as owner     # prints a show-once token to stderr
nodum grant researcher main suggest --as owner
NODUM_AGENT_TOKEN=ndm_… nodum mcp serve      # the agent's surface, over stdio
```

Over MCP the agent's writes on `main` land `proposed` (the `suggest` grant) and
wait in the review queue. Review them with the human CLI:

```sh
nodum review queue --created-by agent:researcher --as owner
nodum accept <id> --as owner
nodum reject <id> --reason "duplicate of the existing note" --as owner
```

An agent `update` stages a proposed *version* recording which fields it named.
Accepting applies **only those fields** to the node as it stands then — so a
human edit made while the proposal waited is not reverted.

To let the agent write directly, raise its grant (human only):

```sh
nodum grant researcher main edit --as owner
```

## Let the graph tend itself

A consolidation cycle runs the gardener's deterministic jobs — duplicate
candidates, link pruning and inference, housekeeping, a neglect report — and
files what it finds in the review queue rather than asserting it. Rehearse one
first; a dry run writes its journal entry and no event at all:

```sh
nodum consolidate --dry-run --as owner
nodum consolidate --as owner
nodum cycle-list --as owner            # the dream journal, newest first
nodum cycle-get <cycle-id> --as owner  # what ran, and what it measured
nodum events --cycle <cycle-id> --as owner   # what it actually changed
```

If a cycle did something you did not want, take the whole of it back. It is all
or nothing, and it refuses rather than clobbering when the graph has moved on:

```sh
nodum rollback <cycle-id> --dry-run --as owner   # would this succeed?
nodum rollback <cycle-id> --as owner
```

The same machinery is behind the curative commands you type yourself —
`merge-nodes`, `retype`, `supersede-edge`, `bulk-relink` — so `rollback` is the
way back from those too, and `undo` will tell you so rather than reversing half
of one.

## Serve the UI

```sh
nodum serve
```

This runs the HTTP API and the web UI from one process on
`http://127.0.0.1:8600`. Every write through this surface is attributed to the
session's human, and no request field can say otherwise. To run one
consolidation cycle a night, name a local time — unset means off, which is the
default:

```sh
NODUM_CONSOLIDATE_AT=03:30 nodum serve
```

## Next

- [Concepts](concepts.md) — the state machine, the event log, and actors.
- [Architecture](architecture.md) — how the layers fit together.
- [Commands](commands.md) — the full CLI surface.
