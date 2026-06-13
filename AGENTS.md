# AGENTS.md — nodum

Agent-facing instructions for working in this repository. Read this before
editing anything here.

## What this repo is

`nodum` is a minimal **atomic-notes knowledge system**: a mutable PostgreSQL
graph of typed, UUID-keyed nodes and edges, with full-text search and recursive
subgraph expansion. All logic lives in one data-service layer; a CLI, an HTTP
API, and a React single-page web UI are thin adapters over it. The full app ships
as a Docker image; the PyPI wheel is the CLI/library (no UI). See **Distribution**.

It is a **typed graph**. Earlier the graph was open — a node carried a free
`data.type` string and types were themselves nodes. That is gone. Kinds now live
in a code registry, `nodum.metamodel`: a curated set of node kinds (each with a
field schema) and edge kinds (each with a `from_kinds → to_kinds` signature).
Every node and edge row carries a `kind` column that references that registry.

Retrieval is Postgres full-text plus graph traversal — no embeddings.

## The metamodel is the typed layer

`nodum.metamodel` is the single source of truth for kinds. It is plain code: two
dicts, `NODE_KINDS` and `EDGE_KINDS`, built from frozen dataclasses (`NodeKind`,
`EdgeKind`, `FieldSpec`). There is **no per-kind table and no per-kind model
class** — instances all live in the one generic `nodes` table and the one
generic `edges` table, and the metamodel is the typed contract laid over them.

**Adding a kind is a registry edit.** To introduce a node or edge kind you edit
`NODE_KINDS` / `EDGE_KINDS` in `metamodel.py` — nothing else structural changes.
`nodum.db` seeds the new name into the `node_kinds` / `edge_kinds` lookup tables
(run `init-db`, or `migrate` on an existing database) so the DB-level FK on
`kind` stays in step with the registry.

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
    svc --> mm["nodum.metamodel (typed registry)"]
    svc --> pg[("PostgreSQL")]
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

- **`nodum.metamodel`** — the typed registry: node-kind field schemas, edge-kind
  endpoint signatures, the `validate_node` / `validate_edge` checks, and
  `schema()`, which serialises the whole thing as the machine-readable contract.
- **`nodum.service`** — the data-service layer. The only place that talks to the
  database and the only place that calls the metamodel to validate input.
- **`nodum.models`** — the single pydantic I/O schema shared by every surface
  (`NodeOut`, `EdgeOut`, `SearchHit`, `NodeWithEdges`, `SearchResult`,
  `Subgraph`, `Deleted`, plus the `AddNodeIn` / `AddEdgeIn` / `UpdateNodeIn` /
  `UpdateEdgeIn` inputs). A node/edge carries its `kind`; kind-specific fields
  live in `data`. UUID and datetime fields render as strings under
  `model_dump(mode="json")`.
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
  idempotent schema init + kind seeding from `schema.sql`, the MVP + auth
  migrations, and environment-loaded config.

## Node kinds

Seven kinds, grouped (`entity` / `literature` / `note`). Every kind defines what
its universal `text` means (`text_label`) and an optional field schema.

| Kind | Group | `text` is | Typed fields |
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

## Edge kinds (signatures)

Each edge kind constrains its endpoints to specific node kinds — the
`from_kinds → to_kinds` signature, checked at create time.

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
  the full typed shape — known kind, non-empty `text`, required fields present,
  declared fields matching their type, enum choices, and edge endpoint kinds
  inside the signature. A violation raises `metamodel.ValidationError` (a
  `ValueError`). Undeclared payload keys are allowed (forward-compatible).
- **Cheap-hard, in the database.** `schema.sql` enforces only the universal
  invariants that are free to check: the `kind` FK into the `node_kinds` /
  `edge_kinds` lookup tables, `CHECK (data ? 'text')` on every node, the
  `from_uuid` / `to_uuid` FKs into `nodes` with `ON DELETE CASCADE`, and
  `CHECK (from_uuid <> to_uuid)` (no self-edges). The endpoint-kind *signatures*
  are not enforced in SQL — that stays in the service.

**One-table invariant.** Keep one `nodes` table and one `edges` table. Typing is
a `kind` column plus the registry, never a table per kind. This is what lets
`expand` stay a single uniform recursive CTE over `edges` regardless of kind —
do not shard the graph into per-kind tables.

**Open process, closed format.** Every node carries a universal natural-language
`text` (the FTS-indexed, LLM-readable surface) *in addition to* its typed
fields. Authoring stays open — you write prose first — while the format stays
closed enough that machines can traverse and validate it.

