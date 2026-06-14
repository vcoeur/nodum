# AGENTS.md — nodum

Agent-facing instructions for working in this repository. Read this before
editing anything here.

## What this repo is

`nodum` is a minimal **atomic-notes knowledge system**: a mutable PostgreSQL
graph of typed, UUID-keyed nodes and edges, with full-text search and recursive
subgraph expansion. All logic lives in one data-service layer; a CLI, an HTTP
API, and a React single-page web UI are thin adapters over it. The full app ships
as a Docker image; the PyPI wheel is the CLI/library (no UI). See **Distribution**.

It is a **typed graph with a runtime-evolvable schema**. Earlier the graph was
open — a node carried a free `data.type` string and types were themselves nodes;
then kinds moved to a frozen code registry. Now kinds live **in the database**:
the `node_kinds` / `edge_kinds` tables store each kind's name plus a `spec` (its
field schema, or its `from_kinds → to_kinds` signature) as JSONB. Every node and
edge row carries a `kind` column referencing that catalog, and the catalog is
**editable at runtime** through the service / CLI / API. Every node also carries a
plain-text `content` column (the embeddable body) alongside its JSON `data`.

Retrieval is Postgres full-text plus graph traversal — no embeddings yet
(`content` is stored ready for them).

## The schema is the typed, evolvable layer

Kinds are **data**, stored in `node_kinds` / `edge_kinds` (name + `spec` JSONB).
`nodum.metamodel` holds the value types (`NodeKind`, `EdgeKind`, `FieldSpec`), the
validation logic, the spec (de)serialisation, and the **default seed catalog**
(`DEFAULT_NODE_KINDS` / `DEFAULT_EDGE_KINDS`) — but it is no longer the runtime
source of truth: the service loads a kind from the DB and hands the resolved
object to `metamodel.validate_*`. There is still **no per-kind table and no
per-kind model class** — instances all live in the one generic `nodes` table and
the one generic `edges` table.

**Adding a kind is a runtime operation**, not a code edit: `nodum node-kind add`
/ `edge-kind add` (or `POST /node-kinds` / `/edge-kinds`) insert a row into the
catalog; `init-db` seeds the defaults into an **empty** catalog only (it never
resurrects a deleted kind). Editing a kind (`… edit` / `PATCH`) replaces only the
attributes given; deleting (`… rm` / `DELETE`) is refused while the kind is in
use unless you resolve the using rows first — `--into <kind>` / `?into=<kind>`
reassigns them into another kind, or (edge kinds only) `--purge` / `?purge=true`
deletes the kind's edges. `into` and `purge` are mutually exclusive. `schema()`
annotates every kind with a `usage` count, so a client can show the impact before
deleting. **Validation is a write-time gate, never retroactive** — editing or
reassigning a kind never re-validates stored rows.

The rule for **when a new kind is warranted:** it must unlock a typed edge or a
typed field. If a distinction only labels or groups nodes, model it as a `role`
(the `Note.role` enum) or a tag, not a new kind.

### Architecture — service is the spine, metamodel is the contract

`nodum.service` is the single source of truth for every operation and all
validation. Each function opens its own short-lived connection and commits, so
the adapters stay stateless and hold no logic of their own.

```mermaid
flowchart LR
    cli["nodum.cli (Typer)"] --> svc["nodum.service"]
    api["nodum.api (FastAPI)"] --> svc
    web["nodum.web (browser)"] --> api
    svc --> mm["nodum.metamodel (value types + validation)"]
    svc --> pg[("PostgreSQL (kinds + instances)")]
    cli --> auth["nodum.auth (main password)"]
    api --> auth
    web --> auth
    auth --> pg
    style cli fill:#e6f0ff,color:#000
    style api fill:#e6f0ff,color:#000
    style web fill:#e6f0ff,color:#000
    style svc fill:#fff3cd,color:#000
    style mm fill:#ffe6cc,color:#000
    style auth fill:#ffd9d9,color:#000
    style pg fill:#d9f2d9,color:#000
```

