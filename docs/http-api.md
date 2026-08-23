---
title: HTTP API · nodum
description: The full nodum HTTP surface — every route, its method, handler, auth class, and one line on what it does, generated from the live route table.
---

# HTTP API

The route reference for `nodum serve` — the JSON API under `/api` plus the
`/healthz` liveness probe, exactly as `nodum.http_api` builds them.

This page is **generated** from the live route table — the `api_routes` list
inside `nodum.http_api.create_app` — and is committed so the docs site ships
it without a build step. Never edit it by hand; when the route table changes
(a route added, renamed, re-verbed, or removed), regenerate and commit:

```sh
uv run python scripts/gen-http-api-docs.py
```

`tests/test_docs.py` runs that exact command and fails if the committed page
is not what the generator produces, so the route table and this page cannot
drift apart silently.

## Auth model

The session gate is one rule: every `/api` route requires a valid session —
reads included — with exactly these exemptions:

* `POST /api/login` — **open**: the route that *makes* the session (name +
  password, argon2id; sets the `HttpOnly; SameSite=Strict` session cookie,
  and a failed-login lockout throttles brute force).
* `GET /api/download/{token}` and `PUT /api/uploads/{token}` — **token**:
  the single-use, minutes-long capability URL *is* the authorisation, minted
  by `nodum.urls` against a principal that already passed the session gate.
  No other route carries its own credential.

`/healthz` and the static UI at `/` are open, but neither is part of the
`/api` surface. Everything else is **session**-gated: the session middleware
verifies the cookie into the request scope, and every handler binds its
principal from there — no request field, header, or query parameter can set
an identity. The `Host` check, the same-origin proof for state-changing
requests, and the content-type rule apply to every route independently of
this gate.

The method column lists the methods the route table configured; Starlette
answers `HEAD` for any route configured `GET`.

The tables below list **69 routes**, grouped by family.


### Session

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| POST | `/api/login` | `login` | open — makes the session | Password login — the one ``/api`` route outside the session gate. |
| POST | `/api/logout` | `logout` | session | Log out: drop the server-side session row and clear the cookie. |

### Catalog & schema

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| GET | `/api/types` | `get_types` | session | The live type catalog (node types and edge types). |
| GET | `/api/schema/{type}` | `get_schema` | session | One node or edge type's catalog entry, including its JSON schema. |

### Nodes

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| GET | `/api/nodes` | `list_nodes` | session | List nodes in creation order, optionally filtered. |
| POST | `/api/nodes` | `create_node` | session | Create a node. It lands ``active``: this is the human surface. |
| GET | `/api/nodes/resolve` | `resolve_nodes` | session | Resolve ``[[wikilink]]`` titles to node ids — exact, casefolded, batch. |
| GET | `/api/nodes/{id}` | `get_node` | session | One node, or its active-edge neighborhood when ``depth`` is given. |
| PATCH | `/api/nodes/{id}` | `update_node` | session | Update the named fields of a node, and only those. |
| GET | `/api/nodes/{id}/children` | `list_children` | session | A node's children in ``position`` order (the document tree). |
| GET | `/api/nodes/{id}/history` | `node_history` | session | A node's version snapshots, chronological. |
| POST | `/api/nodes/{id}/archive` | `archive_node` | session | Retire a node (``active`` → ``archived``) — the service's human tier. |

### Edges

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| GET | `/api/edges` | `list_edges` | session | List edges, optionally filtered by incident node, type, state, or validity window. |
| POST | `/api/edges` | `create_edge` | session | Create a typed, directed edge between two nodes. |
| POST | `/api/edges/{id}/archive` | `archive_edge` | session | Retire an edge (``active`` → ``archived``) — the service's human tier. |

### Search & ask

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| GET | `/api/search` | `search` | session | Hybrid search (BM25 + vector, RRF-fused) with optional graph expansion. |
| POST | `/api/ask` | `ask` | session | Answer a question from the graph, with citations, or say it could not. |
| POST | `/api/summarize` | `summarize` | session | Summarise a node and its neighbourhood. Reads only (design E1). |
| GET | `/api/links/suggest` | `suggest_links` | session | Title-prefix candidates for the editor's ``[[`` autocomplete. |

### Graph

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| GET | `/api/graph/subgraph` | `get_subgraph` | session | A bounded, filtered neighborhood — node and edge caps both applied while walking. |
| GET | `/api/graph/path` | `get_path` | session | The shortest active-edge path between two nodes. |

### Review

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| GET | `/api/review/queue` | `review_queue` | session | Pending proposals with reviewer context, oldest first. |
| POST | `/api/review/accept` | `review_accept` | session | Accept proposals by id, or every proposal matching a filter. |
| POST | `/api/review/reject` | `review_reject` | session | Reject proposals by id, or by filter. The reason is mandatory. |

### Diff & events

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| GET | `/api/diff` | `diff_versions` | session | Unified diff between two versions of one node. |
| GET | `/api/events` | `list_events` | session | The append-only event log, newest first. |

