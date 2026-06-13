---
title: Commands · nodum
description: The nodum CLI and HTTP API reference — every verb and route, the --set field syntax, the byte-identical JSON envelopes, the error contract, and the schema self-orientation call.
---

# Commands

The CLI and the HTTP API are thin adapters over one `nodum.service` layer, sharing a single pydantic
I/O schema. For identical data they emit **byte-identical JSON** — parity tests assert it — so you can
prototype with the CLI and ship against the API without surprises.

## CLI

Run a command with `nodum <cmd>` (installed) or `uv run nodum <cmd>` / `make run -- <cmd>` (from a
checkout). Every command prints a **single JSON object to stdout**; human messages and errors go to
**stderr**, so `nodum … > out.json` captures clean JSON.

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
| `init-db` | create the schema + seed the kind lookup tables |
| `migrate` | upgrade a pre-typed (MVP) database |
| `serve` | run the HTTP API (serves the SPA when `NODUM_WEB_DIST` is set) |

### The `--set` field syntax

`--set key=value` is **repeatable**, and each value is parsed as JSON, falling back to the raw string
if that fails. So scalars and structured values both work:

```bash
nodum add Person "Ada Lovelace" --set born=1815                 # 1815 is an int
nodum add Reference "Menabrea/Lovelace, Sketch of the Analytical Engine (1843)" \
  --set year=1843 \
  --set 'authors=["Ada Lovelace","L. F. Menabrea"]' \           # a JSON list
  --set venue=Nature                                            # a bare string
```

`TEXT` (the first positional) sets the node's universal `text`; `--set` carries the kind's typed
fields into `data`. `edit-node` merges: `--text` replaces the text, `--set` merges into `data`, and
the result is re-validated against the kind.

### Examples

```bash
nodum link <person-uuid> <reference-uuid> AuthorOf      # Person → Reference (signature-checked)
nodum get <uuid>                                        # node + every incident edge
nodum search "analytical engine" --kind Reference --limit 10
nodum expand <uuid> --depth 2 --edge-kind AuthorOf --edge-kind cites
nodum schema                                            # the whole metamodel contract
```

## HTTP API

FastAPI. Start it with `nodum serve` (or the Docker image). Every data route is **gated by
authentication**; each returns the same JSON envelope the CLI prints, via `model_dump(mode="json")`
with no `response_model`, so keys are neither added, dropped, nor reordered.

| Method & path | Does |
|---|---|
| `POST /nodes` | create a typed node (`{"kind","text","data"}`) |
| `GET /nodes/{uuid}` | a node plus its incident edges |
| `PATCH /nodes/{uuid}` | merge `{"text","data"}` into a node, re-validate |
| `DELETE /nodes/{uuid}` | delete a node; edges cascade (returns the count) |
| `POST /edges` | create an edge (`{"kind","from_uuid","to_uuid","data"}`) |
| `PATCH /edges/{uuid}` | merge `{"data"}` into an edge (kind + endpoints fixed) |
| `DELETE /edges/{uuid}` | delete one edge (returns the count) |
| `GET /search?q=&kind=&limit=` | ranked full-text search (`limit` default 20) |
| `GET /expand?seed=&depth=&edge_kind=` | seed → subgraph (`depth` default 1; `edge_kind` repeatable) |
| `GET /schema` | the metamodel contract |

Open (unauthenticated) routes: `POST /auth/login`, `POST /auth/logout`, `GET /auth/session`,
`GET /healthz`, and — only when the SPA is mounted (`NODUM_WEB_DIST` set) — `GET /` and `/assets`.

### Examples

```bash
# create a node
curl -s -X POST http://127.0.0.1:8600/nodes \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer <token>' \
  -d '{"kind":"Note","text":"Spaced repetition improves long-term retention","data":{"role":"claim"}}'

# search, and expand a subgraph two hops out along one edge kind
curl -s 'http://127.0.0.1:8600/search?q=retention&kind=Note&limit=10' -H 'authorization: Bearer <token>'
curl -s 'http://127.0.0.1:8600/expand?seed=<uuid>&depth=2&edge_kind=cites' -H 'authorization: Bearer <token>'

# the contract — one call self-orients a client
curl -s http://127.0.0.1:8600/schema -H 'authorization: Bearer <token>'
```

## Authentication

The data routes require a valid session (see [Install &amp; run](install.md#authentication) for the
model). Obtain a token by logging in with the main password, then send it as a Bearer header:

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8600/auth/login \
  -H 'content-type: application/json' \
  -d '{"password":"change-me"}' | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')

curl -s http://127.0.0.1:8600/schema -H "authorization: Bearer $TOKEN"
```

Browsers get the token as an HttpOnly cookie set by `POST /auth/login` and never handle it directly;
the SPA calls `GET /auth/session` → `{configured, authenticated}` to choose between the setup hint,
the sign-in view, and the app.

## Error contract

The service raises `NodeNotFound` / `EdgeNotFound` for missing rows and `ValueError` (including the
metamodel `ValidationError`) for bad input. The surfaces map them consistently:

| Condition | CLI | API |
|---|---|---|
| Missing node/edge | stderr message, exit code 1 | `404` `{"detail": …}` |
| Invalid input (bad kind, missing field, bad endpoint) | stderr message, exit code 1 | `422` `{"detail": …}` |
| Unauthenticated | — (the CLI is trusted, talks to the DB directly) | `401` |
| No main password set yet | — | `503` (locked; set one with `nodum auth set-password`) |

## The `schema` contract

`nodum schema` (CLI) and `GET /schema` (API) return the live metamodel — every node kind with its
field schema, every edge kind with its `from → to` signature. It is the one call a client or an agent
makes first to self-orient before any write, which is why every surface ships it. See
[Concepts](concepts.md) for what the kinds and signatures mean.
