# AGENTS.md — nodum

Agent-facing instructions for working in this repository. Read this before
editing anything here.

## What this repo is

`nodum` is a minimal **atomic-notes knowledge system**: a mutable PostgreSQL
graph of typed, UUID-keyed nodes and edges, with full-text search and
recursive subgraph expansion. All logic lives in one data-service layer; a
CLI, an HTTP API, and a minimal read-first web view are thin adapters over it.
This is an MVP — retrieval is Postgres full-text plus graph traversal, with no
embeddings yet.

## Architecture — the service layer is the spine

`nodum.service` is the single source of truth. Every operation (`add_node`,
`add_edge`, `get`, `search`, `expand`) and **all** validation live there. Each
function opens its own short-lived connection and commits, so the adapters stay
stateless and hold no logic of their own.

```mermaid
flowchart LR
    cli["nodum.cli (Typer)"] --> svc["nodum.service"]
    api["nodum.api (FastAPI)"] --> svc
    web["nodum.web (browser)"] --> api
    svc --> pg[("PostgreSQL")]
    style cli fill:#e6f0ff,color:#000
    style api fill:#e6f0ff,color:#000
    style web fill:#e6f0ff,color:#000
    style svc fill:#fff3cd,color:#000
    style pg fill:#d9f2d9,color:#000
```

- **`nodum.service`** — the data-service layer. The only place that talks to the
  database and the only place that validates input.
- **`nodum.models`** — the single pydantic I/O schema shared by every surface
  (`NodeOut`, `EdgeOut`, `SearchHit`, `NodeWithEdges`, `SearchResult`,
  `Subgraph`, plus the `AddNodeIn` / `AddEdgeIn` inputs). UUID and datetime
  fields render as strings under `model_dump(mode="json")`.
- **`nodum.cli`** (Typer) — each command calls one service function and prints
  the result as a single JSON object on **stdout**; human and error messages go
  to **stderr**.
- **`nodum.api`** (FastAPI) — each route calls one service function and returns
  the model via `model_dump(mode="json")` wrapped in a `JSONResponse`, with no
  `response_model` so keys are neither added nor reordered.
- **`nodum.web`** — a read-first single-page browser client of the API
  (`web.register(app)` mounts `GET /` and `/static`); it holds no logic and
  fetches the API's JSON read endpoints from the browser.
- **`nodum.db`** / **`nodum.settings`** — connection management (`dict_row`),
  idempotent schema init from `schema.sql`, and environment-loaded config.

**Keep the adapters mirrored.** The CLI and the API serialise the *same*
`model_dump(mode="json")` envelope, so identical data yields byte-identical JSON
across both surfaces; tests assert this CLI ↔ API parity. When you add or change
an operation, update the service first, then update **both** the CLI command and
the API route in lockstep — never let one surface drift ahead of the other.

Error contract: the service raises `NodeNotFound` (missing node) and
`ValueError` (bad input). The CLI maps both to a stderr line plus exit code 1;
the API maps `NodeNotFound` → 404 and `ValueError` → 422, each as a clean
`{"detail": ...}` body.

## Data model

A mutable JSONB graph. The schema (`nodum/schema.sql`) is idempotent — safe to
re-run on every start-up.

- **nodes** — `uuid` (PK, `gen_random_uuid()`), `data` JSONB (`CHECK (data ?
  'text')` — every node carries a primary `text`), `created_at`, `updated_at`.
  Indexed with a GIN index on `data` and a GIN full-text index on
  `to_tsvector('english', data ->> 'text')`.
- **edges** — `uuid` (PK), `from_uuid` / `to_uuid` (FK to `nodes`,
  `ON DELETE CASCADE`), `data` JSONB carrying the edge `type` (and any extra
  keys), `created_at`, `updated_at`. `CHECK (from_uuid <> to_uuid)` — no
  self-edges. Indexed on `from_uuid`, `to_uuid`, and `data`.
- **Type-as-node.** Types are not a separate concept: `add_node(text, type=...)`
  resolves-or-creates a type node (payload `{"text": <type>, "kind": "type"}`)
  and links the new node to it with an `is` edge. The type string is also kept
  on the node payload as `data.type` for convenience.
- **Retrieval.** `search` is Postgres full-text (`plainto_tsquery('english')`,
  AND of terms) ranked by `ts_rank`, best first. `expand` walks directed edges
  (`from_uuid → to_uuid`) outward from a seed set up to `depth` hops via a
  recursive CTE, then loads every node touched — serialised, that `Subgraph` is
  the context payload. `get` returns a node plus every edge incident on it in
  either direction.
- **No embeddings in this MVP** — no vector column, no embeddings table.

## Dev workflow

Prerequisites: Python ≥ 3.12, `uv`, and Docker (for the local Postgres). The
package version is derived from the git tag (`vX.Y.Z`) at build time by
hatch-vcs and is never committed.

Make targets (run `make help` for the live list):

| Target | Does |
|---|---|
| `make install` | `uv sync` (runtime deps) |
| `make dev-install` | `uv sync --all-groups` (adds dev deps) |
| `make db-up` / `make db-down` | start / stop the local Postgres container |
| `make init-db` | create the schema (`uv run nodum init-db`) |
| `make run` | run the CLI (`make run -- search foo`) |
| `make serve` | run the HTTP API + web view (uvicorn) |
| `make test` | run pytest |
| `make coverage` | pytest with line-coverage report |
| `make lint` | `ruff check` + `ruff format --check` |
| `make format` | `ruff check --fix` + `ruff format` |

- **Tests need a running Postgres.** The suite exercises the service against a
  live database, so `make db-up` (and `make init-db` on a fresh volume) must be
  up before `make test`. Test discovery is rooted at `tests/`.
- **Dev ports.** The HTTP API and web view serve on `127.0.0.1:8600`; the local
  Postgres is published on host port `5436` (→ container `5432`).
- **Config via environment.** The only required value is `NODUM_DATABASE_URL`
  (default `postgresql://nodum:nodum@localhost:5436/nodum`, matching
  docker-compose). `NODUM_API_HOST` / `NODUM_API_PORT` override the bind
  address. A local `.env` is read if present; copy `.env.example` to start.

## Conventions

- **Ruff** is the linter and formatter: line length 100, rule sets
  `E, F, I, UP, B, SIM`. Run `make format` before committing; CI runs
  `make lint`.
- **Docstrings on public APIs.** Document every public function, route, and
  model with a one-line summary plus args/returns where applicable. Don't
  annotate or document code you didn't change.
- **Service first, adapters in lockstep.** Put new logic and validation in
  `nodum.service`; expose it through the CLI and the API together so the
  parity tests stay green. Adapters must not add behaviour the service lacks.
- **MVP scope — deferred, do not build here:** embeddings (pgvector / hybrid
  retrieval), an LLM "gardener", contradiction reasoning, reranking, and auth.
  Keep changes inside the full-text + graph feature set.