### Assets

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| GET | `/api/assets` | `list_assets` | session | Registered assets, metadata only — the bytes stay in the database. |
| POST | `/api/assets` | `upload_asset` | session | Register an uploaded file as a content-addressed asset. |
| GET | `/api/assets/{id}` | `get_asset` | session | One asset's metadata, by hash or by asset-reference node id. |
| GET | `/api/assets/{id}/rendition/{profile}` | `get_rendition` | session | The WebP bytes of an image rendition — generated lazily, cached in the DB. |
| POST | `/api/assets/{id}/download-url` | `mint_asset_download_url` | session | Mint a single-use, short-lived URL for one asset's original bytes. |

### Ingestion

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| POST | `/api/ingest` | `ingest_source` | session | Ingest one local file **or** one URL: register, extract, describe, propose. |
| POST | `/api/uploads` | `request_upload_url` | session | Mint a single-use URL to PUT one file to — or answer with a dedup hit. |

### Capability URLs

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| GET | `/api/download/{token}` | `download_original` | token — the URL is the credential | Spend a download token and stream that asset's original bytes. |
| PUT | `/api/uploads/{token}` | `upload_original` | token — the URL is the credential | Spend an upload token and store the raw request body as an asset. |

### History & undo

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| POST | `/api/undo` | `undo` | session | Reverse one event (default: the latest reversible one) — human tier. |
| GET | `/api/export/node/{id}` | `export_node` | session | Download a node — and optionally its neighborhood — as a JSON file. |

### Consolidation cycles

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| GET | `/api/cycles` | `list_cycles` | session | The consolidation journal, newest first. |
| POST | `/api/cycles` | `run_cycle` | session | Run a consolidation cycle now and answer with its journal entry. |
| GET | `/api/cycles/{id}` | `get_cycle` | session | One journal entry: the row, its metrics, and the events it wrote. |
| POST | `/api/cycles/{id}/abandon` | `abandon_cycle` | session | Close an interrupted cycle as ``failed`` — the door out of a stuck run. |
| POST | `/api/cycles/{id}/stop` | `stop_cycle` | session | Ask a ``running`` cycle to stop, and record who asked (design K1–K3). |
| POST | `/api/cycles/{id}/rollback` | `roll_cycle_back` | session | Take a whole cycle back — all of it, or none of it (design D7). |

### Accounts & sessions

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| GET | `/api/me` | `get_me` | session | The session's own human account (id, name, credential state). |
| GET | `/api/humans` | `list_humans` | session | Every human account. |
| POST | `/api/humans` | `create_human` | session | Create a human account (passwordless until its password is set). |
| POST | `/api/humans/{id}/password` | `set_human_password` | session | Set or change a human's password; the hash never leaves the service. |
| POST | `/api/humans/{id}/disable` | `disable_human` | session | Disable a human — its sessions die, and its agents' tokens with them. |
| POST | `/api/humans/{id}/enable` | `enable_human` | session | Re-enable a disabled human. |
| GET | `/api/agents` | `list_agents` | session | Every agent account. |
| POST | `/api/agents` | `create_agent` | session | Create an external agent owned by the session's human. |
| POST | `/api/agents/{id}/token-rotate` | `rotate_agent_token` | session | Replace an agent's token; the new one is in this body and nowhere else. |
| POST | `/api/agents/{id}/disable` | `disable_agent` | session | Disable an agent — its token dies immediately on HTTP; a running MCP |
| POST | `/api/agents/{id}/enable` | `enable_agent` | session | Re-enable a disabled agent. |

### Grants & spaces

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| GET | `/api/grants` | `list_grants` | session | Grant rows, optionally one agent's (``?agent=``). |
| POST | `/api/grants` | `set_grant` | session | Grant (or re-level) an agent's access to a space. |
| POST | `/api/grants/revoke` | `revoke_grant` | session | Revoke an agent's grant on a space. |
| GET | `/api/spaces` | `list_spaces` | session | Every active space, with its live node count and grant holders. |
| POST | `/api/spaces` | `create_space` | session | Create a space (a node of builtin type ``space``, living in meta). |
| POST | `/api/spaces/{id}/rename` | `rename_space` | session | Rename a space — a space is a node, so this is a node-title update. |
| POST | `/api/spaces/{id}/archive` | `archive_space` | session | Archive a space; its nodes keep their ``space_id`` and grants go inert. |

### Settings

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| GET | `/api/settings` | `get_settings` | session | Every setting: what is in force, where it came from, whether it can be stored. |
| PUT | `/api/settings` | `put_settings` | session | Apply several setting changes atomically: all of them, or none of them. |
| POST | `/api/settings/export` | `export_settings` | session | Stream the effective configuration as a `.env` download — the named envelope exemption. |
| POST | `/api/settings/adopt-env` | `adopt_environment` | session | Adopt every editable setting the environment pins into ``settings.env``. |
| DELETE | `/api/settings/{name}` | `delete_setting` | session | Remove one setting from ``settings.env``, falling back down the ladder. |

### Projectors

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| POST | `/api/projectors/{name}/rebuild` | `rebuild_projector_route` | session | Drop one projector's derived state and replay the event log (human-only). |

### Agent surface (MCP)

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| POST, GET, DELETE | `/mcp` | `StreamableHTTPASGIApp` | bearer — an agent token, per request | The MCP surface for external agents: read and additive tiers only, streamable HTTP. |

### Health

| Method | Path | Handler | Auth | Notes |
|---|---|---|---|---|
| GET | `/healthz` | `healthz` | open | Liveness probe — open even with the session gate on. |
