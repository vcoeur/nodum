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
breakdown, **agent policies** (DB-stored per-agent rulesets, design §8.3,
with auto-accept evaluation on the edge write path), the **review/accept
API** (proposal listing with reviewer context, batch accept/reject by id or
filter — the human tier: a non-`human` actor is refused, as it is for
`archive` and `undo`), **proposed updates** (agent `update_node` stages a
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
six views — Markdown editor, hybrid search, review queue + policy editor,
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
schema reserves room for them (`graph_id`, `merge_redirects`, `cycle_id`,
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
  materialization, agent policies (CRUD + auto-accept evaluation on the write
  path), the review queue (proposal listing, batch accept/reject), the human
  tier (`_require_human_reviewer` refuses a non-`human` actor for `accept`,
  `reject`, `archive`, and `undo` — every operation that writes or retires
  live state), and the curated graph reads
  (`get_neighborhood`, `traverse`, `find_path`, `get_schema`,
  `diff_versions`, `propose_edges`). Two reads exist for interactive clients
  rather than agents: **`subgraph`** — `traverse` plus edge state/confidence/
  author and node-type filters, all applied in SQL, with a node `limit`
  enforced *during* the breadth-first walk (never by slicing afterwards) and
  a `truncated` flag saying whether the cap bit — and **`suggest_links`**, a
  title-prefix lookup for a `[[` autocomplete that reads `nodes` directly, so
  it answers on a database whose projectors have never run. Each public
  function opens its own short-lived connection (applying pending migrations
  idempotently) and commits. New behaviour and validation go here first;
  adapters must not add behaviour the service lacks.
- **`nodum.mcp_server`** — the MCP adapter (stdio, official Python SDK
  FastMCP), the **external-agent** surface. Registers the design §8.1 read +
  additive tiers and nothing else, each tool a thin delegate to a
  service/search function; one configured `--actor` per server attributes
  every write and must be an `agent:<name>` identity. The review tools
  (`accept`, `reject` — the §8.1 "write (human/policy)" tier) and the
  curative tools (`merge_nodes`, `retype`, `supersede_edge`, `bulk_relink`,
  `consolidate` — §8.2) are **never registered**: structural enforcement, not
  a runtime check. Launched by `nodum mcp serve`.
- **`nodum.http_api`** — the HTTP adapter (design §9), the **human** surface
  and the exact inverse of the MCP server. `create_app(*, db_path, token)`
  builds a Starlette app: the JSON API under `/api`, the built UI at `/`,
  launched by `nodum serve` (loopback, port 8420). Every write is attributed
  to `HTTP_ACTOR` (= `service.ACTOR_HUMAN`) and **no request field, header, or
  query parameter can set an actor** — a body carrying `{"actor": "agent:x"}`
  is ignored, not honoured. That absence is structural, not a filter: the
  module binds `actor` in exactly one expression (inside `_write`, to the
  constant), handlers forward only fields they name, and `_write` refuses a
  caller-supplied actor outright. Three tests in `tests/test_http_api.py`
  enforce it over the *live route table* and the module's AST, so a new
  endpoint is covered without being added to a list — if you add an endpoint,
  route its writes through `_write` and never mention an actor in a handler.
  One `EXCEPTION_STATUS` table (the classes `cli._run` catches, plus
  `sqlite3.OperationalError` → 503) becomes the error envelope; unmapped
  exceptions are a generic 500 with no traceback in the body.
- **`nodum.envelope`** — the JSON envelope both the CLI and the HTTP API emit:
  `envelope()`, `list_envelope()` (the `{"<plural>": [...], "count": n}`
  convention), and `render_json()`. Extracted so the surfaces cannot drift;
  `GET /api/nodes/{id}` is byte-identical to `nodum node get <id>` on stdout.
  New list output goes through `list_envelope`, never a hand-built dict.
- **`web/`** — the human UI (React 19 + TypeScript + Vite), built into
  `nodum/_web/` by `make web-build` and served by `nodum serve`. Seven routes
  over six views, each lazily loaded so CodeMirror, Mermaid, and Cytoscape stay
  out of the initial bundle. `src/api/client.ts` is the only `fetch` in the
  app and has **no actor parameter anywhere** — the server's structural rule,
  mirrored in the client. `src/lib/` holds the cross-view invariants
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
- **Docstrings on public APIs**: one-line summary plus args/returns where
  applicable. Comment the *why*, not the *what*. Don't annotate code you
  didn't change.
- **Keep adapters thin.** When you add or change a service operation, expose it
  through the CLI in the same change, and update `README.md`,
  `docs/architecture.md`, and this file in the same commit.
- **Line length 100**; ruff rules `E, F, I, UP, B, SIM`.
- **Frontend**: `make web-install` once, then `make web-build` (which runs
  `tsc --noEmit` first, so the build is the type gate) or `make web-dev` for
  the Vite server on 5173 proxying to `nodum serve` on 8420. Two gates, both in
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

## CLI contract (for agents driving the CLI)

- Every command prints **one JSON object** on stdout and nothing else on the
  success path — parse stdout directly. A command returning a list wraps it in
  a named key plus a `count` (`{"nodes": [...], "count": 2}`); keep new list
  commands to that shape.
- DB path resolution: `--db` flag → `NODUM_DB` env var →
  `~/.local/share/nodum/nodum.db`.
- Writes default to actor `human` (state `active`); pass `--actor agent:<name>`
  to land writes in `proposed` instead — unless the agent's stored policy
  auto-accepts the write (`policy set`). An agent `node update` stages a
  `proposed` *version* recording which fields it named; `accept <version-id>`
  applies **only those fields** to the node as it stands then (so a human edit
  made while the proposal waited is not reverted), `reject` archives it.
  A `[[wikilink]]` written by an agent materialises a `proposed` `mentions`
  edge; accepting the node brings it to `active`.
- **Everything that writes or retires live state requires `--actor human`**
  (the default): `accept`, `reject`, `archive`, `undo`, every `review`
  subcommand, and `policy set` (a policy grants auto-accept, so an agent
  setting one would self-grant the direct live write the human tier withholds).
  An `agent:*` actor exits 1 with `only the 'human' actor may
  <action>`. It is not delegable, whoever filed the proposal — `undo` most of
  all, since restoring an event's payload can write `state = 'active'` back.
  Both spellings of a reject — single-item `reject <id> --reason` and batch
  `review reject … --reason` — require the reason and record it in the reject
  event's payload: one operation, one audit guarantee.
- Errors are always one line on stderr with exit 1, never a traceback — that
  includes a missing file (`asset register /missing.png`), a database another
  writer holds (`database error: database is locked`), and an undo the graph
  has grown past (a created node that now has children).
- `--set key=value` is repeatable; values are parsed as JSON with a raw-string
  fallback.
- A policy rule's `min_confidence` grades the *agent's own* reported
  confidence, so it is inert unless the rule also sets
  `"trust_self_reported_confidence": true`.
- Surface: `init`, `node create/get/update/list/children`, `edge
  create/list/create-batch`, `accept <id>` / `reject <id> --reason` /
  `archive <id>` (each takes a node, edge, or proposed-version id), `undo [seq]`,
  `history <node-id>`, `events`, `types`, `schema <type>`, `search <query>`,
  `traverse`, `subgraph <root-id>`, `suggest-links <prefix>`, `find-path`,
  `diff`, `projector run/status/rebuild`,
  `policy set/get/list`, `review queue/accept/reject/accept-all/reject-all`,
  `asset register/get/list/rendition/purge`,
  `mcp serve --actor agent:<name>`,
  `serve [--host 127.0.0.1] [--port 8420] [--token TOKEN] [--db PATH]`.
- Reads are not state-filtered by default beyond edge traversal: `node get`,
  `node children`, `node list`, and `history` return `proposed` rows, and
  `search --state any` includes them. Only *traversals* (`node get --depth`,
  `traverse`, `subgraph`, `find-path`, `search --expand`) are restricted to
  `active` edges — proposed structure is inert, not hidden. `subgraph
  --edge-state proposed` is the one way to walk it, and it has to be asked
  for. `suggest-links` follows the node-read rule with one exception:
  `archived` titles are never suggested, since a retired node is not a link
  target.
- `subgraph` is the bounded read: `--limit` is a hard node cap applied while
  walking, so no caller can provoke an unbounded result, and `truncated` in
  the response says whether the cap cut the walk short. A limit below 1 is an
  error rather than SQL's "unbounded". Every filter composes as one
  conjunction, and an edge whose far node is filtered out is dropped with it —
  the result never names an edge endpoint it does not also return.
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
  which is the only place the actor is bound.
- **Do not invent request fields the domain has no representation for.**
  `PUT /api/policies/{agent}` takes `{"rules": [...]}` and nothing else: a
  policy is disabled by storing an empty ruleset, which is the service's only
  spelling of it, and `PolicyOut` has no `enabled` field to echo one back. An
  `enabled: false` flag was tried and removed — it silently wiped the stored
  ruleset with no way to recover it. Same rule everywhere: if a body key has
  no counterpart in `nodum.models`/`nodum.service`, it does not belong here.
- Responses use `nodum.envelope`: single results as the model dump, lists as
  `{"<plural>": [...], "count": n}`, rendered exactly as the CLI prints them.
  A new list endpoint keys on the same plural the CLI command uses.
- Failures are `{"error": {"type", "message"}}` from `EXCEPTION_STATUS`; add a
  new mapping there rather than catching in a handler. Anything unmapped is a
  500 with a generic body — never leak a traceback to a client.
- Repeatable filters (`edge_type`, `edge_state`, `node_type`) are repeated
  query keys; `/healthz` sits outside `/api` and outside auth; unknown `/api`
  paths are JSON 404s while unknown non-API paths fall through to the SPA
  entry point (or the "UI not built" placeholder). **`/favicon.ico` is the one
  exemption**: a browser asks for it unprompted and it is definitely not a
  client route, so it is answered with the bundle's icon if there is one and a
  204 otherwise — never an HTML document under a 200, which a client asking for
  an image has no way to detect. Any other path a browser requests on its own
  belongs in that same exemption list, not in the catch-all.
- Asset originals are never served — only `thumb`/`preview` renditions, as
  WebP bytes at `/api/assets/{id}/rendition/{profile}` (design §5.7).

## Frontend contract (for agents touching `web/`)

- **One `fetch`.** Everything goes through `src/api/client.ts`. It has no actor
  parameter and must never grow one — the server forces `actor = human` and the
  client being unable to express an actor is the second layer under that.
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
  harness is unit-only by design — no DOM, no component rendering — so pull the
  logic worth testing out of the component and test it there, which is what
  `filters.ts`, `unifiedDiff.ts`, `signals.ts`, `grouping.ts`, and
  `policyRules.ts` already are. Assert the *semantics* the module encodes (a
  `min_confidence` of 0 is a filter, not a no-op; a 502 is unreachable, not a
  refusal), not its line coverage.
- **A dialog locks body scroll and hands focus somewhere real.** Both the review
  `Modal` and the assets lightbox set `body.style.overflow` on open and restore
  it on close. On close, focus returns to the opener *only if it is still in the
  document* — after a successful confirm it usually is not, and focusing a
  detached node silently drops the user on `<body>`. The view places focus in
  that case (the review inbox sends them to the outcome panel).