- **`nodum.metamodel`** — the value types (`NodeKind` / `EdgeKind` / `FieldSpec`),
  the `validate_node` / `validate_edge` checks (against a *resolved* kind),
  spec (de)serialisation (`node_kind_from_spec` / `…_to_spec`, etc.),
  `schema_from(...)`, and the `DEFAULT_*_KINDS` seed catalog. No DB access — the
  service loads kinds and passes them in.
- **`nodum.service`** — the data-service layer. The only place that talks to the
  database; it loads the kind from the DB (`db.load_node_kind` / …) and calls the
  metamodel to validate input. Also owns **kind CRUD** (`add_node_kind`,
  `update_node_kind`, `delete_node_kind`, and the edge equivalents) and `schema()`.
- **`nodum.models`** — the single pydantic I/O schema shared by every surface
  (`NodeOut` — now with a top-level `content` — `EdgeOut`, `SearchHit`,
  `NodeWithEdges`, `SearchResult`, `Subgraph`, `Deleted`, `KindDeleted`, plus the
  `AddNodeIn` / `AddEdgeIn` / `UpdateNodeIn` / `UpdateEdgeIn` and the kind-definition
  inputs `NodeKindIn` / `NodeKindPatch` / `EdgeKindIn` / `EdgeKindPatch`). A node
  carries its `kind` + `content`; kind-specific metadata lives in `data`. UUID and
  datetime fields render as strings under `model_dump(mode="json")`.
- **`nodum.cli`** (Typer) — each command calls one service function and prints
  the result as a single JSON object on **stdout**; human and error messages go
  to **stderr**.
- **`nodum.api`** (FastAPI) — each route calls one service function and returns
  the model via `model_dump(mode="json")` wrapped in a `JSONResponse`, with no
  `response_model` so keys are neither added, dropped, nor reordered.
- **`nodum.web`** — serves the **built React SPA** (from `frontend/`) when
  `NODUM_WEB_DIST` points at a bundle: it mounts `/assets` and serves `index.html`
  at `GET /`. The SPA itself (in `frontend/`, TypeScript) is the browser client of
  the API. The bundle is **not in the wheel** — it ships in the Docker image. See
  **Web frontend** and **Distribution** below.
- **`nodum.auth`** — the single-main-password gate (transport-agnostic): argon2
  hashing, signed session tokens, and the `auth_secret` reads/writes. The CLI,
  API, and web call it; it imports no FastAPI. See **Authentication** below.
- **`nodum.db`** / **`nodum.settings`** — connection management (`dict_row`),
  idempotent schema init, kind seeding (`_seed_kind_specs` — empty tables only)
  and loading (`load_node_kind(s)` / `load_edge_kind(s)`), the migration chain
  (`migrate` = auth + MVP + kind-specs + content), and environment-loaded config.

## Node kinds (seeded defaults)

Seven seeded kinds, grouped (`entity` / `literature` / `note`) — the catalog is
runtime-evolvable, so these are the defaults, not a fixed set. Every kind defines
what its `content` means (`content_label`) and an optional field schema.

| Kind | Group | `content` is | Typed fields |
|---|---|---|---|
| `Person` | entity | name | `aliases` (list[str]), `born` (int) |
| `Organization` | entity | name | `aliases` (list[str]) |
| `Topic` | entity | label | `aliases` (list[str]) |
| `Entity` | entity | label | `entity_type` (str: place / concept / event / …), `aliases` (list[str]) |
| `Reference` | literature | citation | `citekey`, `authors` (list[str]), `year` (int), `venue`, `doi`, `url`, `ref_type` |
| `Literature` | literature | summary | `key_points` (list[str]) |
| `Note` | note | text | `role` (enum: claim / question / hypothesis / observation / synthesis / definition), `confidence` (float) |

`Entity` is the deliberate catch-all (place / concept / event / …) so the kind
set stays small. `Reference` is a bibliographic record; `Literature` is a note
*on* a source.

## Edge kinds (seeded defaults — signatures)

Each edge kind constrains its endpoints to specific node kinds — the
`from_kinds → to_kinds` signature, checked at create time. (Also runtime-evolvable.)

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

## Enforcement — soft in the service, cheap-hard in the DB

Validation is split deliberately:

