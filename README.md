# nodum

A minimal **atomic-notes knowledge system**: a mutable PostgreSQL graph of
typed, UUID-keyed nodes and edges, with full-text search and recursive
subgraph expansion — all behind a single data-service layer fronted by a CLI,
an HTTP API, and a minimal web view.

> **MVP scope.** Retrieval is Postgres full-text plus graph traversal only —
> no embeddings yet. Vector / hybrid retrieval, an LLM gardener, contradiction
> reasoning, reranking, and auth are deferred.

## Quick start

```bash
make db-up        # start local PostgreSQL (docker-compose, host port 5436)
make dev-install  # uv sync --all-groups
make init-db      # create the schema
make test         # run the suite (needs the database up)
make serve        # run the HTTP API + web view on http://127.0.0.1:8600
```

Configuration is a single environment variable, `NODUM_DATABASE_URL` (default
`postgresql://nodum:nodum@localhost:5436/nodum`, matching docker-compose). Copy
`.env.example` to `.env` to override it. The API and web view bind to
`127.0.0.1:8600`.

## Surfaces

All three call the same `nodum.service` layer over one pydantic I/O schema, so
the CLI and the HTTP API emit byte-identical JSON for identical data.

### CLI

JSON goes to stdout, messages and errors to stderr. Run a command with
`uv run nodum …` (or `make run -- …`).

```bash
# create a node with a type (the type becomes a node, linked by an `is` edge)
uv run nodum add-node "Spaced repetition improves long-term retention" --type claim

# link two existing nodes with a typed, directed edge
uv run nodum add-edge <from-uuid> <to-uuid> --type supports

# full-text search (ranked, best first)
uv run nodum search "spaced repetition"

# expand a seed node into its connected subgraph, two hops out
uv run nodum expand <uuid> --depth 2
```

Other commands: `get <uuid>` (a node plus its incident edges), `init-db`, and
`serve`.

### HTTP API

FastAPI; every response is the same JSON envelope the CLI prints.

```bash
# POST /nodes
curl -s -X POST http://127.0.0.1:8600/nodes \
  -H 'content-type: application/json' \
  -d '{"text": "Spaced repetition improves long-term retention", "type": "claim"}'

# GET /search?q=&limit=
curl -s 'http://127.0.0.1:8600/search?q=spaced+repetition'

# GET /expand?seed=&depth=
curl -s 'http://127.0.0.1:8600/expand?seed=<uuid>&depth=2'
```

Also: `POST /edges`, `GET /nodes/{uuid}`, and `GET /healthz`. A missing node
returns 404; invalid input returns 422.

### Web view

A minimal, read-first browser UI served by the same app — open
<http://127.0.0.1:8600/> after `make serve`. It calls the API's read endpoints
(`/search`, `/nodes/{uuid}`, `/expand`) from the browser.

## Data model

A mutable JSONB graph:

- **Node** — a UUID plus a JSON `data` payload that always carries a `text`
  field (one atomic idea or fact). Full-text indexed.
- **Edge** — a UUID-keyed, directed link `from_uuid → to_uuid` between two
  distinct nodes, with a JSON `data` payload carrying its `type` (and any extra
  keys). Deleting a node cascades to its edges.
- **Type-as-node** — passing `--type`/`type` to `add_node` resolves-or-creates
  a type node and links the new node to it with an `is` edge, so types are part
  of the same graph rather than a separate table.

Retrieval is Postgres full-text (`tsvector` / `ts_rank`) for search and a
recursive CTE for subgraph expansion. **No embeddings in this MVP** — vector /
hybrid retrieval and the rest of the roadmap are deferred.

## License

MIT — see [LICENSE](LICENSE).
