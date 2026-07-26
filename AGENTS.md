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

Phase 1 (core) and Phase 2 (agent-native) have landed: **event-log projectors** (`nodum.projectors`) with per-projector
checkpoints and rebuild mechanics, the **`fts` projector** (FTS5 over node
title + content), the **`vec` projector** (sqlite-vec chunk embeddings,
local in-process fastembed model — migration 0006), **hybrid search**
(`nodum.search`, CLI `search`): BM25 + vector lists fused by reciprocal rank
fusion, then one-hop graph-expansion re-ranking, with a per-signal `signals`
breakdown, **principals, spaces and grants** (Q13: `humans`/`agents`/
`grants` tables, a scope-bound store, `read`/`suggest`/`edit` per
(agent, space) — no policies, no auto-accept anywhere), the
**review/accept API** (proposal listing with reviewer
context, where every referenced node is reported as `{id, title, space_id}` so
the human UI's queue can group by space without chasing ids, plus batch
accept/reject by id or filter — a human, or `edit` on the
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
`/api` and the built web UI at `/`, gated on password-login sessions with
every write attributed to the session's human and no request field able to
say otherwise — the shared **envelope**
module (`nodum.envelope`) both the CLI and the API render through, and the
**web UI** itself (`web/`, React 19 + TypeScript, built into `nodum/_web/` by
`make web-build`; gitignored, shipped in the wheel as a hatchling artifact):
nine views — login, Markdown editor, hybrid search, review queue,
graph, assets, a spaces screen, an accounts-and-grants admin, per-node version
history.
Phase 4 (ingestion) has landed: **text extraction** (`nodum.extract` — a
registry of optional handlers keyed by MIME family, where an absent dependency
is a returned result and never an exception), the **ingestion pipeline**
(`nodum.ingest` — `ingest_file`/`ingest_url`/`ingest_upload`: bytes become an
asset, an `asset_ref` node describing them in one space, a `source` node
carrying the extracted text, a `derived_from` edge, and one `block` per page,
all through the public service API and therefore landing per the writer's
grant; idempotent per `(hash, space)`), **`page:<n>` PDF rasters** beside
`thumb`/`preview`, the `fts` projector's join of `assets.extracted_text` onto
`asset_ref` nodes, and **capability URLs** (`nodum.urls`, migration
`0012_url_tokens` — short-lived, single-use, event-logged download and upload
grants for a host that shares no filesystem with the graph). The surfaces:
CLI `ingest file|url|handlers` and `asset download-url|upload-url`, MCP
`ingest_file`/`ingest_url`/`request_upload_url`/`get_download_url`, and HTTP
`POST /api/ingest`, `POST /api/assets/{id}/download-url`, `POST /api/uploads`,
`GET /api/download/{token}`, `PUT /api/uploads/{token}`.
**Deliberately not built yet** (later phases — do not add): **claim
proposals**, which moved to Phase 5 deliberately rather than being forgotten —
deciding that a sentence *is* a claim is a judgement call and belongs to the
research agent in design §3, and splitting prose into sentences would fill the
review queue with noise instead of knowledge, so ingestion proposes sources and
structure and stops; the internal agent runtime and
consolidation cycle, **Markdown Mirror** and any whole-graph export (the only
export that exists is the thin per-node snapshot,
`GET /api/export/node/{id}?depth=`, which is `get_neighborhood` with a
`content-disposition` header — not a format, not a backup), the curative tier
(`merge_nodes`, `retype`, `supersede_edge`, `bulk_relink`, `consolidate`),
and the **dream-journal view**, which Phase 3 deferred to Phase 5 on purpose —
it belongs with the consolidation cycle that gives it something to show. The
schema reserves room for them (`merge_redirects`, `cycle_id`); each lands as
its own append-only migration.
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
  never run. **Spaces** live here too: the read-side `space` filter on
  `list_nodes` (and its twin in `nodum.search`), and the lifecycle trio
  `create_space` / `rename_space` / `archive_space` plus the `list_spaces`
  aggregation — thin delegates to `create_node` / `update_node` / `transition`
  that own the "a space is a node of type `space` in meta" rule so that neither
  adapter has to restate it, and no new SQL path exists for a space write.
  Two space rules are enforced **here** rather than on a screen, because a
  disabled button leaves the CLI and the API wide open. **`main` and `meta`
  cannot be archived** (`STRUCTURAL_SPACE_IDS`): the check sits in
  `_transition_row`, not in `archive_space`, since `archive <id>` and
  `POST /api/nodes/{id}/archive` reach the same row without going near the
  lifecycle helper. Archiving `main` is destructive in the quietest way there
  is — it vanishes from `list_spaces` and every picker, while
  `resolve_space_id(None)` keeps returning it without reading the row's state,
  so writes go on landing in a space the human can no longer see. A *rename*
  of either stays allowed: it moves the title, and the **id** is what the
  schema and the default write target depend on. **No two spaces may answer to
  one name** (`_require_space_name_free`): a reference resolves as
  `id = ? OR title = ?`, so a duplicate makes `--space research` mean whichever
  row SQLite reached first. The check runs in `create_node` and `update_node`
  (conditioned on the node being a space), in `_transition_version`'s accept
  (where a proposed rename actually lands) and in `undo` (which writes a
  recorded row back past every other guard), because those are the paths that
  bypass the lifecycle helpers; migration `0013_unique_space_titles` is the
  structural half under it — a unique index over `nodes(title)` where
  `type_id = 'space'`, with **no state predicate**. **A space title is reserved
  forever, archived ones included.** The first cut scoped the index to
  `state != 'archived'`, arguing that an archived space stops resolving and
  that nothing un-archives; `undo` does — it restores the `before` row with a
  raw UPDATE past `TRANSITIONS` — so a freed-then-retaken name made undoing an
  archive die on `UNIQUE constraint failed` (a 500 on `/api/undo`). Archiving
  is not deletion, so it must be reversible; the accepted cost is that a
  retired space's name cannot be reused. A collision is `SpaceNameTaken`
  (a `ValueError`, **409** over HTTP), and when the holder is archived the
  message says so — nothing lists archived spaces, so a bare "taken" would name
  something the human cannot see. Comparison is BINARY, like the lookup's —
  `Research` beside `research` is two names that genuinely tell themselves
  apart. The service check additionally catches the half no index can express:
  a title equal to another space's *id*.
  Each public function opens its own short-lived connection
  (applying pending migrations idempotently) and commits. New behaviour and
  validation go here first; adapters must not add behaviour the service lacks.
- **`nodum.mcp_server`** — the MCP adapter (stdio, official Python SDK
  FastMCP), the **external-agent** surface. Registers the design §8.1 read +
  additive tiers and nothing else, each tool a thin delegate to a
  service/search/ingest function. Phase 4 adds `ingest_file`, `ingest_url` and
  `request_upload_url` (additive) plus `get_download_url` (read — where §8.1's
  own table puts it), and `get_asset` now carries the **extracted text**
  (capped at the `source` node's own cap, with the real length and a
  truncation flag reported) and serves **`page:<n>` PDF rasters** beside
  `preview`/`thumb`; an unknown profile is still refused and originals still
  never cross this surface. Ingestion is **by reference** (§5.7 rule 2): the
  tool takes a path the server can read or a URL it can fetch — an
  `http`/`https` value routes to `ingest_url`, anything else is a local path —
  and **no base64 ever crosses MCP**; a host sharing no filesystem with the
  server asks `request_upload_url` for somewhere to PUT instead.
  `get_download_url` is the design's one documented exception to "LLMs never
  receive original binaries" (§5.7 rule 4): a single-use, minutes-long URL
  built on `NODUM_PUBLIC_URL`, with the mint and the redemption both in the
  event log. Annotations state each tool's **worst case**: reads are
  `readOnlyHint` — `get_download_url` included, since it writes an expiring
  capability row and an audit entry but no node, edge, or version — the
  additive tools are `destructiveHint=False` (they only ever add state,
  whatever grant the caller holds: every graph write ingestion makes is a
  `create_node` / `create_edge`, so an `edit` grant's worst case is a subgraph
  landing `active` instead of `proposed`, which is more state and not state
  replaced), and `update_node` is `destructiveHint=True` because under an
  `edit` grant it overwrites the node in place — MCP hosts auto-approve on
  that flag, so it must not lie. Every write tool's description says what an
  `edit` grant changes rather than promising `proposed`. Auth is the agent token in `NODUM_AGENT_TOKEN` —
  an `ndm_…` token minted by `nodum agent create` / `token-rotate`, shown
  once and stored hashed — carried in the environment, never a flag (a flag
  leaks into `ps` and shell history). At startup it is verified against the
  `agents` table (an unknown or disabled agent is a startup error), the
  verified agent's principal is loaded with its grant set, and every read
  and write is confined to those grants. The review tools
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
  expiry, the row keyed by the cookie's sha-256 so the table never holds a
  live credential) and sets an `HttpOnly; SameSite=Strict` cookie;
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
  `nodum/_web/` by `make web-build` and served by `nodum serve`. Ten routes
  over nine views, each lazily loaded so CodeMirror, Mermaid, and Cytoscape stay
  out of the initial bundle. `src/api/client.ts` is the only `fetch` in the
  app and has **no identity parameter anywhere** — the server's structural
  rule, mirrored in the client. It sends `Content-Type: application/json` on every
  non-GET request, bodyless ones included, because the server requires it.
  `src/lib/` holds the cross-view invariants
  (timestamps, failure classification, the sticky write target);
  `src/components/` holds shared React
  components plus the space filter's two halves (`spaceOptions.ts`,
  `useSpaces.ts`); a view owns its own directory and links to other views by URL,
  never by import. Spaces reach the UI as the CLI's two independent controls —
  a per-view read filter and one app-wide write target — never as a mode. Full
  conventions: `web/README.md`.
