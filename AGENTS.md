# AGENTS.md — nodum

Agent-facing instructions for working in this repository. Read this before
editing anything here.

## What this repo is

`nodum` is a **DB-native knowledge graph**: a typed graph of Markdown-content
nodes and typed edges in one SQLite file (WAL mode), behind a deterministic,
LLM-free service layer. Every mutation is validated, state-machine-checked,
logged in an append-only event log with full before/after payloads, versioned
(nodes), and reversible (`undo`). `[[wikilinks]]` in content are materialized
as `mentions` edges on write. The Typer CLI is a thin adapter emitting exactly
one JSON object per command.

Phase 1 (core) landed; Phase 2 (agent-native) is underway. Built so far in
Phase 2: **event-log projectors** (`nodum.projectors`) with per-projector
checkpoints and rebuild mechanics, the **`fts` projector** (FTS5 over node
title + content), the **`vec` projector** (sqlite-vec chunk embeddings,
local in-process fastembed model — migration 0006), **hybrid search**
(`nodum.search`, CLI `search`): BM25 + vector lists fused by reciprocal rank
fusion, then one-hop graph-expansion re-ranking, with a per-signal `signals`
breakdown, **principals, spaces and grants** (Q13: `humans`/`agents`/
`grants` tables, a scope-bound store, `read`/`suggest`/`edit` per
(agent, space)), the **review/accept API** (proposal listing with reviewer
context, batch accept/reject by id or filter — a human, or `edit` on the
item's space; `undo` stays human-only), **proposed updates** (agent `update_node` stages a
`proposed` version recording which fields it named; accept applies exactly
those, reject archives it — migrations 0005/0008), the **MCP server**
(`nodum.mcp_server`, stdio, read + additive tiers only; review and curative
tools are never registered), and **assets + image renditions**
(`nodum.assets` — migration 0007): thin content-addressed asset registration
(a metadata row + an in-database blob + sha256) and lazily generated, stored,
evictable `thumb`/`preview` WebP renditions (design §5.7), exposed over MCP
as `get_asset` (metadata + rendition image block — never the original).
Phase 3 (human UI) has landed: the **HTTP API** (`nodum.http_api`, `nodum
serve`) is the human surface — a Starlette app serving the JSON API under
`/api` and the built web UI at `/`, with every write forced to `actor =
human` and no request field able to say otherwise — the shared **envelope**
module (`nodum.envelope`) both the CLI and the API render through, and the
**web UI** itself (`web/`, React 19 + TypeScript, built into `nodum/_web/` by
`make web-build`; gitignored, shipped in the wheel as a hatchling artifact):
six views — Markdown editor, hybrid search, review queue,
graph, assets, per-node version history.
**Deliberately not built yet** (later phases — do not add): the
Phase-4 ingestion pipeline (text extraction, chunking, source/claim
proposals, `ingest_file`/`ingest_url`), `page:<n>` PDF rasters,
`get_download_url`/`request_upload_url`, the internal agent runtime and
consolidation cycle, **Markdown Mirror** and any whole-graph export (the only
export that exists is the thin per-node snapshot,
`GET /api/export/node/{id}?depth=`, which is `get_neighborhood` with a
`content-disposition` header — not a format, not a backup), the curative tier
(`merge_nodes`, `retype`, `supersede_edge`, `bulk_relink`, `consolidate`),
and the **dream-journal view**, which Phase 3 deferred to Phase 5 on purpose —
it belongs with the consolidation cycle that gives it something to show. The
schema reserves room for them (`space_id`, `merge_redirects`, `cycle_id`,
`assets.extracted_text`); each lands as its own append-only migration.
A node's `type` is likewise **fixed at creation by design**, not by omission:
`service.update_node` takes `title`/`content`/`props` only, and retyping is a
curative operation (§8.2 `retype`). Do not add a `type` field to
`PATCH /api/nodes/{id}` — the editor withholds its type commands on a saved
node for exactly this reason.

## Architecture

