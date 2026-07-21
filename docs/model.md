---
title: Data model · nodum
description: The nodum data model in diagrams — one service layer over a single PostgreSQL database; the nodes/edges/kind-catalog tables; the seven seeded node kinds and twelve edge-kind signatures; how get and expand walk the typed graph; and the title/tags conventions on top of the schema.
---

# Data model

This page is the diagram companion to [Concepts](concepts.md): the same model, drawn. Every diagram
below is generated from what `schema.sql`, `nodum.metamodel`, and `nodum.service` actually do.

## Big picture — one spine, three adapters, one database

All logic lives in the **service layer**. The CLI, the HTTP API, and the React SPA are thin adapters
that hold no logic of their own; the SPA talks only to the API. Every adapter serialises the same
pydantic models, so identical data yields byte-identical JSON on the CLI and the API.

```mermaid
flowchart LR
    cli["CLI<br>(nodum.cli)"] --> svc["nodum.service"]
    api["HTTP API<br>(nodum.api)"] --> svc
    spa["React SPA<br>(nodum.web)"] --> api
    svc --> pg[("PostgreSQL")]
    style svc fill:#fff3cd,color:#000
    style pg fill:#d9f2d9,color:#000
```

The service is the only place that talks to the database: it resolves kinds from the catalog, runs
the write-time validation, and executes every query. Clients self-orient by reading the live schema
first (`nodum schema` / `GET /schema`).

## The tables — two instance tables, one kind catalog

There is **no table per kind**. Every instance lives in the one `nodes` table or the one `edges`
table; typing is a `kind` column whose foreign key points into the runtime-editable catalog.

```mermaid
erDiagram
    node_kinds ||--o{ nodes : "kind"
    edge_kinds ||--o{ edges : "kind"
    nodes ||--o{ edges : "from_uuid"
    nodes ||--o{ edges : "to_uuid"
    node_kinds {
        text name PK
        jsonb spec "group, content_label, fields"
    }
    edge_kinds {
        text name PK
        jsonb spec "from, to, symmetric, fields"
    }
    nodes {
        uuid uuid PK
        text kind FK
        text content "NOT NULL — the FTS-indexed body"
        jsonb data "kind metadata, GIN-indexed"
        timestamptz created_at
        timestamptz updated_at
    }
    edges {
        uuid uuid PK
        text kind FK
        uuid from_uuid FK "ON DELETE CASCADE"
        uuid to_uuid FK "ON DELETE CASCADE"
        jsonb data
        timestamptz created_at
        timestamptz updated_at
    }
```

What the database enforces (the *cheap-hard* invariants — everything richer is validated in the
service):

- the `kind` foreign keys into the catalog tables,
- `content NOT NULL` on every node,
- both endpoint foreign keys, with `ON DELETE CASCADE` — deleting a node removes its edges,
- `CHECK (from_uuid <> to_uuid)` — no self-edges.

The `spec` JSONB on each kind row carries the kind's own definition: for a node kind its `group`,
`content_label`, and `fields` schema; for an edge kind its `from → to` signature. That is what makes
the schema **data** — editable at runtime through kind CRUD, never a code change. (A fifth,
single-row `auth_secret` table holds the password hash and signing key; it is unrelated to the
graph.)

## The seeded kind catalog

Seven seeded node kinds in three groups, and twelve edge kinds whose signatures constrain their
endpoints. The default catalog as a graph — each arrow is one edge kind, drawn from a *from* node
kind to a *to* node kind:

```mermaid
flowchart LR
    Person -- AuthorOf --> Reference
    Person -- AffiliatedWith --> Organization
    Organization -- Publishes --> Reference
    Literature -- summarizes --> Reference
    Note -- cites --> Reference
    Note -- cites --> Literature
    Note -- IsAbout --> Topic
    Literature -- IsAbout --> Topic
    Reference -- IsAbout --> Topic
    Topic -- BroaderThan --> Topic
    any(["any kind"]) -- mentions --> Entity
    any -- mentions --> Person
    any -- mentions --> Organization
    any -- mentions --> Topic
    Note -- "supports · contradicts · refines · answers" --> Note
    style Person fill:#e6f0ff,color:#000
    style Organization fill:#e6f0ff,color:#000
    style Topic fill:#e6f0ff,color:#000
    style Entity fill:#e6f0ff,color:#000
    style Reference fill:#fff3cd,color:#000
    style Literature fill:#fff3cd,color:#000
    style Note fill:#d9f2d9,color:#000
    style any fill:#eee,color:#000
```

Colours follow the three groups: blue = **entity**, yellow = **literature**, green = **note**.