- **`nodum.projectors`** — derived-index consumers of the event log. A
  projector registry (`REGISTRY`), per-projector checkpoints in
  `projector_checkpoints`, incremental `run_projectors`, and
  `rebuild_projector` (reset derived state, replay from event 0). The `fts`
  projector maintains `node_fts`; the `vec` projector maintains `chunks` +
  `node_vec` (rebuild = the model-change re-embed path, design D6). The
  `fts` projector also joins `assets.extracted_text` into the index row —
  **for `asset_ref` nodes only**, and that restriction is load-bearing, not
  tidiness. Ingestion records `asset_hash` on the `source` node and on every
  per-page `block` too, but those nodes already carry their own text: joining
  on the prop alone gave every page of a document the *whole document's* text,
  so a word on page 3 matched pages 1, 2 and 4 just as strongly, and the
  `source` node got its text twice — in `content` and again here —
  double-weighting it in BM25. The `asset_ref` node is the one whose own
  `content` is empty, so without the join a PDF's text would be findable
  through nothing at all. It is a read of *live* state inside an event replay,
  deliberately: `assets` is not event-logged (there is nothing to undo about
  content-addressed bytes), so text stored after a node was projected is not
  indexed until that node is projected again or `projector rebuild fts` runs —
  which is exactly why the pipeline calls `assets.set_extracted_text` **before**
  it creates the `asset_ref` node. The
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
  renditions (design §5.5/§5.7). Reads take a principal, and **an asset is as
  reachable as its describing nodes**: a principal may read an asset iff it can
  read an active `asset_ref` node carrying the hash. Asset rows are deduped
  globally by sha256, so a `space_id` column here could only lie about the
  second space to register the same bytes; the per-space thing is the node, and
  0009's unique index is already `(asset_hash, space_id)` over those nodes.
  Bytes nobody has described are visible to humans only — the right default for
  freshly registered bytes whose ingestion has not run. **Bytes live in the database, not on the
  filesystem**: `assets` holds metadata (including the `extracted_text`
  ingestion writes through `set_extracted_text`, which takes no principal and
  logs no event — content-addressed base state, like registration itself),
  `asset_blobs` holds the bytes under
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
  `asset purge`) — fully regenerable. Non-image assets are rejected cleanly for
  those two profiles. **`page:<n>` is the third profile shape**
  (`resolve_profile`): a 1-based page of a PDF rasterised by `pypdfium2` at
  `PAGE_DPI` (144 — exactly 2× the PDF canvas unit, so a text page is legible
  without a resample), then encoded down the *same* WebP path, so a page and a
  photograph share their quality stepping, their id scheme, their cache, and
  their eviction. `pypdfium2` won on licence: PyMuPDF renders at least as well
  and is AGPL, which would reach anything embedding nodum, while PDFium ships
  permissive wheels needing no system package. The import is lazy and the
  dependency sits behind the `pdf` extra, so an install without it still serves
  image renditions and answers a page request with an `UnsupportedRendition`
  naming the extra rather than an `ImportError` at startup. A raster has no
  image header to read, so its pixel budget is arithmetic (page geometry × the
  DPI scale) — PDF permits a 200×200 inch page, which is 829 MP at 144 DPI.
  Pillow reads originals through
  `_BlobReader`, which restores the file-style tolerant seeks that
  `sqlite3.Blob` refuses and Pillow's format probing depends on.
