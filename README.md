# nodum

A minimal **atomic-notes knowledge system**: a mutable PostgreSQL graph of
typed, UUID-keyed nodes and edges, with full-text search and recursive subgraph
expansion — all behind a single data-service layer fronted by a CLI, an HTTP
API, and a web view.

nodum is a **typed graph**: kinds live in a code registry (`nodum.metamodel`),
not as free strings. Each node has a kind with a field schema; each edge has a
kind whose `from → to` signature constrains its endpoints. The guiding
principle is **open process, closed format** — every node carries a universal
natural-language `text` (so authoring stays prose-first and the content is
LLM-readable) alongside its typed fields.

> **Scope.** Retrieval is Postgres full-text plus graph traversal — no
> embeddings. Vector / hybrid retrieval, an LLM gardener, contradiction
> reasoning, reranking, auth, and runtime-editable kinds are deferred.

## Quick start

```bash
make db-up        # start local PostgreSQL (docker-compose, host port 5436)
make dev-install  # uv sync --all-groups
make init-db      # create the schema + seed the kind lookup tables
make test         # run the suite (needs the database up)
make serve        # run the HTTP API + web view on http://127.0.0.1:8600
```

Configuration is a single environment variable, `NODUM_DATABASE_URL` (default
`postgresql://nodum:nodum@localhost:5436/nodum`, matching docker-compose). Copy
`.env.example` to `.env` to override it. The API and web view bind to
`127.0.0.1:8600`.

## The metamodel

Kinds are defined in code, in `nodum.metamodel`. Adding a kind is a registry
edit there — there is no per-kind table or model class; every instance lives in
the one `nodes` table and the one `edges` table, with a `kind` column referencing
the metamodel. A node/edge is validated softly in the service against its kind;
the database enforces only the cheap universals (the `kind` foreign key, a
`text` field on every node, valid endpoints, no self-edges).

### Node kinds

| Kind | `text` is | Typed fields |
|---|---|---|
| `Person` | name | `aliases`, `born` |
| `Organization` | name | `aliases` |
| `Topic` | label | `aliases` |
| `Entity` | label | `entity_type`, `aliases` |
| `Reference` | citation | `citekey`, `authors`, `year`, `venue`, `doi`, `url`, `ref_type` |
| `Literature` | summary | `key_points` |
| `Note` | text | `role` (claim / question / hypothesis / observation / synthesis / definition), `confidence` |

`Reference` is a bibliographic record; `Literature` is a note *on* a source;
`Entity` is the catch-all (place / concept / event / …).

### Edge kinds

| Edge kind | From → To |
|---|---|
| `AuthorOf` | Person → Reference |
| `AffiliatedWith` | Person → Organization |
| `Publishes` | Organization → Reference |
| `summarizes` | Literature → Reference |
| `cites` | Note → Literature, Reference |
| `IsAbout` | Note, Literature, Reference → Topic |
| `BroaderThan` | Topic → Topic |
| `mentions` | any → Person, Organization, Topic, Entity |
| `supports` / `contradicts` / `refines` / `answers` | Note → Note |

Read the live contract any time with `uv run nodum schema` or `GET /schema`.

## Surfaces

The CLI and the HTTP API call the same `nodum.service` layer over one pydantic
I/O schema, so they emit byte-identical JSON for identical data. Both offer full
CRUD plus query.

### CLI

JSON goes to stdout, messages and errors to stderr. Run a command with
`uv run nodum …` (or `make run -- …`).

```bash
# create a typed node; --set carries kind-specific fields (parsed as JSON, else raw)
uv run nodum add Person "Ada Lovelace" --set born=1815
uv run nodum add Reference "Lovelace, Notes on the Analytical Engine (1843)" \
  --set year=1843 --set 'authors=["Ada Lovelace"]'

# link two existing nodes with a typed, directed edge (endpoints checked against the signature)
uv run nodum link <person-uuid> <reference-uuid> AuthorOf

# ranked full-text search, optionally filtered by kind
uv run nodum search "analytical engine" --kind Reference

# expand a seed node into its connected subgraph, two hops out
uv run nodum expand <uuid> --depth 2 --edge-kind AuthorOf
```

Also: `get <uuid>`, `edit-node` / `edit-edge`, `rm-node` / `rm-edge`, `schema`,
`init-db`, `migrate`, and `serve`.

### HTTP API

FastAPI; every response is the same JSON envelope the CLI prints.

```bash
# POST /nodes — create a typed node
curl -s -X POST http://127.0.0.1:8600/nodes \
  -H 'content-type: application/json' \
  -d '{"kind": "Note", "text": "Spaced repetition improves long-term retention",
       "data": {"role": "claim"}}'

# GET /schema — the metamodel contract (node kinds + edge kinds + signatures)
curl -s http://127.0.0.1:8600/schema

# GET /expand — seed node → connected subgraph
curl -s 'http://127.0.0.1:8600/expand?seed=<uuid>&depth=2&edge_kind=cites'
```

Full route set: `POST /nodes`, `GET|PATCH|DELETE /nodes/{uuid}`, `POST /edges`,
`PATCH|DELETE /edges/{uuid}`, `GET /search`, `GET /expand`, `GET /schema`,
`GET /healthz`. A missing node/edge returns 404; invalid input returns 422.

### Web view

A schema-driven, full-CRUD browser UI served by the same app — open
<http://127.0.0.1:8600/> after `make serve`. It fetches `GET /schema` and drives
its forms from the metamodel: create/edit a node by kind (fields rendered per
the kind's schema), create an edge by type (endpoint pickers filtered to the
signature, with validation feedback), delete (with a cascade-aware confirm),
search (with a kind filter), open a node, and render its subgraph as a visual
node-link **SVG diagram**. Dependency-free — no CDNs.

## Data model

A mutable JSONB graph, one `nodes` table and one `edges` table:

- **Node** — a UUID, a `kind` (referencing the metamodel), and a JSON `data`
  payload that always carries a universal `text` field plus the kind's typed
  fields. The `text` is full-text indexed.
- **Edge** — a UUID, a `kind`, and a directed link `from_uuid → to_uuid` between
  two distinct nodes, with a JSON `data` payload. The edge kind's signature
  constrains which node kinds the endpoints may be. Deleting a node cascades to
  its edges.
- **Typed, not open.** Kinds come from `nodum.metamodel`; the `node_kinds` /
  `edge_kinds` lookup tables back the `kind` foreign keys. Validation is soft in
  the service and cheap-hard in the database.

Retrieval is Postgres full-text (`tsvector` / `ts_rank`) for search and a
recursive CTE for subgraph expansion — **no embeddings**.

### Migrating an MVP database

`uv run nodum migrate` upgrades an earlier (open-type) MVP database in place,
idempotently: it adds the `kind` columns, seeds the lookup tables, drops the old
type-as-node rows, backfills kinds (content nodes become `Note`, carrying their
old `data.type` into `role`), and enforces the new constraints.

## License

MIT — see [LICENSE](LICENSE).