- **`nodum.service`** is the spine and the only writer — validation, the
  `proposed → active → archived` state machine, the event log, versions
  (including `proposed` version updates: agent edits stage the fields they
  name, accept applies exactly those, reject archives), undo, wikilink
  materialization, the review queue (proposal listing, batch accept/reject),
  and grant enforcement through the scope-bound store (`suggest` lands
  `proposed`, `edit` lands `active` and carries in-space
  accept/reject/archive; `undo` stays human-only), and the curated graph reads
  (`get_neighborhood`, `traverse`, `find_path`, `get_schema`,
  `diff_versions`, `propose_edges`). Two reads exist for interactive clients
  rather than agents: **`subgraph`** — `traverse` plus edge state/confidence/
  author and node-type filters, all applied in SQL, with a node `limit`
  enforced *during* the breadth-first walk — tested **before** the far side
  of an edge is read, so a hub costs `limit` node reads and not one per
  neighbour — a separate edge cap (`limit * SUBGRAPH_EDGE_FACTOR`), since a
  node cap bounds nodes only and one pair of nodes can carry any number of
  edges, a server-side ceiling on `limit` itself (`MAX_SUBGRAPH_LIMIT`, 2000 —
  the value the graph view's slider already clamps to), an edge list **closed
  over the returned node set** so the outermost ring is joined up rather than
  drawn with gaps, and a `truncated` flag saying whether **either** cap bit —
  and **`suggest_links`**, a title-prefix lookup for a `[[` autocomplete that
  reads `nodes` directly, so it answers on a database whose projectors have
  never run. Each public function opens its own short-lived connection
  (applying pending migrations idempotently) and commits. New behaviour and
  validation go here first; adapters must not add behaviour the service lacks.
- **`nodum.mcp_server`** — the MCP adapter (stdio, official Python SDK
  FastMCP), the **external-agent** surface. Registers the design §8.1 read +
  additive tiers and nothing else, each tool a thin delegate to a
  service/search function; one configured `--actor` per server names the
  agent, whose principal is loaded with its grant set — every write and read
  is confined to those grants (INTERIM: unauthenticated until token
  verification lands). The review tools
  (`accept`, `reject` — the §8.1 "write (human)" tier) and the
  curative tools (`merge_nodes`, `retype`, `supersede_edge`, `bulk_relink`,
  `consolidate` — §8.2) are **never registered**: structural enforcement, not
  a runtime check. Launched by `nodum mcp serve`.
- **`nodum.http_api`** — the HTTP adapter (design §9), the **human** surface
  and the exact inverse of the MCP server. `create_app(*, db_path,
  allowed_hosts, secure_cookies)` builds a Starlette app: the JSON API under
  `/api`, the built UI at `/`, launched by `nodum serve` (loopback, port
  8600). Auth is password login: `POST /api/login` (name + password, argon2id,
  constant-time on failure) creates a server-side session row (30-day sliding
  expiry) and sets an `HttpOnly; SameSite=Strict` cookie;
  `SessionMiddleware` resolves it to the session's human principal on every
  `/api` request — reads included; only `/healthz`, `/api/login` and the
  static UI stay open. Every write is attributed to that principal and **no
  request field, header, or query parameter can set an identity** — a body
  carrying `{"actor": "agent:x"}` is ignored, not honoured. That absence is
  structural, not a filter: every `principal=` binding in the module is
  `_session_principal(request)`, which reads only what the middleware
  verified into the scope (no principal without a verified session), handlers
  forward only fields they name, and `_write` refuses a caller-supplied
  principal outright. Tests in `tests/test_http_api.py`
  enforce it over the *live route table* and the module's AST, so a new
  endpoint is covered without being added to a list — if you add an endpoint,
  route its writes through `_write` and never mention an identity in a handler.
  One `EXCEPTION_STATUS` table becomes the error envelope. It covers every
  class `cli._run` catches — the `sqlite3.Error` and `OSError` rows are the
  **base** classes, so `DatabaseError`/`IntegrityError`/`ProgrammingError`/
  `DataError` land on a status rather than a generic 500 — plus
  `sqlite3.OperationalError` → 503, `OverflowError` → 400, `PayloadTooLarge` →
  413 and `ClientDisconnect` → 499, which only a network surface meets.
  `test_every_exception_cli_run_catches_is_mapped` reads `cli._run`'s own
  except clauses and asserts the claim instead of restating it. Unmapped
  exceptions are a generic 500 with no traceback in the body.
  `RequestGuardMiddleware` is the origin control under all of it (see the
  HTTP contract below) — binding loopback keeps other machines out, not other
  *origins*, and a browser reaches `127.0.0.1` from any page.
- **`nodum.envelope`** — the JSON envelope both the CLI and the HTTP API emit:
  `envelope()`, `list_envelope()` (the `{"<plural>": [...], "count": n}`
  convention), and `render_json()`. Extracted so the surfaces cannot drift;
  `GET /api/nodes/{id}` is byte-identical to `nodum node get <id>` on stdout.
  New list output goes through `list_envelope`, never a hand-built dict.
- **`web/`** — the human UI (React 19 + TypeScript + Vite), built into
  `nodum/_web/` by `make web-build` and served by `nodum serve`. Seven routes
  over six views, each lazily loaded so CodeMirror, Mermaid, and Cytoscape stay
  out of the initial bundle. `src/api/client.ts` is the only `fetch` in the
  app and has **no identity parameter anywhere** — the server's structural
  rule, mirrored in the client. It sends `Content-Type: application/json` on every
  non-GET request, bodyless ones included, because the server requires it.
  `src/lib/` holds the cross-view invariants
  (timestamps, failure classification); `src/components/` holds shared React
  components; a view owns its own directory and links to other views by URL,
  never by import. Full conventions: `web/README.md`.
- **`nodum.projectors`** — derived-index consumers of the event log. A
  projector registry (`REGISTRY`), per-projector checkpoints in
  `projector_checkpoints`, incremental `run_projectors`, and
  `rebuild_projector` (reset derived state, replay from event 0). The `fts`
  projector maintains `node_fts`; the `vec` projector maintains `chunks` +
  `node_vec` (rebuild = the model-change re-embed path, design D6). The
  service layer never calls projectors — the event log is the only coupling.
  A projector whose requirements are unmet (`vec` without a usable embedding
  provider) reports itself unavailable in `projector status` and its runs
  are no-ops — the backlog waits, nothing crashes.
- **`nodum.embeddings`** — the embedding provider seam (design D10) and
  chunking (design D6). The provider interface is `model_id` + `dimensions`
  + `embed(texts) -> vectors`; the default is a local in-process fastembed
  model (`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`,
  384-dim, multilingual, ONNX/CPU — no daemon, no API key) behind the
  optional `embeddings` extra. A model is never downloaded implicitly: the
  provider resolves only from the local HF cache unless
  `NODUM_EMBED_DOWNLOAD=1` is set (first run fetches it).
  `NODUM_EMBED_MODEL` overrides the model name (a different dimensionality
  needs a new migration — the vec0 table is fixed at 384). Tests inject a
  deterministic hashing fake via `embeddings.set_provider`.
- **`nodum.assets`** — content-addressed binaries and their derived
  renditions (design §5.5/§5.7). **Bytes live in the database, not on the
  filesystem**: `assets` holds metadata, `asset_blobs` holds the bytes under
  the same sha256 key, so the whole system is one file and disaster recovery
  is `DB = everything`. Registration is idempotent sha256 dedup with no
  event-log entry (there is nothing to undo), and streams through
  `Connection.blobopen` so a large file is never held in memory — never
  inline asset bytes into an event payload. The two read passes (hash, then
  copy) are cross-checked: the copy is re-hashed, so a source that changed in
  between is refused (`AssetSourceChanged`) instead of stored under a key it
  does not match, and a file above `SQLITE_LIMIT_LENGTH` (1 GB) is refused up
  front (`AssetTooLarge`). Note the streamed copy holds SQLite's single write
  lock for its whole duration. Renditions (`thumb` ≤256px WebP
  q75, `preview` ≤1024px WebP q80 with a 300 KB quality-stepping target) are
  keyed by `sha256(asset_hash + ':' + profile)`, generated lazily with Pillow
  on first request, stored as blobs, and evicted by `purge_renditions` (CLI
  `asset purge`) — fully regenerable. Non-image assets are rejected cleanly;
  `page:<n>` rasters are Phase 4. Pillow reads originals through
  `_BlobReader`, which restores the file-style tolerant seeks that
  `sqlite3.Blob` refuses and Pillow's format probing depends on.
- **`nodum.search`** — the query path (design §7). BM25 over the `fts`
  projector's index and vector ANN over the `vec` projector's chunks
  (closest chunk per node wins), fused by reciprocal rank fusion (K=60) with
  `type`/`state`/`created_by`/date filters; optional one-hop graph expansion
  over `active` edges (`--expand`) applies after fusion. Hits carry the
  fused `score` plus a per-signal `signals` breakdown (`bm25` / `vector` /
  `graph`). With no embedding provider the vector signal is skipped —
  search silently degrades to BM25 + graph.
- **`nodum.db`** — connection management (WAL, foreign keys), `NODUM_DB`
  resolution, the migration runner. Each migration's script and its
  `schema_migrations` row are one transaction (`apply_migration`), so an
  interrupted upgrade rolls back whole and retries cleanly instead of wedging
  the database half-migrated.
- **`nodum.migrations`** — the append-only migration list (`0001_core` …
  `0008_version_proposed_fields`). Never edit a shipped migration; append a
  new one. A migration must never leave data readable only through a store a
  later migration replaces: introduce a table where its bytes already belong
  (this is why asset bytes are part of `0007` and there is no `path` column
  anywhere).
- **`nodum.models`** — the pydantic I/O schema shared by every surface.
- **`nodum.cli`** (Typer) — each command calls one service function and prints
  the result as a single JSON object on stdout; human/error messages go to
  stderr with exit code 1. No `--json` flag.

See `docs/architecture.md` for the design-section → module mapping and the
Phase-1 decision log.

## Workflow rules

- **uv for everything.** `uv sync --all-groups` (or `make dev-install`), `uv
  run nodum …`, `uv run pytest`. Never raw `pip`/`venv`. Commit `uv.lock`;
  `.venv/` stays gitignored. Python ≥ 3.12. The local embedding model lives
  behind the optional `embeddings` extra (`uv sync --extra embeddings`) —
  tests never need it (they inject a fake provider; one real-model smoke
  test is opt-in via `NODUM_RUN_SLOW=1`).
- **`make format` after every code change** (ruff check --fix + format); CI
  runs `make lint` and `make test` on Python 3.12 and 3.13.
- **Tests**: `make test` (pytest, rooted at `tests/`). No external services:
  every test that touches the database takes the `fresh_db` fixture, which
  points `NODUM_DB` at a fresh temp file and migrates it (it is opt-in — the
  pure-logic tests in `tests/test_embeddings.py` need no database at all), and
  the autouse `_no_embedding_provider` fixture forces the embedding provider
  unavailable so nothing can reach the network.
- **Version** comes from the git tag (`vX.Y.Z`) via hatch-vcs at build time;
  never bump a version in code.
- **Releasing.** Land the change on `main`, then push an annotated `vX.Y.Z`
  tag on a commit reachable from `origin/main`. That triggers
  `.github/workflows/release.yml`: the test matrix and the clean-install smoke
  gate a `uv build`, which publishes to PyPI over OIDC trusted publishing (no
  API token). Tag pushes do **not** trigger `ci.yml`, which is why the release
  workflow re-runs the suite itself. The publish step sets `skip-existing:
  true`, so re-pushing a tag onto an already-released version is a no-op rather
  than a `400 File already exists` failure, and pins the publish action to an
  exact tag because that job holds OIDC publish rights.
- **Docstrings on public APIs**: one-line summary plus args/returns where
  applicable. Comment the *why*, not the *what*. Don't annotate code you
  didn't change.
- **Keep adapters thin.** When you add or change a service operation, expose it
  through the CLI in the same change, and update `README.md`,
  `docs/architecture.md`, and this file in the same commit.
- **Line length 100**; ruff rules `E, F, I, UP, B, SIM`.
- **Frontend**: `make web-install` once, then `make web-build` (which runs
  `tsc --noEmit` first, so the build is the type gate) or `make web-dev` for
  the Vite server on 5700 proxying to `nodum serve` on 8600. Two gates, both in
  CI: `tsc --noEmit` over the whole tree, and **`make web-test`** — Vitest over
  the pure modules in `web/src` (`*.test.ts` beside the module it covers).
  There is no ESLint and no component/DOM harness, so anything React renders is
  still verified by type-checking it and driving it in a browser.
  **The Vitest run pins `TZ` to a non-UTC zone** (`web/vitest.config.ts`) and
  `time.test.ts` asserts the pin took: the zone-less-timestamp bug `lib/time.ts`
  fixes is invisible in UTC, so an ambient-timezone run would pass while the
  code was broken. Do not remove the pin, and do not add a test that depends on
  the ambient zone. `nodum/_web/` is gitignored whole and rewritten by every
  build; a release must `make web-build` before `uv build --wheel`.
  `release.yml` does this **inside the `build-and-publish` job**, not in a
  separate one — Actions jobs do not share a filesystem, so a bundle built
  elsewhere cannot reach the wheel. The `smoke` job builds it too and runs with
  `NODUM_SMOKE_REQUIRE_WEB=1`, which turns a missing bundle from a note into a
  failure; that is the check that stops a placeholder wheel reaching PyPI.
  **v0.1.0 and v0.2.0 predate the working version and ship the placeholder.**
- **`uv build` builds the wheel *from the sdist*, so the sdist must carry the
  bundle too.** `artifacts = ["nodum/_web/**"]` is declared on **both** the
  `wheel` and the `sdist` hatch targets, and the sdist one is not redundant: a
  bare `uv build` (what `release.yml` publishes with) builds the sdist first and
  then builds the wheel from it, so anything the sdist drops cannot reappear in
  the wheel. `uv build --wheel` reads the source tree directly and does not have
  this problem — which is precisely the trap. **Never build the wheel a
  different way in a test than the release does:** v0.2.0 published a
  placeholder-UI wheel with a fully green release because `scripts/smoke-install.sh`
  used `--wheel` and so validated a build path the release never performs. The
  script now uses plain `uv build`.
- **Docs site.** `docs/` + `mkdocs.yml` build the mkdocs-material site at
  <https://nodum.vcoeur.com/>, deployed by `.github/workflows/docs.yml` on any
  push to `main` that touches those paths. The build runs `--strict`, so a
  broken internal link or a page missing from `nav` **fails CI** — check a docs
  change locally with `uv run --with mkdocs-material mkdocs build --strict`.
  `docs/CNAME` carries the custom domain and must survive any docs
  reorganisation. **`docs/llms.txt`** is the agent-facing summary published at
  `/llms.txt` (mkdocs copies non-Markdown files through verbatim); it states the
  CLI contract, the actor/privilege split, and the MCP tier boundary, so a
  change to any of those belongs in it as well as in this file. `docs/architecture.md` is both the in-repo architecture doc
  and a site page, so links out of it must be absolute URLs — a relative link
  to something outside `docs/` resolves in the repo but breaks the site build.

## CLI contract (for agents driving the CLI)

- Every command prints **one JSON object** on stdout and nothing else on the
  success path — parse stdout directly. A command returning a list wraps it in
  a named key plus a `count` (`{"nodes": [...], "count": 2}`); keep new list
  commands to that shape.
- DB path resolution: `--db` flag → `NODUM_DB` env var →
  `~/.local/share/nodum/nodum.db`.
- **The CLI is human-only, and every write command names its human** with a
  required `--as human:<id>` (or the bare id): attribution is explicit, always
  (there is no `--actor` — agents drive MCP, never the CLI). A write by a
  human lands `active`. An agent's write (over MCP) lands per its grants:
  `suggest` → `proposed`, `edit` → `active`. An agent `node update` with
  `suggest` stages a `proposed` *version* recording which fields it named;
  `accept <version-id>` applies **only those fields** to the node as it
  stands then (so a human edit made while the proposal waited is not
  reverted), `reject` archives it. A `[[wikilink]]` written by an agent
  materialises a `proposed` `mentions` edge; accepting the node brings it to
  `active`.
- **Review authority is a human, or `edit` on the item's space** (Q13):
  `accept`, `reject`, `archive`, and every `review` subcommand. `undo` stays
  human-only — restoring an event's payload can write `state = 'active'`
  back, and no grant delegates that.
  Both spellings of a reject — single-item `reject <id> --reason` and batch
  `review reject … --reason` — require the reason and record it in the reject
  event's payload: one operation, one audit guarantee.
- Errors are always one line on stderr with exit 1, never a traceback — that
  includes a missing file (`asset register /missing.png`), a database another
  writer holds (`database error: database is locked`), and an undo the graph
  has grown past (a created node that now has children).
- `--set key=value` is repeatable; values are parsed as JSON with a raw-string
  fallback.
- `--version` prints `nodum <version>` and exits 0; `schema-dump` prints the
  CLI's whole command tree as JSON. Both short-circuit without touching a
  database, so they work on a bare install — that is what
  `scripts/smoke-install.sh` asserts against a freshly built wheel. Note
  `schema-dump` (the CLI adapter's own surface) is a different thing from
  `schema <type>` (one node/edge type's catalog entry from the database).
- Surface: `init`, `node create/get/update/list/children`, `edge
  create/list/create-batch`, `accept <id>` / `reject <id> --reason` /
  `archive <id>` (each takes a node, edge, or proposed-version id), `undo [seq]`,
  `history <node-id>`, `events`, `types`, `schema <type>`, `schema-dump`,
  `search <query>`,
  `traverse`, `subgraph <root-id>`, `suggest-links <prefix>`, `find-path`,
  `diff`, `projector run/status/rebuild`,
  `review queue/accept/reject/accept-all/reject-all`,
  `asset register/get/list/rendition/purge`,
  `mcp serve --actor agent:<name>`,
  `serve [--host 127.0.0.1] [--port 8600] [--allow-host NAME]
  [--db PATH]`. `serve` prints the database path on stderr and translates
  uvicorn's own startup failure (a port already in use) into the contract's
  exit 1 — it used to escape as uvicorn's exit 3. A non-loopback bind is
  allowed (password login, not the bind, is the boundary) and marks the
  session cookie `Secure` there.
- Reads are not state-filtered by default beyond edge traversal: `node get`,
  `node children`, `node list`, and `history` return `proposed` rows, and
  `search --state any` includes them. Only *traversals* (`node get --depth`,
  `traverse`, `subgraph`, `find-path`, `search --expand`) are restricted to
  `active` edges — proposed structure is inert, not hidden. `subgraph
  --edge-state proposed` is the one way to walk it, and it has to be asked
  for. `suggest-links` follows the node-read rule with one exception:
  `archived` titles are never suggested, since a retired node is not a link
  target.
- `subgraph` is the bounded read, and it is bounded twice: `--limit` is a hard
  node cap applied while walking (tested before the far node is read, so the
  cost is `O(limit)`, not `O(neighbours)`), and the edge list has its own cap
  at `limit * SUBGRAPH_EDGE_FACTOR` — without it a single pair of nodes with
  300 edges between them returns 300 edges under a 2-node cap. `--limit` is
  itself clamped to `MAX_SUBGRAPH_LIMIT` (2000), so a caller passing
  `--limit 1000000000` gets the ceiling rather than the graph. `truncated` is
  true when **either** cap bit and is deliberately conservative: it reports a
  walk that stopped early even if the graph happened to have nothing more to
  give. A filter removing nodes is **not** truncation — the caller asked for
  that. A limit below 1 is still an error rather than SQL's "unbounded". Every
  filter composes as one conjunction, and an edge whose far node is filtered
  out is dropped with it — the result never names an edge endpoint it does not
  also return. The edge list is also *closed* over the node list: an edge
  between two returned nodes comes back even when the walk never traversed it
  (the B–C edge of a triangle read at depth 1), which the uncapped `traverse`
  does not do.
- Asset images reach agents only as renditions: `asset rendition` prints
  rendition metadata alone — the WebP bytes stay in the database and are never
  inlined into the JSON (`--out <file>` is how you extract them); the MCP
  `get_asset` tool returns metadata + a WebP image block of the requested
  rendition — originals are never served over MCP (design §5.7).

## HTTP contract (for agents touching `nodum serve`)

- **The HTTP surface is the human's.** Every write it makes is `actor =
  human`; the actor is never read from a request. Do not add an "actor"
  parameter, header, or override "for testing" — the MCP surface is where
  agent identity lives, and the inversion is the whole point.
- Route handlers are thin delegates: one service/search/assets call each, no
  behaviour the service lacks. Writes go through `_write(service.fn, …)`,
  which is the only place the actor is bound. **Never import a service function
  that takes an `actor` into `http_api`** — an alias hides it from every
  source-level check, and `test_no_write_service_function_is_reachable_under_
  any_name` fails on the import itself. Never splat request data into a call
  either: `**` may only unpack a dict an allowlisting helper built, and any new
  one fails `test_no_call_splats_anything_but_an_allowlisting_helper` until it
  is reviewed.
- **The test that actually holds the boundary is the runtime sweep**
  (`test_no_endpoint_can_attribute_a_write_to_an_agent`): it drives every
  state-changing method of every route in `app.routes` with actor-carrying
  bodies, query strings and headers, then asserts nothing written during the
  sweep names anything but `human`. The AST properties beside it are a belt —
  all of them were evadable by a handler that forwarded a body it never
  inspected, which is how a rogue endpoint once produced
  `created_by: "agent:evil"` on a fully green suite.
- **A state-changing request must prove it is same-origin**
  (`RequestGuardMiddleware`), because `nodum serve` binds loopback with no token
  and loopback is reachable from every page the user visits. The rule:
  `Sec-Fetch-Site` in `{same-origin, none}`, **or** an `Origin` whose host is
  allowed, **or** the `X-Nodum-Client` header — which is how a non-browser
  client declares itself, since a browser always sends one of the first two and
  cannot be scripted out of either. A cross-site `Sec-Fetch-Site` or a
  mismatched `Origin` is refused outright. Reads are unencumbered.
- **Every JSON route requires `Content-Type: application/json`, bodyless ones
  included.** That is not pedantry: `application/json` is not a CORS-simple
  content type, so a cross-origin page cannot send it without a preflight, and
  this app answers none. `POST /api/assets` is the one exception — multipart
  *is* simple — so it rests entirely on the same-origin proof above. A new
  upload route goes in `MULTIPART_ROUTES` or it inherits the JSON rule.
- **The `Host` header is validated** against `resolve_allowed_hosts(host,
  --allow-host)`. This is the DNS-rebinding defence and the only check that
  protects *reads*: after a rebind the attacker's page is same-origin by every
  other measure. Host names are compared without ports, which is what keeps the
  `make web-dev` proxy (`Host: localhost:5700`) working.
- **The session gate is one rule: every `/api` route but `/api/login` needs a
  valid session, reads included.** A single-human file has nothing an
  anonymous caller should see, and one rule is the one no future endpoint can
  forget. The cookie is `HttpOnly; SameSite=Strict` over a server-side row
  with a 30-day sliding expiry; logout, expiry, and `human disable` all kill
  it at the next request (verification-time, no cache). Any local process can
  satisfy every origin check with three curl headers, so it may *attempt* a
  login — the human's password is the whole defence there, and the `serve`
  banner says so.
- **Account and grant administration is on the API too.** `GET /api/me`
  returns the session's human; `/api/humans`, `/api/agents` and `/api/grants`
  mirror the CLI's `human`/`agent`/`grant`/`revoke`/`grants` commands — thin
  delegates over the service's human-only admin surface, with disable/enable
  and password/rotate as verb-POSTs (`/api/humans/{id}/password`,
  `/api/agents/{id}/token-rotate`, …) in the `/api/nodes/{id}/archive` style.
  Agent creation over HTTP is external-kind and owned by the session's human;
  the show-once token comes back in the create and token-rotate response
  bodies, since HTTP has no stderr to print it to the way the CLI does.
- **A wrong verb on a real route is a 405 with an `Allow` header**, not the
  catch-all's 404. The catch-all claims every method so a `fetch` never gets
  HTML, which also means it out-matches a real route's 405 unless it asks the
  real routes what they would have matched — which `api_not_found` does.
- **`/healthz` reports liveness only.** It sits outside auth, so anything it
  says is said to everyone; it used to say the absolute database path.
- **`POST /api/assets` is bounded before it buffers**: `MAX_REQUEST_BYTES` is
  checked against `Content-Length` and then enforced mid-stream (the header is
  client-supplied and cannot be the only guard), the type is sniffed from the
  bytes against `UPLOAD_MIME_ALLOWLIST` rather than read off the filename, and
  `assets.MAX_IMAGE_PIXELS` refuses a decompression bomb from the image header.
  The allowlist is deliberately narrower than what `assets.register_asset` will
  store: the CLI registers a local file the operator owns, this one takes a
  file from a stranger. **There is no delete route**, so anything that does land
  is only reclaimable out of band — a known gap, not an oversight.
- **Do not invent request fields the domain has no representation for.** If a
  body key has no counterpart in `nodum.models`/`nodum.service`, it does not
  belong here. (The lesson was learned on the since-deleted policies API: an
  `enabled: false` flag, accepted once, silently wiped the stored ruleset with
  no way to recover it.)
- Responses use `nodum.envelope`: single results as the model dump, lists as
  `{"<plural>": [...], "count": n}`, rendered exactly as the CLI prints them.
  A new list endpoint keys on the same plural the CLI command uses.
- Failures are `{"error": {"type", "message"}}` from `EXCEPTION_STATUS`; add a
  new mapping there rather than catching in a handler. Anything unmapped is a
  500 with a generic body — never leak a traceback to a client.
- Repeatable filters (`edge_type`, `edge_state`, `node_type`) are repeated
  query keys; `/healthz` sits outside `/api` and outside auth; an unknown `/api`
  path is a JSON 404 while unknown non-API paths fall through to the SPA
  entry point (or the "UI not built" placeholder). **`/favicon.ico` is the one
  exemption**: a browser asks for it unprompted and it is definitely not a
  client route, so it is answered with the bundle's icon if there is one and a
  204 otherwise — never an HTML document under a 200, which a client asking for
  an image has no way to detect. Any other path a browser requests on its own
  belongs in that same exemption list, not in the catch-all.
- Asset originals are never served — only `thumb`/`preview` renditions, as
  WebP bytes at `/api/assets/{id}/rendition/{profile}` (design §5.7).

## Frontend contract (for agents touching `web/`)

- **One `fetch`.** Everything goes through `src/api/client.ts`. It has no
  identity parameter and must never grow one — the server binds the principal
  and the client being unable to express one is the second layer under that.
  It also owns `Content-Type: application/json` on every non-GET request,
  bodyless ones included, because the server requires it. (INTERIM: the
  `#token=…` bearer adoption still lives here; the login flow replaces it in
  the surfaces step.)
- **Never call `new Date()` on a server string.** SQLite writes
  `datetime('now')` — UTC, no zone marker — which every browser reads as *local*
  time. Parse through `parseTimestamp` (`src/lib/time.ts`) and format through
  its formatters. `new Date()` on a client-side epoch number ("saved at",
  "checked at") is fine and is the only exception.
- **Never re-derive a failure's meaning.** `describeFailure` (`src/lib/failure.ts`)
  is the one place that tells *the API refused this* apart from *nothing was
  listening* — and the two are not one test: same-origin it is a `fetch`
  `TypeError`, behind the dev proxy it is a 502. Map its `kind` onto your own
  panel; do not re-test `status` or `instanceof`.
- **A view owns its directory and links to other views by URL.** No view imports
  another. Route paths live in `src/router.tsx`; grep for the path string before
  renaming one. A view's entry component keeps a **default export** — the routes
  are lazily loaded and `lazy()` needs it.
- **Promote to `src/lib/` or `src/components/` on the second user, not the
  first.** Both are inherited by every view.
- **Do not render a control for something the service cannot do.** A node's
  `type` is immutable after creation, so the editor drops the type commands on a
  saved node rather than offering one that silently no-ops. Same rule as the
  HTTP contract's "do not invent request fields", one layer up.
- **The design system has two colour axes and both are taken**: the brass accent
  means "you can act on this", the state ramp means the service-layer state
  machine (`proposed` violet, `active` sea-green, `archived` lowest-contrast).
  Anything else needs its own hue, kept view-local until a second view names it.
  Class names are `nd-`-prefixed because Mermaid and Cytoscape inject global
  stylesheets on `.node`, `.label`, and `.edge`.
- **A pure module gets a `*.test.ts` beside it** (`make web-test`, Vitest). The
  harness is unit-only by design — no component rendering — so pull the logic
  worth testing out of the component and test it there, which is what
  `filters.ts`, `unifiedDiff.ts`, `signals.ts`, and `grouping.ts` already
  are. Assert the *semantics* the module encodes (a
  `min_confidence` of 0 is a filter, not a no-op; a 502 is unreachable, not a
  refusal), not its line coverage. The global environment is `node`; a suite
  that genuinely needs a DOM says so in **its own** docblock
  (`// @vitest-environment jsdom`, as `markdownRender.test.ts` does) rather than
  changing the config for everyone.
- **Nothing reaches `innerHTML` without going through DOMPurify.** The preview
  renders Markdown that *agents* wrote, in the origin that may write to the API,
  so `markdownRender.ts` reduces it to an allowlist with **no SVG and no
  MathML** — that namespace is where `<animate>` retargets an anchor's `href` to
  `javascript:` and where a lowercase `<style>` slips past any check keyed on
  `tagName`. `mermaidRender.ts` runs a second, SVG-shaped policy over mermaid's
  output. Both are covered by `markdownRender.test.ts`; a new sink means a new
  policy, not a new exception. `nodum.http_api.CONTENT_SECURITY_POLICY` is the
  runtime backstop under both — `script-src 'self'`, no `'unsafe-inline'`.
- **A dialog locks body scroll and hands focus somewhere real.** Both the review
  `Modal` and the assets lightbox set `body.style.overflow` on open and restore
  it on close. On close, focus returns to the opener *only if it is still in the
  document* — after a successful confirm it usually is not, and focusing a
  detached node silently drops the user on `<body>`. The view places focus in
  that case (the review inbox sends them to the outcome panel).