- **`nodum.extract`** — MIME → text, through handlers that degrade instead of
  failing. A registry shaped exactly like the embedding provider seam: each
  handler declares the MIME families it claims and whether it can run, and
  **an absent dependency is a returned `Extraction`, never an exception** —
  `extract()` on a machine with no OCR still returns a result, so ingestion
  still registers the asset, still writes the describing node, and says in
  `detail` that no text came out. The same rule covers a broken input: a
  corrupt PDF is a `detail` string, not a traceback climbing out of the
  pipeline. Registry order is `text`, `html`, `pdf`, `image`, `audio` and the
  first handler claiming the MIME wins; `text` claims `text/*` plus JSON but
  stands aside for the two HTML types, which the next handler parses properly
  (otherwise markup lands in the graph as literal tags). `text` and `html` are
  stdlib-only and therefore **always available**, which is what makes the
  pipeline end-to-end on a bare install; `pdf` (`pypdf`), `image`
  (`pytesseract` *and* the tesseract binary — two conditions, reported apart,
  because "install the extra" is the wrong advice for a missing binary) and
  `audio` (`faster-whisper`) sit behind the `pdf`/`ocr`/`audio` extras and
  report themselves unavailable until installed. `NODUM_AUDIO_MODEL` and
  `NODUM_AUDIO_DOWNLOAD` mirror the embeddings posture exactly: without the
  download flag faster-whisper is held to its local cache, so an uncached model
  is an unavailable handler rather than a few hundred megabytes off the network
  because someone ingested an `.mp3`. `video/*` is deliberately **unclaimed** —
  pulling text out of one means demuxing with ffmpeg, a non-Python binary this
  project does not otherwise need, to transcribe a file whose visual content is
  usually the point. Every result is capped at `MAX_TEXT_CHARS` (2 M, ~600
  pages) because the text becomes a database row, and a cap that bit is
  reported in `detail` — truncation is never silent. Paginated formats also
  return per-page text (`pages[n - 1]` is page *n*, empty pages kept so the
  numbering holds), and pages are capped rather than the joined text so a
  caller writing one block per page is never handed page text the capped `text`
  dropped.
- **`nodum.ingest`** — the pipeline (design §5.5–§5.7): bytes in, reviewable
  subgraph out. `ingest_file`, `ingest_url`, `ingest_upload` all converge on
  one path — register the asset, extract, then write an `asset_ref` node
  (the description that makes the bytes reachable *in one space*), a `source`
  node whose content is the extracted text, a `derived_from` edge from source
  to bytes, and one `block` child per page that carries text. **Every graph
  write goes through the public `nodum.service` API**: ingestion adds no
  authority of its own, so a `suggest` grant gets the whole subgraph
  `proposed` and an `edit` grant gets it live. Extracted text lives in **two**
  places on purpose — the full text on `assets.extracted_text`, where the FTS
  projector joins it and BM25 reaches every word, and a capped copy
  (`SOURCE_CONTENT_CHARS`, marked when cut) as the `source` node's content,
  which is what the vec projector chunks and embeds, since semantic search only
  ever sees node text. One store would have cost one of the two signals.
  **Idempotent per `(hash, space)`** — registration is content-addressed and
  0009's unique index allows one live `asset_ref` per pair, so a re-run finds
  the describing node instead of tripping the index, and a run interrupted
  between the two node writes is repaired by running it again. Blank pages are
  skipped (a scanned PDF with no OCR handler would otherwise propose a hundred
  empty nodes) while the page number stays in props, so numbering is honestly
  sparse rather than quietly renumbered, and `MAX_PAGE_BLOCKS` (100) stops a
  900-page scan from becoming a 900-item review queue — the overflow is
  reported through `pages_truncated`, never dropped silently. `ingest_url` is
  `http`/`https` only, one bounded read with a timeout, redirects confined to
  the same two schemes (urllib would otherwise follow one to `ftp:`); it does
  **not** block loopback or private ranges, because the server is itself a
  loopback service and its own test fixture is one — anything that can call it
  already has the machine's network position, and that is stated rather than
  half-defended. `ingest_upload` exists because the upload hatch would
  otherwise dead-end: it re-mints the principal from the token row's
  `created_by` (the account that authorised the upload, while it was still
  authenticated), which is why it lives here and not in the HTTP adapter — that
  adapter is structurally forbidden from minting an identity. A disabled
  account fails there, so a capability cannot outlive its principal's
  revocation. **Claim extraction is deliberately absent** (Phase 5).