- **Soft, in the service.** `metamodel.validate_node` / `validate_edge` enforce
  the full typed shape — non-empty `content`, required fields present, declared
  fields matching their type, enum choices, and edge endpoint kinds inside the
  signature (the kind itself is resolved from the DB first; an unknown kind is
  rejected there). A violation raises `metamodel.ValidationError` (a `ValueError`).
  Undeclared payload keys are allowed (forward-compatible).
- **Cheap-hard, in the database.** `schema.sql` enforces only the universal
  invariants that are free to check: the `kind` FK into the `node_kinds` /
  `edge_kinds` tables, `content NOT NULL` on every node, the `from_uuid` /
  `to_uuid` FKs into `nodes` with `ON DELETE CASCADE`, and
  `CHECK (from_uuid <> to_uuid)` (no self-edges). The endpoint-kind *signatures*
  are not enforced in SQL — that stays in the service.

**One-table invariant.** Keep one `nodes` table and one `edges` table. Typing is
a `kind` column plus the registry, never a table per kind. This is what lets
`expand` stay a single uniform recursive CTE over `edges` regardless of kind —
do not shard the graph into per-kind tables.

**Open process, closed format.** Every node carries a plain-text `content` body
(the FTS-indexed, LLM-readable surface, ready to embed later) *in addition to* its
typed fields. Authoring stays open — you write prose first — while the format
stays closed enough that machines can traverse and validate it (and the format
itself can evolve via kind CRUD).

Error contract: the service raises `NodeNotFound` / `EdgeNotFound` / `KindNotFound`
(missing rows), `KindInUse` (deleting a still-referenced kind without `into`), and
`ValueError` (bad input, including `ValidationError`). The CLI maps all of these to
a stderr line plus exit code 1. The API maps the not-found errors → 404,
`KindInUse` → 409, and `ValueError` → 422, each as a clean `{"detail": ...}` body.

## CRUD surfaces — `schema` is the contract

The service offers full CRUD plus query: `add_node` / `add_edge`, `get`,
`search` (optional `kind` filter), `expand` (optional `edge_kinds` filter),
`update_node` / `update_edge`, `delete_node` / `delete_edge`, `schema()`, and the
**kind CRUD** ops (`add_node_kind` / `update_node_kind` / `delete_node_kind` and
the edge equivalents). The CLI and API expose all of it; any client self-orients
by reading `schema()` first, which is why it is the contract every surface ships.

**CLI commands** (`uv run nodum <cmd>`):

| Command | Does |
|---|---|
| `add KIND CONTENT [--set k=v …]` | create a typed node |
| `link FROM TO EDGE_KIND [--set k=v …]` | create a typed directed edge |
| `get UUID` | a node plus its incident edges |
| `search QUERY [--kind K] [--limit N]` | ranked full-text search |
| `expand UUID [--depth N] [--edge-kind K …]` | seed → connected subgraph |
| `edit-node UUID [--content …] [--set k=v …]` | merge + re-validate a node |
| `edit-edge UUID [--set k=v …]` | merge an edge's payload |
| `rm-node UUID` | delete a node (edges cascade) |
| `rm-edge UUID` | delete one edge |
| `schema` | print the live schema (each kind carries a `usage` count) |
| `node-kind add/edit/rm NAME [--group/--content-label/--fields] [--into KIND]` | manage node kinds |
| `edge-kind add/edit/rm NAME [--from/--to/--symmetric/--fields] [--into KIND] [--purge]` | manage edge kinds (`rm --purge` deletes its edges too) |
| `auth set-password` | set/replace the main password (prompt or piped stdin) |
| `auth status` | report whether a password is configured (+ timestamp) |
| `auth ensure-password` | set the password from `NODUM_ADMIN_PASSWORD[_FILE]` if unconfigured (entrypoint bootstrap) |
| `init-db` | create schema + seed the default kind catalog |
| `migrate` | upgrade an older database in place (kinds, content, auth) |
| `serve` | run the HTTP API (serves the SPA when `NODUM_WEB_DIST` is set) |