Error contract: the service raises `NodeNotFound` / `EdgeNotFound` (missing
rows) and `ValueError` (bad input, including `ValidationError`). The CLI maps all
three to a stderr line plus exit code 1. The API maps `NodeNotFound` /
`EdgeNotFound` → 404 and `ValueError` → 422, each as a clean `{"detail": ...}`
body.

## CRUD surfaces — `schema` is the contract

The service offers full CRUD plus query: `add_node` / `add_edge`, `get`,
`search` (optional `kind` filter), `expand` (optional `edge_kinds` filter),
`update_node` / `update_edge`, `delete_node` / `delete_edge`, and `schema()`.
The CLI and API expose all of it; any client can self-orient by reading
`schema()` first, which is why it is the contract every surface ships.

**CLI commands** (`uv run nodum <cmd>`):

| Command | Does |
|---|---|
| `add KIND TEXT [--set k=v …]` | create a typed node |
| `link FROM TO EDGE_KIND [--set k=v …]` | create a typed directed edge |
| `get UUID` | a node plus its incident edges |
| `search QUERY [--kind K] [--limit N]` | ranked full-text search |
| `expand UUID [--depth N] [--edge-kind K …]` | seed → connected subgraph |
| `edit-node UUID [--text …] [--set k=v …]` | merge + re-validate a node |
| `edit-edge UUID [--set k=v …]` | merge an edge's payload |
| `rm-node UUID` | delete a node (edges cascade) |
| `rm-edge UUID` | delete one edge |
| `schema` | print the metamodel contract |
| `auth set-password` | set/replace the main password (prompt or piped stdin) |
| `auth status` | report whether a password is configured (+ timestamp) |
| `auth ensure-password` | set the password from `NODUM_ADMIN_PASSWORD[_FILE]` if unconfigured (entrypoint bootstrap) |
| `init-db` | create schema + seed kind tables |
| `migrate` | upgrade a pre-typed (MVP) database |
| `serve` | run the HTTP API (serves the SPA when `NODUM_WEB_DIST` is set) |

`--set key=value` is repeatable; each value is parsed as JSON, falling back to
the raw string (so `--set born=1815` is an int, `--set venue=Nature` a string).