- **`nodum.urls`** — short-lived, single-use capability URLs (design §5.7
  rule 4), the escape hatch for an agent host that shares no filesystem with
  the graph: `mint_download` hands out a URL for an asset's original,
  `mint_upload` a place to PUT bytes exactly once. They are escape hatches, so
  both ends of both are event-logged (`asset.download_url`/`asset.upload_url`
  on the mint, `asset.download`/`asset.upload` on the redemption) — audit
  records only, which `service.undo` refuses by name, and **no payload ever
  carries bytes or the secret**; a payload names the token's public id. **A
  token is a capability, not a signature**: 256 bits from `secrets`, only its
  sha-256 stored, the same treatment `nodum.auth` gives an agent token. An
  HMAC-signed URL would move the authority into a key to generate, store,
  rotate and keep out of every backup — and would still need a table the moment
  anyone wanted a URL spent or revoked, because a signature is valid until it
  expires and not one moment less. Here the row *is* the authority: expiry,
  single use and revocation are one `UPDATE` on it. Single use is enforced by
  **rowcount, not by reading first** — redemption is one
  `UPDATE … WHERE used_at IS NULL AND expires_at > datetime('now')` and the
  token is spent iff that matched a row, so two concurrent redemptions cannot
  both win. **No Python clock is involved anywhere in the module**: every
  timestamp is SQLite's `datetime('now')`, because the stored strings carry no
  zone marker and a naive `datetime.now()` comparison would honour expired
  tokens for the length of the host's UTC offset — and pass every test run in
  UTC. TTL defaults to five minutes and is bounded at one hour; it is checked
  when the request *starts*, so a slow transfer is never cut off by it.
  `MAX_UPLOAD_BYTES` is deliberately equal to `http_api.MAX_REQUEST_BYTES` and
  must never exceed it (a grant promising more than the server will read fails
  halfway through the transfer); the value is duplicated rather than imported,
  because a domain module has no business importing an adapter. `TokenInvalid`
  is one class with one message for unknown, expired, spent, and wrong-kind —
  telling them apart is free intelligence for whoever is guessing. Both mints
  resolve their asset through `assets.get_asset`, so an asset the principal
  cannot read answers *not found*; the upload dedup shortcut is scoped the same
  way, since answering "that already exists" for anything else would turn the
  endpoint into an existence oracle over every byte in the file.
- **`nodum.search`** — the query path (design §7). BM25 over the `fts`
  projector's index and vector ANN over the `vec` projector's chunks
  (closest chunk per node wins), fused by reciprocal rank fusion (K=60) with
  `type`/`state`/`created_by`/date filters; optional one-hop graph expansion
  over `active` edges (`--expand`) applies after fusion. Hits carry the
  fused `score` plus a per-signal `signals` breakdown (`bm25` / `vector` /
  `graph`) **and their `space_id`** — a result list spans every space in scope
  unless `space` narrowed it, so a hit that did not name its own would be
  unplaceable on the one surface a human scans rather than reads. All three
  hit shapes carry it (both ranked lists build `_RankedRow`, graph expansion
  builds its own `SearchHit`); adding a fourth means carrying it there too.
  With no embedding provider the vector signal is skipped —
  search silently degrades to BM25 + graph.
- **`nodum.db`** — connection management (WAL, foreign keys), `NODUM_DB`
  resolution, the migration runner. Each migration's script and its
  `schema_migrations` row are one transaction (`apply_migration`), so an
  interrupted upgrade rolls back whole and retries cleanly instead of wedging
  the database half-migrated. A migration runs with **`foreign_keys=OFF`** and
  is checked with `PRAGMA foreign_key_check` before its commit: deferring the
  constraints instead cannot work for a table rebuild, because dropping a
  populated parent leaves a deferred-violation counter the rename does not
  clear — 0009 could not upgrade a database holding a single node and its
  version row. The schema-consistency check runs **before** the apply loop, so
  a database whose only cure is deletion never gets a new (possibly
  irreversible) migration committed onto it first.