`--set key=value` is repeatable (node/edge payload); each value is parsed as JSON,
falling back to the raw string (so `--set born=1815` is an int, `--set venue=Nature`
a string). `--fields` (kind CRUD) takes a JSON object mirroring `schema`'s `fields`
shape: `name → {type, required, choices, description}`. A field `type` is one of
`str`, `int`, `float`, `bool`, `list[str]`, `enum` (with `choices`), `date`
(a plain calendar date `YYYY-MM-DD`), or `datetime`. **`datetime` is canonical
UTC**: it is validated and stored as ISO-8601 with a `Z` suffix (an offset is
converted to UTC, a naive value is assumed UTC), and the SPA alone shows/enters it
in the browser's local time. `date` carries no timezone.

**API routes:** `POST /nodes`, `GET /nodes/{uuid}`, `PATCH /nodes/{uuid}`,
`DELETE /nodes/{uuid}`, `POST /edges`, `PATCH /edges/{uuid}`,
`DELETE /edges/{uuid}`, `GET /search`, `GET /expand`, `GET /schema`,
`POST /node-kinds`, `PATCH|DELETE /node-kinds/{name}` (delete takes `?into=`),
`POST /edge-kinds`, `PATCH|DELETE /edge-kinds/{name}` (delete takes `?into=` or
`?purge=true`) — all **gated by
`require_auth`** — plus the open `POST /auth/login`, `POST /auth/logout`,
`GET /auth/session` (the SPA's auth probe), `GET /healthz`, and (only when
`NODUM_WEB_DIST` is set) the open `GET /` + `/assets` that serve the SPA shell.
Edge-kind bodies accept the signature as `from` / `to` (the schema wire names).

**Keep the adapters mirrored.** The CLI and the API serialise the *same*
`model_dump(mode="json")` envelope, so identical data yields byte-identical JSON
across both surfaces; the parity tests assert this. When you add or change an
operation: update the service first, then update **both** the CLI command and the
API route in lockstep — never let one surface drift ahead of the other.

## Web frontend — the React SPA

The UI is a **React + Vite (TypeScript) SPA** in `frontend/`, a pure client of the
JSON API. It is schema-driven: it fetches `GET /schema` and renders its forms from
the live schema. A header switch toggles two views:

- **Graph** — create/edit a node by kind (a `content` body plus the kind's typed
  fields, rendered per type — incl. a date picker for `date` and a
  `datetime-local` control for `datetime`, which converts the stored UTC instant
  to/from the browser's local time), create an edge by type with endpoint pickers
  filtered to the signature, delete with a cascade-aware confirm, search, open a
  node, and render its subgraph as a dependency-free node-link **SVG diagram**.
- **Schema** — full CRUD over the runtime-evolvable schema itself: list the live
  node + edge kinds (each with a **usage** badge from `GET /schema`) and create /
  edit / delete them (`POST`/`PATCH`/`DELETE` on `/node-kinds` and `/edge-kinds`).
  A reusable field-schema editor builds each kind's `FieldSpec` map (the enum
  `choices` input appears only for `enum`); an edge kind's `from`/`to` are checkbox
  groups over the node kinds. Deleting an in-use kind surfaces the **409** and
  offers a choice: reassign the using rows into another kind (`into`, mirroring the
  CLI's `--into`) or — for an edge kind — remove its edges (`purge`, the `--purge`
  flag). Every kind mutation reloads `GET /schema`, so the Graph view's pickers and
  the header counts update immediately.

It holds no logic — every mutation goes through the API — so it stays in lockstep
with the CLI. Keep it driven by `GET /schema` (never hardcode kinds). In dev
(`npm run dev`) the SPA runs on Vite (5700) and **proxies the API to 8600**: the
proxied-prefix list in `frontend/vite.config.ts` must name **every** API prefix
(`/nodes`, `/edges`, `/node-kinds`, `/edge-kinds`, …) — a missing prefix 404s only
in dev, since production serves the SPA same-origin from FastAPI (no proxy).

- **Build:** `npm run build` (in `frontend/`) typechecks with `tsc` then emits
  `frontend/dist/` (hashed, same-origin assets); `nodum.web` serves that bundle
  from `NODUM_WEB_DIST`.
- **Dev:** `npm run dev` runs Vite on **5700**, proxying the API routes to FastAPI
  on 8600 (one origin, so the session cookie flows). Or `make serve-spa` builds
  the bundle and serves it through FastAPI on 8600.
