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

## Search it

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

## Serve the UI

```sh
nodum serve
```

This runs the HTTP API and the web UI from one process on
`http://127.0.0.1:8600`. Every write through this surface is attributed to the
session's human, and no request field can say otherwise.

## Next

- [Concepts](concepts.md) — the state machine, the event log, and actors.
- [Architecture](architecture.md) — how the layers fit together.
- [Commands](commands.md) — the full CLI surface.