- **`nodum.migrations`** — the append-only migration list (`0001_core` …
  `0013_unique_space_titles`). Never edit a shipped migration; append a
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
- **The CLI is human-only, and every command that touches the graph names its
  human** with a required `--as human:<id>` (or the bare id) — reads included,
  since reads are grant-scoped like writes: attribution is explicit, always
  (there is no `--actor` — agents drive MCP, never the CLI). A write by a
  human lands `active`. An agent's write (over MCP) lands per its grants:
  `suggest` → `proposed`, `edit` → `active`. An agent `node update` with
  `suggest` stages a `proposed` *version* recording which fields it named;
  `accept <version-id>` applies **only those fields** to the node as it
  stands then (so a human edit made while the proposal waited is not
  reverted), `reject` archives it. A `[[wikilink]]` written by an agent
  materialises a `proposed` `mentions` edge; accepting the node brings it to
  `active` — but only for the edges the acceptor could review directly, so a
  mention into a space they hold nothing on stays queued. Re-materialisation
  is gated the same way: retiring a `mentions` edge needs `edit` on **both**
  endpoint spaces, and a target the writer cannot read is never treated as a
  link that disappeared.
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
- **A space is two independent controls, not a mode** (the human-UI phase's
  D1): reads take an optional `--space` **filter** that defaults to *every
  space in scope*, and writes take a `--space` **target** that defaults to
  `main`. Reading `research` while still filing into `main` is the ordinary
  case, so one switch could not serve both. The filter **narrows** and never
  widens: it resolves through the same rule every other space reference does
  (a space the principal holds no grant on does not resolve, and reads
  identically to a nonexistent one), and the principal's scope clause is still
  ANDed underneath it — an agent is confined by its grants whatever it asks
  for. `--include-meta` is the other read-side control, off by default;
  naming the meta space with `--space meta` is the same opt-in said precisely,
  since `meta` is itself in the space list and a filter that silently returned
  nothing there would be a trap.
