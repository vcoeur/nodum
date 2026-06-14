# nodum

An **atomic-notes knowledge system**: a mutable PostgreSQL graph of typed,
UUID-keyed nodes and edges, with full-text search and recursive subgraph
expansion — all behind a single data-service layer fronted by a CLI, an HTTP
API, and a React single-page web UI. The full app ships as a **Docker image**;
the **PyPI wheel** is the CLI/library (see [Distribution](#distribution)).

nodum is a **typed graph** with a **runtime-evolvable schema**: kinds are stored
in the database (not frozen in code), so you create, edit, and delete node and
edge kinds at runtime through the CLI and API. Each node has a kind with a field
schema; each edge has a kind whose `from → to` signature constrains its
endpoints. The guiding principle is **open process, closed format** — every node
carries a plain-text `content` field (so authoring stays prose-first and the body
is LLM-readable, and ready to embed later) alongside its typed `data`.

> **Scope.** Retrieval is Postgres full-text plus graph traversal — no
> embeddings yet (`content` is stored ready for them). Access is gated by a single
> main password (see [Authentication](#authentication)). Vector / hybrid
> retrieval, an LLM gardener, contradiction reasoning, reranking, and multi-user
> accounts are deferred.

**Documentation:** <https://nodum.vcoeur.com>

## Quick start

### Run the full app with Docker

The image bundles the API + the built web UI. Point it at a Postgres and give it
a password secret; it self-initialises the schema and sets the main password on
first boot:

```bash
echo 'change-me' > nodum_admin_password.txt        # the initial main password
docker compose -f docker-compose.example.yml up     # nodum + Postgres
# → open http://127.0.0.1:8600 and sign in
```

### Local development

```bash
make dev-install   # install everything for dev: uv sync --all-groups + frontend npm ci
make db-up         # start local PostgreSQL (docker-compose, host port 5436)
make init-db       # create the schema + seed the default kind catalog
uv run nodum auth set-password   # set the main password (gates the API + UI)
make test          # run the Python suite (needs the database up)

# run the API (:8600) and the Vite frontend (:5700) together — brings up the DB
# first, stops both when either exits:
make dev-run

# …or the web UI on its own (React + Vite, in frontend/):
make frontend-dev   # Vite dev server on http://127.0.0.1:5700 (proxies the API)
make serve-spa      # build the SPA and serve it through FastAPI on 8600
```

Configuration is environment variables, chiefly `NODUM_DATABASE_URL` (default
`postgresql://nodum:nodum@localhost:5436/nodum`, matching docker-compose). Copy
`.env.example` to `.env` to override. The API serves on `127.0.0.1:8600`; the
Vite dev server on `5700`.

## The schema (runtime-evolvable)

Kinds are **stored in the database**, in the `node_kinds` / `edge_kinds` tables —
each row a kind name plus a `spec` (its field shape, or its `from → to`
signature). They are seeded with the defaults below on `init-db`, and editable at
runtime thereafter: `nodum node-kind add/edit/rm` and `nodum edge-kind add/edit/rm`
(and the matching `/node-kinds` / `/edge-kinds` API routes). There is no per-kind
table or model class; every instance lives in the one `nodes` table and the one
`edges` table, with a `kind` column referencing the catalog. A node/edge is
validated softly in the service against its (DB-resolved) kind; the database
enforces only the cheap universals (the `kind` foreign key, valid endpoints, no
self-edges).

Deleting a kind that is still in use is **refused** (the error reports the usage);
`--into <kind>` (CLI) / `?into=<kind>` (API) reassigns the using rows — and, for a
node kind, rewrites the edge signatures that named it — then deletes.

The tables below are the **seeded defaults**, not a fixed set.

### Node kinds

| Kind | `content` is | Typed fields |
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
`uv run nodum …` (or `make cli -- …`).

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

# evolve the schema: add a node kind, then an edge kind that uses it
uv run nodum node-kind add Dataset --group entity --content-label name \
  --fields '{"rows": {"type": "int"}, "license": {"type": "str"}}'
uv run nodum edge-kind add DerivedFrom --from Dataset --to Reference

# delete a kind; refused while in use, then reassigned with --into
uv run nodum node-kind rm Dataset --into Entity
```

Also: `get <uuid>`, `edit-node` (`--content`) / `edit-edge`, `rm-node` / `rm-edge`,
`schema`, `node-kind add/edit/rm` and `edge-kind add/edit/rm`,
`auth set-password` / `auth status`, `init-db`, `migrate`, and `serve`.

### HTTP API

FastAPI; every response is the same JSON envelope the CLI prints.

```bash
# POST /nodes — create a typed node
curl -s -X POST http://127.0.0.1:8600/nodes \
  -H 'content-type: application/json' \
  -d '{"kind": "Note", "content": "Spaced repetition improves long-term retention",
       "data": {"role": "claim"}}'

# GET /schema — the live schema (node kinds + edge kinds + signatures)
curl -s http://127.0.0.1:8600/schema

# POST /node-kinds — evolve the schema at runtime
curl -s -X POST http://127.0.0.1:8600/node-kinds \
  -H 'content-type: application/json' \
  -d '{"name": "Dataset", "group": "entity", "content_label": "name"}'

# GET /expand — seed node → connected subgraph
curl -s 'http://127.0.0.1:8600/expand?seed=<uuid>&depth=2&edge_kind=cites'
```

Full route set: `POST /nodes`, `GET|PATCH|DELETE /nodes/{uuid}`, `POST /edges`,
`PATCH|DELETE /edges/{uuid}`, `GET /search`, `GET /expand`, `GET /schema`,
`POST /node-kinds`, `PATCH|DELETE /node-kinds/{name}`, `POST /edge-kinds`, and
`PATCH|DELETE /edge-kinds/{name}` — all behind auth — plus `POST /auth/login`,
`POST /auth/logout`, `GET /auth/session`, and `GET /healthz`. A missing
node/edge/kind returns 404; deleting an in-use kind without `into` returns 409;
invalid input returns 422; an unauthenticated request returns 401 (or 503 until a
password is set). Pass the token from `POST /auth/login` as `Authorization: Bearer
<token>` (see [Authentication](#authentication)).

### Web UI

A **React + Vite (TypeScript) single-page app** (in `frontend/`), a schema-driven
client of the JSON API. It fetches `GET /schema` and drives its forms from the
live schema, behind a `Graph` / `Schema` view switch:

- **Graph** — create/edit a node by kind, create an edge by type (endpoint pickers
  filtered to the signature), delete (with a cascade-aware confirm), search (with a
  kind filter), open a node, and render its subgraph as a node-link **SVG diagram**.
- **Schema** — manage the runtime-evolvable schema itself: list, create, edit, and
  delete node kinds and edge kinds (a field-schema editor builds each kind's typed
  fields; an edge kind's `from`/`to` are picked as checkbox groups). Deleting a
  kind that is still in use offers an `into` reassignment, just like the CLI's
  `--into`. Mutations reload the schema so the Graph view stays in sync.

Sign-in is in-app (the SPA reads `GET /auth/session` to know its state), with a
`Logout` control. The UI ships in the Docker image — see
[Distribution](#distribution).

## Authentication

The API and web view are gated by a **single main password**, set from the CLI on
the machine where nodum runs:

```bash
uv run nodum auth set-password   # prompts twice (or reads a piped stdin line)
uv run nodum auth status         # is a password set? (never prints the hash)
```

Until a password is set the install is **locked** (protected routes return 503).
The password is stored as an argon2 hash (with a random signing key) in a
single-row table; logging in mints a session token signed with that key
(`itsdangerous`, 7-day expiry). Browsers carry it in an **HttpOnly, SameSite=
Strict cookie** set by `POST /auth/login`; API/CLI clients send it as an
`Authorization: Bearer <token>`. The per-request check verifies only the token
signature — argon2 runs at login only. Set `NODUM_COOKIE_SECURE=1` to mark the
cookie Secure behind a TLS-terminating proxy. Multi-user accounts are out of
scope — this is one shared password.

## Distribution

Two artifacts from one codebase:

- **Docker image — the full app.** A multi-stage build compiles the React SPA and
  bundles it with the API. The entrypoint waits for Postgres, runs `init-db`, sets
  the main password from `NODUM_ADMIN_PASSWORD_FILE` / `NODUM_ADMIN_PASSWORD` (only
  if unconfigured), and serves on `0.0.0.0:8600`. So a deploy is: declare the
  image, point `NODUM_DATABASE_URL` at your Postgres, provide a password secret —
  that's all. `docker-compose.example.yml` is a turnkey starting point; the image
  does not bundle Postgres.
- **PyPI wheel — the CLI / library.** `pip install nodum` gives the `nodum`
  command and the HTTP API, but **no web UI** (the bundle is image-only). Useful
  for scripting, automation, or embedding the service. `nodum serve` without
  `NODUM_WEB_DIST` serves the API alone.

## Data model

A mutable graph, one `nodes` table and one `edges` table:

- **Node** — a UUID, a `kind` (referencing the catalog), a plain-text `content`
  column (the embeddable body, full-text indexed), and a JSON `data` payload for
  the kind's typed fields.
- **Edge** — a UUID, a `kind`, and a directed link `from_uuid → to_uuid` between
  two distinct nodes, with a JSON `data` payload. The edge kind's signature
  constrains which node kinds the endpoints may be. Deleting a node cascades to
  its edges.
- **Typed, and evolvable.** Kinds live in the `node_kinds` / `edge_kinds` tables
  (name + `spec`), which also back the `kind` foreign keys. Editing a kind never
  retro-invalidates stored rows — validation is a write-time gate. Validation is
  soft in the service and cheap-hard in the database.

Retrieval is Postgres full-text (`tsvector` / `ts_rank`) over `content` for search
and a recursive CTE for subgraph expansion — **no embeddings yet**.

### Migrating an older database

`uv run nodum migrate` upgrades an earlier database in place, idempotently. It
runs the full chain: the MVP (open-type) upgrade if needed (adds `kind` columns,
drops type-as-node rows, backfills `Note` kinds from `data.type` into `role`);
adds the kind `spec` columns and backfills them from the defaults; and promotes
each node's `data.text` into the new `content` column (moving the full-text index
with it). Safe to re-run.

## License

MIT — see [LICENSE](LICENSE).
