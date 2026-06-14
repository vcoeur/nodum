---
title: Concepts · nodum
description: The nodum typed graph — nodes and edges, the runtime-evolvable schema of DB-stored kinds, the from → to edge signatures, soft service validation vs cheap-hard database constraints, and the open-process-closed-format principle.
---

# Concepts

nodum is a **typed graph**. This page explains the model that everything else — the CLI, the API,
the web UI — is a thin surface over.

## Nodes and edges

The whole store is two tables:

- A **node** is a UUID, a `kind`, a plain-text `content` column, and a JSON `data` payload. `content`
  is the natural-language surface (the body that is full-text indexed, and that later embeddings will
  target); `data` holds the kind's typed fields.
- An **edge** is a UUID, a `kind`, and a directed link `from_uuid → to_uuid` between **two distinct**
  nodes, with its own JSON `data`. Deleting a node cascades to its edges.

Edges are `node → node` only. To say something *about* a relationship (a claim, a qualification), you
reify it as a `Note` and link to it — the graph never grows edges-on-edges.

## Kinds live in the database, and evolve

Earlier the graph was *open*: a node carried a free `data.type` string, and types were themselves
nodes. Then kinds moved to a frozen code registry. Now they live in the **database** and **evolve at
runtime**: the `node_kinds` / `edge_kinds` tables store each kind's name plus a `spec` (its field
shape, or its `from → to` signature) as JSONB. The default catalog (below) is seeded on `init-db`, and
editable thereafter — see [Editing the schema](#editing-the-schema-at-runtime).

Crucially there is still **no per-kind table and no per-kind model class**. Every instance lives in the
one `nodes` table and the one `edges` table; typing is a `kind` column referencing the catalog. That
uniformity is deliberate — it lets `expand` stay a single recursive CTE over `edges` regardless of
kind, instead of joining a sharded per-type schema. The value types and validation logic live in
`nodum.metamodel`, but the *catalog* is data: the service resolves a kind from the DB and validates an
instance against it.

## Node kinds

Seven **seeded** kinds, in three groups (the catalog is evolvable — these are the defaults, not a
fixed set). Every kind defines what its `content` means and an optional typed-field schema. A field's
`type` is one of `str`, `int`, `float`, `bool`, `list[str]`, `enum` (with `choices`), `date` (a plain
calendar date, no timezone), or `datetime`. A `datetime` is stored canonically as **UTC** (ISO-8601
with a `Z` suffix); the web UI shows and accepts it in your local time, converting on the edges.

| Kind | Group | `content` is | Typed fields |
|---|---|---|---|
| `Person` | entity | name | `aliases` (list), `born` (int) |
| `Organization` | entity | name | `aliases` (list) |
| `Topic` | entity | label | `aliases` (list) |
| `Entity` | entity | label | `entity_type` (place / concept / event / …), `aliases` (list) |
| `Reference` | literature | citation | `citekey`, `authors` (list), `year` (int), `venue`, `doi`, `url`, `ref_type` |
| `Literature` | literature | summary | `key_points` (list) |
| `Note` | note | text | `role` (claim / question / hypothesis / observation / synthesis / definition), `confidence` (float) |

`Entity` is the deliberate catch-all (place, concept, event, …) that keeps the kind set small.
`Reference` is a bibliographic record; `Literature` is a note *on* a source (≈ the role
[`quelle`](https://quelle.vcoeur.com) plays for a vault).

## Edge kinds and their signatures

Each edge kind constrains its endpoints to specific node kinds — the `from_kinds → to_kinds`
signature, checked when the edge is created.

| Edge kind | From | To |
|---|---|---|
| `AuthorOf` | Person | Reference |
| `AffiliatedWith` | Person | Organization |
| `Publishes` | Organization | Reference |
| `summarizes` | Literature | Reference |
| `cites` | Note | Literature, Reference |
| `IsAbout` | Note, Literature, Reference | Topic |
| `BroaderThan` | Topic | Topic |
| `mentions` | any node kind | Person, Organization, Topic, Entity |
| `supports` | Note | Note |
| `contradicts` | Note | Note |
| `refines` | Note | Note |
| `answers` | Note | Note |

Read the live version any time with `nodum schema` or `GET /schema` — the table above is generated
from the same registry.

## When a new kind is warranted

The rule: **a kind earns its place only if it unlocks a typed edge or a typed field.** If a
distinction merely labels or groups nodes, model it as a **role** (the `Note.role` enum) or a tag —
not a new kind. This is what keeps the kind set small enough to hold in your head and the signatures
meaningful.

## Editing the schema at runtime

Because the catalog is data, you evolve it without a code change or redeploy — through the CLI
(`nodum node-kind add/edit/rm`, `nodum edge-kind add/edit/rm`) or the API (`POST`/`PATCH`/`DELETE` on
`/node-kinds` and `/edge-kinds`). Adding a node kind takes a `--group`, a `--content-label`, and a
`--fields` JSON spec; an edge kind takes `--from` / `--to` node kinds (its signature) and optional
fields. Editing replaces only the attributes you pass.

**Deleting is guarded.** A node kind in use by nodes — or named in an edge kind's signature — cannot
be deleted outright; the error reports what blocks it (and `schema` exposes a `usage` count per kind).
Pass `--into <kind>` (CLI) / `?into=<kind>` (API) to **reassign** the using nodes to another kind (and
rewrite the signatures that named the old kind) before deleting. An edge kind in use by edges resolves
the same way — `--into` reassigns its edges — or with `--purge` (CLI) / `?purge=true` (API), which
**removes** its edges before deleting. `into` and `purge` are mutually exclusive.

One invariant holds the whole thing together: **validation is a write-time gate, never retroactive.**
Editing a kind to be narrower, or reassigning rows to a different kind, never re-validates stored data
— existing rows are grandfathered, and only subsequent writes are checked against the current schema.
That keeps the *process* open while letting the *format* change.

## Open process, closed format

Every node carries a plain-text `content` body *in addition to* its typed fields. So:

- **Open process** — you author prose first; `content` is the human- and LLM-readable surface, what
  full-text search indexes, and what later embeddings will target.
- **Closed format** — the kinds and signatures are closed enough that a machine can traverse and
  validate the graph — yet the format itself can evolve (see above).

You get both: write naturally, and still get a typed, queryable structure.

## Enforcement: soft in the service, cheap-hard in the database

Validation is split on purpose:

- **Soft, in the service.** `validate_node` / `validate_edge` enforce the full typed shape — known
  kind, non-empty `content`, required fields present, declared fields matching their type, enum
  choices, and edge endpoints inside the signature. A violation raises a `ValidationError`. Undeclared
  payload keys are allowed, so the format stays forward-compatible.
- **Cheap-hard, in the database.** `schema.sql` enforces only the universal invariants that are free
  to check: the `kind` foreign keys, `content NOT NULL` on every node, the endpoint foreign keys with
  `ON DELETE CASCADE`, and `CHECK (from_uuid <> to_uuid)` (no self-edges). The endpoint-kind
  *signatures* are not enforced in SQL — that stays in the service.

## Retrieval

Two primitives, both over the uniform tables:

- **Search** — Postgres full-text (`plainto_tsquery('english')`, AND of terms) over each node's
  `content`, ranked by `ts_rank`, with an optional `kind` filter.
- **Expand** — a recursive CTE walks directed edges outward from a seed set up to `depth` hops,
  optionally restricted to given edge kinds, then loads every node touched. The serialised subgraph
  is the context payload a client (or an agent) reads back.

There are **no embeddings** — no vector column, no embeddings table. Vector / hybrid retrieval is a
deferred design target, not a current feature.

## What is deliberately out of scope

nodum keeps to the typed full-text + graph feature set. Deferred (not built): embeddings (pgvector /
hybrid retrieval) — `content` is stored ready for them — an LLM "gardener", contradiction reasoning,
reranking, and multi-user accounts / roles (access is a single shared main password — see
[Authentication](install.md#authentication)).
