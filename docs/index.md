---
title: nodum — a typed graph of atomic notes
description: nodum is an atomic-notes knowledge system — a mutable PostgreSQL graph of typed, UUID-keyed nodes and edges with full-text search and recursive subgraph expansion, behind one data-service layer fronted by a CLI, an HTTP API, and a React SPA. Built to be driven by LLM agents.
---

# nodum

<p class="tagline">Atomic notes, typed and knotted together.</p>

`nodum` is an **atomic-notes knowledge system**: a mutable graph of small, typed notes (nodes)
joined by typed, directed edges. It is built to be driven by LLM agents — every note carries a
universal natural-language `text`, every operation returns the same JSON whether you call it from the
CLI or the HTTP API, and one `schema` call hands a machine the whole contract.

## What it is

- A **mutable PostgreSQL graph** — one `nodes` table and one `edges` table, each row carrying a
  `kind` and a JSON `data` payload. No table-per-type; the graph stays uniform.
- A **typed metamodel** — kinds live in a code registry (`nodum.metamodel`), not as free strings.
  Each node kind has a field schema; each edge kind has a `from → to` signature that constrains its
  endpoints.
- A **single data-service spine** with three thin adapters over it: a **CLI**, an **HTTP API**, and a
  **React single-page web UI**. The CLI and API emit byte-identical JSON for the same data.
- **Full-text + graph retrieval** — Postgres full-text search plus recursive-CTE subgraph expansion.
  No embeddings (that is a deferred design target, not a current feature).

## What it does

- **Stores typed atomic notes.** Seven node kinds (Person, Organization, Topic, Entity, Reference,
  Literature, Note) and twelve edge kinds with checked endpoint signatures. A node/edge is validated
  softly in the service; the database enforces the cheap universals (the `kind` foreign key, a `text`
  field on every node, valid endpoints, no self-edges).
- **Keeps authoring prose-first.** *Open process, closed format*: every node has a universal
  natural-language `text` — the full-text-indexed, LLM-readable surface — alongside its typed fields.
- **Searches and expands.** Ranked full-text search with a kind filter, and `expand` walks a seed
  node's connected subgraph out to N hops via a single recursive CTE — the context payload an agent
  reads back.
- **Stays self-describing.** `nodum schema` (CLI) and `GET /schema` (API) return the live metamodel —
  node kinds, edge kinds, and signatures — so any client can self-orient before its first write.
- **Gates the network surfaces.** A single main password protects the API and web UI; the local CLI
  is trusted and sets the secret. See [Authentication](install.md#authentication).

## Who it's for

- **Agents** that need a typed, queryable knowledge base — `nodum schema --json` self-orients an LLM,
  and the CLI/API JSON envelopes are stable. nodum is a sibling of [`knoten`](https://knoten.vcoeur.com)
  and [`quelle`](https://quelle.vcoeur.com): Claude-first, JSON-everywhere CLIs.
- **Developers** who want a small typed graph over Postgres without a heavyweight platform — embed the
  service, script the CLI, or run the full app from a container.
- **Researchers** building a structured note graph (claims, references, literature, topics) that a
  machine can traverse and a human can read.

## Two ways to run it

nodum ships as **two artifacts from one codebase** (see [Install &amp; run](install.md)):

- **Docker image — the full app** (API + built web UI). Point it at a Postgres, give it a password
  secret, and it self-initialises on first boot.
- **PyPI wheel — the CLI / library** (no web UI). `pip install nodum` for scripting, automation, or
  embedding the service.

```bash
# Full app, in one command:
echo 'change-me' > nodum_admin_password.txt
docker compose -f docker-compose.example.yml up     # → http://127.0.0.1:8600

# …or just the CLI:
pipx install nodum        # or: uv tool install nodum
```

## Learn more, in order

The pages are written to be read top-to-bottom the first time:

1. **[Quick start](quick-start.md)** — run the full app (or the CLI), sign in, then create a typed
   node, link a typed edge, search, and expand a subgraph.
2. **[Concepts](concepts.md)** — the typed metamodel: nodes, edges, kinds, the `from → to`
   signatures, and the *open process, closed format* principle that shapes everything else.
3. **Reference** — the long form: [Install &amp; run](install.md) (both distribution tracks,
   configuration, auth, migration) and [Commands](commands.md) (every CLI verb and API route, with
   the JSON contract).

## Links

- [Source on GitHub](https://github.com/vcoeur/nodum)
- [`nodum` on PyPI](https://pypi.org/project/nodum/)
- [Author](https://vcoeur.com)
