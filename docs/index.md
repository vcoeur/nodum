---
title: nodum — a typed graph of atomic notes
description: nodum is an atomic-notes knowledge system — a mutable PostgreSQL graph of typed, UUID-keyed nodes and edges with full-text search and recursive subgraph expansion, behind one data-service layer fronted by a CLI, an HTTP API, and a React SPA. Built to be driven by LLM agents.
---

# nodum

<p class="tagline">Atomic notes, typed and knotted together.</p>

`nodum` is an **atomic-notes knowledge system**: a mutable graph of small, typed notes (nodes)
joined by typed, directed edges, over a **runtime-evolvable schema**. It is built to be driven by LLM
agents — every note carries a plain-text `content` body, every operation returns the same JSON whether
you call it from the CLI or the HTTP API, and one `schema` call hands a machine the whole contract.

## What it is

- A **mutable PostgreSQL graph** — one `nodes` table and one `edges` table; each node carries a
  `kind`, a plain-text `content` body, and a JSON `data` payload. No table-per-type; the graph stays
  uniform.
- A **runtime-evolvable schema** — kinds live in the database (the `node_kinds` / `edge_kinds` tables),
  not as frozen code or free strings, so you add/edit/delete them at runtime. Each node kind has a
  field schema; each edge kind has a `from → to` signature that constrains its endpoints.
- A **single data-service spine** with three thin adapters over it: a **CLI**, an **HTTP API**, and a
  **React single-page web UI**. The CLI and API emit byte-identical JSON for the same data.
- **Full-text + graph retrieval** — Postgres full-text search plus recursive-CTE subgraph expansion.
  No embeddings (that is a deferred design target, not a current feature).

## What it does

- **Stores typed atomic notes.** Seven seeded node kinds (Person, Organization, Topic, Entity,
  Reference, Literature, Note) and twelve edge kinds with checked endpoint signatures — all editable
  at runtime. A node/edge is validated softly in the service; the database enforces the cheap
  universals (the `kind` foreign key, non-null `content`, valid endpoints, no self-edges).
- **Lets the schema evolve.** Create, edit, and delete node and edge kinds at runtime through the CLI,
  the API, and the web UI's **Schema** view — no code change or redeploy. Deleting an in-use kind is
  refused unless you reassign its rows (`--into`).
- **Keeps authoring prose-first.** *Open process, closed format*: every node has a plain-text
  `content` body — the full-text-indexed, LLM-readable surface, ready to embed later — alongside its
  typed fields.
- **Searches and expands.** Ranked full-text search with a kind filter, and `expand` walks a seed
  node's connected subgraph out to N hops via a single recursive CTE — the context payload an agent
  reads back.
- **Stays self-describing.** `nodum schema` (CLI) and `GET /schema` (API) return the live schema —
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
2. **[Concepts](concepts.md)** — the typed graph: nodes, edges, the runtime-evolvable schema of
   DB-stored kinds, the `from → to` signatures, and the *open process, closed format* principle.
3. **Reference** — the long form: [Install &amp; run](install.md) (both distribution tracks,
   configuration, auth, migration) and [Commands](commands.md) (every CLI verb and API route, with
   the JSON contract).

## Links

- [Source on GitHub](https://github.com/vcoeur/nodum)
- [`nodum` on PyPI](https://pypi.org/project/nodum/)
- [Author](https://vcoeur.com)
