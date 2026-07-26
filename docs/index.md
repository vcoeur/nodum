---
title: nodum — a DB-native knowledge graph
description: A typed graph of Markdown-content nodes and typed edges in one SQLite file, with an append-only event log, node versions, undo, and hybrid search.
---

# nodum

A **DB-native knowledge graph**: knowledge is a typed graph of nodes in one
SQLite file — not files with an index on top. Every mutation flows through a
deterministic, LLM-free service layer that validates, enforces a
`proposed → active → archived` state machine, and appends to an event log with
full before/after payloads — so every change is versioned, auditable, and
reversible.

```sh
pipx install nodum
nodum init
nodum node create --type concept --title "Graph Theory" --as owner
```

## Why one file

The whole graph — nodes, edges, versions, the event log, derived indexes, and
even binary assets — lives in a single SQLite database. There is no server to
run, no daemon, no external index to keep in sync, and no API key. Copying the
file copies the knowledge base.

## What makes it agent-native

nodum assumes both humans and agents write to it, and gives them **different
privileges enforced at the service layer**, not by convention:

- A human write lands `active` immediately.
- An agent write lands `proposed` when its grant on the space is `suggest`, and
  `active` when its grant is `edit`.
- Anything that retires or rewrites live state (`accept`, `reject`, `archive`,
  `undo`, every `review` subcommand) is limited to a human — or, in-space, to an
  agent holding `edit` — and `undo` is human-only.

An agent edit does not overwrite: it stages a `proposed` version recording
*which fields it named*, so accepting it applies only those fields to the node
as it stands then. A human edit made while the proposal waited is not reverted.

## Surfaces

The same service layer sits behind every surface, so they cannot drift:

| Surface | For | Entry point |
|---|---|---|
| CLI | humans, scripts | `nodum …` — one JSON object per command |
| HTTP API | the web UI | `nodum serve` |
| MCP server | external agents | `nodum mcp serve` — agent token in `NODUM_AGENT_TOKEN` |
| Web UI | humans | served by `nodum serve` |

The MCP server exposes the read and additive tool tiers **and nothing else** —
an agent can propose, never dispose.

## Search

Two derived indexes are projected from the event log, each with checkpoint and
rebuild mechanics: an **FTS5** full-text index and a **sqlite-vec** chunk
embedding index (a local in-process model — no daemon, no API key). Hybrid
search fuses BM25 and vector results by reciprocal rank fusion, then re-ranks
by graph expansion.

The embedding model is an optional extra; without it, search falls back to
BM25 keyword ranking. An ingested document's full extracted text is joined onto
the `asset_ref` node that stands for its bytes, so BM25 reaches every word of a
long PDF while its per-page blocks keep their own precision.

## Status

**Phase 1 (core)** landed: schema and migrations, the service layer, the event
log with versions and undo, Markdown-as-truth content, wikilink
materialization, and the JSON-emitting CLI.

**Phase 2 (agent-native)** landed: projectors and the two derived indexes,
hybrid search, principals, spaces, and per-(agent, space) grants (`read`/`suggest`/`edit`),
the review/accept API, proposed updates, the MCP server, and content-addressed
assets with lazily generated `thumb`/`preview` renditions (agents get
renditions, never originals).

**Phase 3 (human UI)** landed: `nodum serve` runs the HTTP API — the human
surface, where every write is attributed to the session's human and no request
field can say otherwise — and serves the web UI from the same process: a Markdown
editor, hybrid search, the review queue, a graph view, an asset browser, an
accounts-and-grants admin view, and per-node version history.

**Phase 4 (ingestion)** landed: `nodum ingest` turns a file, a folder, or a URL
into a reviewable subgraph — an `asset_ref` node for the bytes, a `source` node
holding the extracted text, and one block per page. Text extraction runs
through optional per-format handlers (PDF, image OCR, audio), and a missing one
is reported rather than fatal. Also: `page:<n>` PDF page rasters, and
short-lived, single-use capability URLs for agent hosts that share no
filesystem with the graph.

Still to come: claim proposals and the consolidation cycle.