- **Auth in the SPA:** the session cookie is HttpOnly (JS can't read it), so the
  app calls the open `GET /auth/session` → `{configured, authenticated}` to choose
  between the setup hint, the sign-in view, and the app; a 401 from any data call
  drops it back to sign-in.
- **CSP:** the production build emits only external same-origin scripts/styles, so
  `Content-Security-Policy: default-src 'self'` holds with no inline exceptions.
  Keep it so — no inline `<script>`, no inline `style=` props, no CSS-in-JS (use
  the `.css` files); the Vite config disables inline asset/preload emission.
- **Look & fonts:** a dark, editorial-cartographic theme lives entirely in
  `styles.css` (CSS variables; atmosphere is pure CSS gradients, never images or
  `data:` URIs). Fonts are **self-hosted via `@fontsource-variable/*`** (Fraunces
  display, Hanken Grotesk body, JetBrains Mono data), bundled same-origin by Vite
  — **never link a webfont CDN**, that would break the strict CSP. Per-group
  colours (graph nodes + schema cards) use a `data-group` attribute keyed to the
  `--group-*` variables; SVG node colours are presentation attributes, never
  `style=`.

## Distribution — Docker is the full app, PyPI is the CLI/library

Two artifacts, one codebase:

- **Docker image (full app).** A multi-stage `Dockerfile`: the node stage builds
  the SPA; the python stage `pip install`s the package and copies the bundle to
  `/app/web-dist` (`NODUM_WEB_DIST`). The entrypoint (`docker/entrypoint.sh`)
  waits for Postgres, runs `init-db`, then `auth ensure-password`, then `serve` on
  `0.0.0.0`. A deploy is: declare the image, point `NODUM_DATABASE_URL` at a
  Postgres, provide a password secret — done. `docker-compose.example.yml` is a
  turnkey start. The image does **not** bundle Postgres. The build passes the
  version as `SETUPTOOLS_SCM_PRETEND_VERSION` (no `.git` in the build context).
- **PyPI wheel (CLI / library).** Ships the service + CLI + API + `schema.sql`
  only — **no UI**. `pip install nodum` gives the `nodum` command and the API;
  `nodum serve` without `NODUM_WEB_DIST` serves the API with no web view.

## Authentication — one main password

The network surfaces (HTTP API + web view) are gated by a **single main
password**, initialised from the CLI on the machine where nodum runs. The local
CLI is trusted and never logs in — it *sets* the secret. Until a password is set
the install is **locked**: protected routes return `503` pointing at the CLI.

- **Storage.** A single-row `auth_secret` table holds an argon2 hash
  (`argon2-cffi`) of the password plus a random `signing_key`. argon2 runs only
  at login and at `set-password` — never on the per-request path.
- **Tokens (dual auth).** Login verifies the password, then mints a session token
  signed with the `signing_key` (`itsdangerous`, 7-day expiry). Browsers carry it
  in an **HttpOnly, Secure, SameSite=Strict cookie**; API/CLI clients carry it as
  an `Authorization: Bearer` token. `require_auth` checks the **cookie first,
  then the Bearer header**, verifying only the cheap HMAC signature.
- **Rotation.** `set-password` recomputes the hash but **preserves the signing
  key**, so changing the password does not invalidate live sessions.
- **Defence in depth.** Every response carries `Content-Security-Policy:
  default-src 'self'`, `X-Content-Type-Options: nosniff`, and
  `X-Frame-Options: DENY` (all web assets are same-origin, so the CSP needs no
  inline exceptions).
- **Open routes:** `GET /healthz`, `POST /auth/login`, `POST /auth/logout`,
  `GET /auth/session`, and (when the SPA is mounted) `GET /` + `/assets`.
  Everything else requires a valid session. The SPA reads `GET /auth/session`
  (`{configured, authenticated}`) to drive its login state — there is no
  server-rendered login page.
- **Bootstrap.** `auth ensure-password` sets the password from
  `NODUM_ADMIN_PASSWORD_FILE` / `NODUM_ADMIN_PASSWORD` only when unconfigured —
  used by the Docker entrypoint so a deploy is hands-off (a later manual change is
  not clobbered on restart).
- **Config.** `NODUM_COOKIE_SECURE=1` marks the cookie Secure (set it behind a
  TLS-terminating reverse proxy; off by default for local HTTP dev).

## Data model

A mutable JSONB graph. The schema (`nodum/schema.sql`) is idempotent — safe to
re-run on every start-up.

- **node_kinds / edge_kinds** — the kind catalog: `name TEXT PRIMARY KEY` + a
  `spec JSONB NOT NULL` (node: `{group, content_label, fields}`; edge:
  `{from, to, symmetric, fields}`). Seeded with the defaults into an empty table;
  editable at runtime via kind CRUD. The `kind` FKs point at `name`.
- **auth_secret** — single-row table (`id BOOLEAN PK CHECK (id)`) holding the
  argon2 `password_hash` + random `signing_key` + `updated_at`. Empty until
  `nodum auth set-password` writes it. `nodum.db.migrate_auth` creates it on an
  already-initialised database (idempotent). See **Authentication**.
- **nodes** — `uuid` (PK, `gen_random_uuid()`), `kind` (FK → `node_kinds`),
  `content TEXT NOT NULL` (the embeddable body), `data` JSONB (kind metadata,
  default `{}`), `created_at`, `updated_at`. Indexed with a GIN index on `data`, a
  GIN full-text index on `to_tsvector('english', content)`, and a btree on `kind`.
- **edges** — `uuid` (PK), `kind` (FK → `edge_kinds`), `from_uuid` / `to_uuid`
  (FK → `nodes`, `ON DELETE CASCADE`), `data` JSONB, `created_at`,
  `updated_at`. `CHECK (from_uuid <> to_uuid)`. Indexed on `kind`, `from_uuid`,
  `to_uuid`, and `data`.
- **Retrieval.** `search` is Postgres full-text (`plainto_tsquery('english')`,
  AND of terms) over `content`, ranked by `ts_rank`, with an optional `kind`
  filter. `expand` walks directed edges (`from_uuid → to_uuid`) outward from a
  seed set up to `depth` hops via a recursive CTE — optionally restricted to given
  edge kinds — then loads every node touched; serialised, that `Subgraph` is the
  context payload. `get` returns a node plus every edge incident on it in either
  direction.
- **No embeddings yet** — no vector column; `content` is the column they will
  target.

### Migration of older databases

`nodum.db.migrate` (CLI `migrate`) upgrades any older database in place,
idempotently, as a chain: `migrate_auth` (the `auth_secret` table); `migrate_mvp`
(only on a pre-typed MVP DB — adds `kind` columns, drops type-as-node rows whose
`is` edges cascade, backfills `Note` kinds from `data.type` into `role`, enforces
the kind FKs); `migrate_kind_specs` (adds the `spec` column to the kind tables and
backfills it from the defaults — the name-only → evolvable upgrade); and
`migrate_content` (promotes `data.text` into the `content` column, drops the old
`data ? 'text'` check, moves the FTS index onto `content`). Each step is a no-op
once applied.

## Dev workflow

Prerequisites: Python ≥ 3.12, `uv`, Node ≥ 24 + npm (for the frontend), and
Docker (for the local Postgres and the image). The package version is derived
from the git tag (`vX.Y.Z`) at build time by hatch-vcs and is never committed.

Make targets — `make help` lists them live. Two install/run pairs: **dev**
(`make dev-install` then `make dev-run`) and a bare-host **deploy** (`make install`
then `make run`). The real deployed unit is the **Docker image**
(`make docker-build`) — it bakes the built SPA in, sets `NODUM_WEB_DIST`, and
self-bootstraps the DB + admin password via its entrypoint. Everything else is an
explicit building block.

| Target | Does |
|---|---|
| `make install` | install runtime deps (`uv sync`) — bare-host deploy |
| `make run` | serve the HTTP API; also serves the SPA when `NODUM_WEB_DIST` points at a built bundle (`uv run nodum serve`) |
| `make dev-install` | install everything for dev: `uv sync --all-groups` + frontend `npm ci` |
| `make dev-run` | bring up the DB, then run the API (:8600) + Vite frontend (:5700) together; stops both when either exits |
| `make cli` | run the nodum CLI (`make cli -- search foo`) |
| `make db-up` / `make db-down` | start / stop the local Postgres container (`db-up` waits until healthy) |
| `make db-check` | fail fast unless the Postgres container is running |
| `make init-db` | create the schema + seed kind tables (`uv run nodum init-db`) |
| `make frontend-install` | `npm ci` in `frontend/` |
| `make frontend-dev` | Vite dev server on 5700 (proxies the API to 8600) |
| `make frontend-build` | build the SPA into `frontend/dist` |
| `make serve-spa` | build the SPA and serve it via FastAPI on 8600 |
| `make docker-build` | build the full-app Docker image — the real deployed unit (API + baked-in UI, self-bootstrapping) |
| `make test` | run pytest |
| `make coverage` | pytest with line-coverage report |
| `make lint` | `ruff check` + `ruff format --check` |
| `make format` | `ruff check --fix` + `ruff format` |

- **Tests need a running Postgres.** The suite exercises the service against a
  live database (schema created once per session; the graph truncated before
  each test), so `make db-up` must be up before `make test`. Test discovery is
  rooted at `tests/`. The Python suite does not build or need the SPA.
- **Dev ports.** The HTTP API serves on `127.0.0.1:8600`; the Vite dev server on
  `5700` (preview `5701`); the local Postgres is published on host port `5436`
  (→ container `5432`).
- **Config via environment.** The only required value is `NODUM_DATABASE_URL`
  (default `postgresql://nodum:nodum@localhost:5436/nodum`, matching
  docker-compose). `NODUM_API_HOST` / `NODUM_API_PORT` override the bind address
  (the image sets host `0.0.0.0`); `NODUM_COOKIE_SECURE=1` marks the cookie Secure
  (behind TLS); `NODUM_WEB_DIST` points at the built SPA (set in the image);
  `NODUM_ADMIN_PASSWORD_FILE` / `NODUM_ADMIN_PASSWORD` seed the password on first
  boot (see `auth ensure-password`). A local `.env` is read if present; copy
  `.env.example` to start.

## Documentation

The public docs site is **MkDocs Material** — sources under `docs/` plus `mkdocs.yml` at the repo
root, deployed to **<https://nodum.vcoeur.com>** by `.github/workflows/docs.yml` on every push to
`main` that touches `docs/**` or `mkdocs.yml`.

- **Edit `docs/`, never the generated `site/`** (the build output is gitignored).
- The build must pass **`mkdocs build --strict`** (it fails on broken links and bad nav refs) — the
  Pages workflow runs exactly that. Preview/build locally with
  `uv run --with "mkdocs-material==9.5.49" mkdocs serve` (or `… mkdocs build --strict`).
- `docs/CNAME` pins the custom domain. `docs/legal.md` is the GitHub-Pages mentions-légales page,
  kept aligned with the knoten/quelle/condash docs-site legal pages — and it publishes **no
  retention durations**.
- When CLI verbs, API routes, kinds, or the distribution model change, update the matching page in
  `docs/` in the same PR so the site stays in step with `schema()` and this file.

## Conventions

- **Ruff** is the linter and formatter: line length 100, rule sets
  `E, F, I, UP, B, SIM`. Run `make format` before committing; CI runs
  `make lint`.
- **Docstrings on public APIs.** Document every public function, route, model,
  and metamodel helper with a one-line summary plus args/returns where
  applicable. Don't annotate or document code you didn't change.
- **Service first, adapters in lockstep.** New behaviour and validation go in
  `nodum.service`; expose it through the CLI and the API together so the parity
  tests stay green. Adapters must not add behaviour the service lacks. A new
  *default* kind is a `DEFAULT_*_KINDS` edit in `metamodel.py` (it seeds fresh
  installs); kinds are otherwise created at runtime through kind CRUD.
- **Deferred — do not build here:** embeddings (pgvector / hybrid retrieval —
  `content` is the column they will target), an LLM "gardener", contradiction
  reasoning, reranking, and multi-user accounts / roles (auth is a single shared
  main password — see **Authentication**). Keep changes inside the typed
  full-text + graph feature set.