| Group | Kind | `content` is | Typed fields (in `data`) |
|---|---|---|---|
| entity | `Person` | name | `aliases`, `born` |
| entity | `Organization` | name | `aliases` |
| entity | `Topic` | label | `aliases` |
| entity | `Entity` | label | `entity_type`, `aliases` |
| literature | `Reference` | citation | `citekey`, `authors`, `year`, `venue`, `doi`, `url`, `ref_type` |
| literature | `Literature` | summary | `key_points` |
| note | `Note` | text | `role` (enum), `confidence` |

Signatures are checked **in the service at write time** — the SQL only enforces that both endpoints
exist. The catalog is seeded into an empty database by `init-db` and evolves at runtime thereafter;
the tables above are the defaults, not a fixed set. The full signature table is on
[Concepts](concepts.md#edge-kinds-and-their-signatures); read the live version with `nodum schema`.

## Walking the graph — `get` and `expand`

Two retrieval primitives read the graph, both over the same two uniform tables.

**`get`** returns a node plus its incident edges, optionally filtered by edge kind and direction:

```mermaid
flowchart TD
    req["get uuid<br>--edge-kind K … · --direction in|out|both"] --> node["SELECT node WHERE uuid"]
    node --> edges["incident edges:<br>from_uuid = uuid (out)<br>to_uuid = uuid (in)"]
    edges --> filter{"filters given?"}
    filter -- yes --> keep["keep matching kind + direction"]
    filter -- no --> all["keep all incident edges"]
    keep --> out["NodeWithEdges { node, edges }"]
    all --> out
    style out fill:#d9f2d9,color:#000
```

**`expand`** walks *directed* edges outward from a seed set, up to `depth` hops, via a single
recursive CTE — optionally restricted to given edge kinds — then loads every node touched:

```mermaid
flowchart TD
    seed["seed uuid(s) · depth N · edge kinds?"] --> hop1["hop 1: edges leaving any seed"]
    hop1 --> rec{"hop < depth?"}
    rec -- yes --> hopn["next hop: edges leaving<br>the previous hop's targets"]
    hopn --> rec
    rec -- no --> load["load every node touched"]
    load --> sub["Subgraph { seed, depth, nodes, edges }"]
    style sub fill:#d9f2d9,color:#000
```

The one-table invariant is what keeps this uniform: `expand` never joins a per-kind schema, so it
walks any mix of kinds with the same query. The serialised `Subgraph` is the context payload a
client — or an agent — reads back. `search` is the third primitive: Postgres full-text over
`content`, ranked by `ts_rank`, with optional `kind` and `tags` filters.

## Conventions on top of the schema

Two agent-facing conveniences sit *above* the schema — neither is a stored column or a kind.

**Derived `title`.** Every serialised node carries a `title` — the first non-blank line of its
`content`, stripped, capped at 80 characters. It is a pydantic computed field on `NodeOut`, computed
at read time and never stored, so it tracks edits for free and lands identically on every surface.

```mermaid
flowchart LR
    content["content:<br>· (blank line)<br>· Claims about X need Y<br>· body continues …"] --> derive["first non-blank line,<br>stripped, ≤ 80 chars"]
    derive --> title["title: Claims about X need Y"]
    style title fill:#d9f2d9,color:#000
```

**Tags.** Any node may carry `"tags": ["…"]` in its `data` payload — a convention, not a schema
feature. `search --tag T` (repeatable) filters by JSONB containment with **AND** semantics: a hit's
`data.tags` array must contain *every* given tag. The GIN index on `data` serves the check.

```mermaid
flowchart TD
    q["search QUERY --tag alpha --tag beta"] --> fts["full-text match on content"]
    fts --> tags{"data-&gt;'tags' @&gt; '[#quot;alpha#quot;, #quot;beta#quot;]'"}
    tags -- yes --> hit["hit (ranked by ts_rank)"]
    tags -- no --> drop["filtered out"]
    style hit fill:#d9f2d9,color:#000
```

Because both conventions are additive and read-time, payloads that predate them are unchanged except
for the extra `title` key — and a node without a `tags` array simply never matches a `--tag` filter.

## What is not in the model

- **No per-kind tables or model classes** — one `nodes` table, one `edges` table, typing via the
  catalog.
- **No edges-on-edges** — to qualify a relationship, reify it as a `Note` and link to it.
- **No embeddings** — no vector column; `content` is stored ready for them (a deferred design
  target).
- **No multi-user accounts** — a single main password gates the network surfaces (see
  [Authentication](install.md#authentication)).