**API routes:** `POST /nodes`, `GET /nodes/{uuid}`, `PATCH /nodes/{uuid}`,
`DELETE /nodes/{uuid}`, `POST /edges`, `PATCH /edges/{uuid}`,
`DELETE /edges/{uuid}`, `GET /search`, `GET /expand`, `GET /schema` — all
**gated by `require_auth`** — plus the open `POST /auth/login`,
`POST /auth/logout`, `GET /auth/session` (the SPA's auth probe), `GET /healthz`,
and (only when `NODUM_WEB_DIST` is set) the open `GET /` + `/assets` that serve
the SPA shell.

**Keep the adapters mirrored.** The CLI and the API serialise the *same*
`model_dump(mode="json")` envelope, so identical data yields byte-identical JSON
across both surfaces; the parity tests assert this. When you add or change an
operation: update the service first, then update **both** the CLI command and the
API route in lockstep — never let one surface drift ahead of the other.

## Web frontend — the React SPA

The UI is a **React + Vite (TypeScript) SPA** in `frontend/`, a pure client of the
JSON API. It is schema-driven: it fetches `GET /schema` and renders its forms from
the metamodel (create/edit a node by kind, create an edge by type with endpoint
pickers filtered to the signature, delete with a cascade-aware confirm, search,
open a node, and render its subgraph as a dependency-free node-link **SVG
diagram**). It holds no logic — every mutation goes through the API — so it stays
in lockstep with the CLI. Keep it driven by `GET /schema` (never hardcode kinds).

- **Build:** `npm run build` (in `frontend/`) typechecks with `tsc` then emits
  `frontend/dist/` (hashed, same-origin assets); `nodum.web` serves that bundle
  from `NODUM_WEB_DIST`.
- **Dev:** `npm run dev` runs Vite on **5700**, proxying the API routes to FastAPI
  on 8600 (one origin, so the session cookie flows). Or `make dev-web` builds the
  bundle and serves it through FastAPI on 8600.
- **Auth in the SPA:** the session cookie is HttpOnly (JS can't read it), so the
  app calls the open `GET /auth/session` → `{configured, authenticated}` to choose
  between the setup hint, the sign-in view, and the app; a 401 from any data call
  drops it back to sign-in.
- **CSP:** the production build emits only external same-origin scripts/styles, so
  `Content-Security-Policy: default-src 'self'` holds with no inline exceptions.
  Keep it so — no inline `<script>`, no inline `style=` props, no CSS-in-JS (use
  the `.css` files); the Vite config disables inline asset/preload emission.

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

- **node_kinds / edge_kinds** — `TEXT PRIMARY KEY` lookup tables, seeded from the
  metamodel; the `kind` FKs point here.
- **auth_secret** — single-row table (`id BOOLEAN PK CHECK (id)`) holding the
  argon2 `password_hash` + random `signing_key` + `updated_at`. Empty until
  `nodum auth set-password` writes it. `nodum.db.migrate_auth` creates it on an
  already-initialised database (idempotent). See **Authentication**.
- **nodes** — `uuid` (PK, `gen_random_uuid()`), `kind` (FK → `node_kinds`),
  `data` JSONB (`CHECK (data ? 'text')`), `created_at`, `updated_at`. Indexed
  with a GIN index on `data`, a GIN full-text index on
  `to_tsvector('english', data ->> 'text')`, and a btree index on `kind`.
- **edges** — `uuid` (PK), `kind` (FK → `edge_kinds`), `from_uuid` / `to_uuid`
  (FK → `nodes`, `ON DELETE CASCADE`), `data` JSONB, `created_at`,
  `updated_at`. `CHECK (from_uuid <> to_uuid)`. Indexed on `kind`, `from_uuid`,
  `to_uuid`, and `data`.
- **Retrieval.** `search` is Postgres full-text (`plainto_tsquery('english')`,
  AND of terms) over `data ->> 'text'`, ranked by `ts_rank`, with an optional
  `kind` filter. `expand` walks directed edges (`from_uuid → to_uuid`) outward
  from a seed set up to `depth` hops via a recursive CTE — optionally restricted
  to given edge kinds — then loads every node touched; serialised, that
  `Subgraph` is the context payload. `get` returns a node plus every edge
  incident on it in either direction.
- **No embeddings** — no vector column, no embeddings table.

### Migration from the MVP

`nodum.db.migrate_mvp` (CLI `migrate`) upgrades a pre-typed MVP database in
place, idempotently: it adds the `kind` columns, seeds the lookup tables, drops
the MVP type-as-node rows (their `is` edges cascade), backfills `kind` (content
nodes → `Note` with their old `data.type` carried into `role`; edges from their
old `data.type`, else `mentions`), then enforces NOT NULL and the lookup FKs.

## Dev workflow

Prerequisites: Python ≥ 3.12, `uv`, Node ≥ 24 + npm (for the frontend), and
Docker (for the local Postgres and the image). The package version is derived
from the git tag (`vX.Y.Z`) at build time by hatch-vcs and is never committed.

Make targets (run `make help` for the live list):

| Target | Does |
|---|---|
| `make install` | `uv sync` (runtime deps) |
| `make dev-install` | `uv sync --all-groups` (adds dev deps) |
| `make db-up` / `make db-down` | start / stop the local Postgres container |
| `make init-db` | create the schema + seed kind tables (`uv run nodum init-db`) |
| `make run` | run the CLI (`make run -- search foo`) |
| `make serve` | run the HTTP API (uvicorn; SPA when `NODUM_WEB_DIST` is set) |
| `make frontend-install` | `npm ci` in `frontend/` |
| `make frontend-dev` | Vite dev server on 5700 (proxies the API to 8600) |
| `make frontend-build` | build the SPA into `frontend/dist` |
| `make dev-web` | build the SPA and serve it via FastAPI on 8600 |
| `make docker-build` | build the full-app Docker image |
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
  and metamodel entry with a one-line summary plus args/returns where
  applicable. Don't annotate or document code you didn't change.
- **Metamodel first, service next, adapters in lockstep.** A new kind is a
  `metamodel.py` registry edit; new behaviour and validation go in
  `nodum.service`; expose it through the CLI and the API together so the parity
  tests stay green. Adapters must not add behaviour the service lacks.
- **Deferred — do not build here:** embeddings (pgvector / hybrid retrieval), an
  LLM "gardener", contradiction reasoning, reranking, multi-user accounts / roles
  (auth is a single shared main password — see **Authentication**), and
  runtime-editable kinds (the metamodel stays a code registry). Keep changes
  inside the typed full-text + graph feature set.