- Surface: `init`, `node create/get/update/list/children`, `edge
  create/list/create-batch`, `accept <id>` / `reject <id> --reason` /
  `archive <id>` (each takes a node, edge, or proposed-version id), `undo [seq]`,
  `history <node-id>`, `events`, `types`, `schema <type>`, `schema-dump`,
  `search <query>`,
  `traverse`, `subgraph <root-id>`, `suggest-links <prefix>`, `find-path`,
  `diff`, `projector run/status/rebuild`,
  `review queue/accept/reject/accept-all/reject-all`,
  `asset register/get/list/rendition/purge/download-url/upload-url`
  (everything except `register`/`purge` reads through the graph and so takes
  `--as`; those two touch the blob store alone),
  `ingest file <path>… / url <url> / handlers` (`handlers` takes no `--as` —
  it reports the install's extraction handlers, not the graph),
  `human create/list/passwd/disable/enable` (a password is at least
  `service.MIN_PASSWORD_LENGTH` characters, and the last enabled human cannot
  be disabled — no enabled human means no principal on any surface, including
  the CLI's own trusted-local path),
  `agent create/list/token-rotate/disable/enable` (create and rotate print
  the show-once `ndm_…` token to stderr; only the hash is stored),
  `grant <agent> <space> <level>` / `revoke <agent> <space>` / `grants [--agent]`
  (`read`/`suggest`/`edit`, event-logged),
  `space-create`/`space-list`/`space-rename`/`space-archive` (a space is a node
  of builtin type `space` in the meta space, so its whole lifecycle is an
  ordinary node's — create, a title update, a state transition — and every one
  is event-logged, versioned and undoable like any other write; the three
  mutating commands go through `service.create_space`/`rename_space`/
  `archive_space`, which own the "a space is a node in meta" rule so no adapter
  has to, and refuse a node that is not a space rather than editing it under a
  space-shaped name. `space-list` (`service.list_spaces`) carries each space's
  **live node count** — `active` + `proposed`, since a space holding only
  proposals is not empty — and the **agents granted on it**, which is human-only
  for the same reason `grants` is),
  `mcp serve` (the agent token comes from `NODUM_AGENT_TOKEN`, never a flag),
  `serve [--host 127.0.0.1] [--port 8600] [--allow-host NAME]
  [--db PATH]`. `serve` prints the database path on stderr and translates
  uvicorn's own startup failure (a port already in use) into the contract's
  exit 1 — it used to escape as uvicorn's exit 3. A non-loopback bind is
  allowed (password login, not the bind, is the boundary), marks the session
  cookie `Secure` there, and warns on stderr that uvicorn speaks plain HTTP —
  the cookie fails closed without TLS, but the login body has already crossed
  the network by then.
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
  `--profile` takes `thumb` or `preview` for an image asset and `page:<n>` for
  a 1-based page of a PDF; a page raster is an ordinary rendition otherwise
  (same lazy generation, same cache, same eviction by `asset purge`) and needs
  the `pdf` extra, which it names rather than failing at import time.
- **`ingest file` takes one or more paths, and a directory argument ingests the
  files directly inside it** (`--recursive` walks deeper). Dot-names and
  anything that is not a regular file are skipped, and the rest are ingested in
  sorted order, so the same folder ingests the same way twice. One path naming
  a *file* prints that ingestion as a single JSON object; anything else —
  several paths, or a directory, whatever it happens to contain — is a batch
  and prints `{"ingestions": [...], "count": n}`. `--name` and `--title`
  describe one document and are refused for a batch; `--space` applies to all
  of it.
- **A batch never loses its successes.** Each file is ingested on its own; one
  that fails prints the same one-line reason a single-file run would, followed
  by `  skipped <path>`, and the batch carries on. Every file that landed is in
  the envelope on stdout, printed before the exit code is decided. **The exit
  code is 1 if any file failed**, so a non-zero exit from `ingest file` means
  "read stderr for what is missing", not "nothing happened". Re-running the
  same batch is safe: ingestion is idempotent per `(hash, space)`, so what
  already landed comes back with `created: false` instead of being duplicated.
- `ingest url` fetches `http`/`https` only, once, with a timeout and a size
  ceiling, and refuses a redirect that leaves those two schemes. It does *not*
  block loopback or private ranges — this is itself a loopback service — so
  granting ingestion grants the server's network position.
- `ingest handlers` is the answer to "my PDF produced no text": it lists every
  extraction handler with its MIME families, `available`, and — when a handler
  cannot run — a `detail` naming the extra to install. It needs no principal
  and no database.
- The two capability-URL commands are the escape hatch for a host that shares
  no filesystem with the graph (design §5.7 rule 4). `asset download-url <id>`
  and `asset upload-url --name --mime --size` mint a short-lived, single-use
  URL, print the token **once** (only its sha256 is stored), and log both the
  mint and the later redemption. `--ttl` is bounded (1 s to 1 h). An
  `upload-url` whose `--sha256` this graph already holds answers with the
  existing `asset` and **no** `grant` — the bytes are here, so no bytes move.
  The URLs resolve against `nodum serve`; set `NODUM_PUBLIC_URL` when that
  server is not on the default address.

## HTTP contract (for agents touching `nodum serve`)

- **The HTTP surface is the human's.** Every write it makes is attributed to
  the session's human principal; the identity is never read from a request.
  Do not add an "actor"
  parameter, header, or override "for testing" — the MCP surface is where
  agent identity lives, and the inversion is the whole point.
- Route handlers are thin delegates: one service/search/assets/ingest/urls call
  each, no behaviour the domain lacks. Writes go through `_write(service.fn, …)`
  — including `ingest.ingest_file`/`ingest_url` and `urls.mint_*`, which take a
  `principal` like any service write —
  and that is the only place the principal is bound for a write. **Never import a
  service function that takes a `principal` into `http_api`** — an alias hides
  it from every
  source-level check, and `test_no_write_service_function_is_reachable_under_
  any_name` fails on the import itself. Never splat request data into a call
  either: `**` may only unpack a dict an allowlisting helper built, and any new
  one fails `test_no_call_splats_anything_but_an_allowlisting_helper` until it
  is reviewed.
- **The test that actually holds the boundary is the runtime sweep**
  (`test_writes_are_attributed_to_the_sessions_human_and_nothing_else`): it
  drives every
  state-changing method of every route in `app.routes` — behind a real
  session, re-logging in when the sweep hits `/api/logout` — with
  actor-carrying
  bodies, query strings and headers, then asserts nothing written during the
  sweep is attributed to anything but the session's human. The AST properties
  beside it are a belt —
  all of them were evadable by a handler that forwarded a body it never
  inspected, which is how a rogue endpoint once produced
  `created_by: "agent:evil"` on a fully green suite.
- **A state-changing request must prove it is same-origin**
  (`RequestGuardMiddleware`), because `nodum serve` binds loopback and loopback
  is reachable from every page the user visits. The rule:
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
- **The session gate is one rule: every `/api` route `_needs_a_session` claims
  needs a valid session, reads included.** A single-human file has nothing an
  anonymous caller should see, and one rule is the one no future endpoint can
  forget. The cookie is `HttpOnly; SameSite=Strict` over a server-side row
  with a 30-day sliding expiry; logout, expiry, and `human disable` all kill
  it at the next request (verification-time, no cache). Any local process can
  satisfy every origin check with three curl headers, so it may *attempt* a
  login — the human's password is the whole defence there, and the `serve`
  banner says so. The predicate has exactly two exemptions — `/api/login`,
  which *makes* sessions, and the two capability-URL routes below — and
  `test_the_only_api_routes_outside_the_session_gate_are_login_and_the_
  capability_urls` reads them off the live route table, so a third one cannot
  arrive quietly. **Add an exemption to the predicate, never to a call site**:
  the string used to be compared inline, and three inline comparisons is how a
  gate and its exemption drift apart.
- **The two capability-URL routes are the one thing here that is not a
  session.** `GET /api/download/{token}` streams an asset's original bytes and
  `PUT /api/uploads/{token}` stores a raw body; both are redeemed by an agent
  host that has no filesystem in common with this server and no account here.
  They sit outside the session gate **and** outside the origin/content-type
  gate, and that is deliberate: those gates exist because a browser attaches
  the session cookie by itself, which is what CSRF rides. A capability URL
  carries no ambient credential — the single-use, minutes-long token in the
  path *is* the authorisation, minted by `nodum.urls` against a principal the
  session gate already checked — so a cross-origin page has nothing to ride,
  and demanding `Content-Type: application/json` on a raw-bytes upload is
  incoherent anyway. Both exemptions key on one predicate,
  `_is_capability_path`, whose docstring carries the argument; read it before
  touching either gate. **What is *not* exempt**: the `Host` check (rebinding
  is about which server was reached, which a capability changes nothing about)
  and the body ceiling (`urls.MAX_UPLOAD_BYTES` is deliberately equal to
  `MAX_REQUEST_BYTES`, so a grant can never promise more than this server will
  read). Neither route may call `_session_principal` — there is no session to
  read, so it would raise — and neither writes to the graph; the redemption is
  attributed inside `urls.consume`, to the token row's own `created_by`, which
  is stored state rather than anything the request said.
- **A downloaded original is served as `application/octet-stream`, never as
  its stored MIME**, with `nosniff`, `attachment`, `no-store` and a filename
  built from the content hash. Serving a stranger's `text/html` back from this
  origin — the origin that may write to this API — is stored XSS, and
  `CONTENT_SECURITY_POLICY` does not reach this route (it is set by the static
  handler). The bytes stream out of the blob in 1 MiB chunks; never read an
  original into memory to send it.
- **`PUT /api/uploads/{token}` registers bytes and stops there.** Registration
  is content-addressed base state and needs no identity, which is why it can
  happen without a session at all. The `asset_ref`/`source`/`derived_from`
  subgraph is a *graph* write, needs a live principal with a grant on the
  space, and this request has none — so freshly uploaded bytes have no
  describing node until someone ingests them behind a session, which Phase 4's
  plan calls the correct default for an ingestion that has not finished.
  Closing that gap belongs in the domain layer, where the token row's
  principal can legitimately be loaded; it must not be closed by an adapter
  inventing a principal.
- **`POST /api/ingest` takes exactly one of `path` and `url`** (plus optional
  `name`/`space`/`title`); both or neither is a 400 rather than a precedence
  rule nobody remembers. Note what it hands the session's human, deliberately:
  `path` is read *by the server*, so it reaches any file the server's user
  can, and `url` is fetched *by the server*, which `nodum.ingest` states
  blocks neither loopback nor private ranges. Both are properties of a
  human-only surface behind a password — which is exactly why this route is
  inside the session gate and the two token routes are not.
- **Spaces reach the human over HTTP as a filter, a target, and a lifecycle.**
  `GET /api/nodes` and `GET /api/search` take `?space=` (narrow to one space)
  and `?include_meta=` (off by default) — the CLI's two read-side controls,
  same names, same rules. `POST /api/nodes` takes `space` in the body: the
  **write target**, optional, `main` when absent. A space names *where a node
  goes*, never *who wrote it* — the session's human is still the only writer,
  and `space` is an ordinary service parameter rather than a new concept, which
  is exactly the test "do not invent request fields the domain has no
  representation for" asks for. The lifecycle is `POST /api/spaces` (create),
  `POST /api/spaces/{id}/rename` and `POST /api/spaces/{id}/archive`, in the
  `/api/nodes/{id}/archive` verb-POST style; `{id}` is a space id *or name* and
  resolves as a **space**, so neither route can be used to rename or retire a
  node that is not one. `GET /api/spaces` carries per space the live node count
  and the agents holding grants on it (the `/spaces` screen's read) and is
  byte-identical to `nodum space-list`, as every list endpoint is to its
  command — **active spaces only**, which is why the name refusal below has to
  explain itself in words. The two space rules are the service's, so both
  archive routes (`/api/spaces/{id}/archive` and `/api/nodes/{id}/archive`)
  answer 400 for `main` and `meta`, and both writers answer **409
  `SpaceNameTaken`** for a name any space already holds — including an archived
  one, whose message says so, since this listing does not carry it. Do not
  re-implement either in a handler or in the UI: the screen may say *why*
  before the click, but the refusal is the server's.
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
- Renditions are WebP bytes at `/api/assets/{id}/rendition/{profile}`, where
  `{profile}` is `thumb`, `preview`, or `page:<n>` for a PDF page raster — the
  colon needs no routing change, since Starlette's default path convertor is
  `[^/]+` (asserted end to end, because a colon is the kind of character a
  router or a proxy can decide is special). Originals are served on **one**
  route only, `GET /api/download/{token}`, and only against a capability URL
  minted through `POST /api/assets/{id}/download-url` (design §5.7).

## Frontend contract (for agents touching `web/`)

- **One `fetch`.** Everything goes through `src/api/client.ts`. It has no
  identity parameter and must never grow one — the server binds the principal
  and the client being unable to express one is the second layer under that.
  It also owns `Content-Type: application/json` on every non-GET request,
  bodyless ones included, because the server requires it. Auth is the
  `HttpOnly` session cookie the browser attaches itself — there is no token
  client-side; a 401 from any route but login is broadcast through
  `src/lib/session.ts`, and the app shell answers it with a redirect to
  `/login`.
- **Never call `new Date()` on a server string.** SQLite writes
  `datetime('now')` — UTC, no zone marker — which every browser reads as *local*
  time. Parse through `parseTimestamp` (`src/lib/time.ts`) and format through
  its formatters. `new Date()` on a client-side epoch number ("saved at",
  "checked at") is fine and is the only exception.
- **Never re-derive a failure's meaning.** `describeFailure` (`src/lib/failure.ts`)
  is the one place that tells *the API refused this* apart from *nothing was
  listening* — and the two are not one test: same-origin it is a `fetch`
  `TypeError`, behind the dev proxy it is a 502. Map its `kind` onto your own
  panel; do not re-test `status` or `instanceof`. The same rule covers a refused
  space: `isUnknownSpace` (`src/api/client.ts`) is the **only** discriminator,
  and the client normalises every call that names a space — `listNodes`,
  `search`, `createNode`, `createSpace`, `renameSpace`, `archiveSpace` — into
  one `UnknownSpaceError`. It is keyed on the message (`unknown space: …`),
  because no status is specific enough: the node listing answers 404 and search
  answers 400 for the same event, while a 404 from `POST /api/nodes` is equally
  an unknown node *type*. Two views once carried their own copy of that match,
  and a second copy of a discriminator is how the two drift apart — if a bare
  `ApiError` with that message ever reaches a view, wrap the call in the client.
- **Nothing user-facing may say a space does not exist.** Not "no such space",
  not "does not exist", not "unknown/missing/nonexistent space", not "not
  found" — and not by handing an `UnknownSpaceError` to `describeFailure`,
  whose 404 body is *"The server has no record of …"*. The server answers a
  space that was never created and a space the caller holds no grant on with
  **word-for-word identical text on purpose** (Q13 review S3): a refusal that
  told them apart would be an existence oracle over every space in the file, and
  the space filter would leak the shape of what an agent cannot read. Say what
  changed instead — a space stops resolving once it is archived, and a renamed
  one no longer answers to its old name. `views/search/spaceFailure.ts`,
  `views/editor/createOutcome.ts` and `views/spaces/spaces.ts` own that copy and
  pin it with tests; new copy goes through one of them. The refusal that names
  an **archived** space holding a name you tried to create is not a breach and
  not an exception: it is the server's own message, shown verbatim, and the
  only principals that can reach it are those writing `meta` — which is the
  grant that already lists every space node, archived included. The service
  asserts that premise as a test rather than assuming it.
- **The space surfaces are shared, and there is one of each.** The read filter
  is `components/SpaceFilter.tsx` (controlled and presentational — the view owns
  the value, and `controlClassName` is how a filter row sizes it rather than
  reaching in with a CSS override); its option vocabulary is
  `components/spaceOptions.ts` (`spaceOptions`, `resolveSpaceValue`,
  `spaceLabel` — a space reference is an id *or* a name everywhere, so resolve
  before comparing); the `GET /api/spaces` read behind all of them is
  `components/useSpaces.ts`. Do not add a seventh copy of that fetch or a second
  `spaceLabel`. `GET /api/spaces` is **active-only and stays that way** — it is
  the vocabulary behind every picker, and a retired space belongs in none of
  them. The review queue is the one surface that must name a space this listing
  cannot (a space archived while its proposals waited), and it does that with a
  *view-local* read of archived space nodes (`views/review/useArchivedSpaces.ts`,
  resolved by `views/review/spaceNaming.ts`) rather than by widening the shared
  one. A second view needing the same thing is the moment to reconsider — not a
  reason to change this endpoint.
- **Every surface that displays a node says which space it is in** — the exit
  criterion of the spaces phase, and search is the surface where it matters
  most, because a result list is *scanned*. The rule for how loudly:
  **a row states a dimension the filter has not already determined.** A concrete
  space filter is ANDed onto both ranked lists and onto graph expansion, so
  under one every hit provably lives there and repeating it per row is the
  filter read back; under *any space* it is the fact the scan needs.
  `views/search/resultSpace.ts` owns that rule, beside the identical one
  `ResultRow.knownState` follows for the state filter.
- **Where the review queue simplifies, it says so.** A cross-space edge proposal
  is filed under **one** space (its source's) while accepting it needs `edit` on
  **both** endpoints (`Store.edge_landing_state`). The filing rule stays — a
  proposal rendered under two sections, or a "crossings" section, is a grouping
  change nothing asked for — so the honesty is carried instead by
  `grouping.edgeCrossing`: the card is marked `cross-space`, the Inspect panel
  names the space of each endpoint and states the both-ends rule, and the
  section header counts how many of its proposals leave it
  (`SpaceSection.crossings`). A header that files a crossing under one space and
  then says nothing is asserting, by omission, that reviewing it is a
  single-space act. The same applies to a section for an archived space: it is
  named and marked, never left as a bare id.
- **The write target is app-wide, sticky, and must be visible** (design decision
  D1a). `src/lib/writeTarget.ts` owns it: one module-level value, persisted in
  `localStorage`, synchronised across tabs through the `storage` event, and
  **never changed without the human being told** — a target naming a space
  archived from somewhere else (the CLI, another session) survives and fails at
  the write, because filing a node somewhere the human did not choose is worse
  than a refusal they can read. The one reset is `clearWriteTarget()`, which
  `/spaces` calls when the human archives the very space they are filing into:
  that is the second half of an act they just performed, not a correction behind
  their back, and it is announced in both the archive confirmation (before) and
  the toast (after). The rule is about *silence*, not about immutability.
  `useWriteTarget()` is the subscription;
  a surface that creates a node **shows** the current target, and the post-create
  confirmation names the space the server actually filed it in. Calling
  `getWriteTarget()` without rendering the answer is the failure this module
  exists to prevent.
- **A view owns its directory and links to other views by URL.** No view imports
  another. Route paths live in `src/router.tsx`; grep for the path string before
  renaming one. A view's entry component keeps a **default export** — the routes
  are lazily loaded and `lazy()` needs it.
- **Promote to `src/lib/` or `src/components/` on the second user, not the
  first.** Both are inherited by every view. `src/lib/` is the plain-function
  tier; a hook or a shared fetch belongs beside the component it serves, in
  `src/components/` (`useSpaces.ts` is there because `SpaceFilter` is
  presentational and cannot own its own data). `writeTarget.ts` is the one hook
  in `lib/`, and only because the state it owns has no component — every
  node-create surface has to render it.
- **Do not render a control for something the service cannot do.** A node's
  `type` is immutable after creation, so the editor drops the type commands on a
  saved node rather than offering one that silently no-ops. Same rule as the
  HTTP contract's "do not invent request fields", one layer up.
- **The design system has two colour axes and both are taken**: the brass accent
  means "you can act on this", the state ramp means the service-layer state
  machine (`proposed` violet, `active` sea-green, `archived` lowest-contrast).
  Anything else needs its own hue, kept view-local until a second view names it.
  Exactly one has: `--nd-crossing` (magenta) means *this edge's endpoints are in
  two different spaces*, which is neither an affordance nor a state. It began
  view-local in the graph (D5) and moved into `styles/tokens.css` when the review
  queue had to mark the same fact, which is the promotion rule working rather
  than an exception to it.
  Class names are `nd-`-prefixed because Mermaid and Cytoscape inject global
  stylesheets on `.node`, `.label`, and `.edge`.
- **A form control carries an `id` or a `name`** — a field with neither is one a
  browser cannot address, which is what DevTools flags and what autofill and
  assistive tooling fall back to guessing about. There is no `<form>` submit
  anywhere here, so the value never travels; the attribute exists to make the
  control a named thing. `SpaceFilter` takes `name` as a prop (default `space`)
  for the same reason it takes `controlClassName`.
- **A pure module gets a `*.test.ts` beside it** (`make web-test`, Vitest). The
  harness is unit-only by design — no component rendering — so pull the logic
  worth testing out of the component and test it there, which is what
  `filters.ts`, `unifiedDiff.ts`, `signals.ts`, `grouping.ts`, `spaceOptions.ts`,
  `createOutcome.ts` and `views/spaces/spaces.ts` already
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
