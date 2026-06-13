---
title: Concepts · nodum
description: The nodum typed metamodel — nodes and edges, the code-registry of kinds, the from → to edge signatures, soft service validation vs cheap-hard database constraints, and the open-process-closed-format principle.
---

# Concepts

nodum is a **typed graph**. This page explains the model that everything else — the CLI, the API,
the web UI — is a thin surface over.

## Nodes and edges

The whole store is two tables:

- A **node** is a UUID, a `kind`, and a JSON `data` payload. `data` always carries a universal
  `text` field (the natural-language surface) plus the kind's typed fields. The `text` is full-text
  indexed.
- An **edge** is a UUID, a `kind`, and a directed link `from_uuid → to_uuid` between **two distinct**
  nodes, with its own JSON `data`. Deleting a node cascades to its edges.

Edges are `node → node` only. To say something *about* a relationship (a claim, a qualification), you
reify it as a `Note` and link to it — the graph never grows edges-on-edges.

## Kinds live in code, not in the data

Earlier the graph was *open*: a node carried a free `data.type` string, and types were themselves
nodes. That is gone. Kinds now live in a **code registry**, `nodum.metamodel` — two dicts,
`NODE_KINDS` and `EDGE_KINDS`, built from frozen dataclasses. Every node and edge row carries a
`kind` column that references that registry.

Crucially there is **no per-kind table and no per-kind model class**. Every instance lives in the one
`nodes` table and the one `edges` table; typing is a `kind` column plus the registry. That uniformity
is deliberate — it lets `expand` stay a single recursive CTE over `edges` regardless of kind, instead
of joining a sharded per-type schema.

**Adding a kind is a registry edit** in `metamodel.py`; `init-db` (or `migrate` on an existing
database) seeds the new name into the `node_kinds` / `edge_kinds` lookup tables so the database-level
foreign key on `kind` stays in step. Kinds are not runtime-editable — the metamodel is a code
registry by design.

## Node kinds

Seven kinds, in three groups. Every kind defines what its universal `text` means and an optional
typed-field schema.

| Kind | Group | `text` is | Typed fields |
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

## Open process, closed format

Every node carries a universal natural-language `text` *in addition to* its typed fields. So:

- **Open process** — you author prose first; the `text` is the human- and LLM-readable surface, and
  it is what full-text search indexes.
- **Closed format** — the kinds and signatures are closed enough that a machine can traverse and
  validate the graph.

You get both: write naturally, and still get a typed, queryable structure.

## Enforcement: soft in the service, cheap-hard in the database

Validation is split on purpose:

- **Soft, in the service.** `validate_node` / `validate_edge` enforce the full typed shape — known
  kind, non-empty `text`, required fields present, declared fields matching their type, enum choices,
  and edge endpoints inside the signature. A violation raises a `ValidationError`. Undeclared payload
  keys are allowed, so the format stays forward-compatible.
- **Cheap-hard, in the database.** `schema.sql` enforces only the universal invariants that are free
  to check: the `kind` foreign keys, `CHECK (data ? 'text')` on every node, the endpoint foreign keys
  with `ON DELETE CASCADE`, and `CHECK (from_uuid <> to_uuid)` (no self-edges). The endpoint-kind
  *signatures* are not enforced in SQL — that stays in the service.

## Retrieval

Two primitives, both over the uniform tables:

- **Search** — Postgres full-text (`plainto_tsquery('english')`, AND of terms) over each node's
  `text`, ranked by `ts_rank`, with an optional `kind` filter.
- **Expand** — a recursive CTE walks directed edges outward from a seed set up to `depth` hops,
  optionally restricted to given edge kinds, then loads every node touched. The serialised subgraph
  is the context payload a client (or an agent) reads back.

There are **no embeddings** — no vector column, no embeddings table. Vector / hybrid retrieval is a
deferred design target, not a current feature.

## What is deliberately out of scope

nodum keeps to the typed full-text + graph feature set. Deferred (not built): embeddings (pgvector /
hybrid retrieval), an LLM "gardener", contradiction reasoning, reranking, multi-user accounts / roles
(access is a single shared main password — see [Authentication](install.md#authentication)), and
runtime-editable kinds.
