# nodum

A **DB-native knowledge graph**: knowledge is a typed graph of nodes in one
SQLite file — not files with an index on top. Every mutation flows through a
deterministic, LLM-free service layer that validates, enforces a
`proposed → active → archived` state machine, and appends to an event log with
full before/after payloads — so every change is versioned, auditable, and
reversible.

**Phase 1 (core)** landed: schema + migrations, service layer, event log +
versions + undo, Markdown-as-truth content, wikilink materialization, and a
JSON-emitting CLI. **Phase 2 (agent-native)** landed: event-log
projectors with checkpoint/rebuild mechanics and two derived indexes — an
FTS5 full-text index and a sqlite-vec chunk-embedding index (local
in-process fastembed model, no daemon, no API key) — feeding **hybrid
search** (BM25 + vector fused by reciprocal rank fusion, then graph-expansion
re-ranking), **principals, spaces and grants** (Q13: human and agent accounts,
`read`/`suggest`/`edit` grants per (agent, space), enforced by a scope-bound
store), the **review/accept API** for the proposal queue (a human, or `edit`
on the item's space; `undo` stays human-only),
**proposed updates** (agent edits stage as `proposed` versions), an **MCP
server** (stdio) exposing the read + additive tool tiers *and nothing else*,
and **content-addressed assets** — binaries and
their lazily generated `thumb`/`preview` renditions stored in the same file
as the graph (agents get renditions, never originals). **Phase 3 (human UI)**
landed: `nodum serve` runs the **HTTP API** — the human surface, where
every write is attributed to the session's human and no request field can say
otherwise — and serves the **web UI** from the same process: a login view,
an accounts-and-grants admin view, a Markdown editor, hybrid
search, the review queue, a graph view, an asset browser,
a spaces view, and per-node version history. Spaces reach that UI as the same
two independent controls the CLI has — a read filter on every listing and a
sticky write target shown wherever a node is created. **Phase 4 (ingestion)** landed: `nodum ingest`
turns a file, a folder, or a URL into a reviewable subgraph — text extraction
through optional per-format handlers (PDF, OCR, audio; a missing one is
reported, never fatal), an `asset_ref` node for the bytes, a `source` node
carrying the extracted text, and one block per page — plus `page:<n>` PDF page
rasters and short-lived, single-use capability URLs for hosts that share no
filesystem with the graph. **Phase 5a (the gardener's spine)** landed: the graph
now maintains itself. A **consolidation cycle** groups a run of writes under one
id so a human can take the whole of it back with `nodum rollback`; the
**gardener** is an internal agent with ordinary grants that runs four
deterministic jobs — duplicate candidates, link pruning and inference,
housekeeping, a neglect report — and files everything it infers in the review
queue rather than asserting it; a **curative tier** (`merge-nodes`, `retype`,
`supersede-edge`, `bulk-relink`) changes structure rather than adding to it; and
a **dream journal** in the CLI and the web UI says what ran, who asked, what it
measured, and what it changed. It runs on demand and, if you ask for it,
nightly. Still to come: claim proposals and the gardener's LLM half — everything
that needs a judgement rather than arithmetic.

## Install

nodum is published to PyPI on every `vX.Y.Z` tag. For the CLI as a standalone
tool, [pipx](https://pipx.pypa.io/) is the least invasive route:

```sh
pipx install nodum
nodum --version
nodum schema-dump      # the whole command surface, as JSON
```

The local embedding model is an optional extra
(`pipx install 'nodum[embeddings]'`); without it, search falls back to BM25
keyword ranking. The extraction handlers are extras too — `pdf` (PDF text plus
`page:<n>` rasters), `ocr` (which also needs the `tesseract` binary on PATH),
and `audio` — and `nodum ingest handlers` reports which of them this install
can actually run. Plain text, Markdown, JSON, and HTML need no extra at all.

> The web UI (`nodum serve`) ships from v0.2.1 onward. **v0.1.0 and v0.2.0
> serve an "UI not built" placeholder** — their CLI, HTTP API, and MCP server
> are unaffected.

## Quick start

For working *on* nodum. Requires Python ≥ 3.12 and
[uv](https://docs.astral.sh/uv/).

```sh
make dev-install        # uv sync --all-groups

# Create the database (path: $NODUM_DB, default ~/.local/share/nodum/nodum.db)
uv run nodum init

# Build a small graph — every command prints one JSON object, and every
# command that touches the graph names its human with `--as` (reads included)
uv run nodum node create --type concept --title "Graph Theory" --as owner
uv run nodum node create --type note --title "My note" --as owner \
    --content "Notes on [[Graph Theory]] and its applications."
uv run nodum edge list --type mentions --as owner   # the wikilink became an edge

uv run nodum search "graph theory" --as owner       # hybrid search (BM25 + vector, RRF-fused)
uv run nodum projector status                 # derived-index checkpoints + availability
uv run nodum projector rebuild fts            # drop + replay from event 0
uv run nodum projector rebuild vec            # the model-change re-embed path

uv run nodum node list --type note --as owner
uv run nodum history <node-id> --as owner           # version snapshots

# Spaces: a second axis beside the type graph. Reading and writing are two
# independent controls — `--space` filters a read, and `--space` targets a
# write — so you can read one space while still filing into another.
uv run nodum space-create research --as owner
uv run nodum space-list --as owner                  # + live node counts and grant holders
uv run nodum space-rename research reference --as owner
uv run nodum node create --type note --title "Filed" --space reference --as owner
uv run nodum node list --space reference --as owner
uv run nodum search "graph theory" --space reference --as owner
uv run nodum node list --include-meta --as owner    # the type vocabulary, off by default
uv run nodum space-archive reference --as owner     # nodes keep their space_id; grants go inert

uv run nodum undo --as owner                        # reverse the latest event
uv run nodum types --as owner                       # the seeded type catalog

# The CLI is human-only and every command names its human explicitly:
uv run nodum node create --type note --title "Draft" --as owner
uv run nodum node update <id> --content "rewrite" --as owner

# Agent writes (over MCP) land per their grants — `suggest` queues them in
# the review queue as `proposed`:
uv run nodum review queue --created-by agent:researcher --as owner   # nodes, edges, updates

# Review authority is a human, or `edit` on the item's space; undo stays
# human-only. An accepted update applies only the fields the agent named, so
# edits made while it waited survive.
uv run nodum review accept-all --created-by agent:researcher --as owner
uv run nodum review reject <id> --reason "not convinced" --as owner
uv run nodum reject <id> --reason "not convinced" --as owner   # same audit trail

# Curated graph reads (the MCP read tier's service functions)
uv run nodum traverse <id> --edge-type supports --depth 2 --as owner
uv run nodum find-path <a> <b> --as owner
uv run nodum diff <version-a> <version-b> --as owner

# A bounded, filtered neighborhood: every filter applied in SQL, the node cap
# enforced *while walking* (and the edge list capped with it). `truncated` says
# whether either cap cut the walk short.
uv run nodum subgraph <id> --depth 2 --edge-type supports --as owner \
    --edge-state active --min-confidence 0.8 --node-type claim --limit 200

# Title-prefix link suggestions for a `[[` autocomplete (no index needed)
uv run nodum suggest-links "Grap" --limit 20 --as owner

# Assets: register a file into the database, derive stored WebP renditions
uv run nodum asset register ./photo.jpg
uv run nodum asset rendition <hash> --profile preview --out preview.webp --as owner
uv run nodum asset rendition <hash> --profile page:2 --out page2.webp --as owner
uv run nodum asset purge                      # evict the stored renditions

# Ingestion: a file, a whole folder, or a URL becomes a reviewable subgraph —
# an asset_ref node for the bytes, a source node holding the extracted text,
# a derived_from edge, and one block per page.
uv run nodum ingest handlers                  # which formats this install can read
uv run nodum ingest file ./paper.pdf --as owner
uv run nodum ingest file ~/papers --recursive --as owner   # batch: {"ingestions": […]}
uv run nodum ingest url https://example.com/paper.pdf --as owner

# Capability URLs for a host with no filesystem in common with the graph:
# single-use, minutes-long, and both the mint and the redemption are logged.
uv run nodum asset download-url <hash> --as owner
uv run nodum asset upload-url --name scan.pdf --mime application/pdf \
    --size 120000 --as owner

# The gardener: a consolidation cycle, its journal, and the way back.
# `--as` names who *asked*; the writes are the internal agent's.
uv run nodum consolidate --dry-run --as owner   # every job computed, no event written
uv run nodum consolidate --as owner             # for real: candidates land in the queue
uv run nodum cycle-list --as owner              # the dream journal, newest first
uv run nodum cycle-get <cycle-id> --as owner    # what ran, what it measured, how it ended
uv run nodum events --cycle <cycle-id> --as owner   # what it changed — the log, not a copy
uv run nodum rollback <cycle-id> --dry-run --as owner   # would this succeed?
uv run nodum rollback <cycle-id> --as owner     # all of it, or none of it

# The curative tier: structure changes, each one inside a cycle, so `rollback`
# is the single way back. `undo` refuses a cycle-stamped event and says so.
uv run nodum merge-nodes <id-a> <id-b> --into <survivor> --as owner
uv run nodum retype <id> --type claim --as owner
uv run nodum supersede-edge <edge-id> --confidence 0.4 --as owner
uv run nodum bulk-relink --dst <old> --to-dst <new> --dry-run --as owner

# MCP server (stdio) for external agents — read + additive tiers only, no
# review tools, no curative tools. The agent authenticates with its token in
# NODUM_AGENT_TOKEN (minted by `nodum agent create`, shown once).
NODUM_AGENT_TOKEN=ndm_… uv run nodum mcp serve

# HTTP server for the human: JSON API under /api plus the web UI at /.
# Every /api route needs a session — log in with a human name and password
# (`nodum human passwd` sets one). Loopback or LAN, the password is the
# boundary.
uv run nodum serve                      # http://127.0.0.1:8600
uv run nodum serve --host 0.0.0.0       # allowed: login, not the bind, is the boundary
curl -s localhost:8600/api/nodes/<id> -b nodum_session=…   # identical bytes to `nodum node get <id>`

# A write from a non-browser client says it is one:
curl -s -X POST localhost:8600/api/nodes \
  -b nodum_session=… \
  -H 'Content-Type: application/json' -H 'X-Nodum-Client: curl' \
  -d '{"type":"note","title":"From curl"}'
```

Run `uv run nodum --help` (or any subcommand with `--help`) for the full
surface.

### Semantic search (optional)

The vector signal runs on a local, in-process embedding model (fastembed,
ONNX on CPU — no daemon, no API key), installed as an extra:

```sh
uv sync --extra embeddings            # adds fastembed
# First run downloads the model (~0.2 GB) — downloads are never implicit:
NODUM_EMBED_DOWNLOAD=1 uv run nodum projector run vec
uv run nodum search "osmosis in plants"   # now fuses BM25 + vector signals
```

Without the extra (or an uncached model) everything still works: the `vec`
projector reports itself unavailable in `projector status` and search
degrades to BM25 + graph expansion. `NODUM_EMBED_MODEL` switches the model
(default: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`,
384-dim, multilingual); a model change is a `projector rebuild vec`.

## How it works

- **Everything is a node.** Pages, blocks, notes, claims, concepts, people,
  sources, tags — one `nodes` table distinguished by `type_id`. Structure
  (document trees) is `parent_id` + fractional `position`; meaning is typed
  `edges` between any two nodes.
- **Markdown is truth.** Node `content` is canonical Markdown. `[[wikilinks]]`
  are parsed on write and materialized as `mentions` edges; deleting the text
  archives the edge. Unresolvable targets are skipped silently. A materialized
  edge lands in the state the writer's grant earns, so a `suggest`-grant
  agent's wikilink is a *proposed*
  edge — writing `[[Your Concept]]` never attaches such an agent to a live node.
  Both directions are grant-bound: retiring a `mentions` edge needs `edit` on
  **both** endpoint spaces, so a writer cannot prune links into a space it
  cannot read — where, by definition, it cannot tell a link disappeared.
- **State machine.** Nodes and edges are `proposed`, `active`, or `archived`.
  Human (CLI) writes land `active`; an agent's writes land per its grant on
  the space — `proposed` on `suggest`, `active` on `edit` — and the proposed
  ones are accepted/rejected explicitly — individually, in batches, or by
  filter through the review queue (`nodum review …`). Every reject requires a
  `--reason` and records it in its event, whether it moves one id or a hundred. Agent *updates* stage
  as `proposed` versions: accepting applies **exactly the fields the agent
  named** to the node as it stands now (an ordinary, undoable `node.update`),
  rejecting archives the version. An edit made while the proposal waited is
  therefore never reverted by accepting it. Accepting a proposed node also
  brings the pending `mentions` edges its wikilinks materialized to `active` —
  those the acceptor could have reviewed directly, that is; a mention into a
  space they hold nothing on stays queued for someone who can.
- **Live state is the human's, structurally.** Review (`accept`, `reject`,
  `archive`) and the curative tier (`merge-nodes`, `retype`, `supersede-edge`,
  `bulk-relink`, `consolidate`)
  require a human principal or an agent holding `edit` on the item's space;
  `undo` and `rollback` are human-only. Either way is refused with
  `GrantNotPermitted`.
  Reviewing turns proposed structure into live structure, archiving retires it,
  and `undo` writes an event's prior payload back verbatim — `state = 'active'`
  included — so leaving any of them open would hand an agent the live state it
  may not write directly. `rollback` does exactly that for a whole cycle at
  once, which is why it cannot be gated more weakly than `undo`. None of them is
  an MCP tool either: agents may only *grow* the graph.
- **Grants, not policies.** Each agent holds one grant per space at `read`,
  `suggest`, or `edit` (`nodum grant …` / `nodum revoke` / `nodum grants`,
  human-only, event-logged). `read` lets it query, `suggest` queues every write
  as `proposed`, and `edit` writes `active` and carries review authority inside
  that space. There is deliberately no auto-accept machinery: an agent earns
  `edit`, or it waits.
- **The gardener is an agent, not an exception.** `nodum consolidate` runs the
  internal agent `builtin-gardener` — seeded with `edit` on `meta` and `main` as
  ordinary grant rows, which show up in `nodum space-list` and on the `/spaces`
  screen beside every other agent's, and which `nodum revoke builtin-gardener
  main` takes away with the command that was already there. It holds no
  credential at all: it authenticates by being in-process, so there is nothing
  to present and nothing to steal, and the supported way to stop it is
  `nodum agent disable builtin-gardener`. Its four jobs are arithmetic over data
  the file already holds — duplicate candidates by title similarity and
  embedding cosine, exact-duplicate and dangling-edge pruning, `relates_to`
  inference from embedding proximity and co-citation, a fractional-position
  check, embedding catch-up, and a report of what nobody has touched in ninety
  days. No model is involved anywhere, and it runs fine on a machine that has
  none. **It proposes; it never merges.** A duplicate becomes a `proposed`
  `duplicate_of` edge in the review queue — which already has a diff and an
  accept button — because a merge is always human-approved. Everything it infers
  is filed `proposed` even though its grant would let it write live: a grant is
  a ceiling, not a mandate, and a suggestion nobody reviews is not a suggestion.
- **A cycle is the unit you take back.** Every write a consolidation run makes
  is stamped with the cycle's id, and `nodum rollback <cycle-id>` reverses the
  whole of it inside one transaction — all of it, or none of it. It **refuses
  rather than clobbers**: if anything outside the cycle has touched a row the
  cycle touched, nothing is written and the refusal names both ends of each
  collision, so you can go and look. A rollback is itself a cycle, so rolling
  *that* back re-applies the original. `nodum cycle-list` and `nodum cycle-get`
  are the journal — what ran, who asked, what it measured, how it ended — and
  what a cycle *changed* is `nodum events --cycle <id>`, read off the same
  append-only log as everything else, so the journal can never become a second
  record that disagrees.
- **The curative tier changes structure; the additive tier only adds to it.**
  `merge-nodes` is soft — the merged-away node is archived, says where it went,
  and its edges are repointed at the survivor keeping their original endpoints,
  so nothing is destroyed. `retype` is the one sanctioned exception to a node's
  type being fixed at creation. `supersede-edge` records two different facts:
  *when* an edge stopped being true, and that it is no longer live.
  `bulk-relink` repoints or retypes many edges at once behind a dry run that
  writes nothing. All four are human-or-`edit`, all four are CLI-only — never
  MCP — and all four run **inside a cycle**, even when you invoke one directly.
  That is why `undo` refuses a cycle-stamped event and points at `rollback`
  instead: a merge is several rows from one decision, and undoing one of them
  would leave the other half standing.
- **A schedule, if you ask for one.** Set `NODUM_CONSOLIDATE_AT=03:30` and
  `nodum serve` runs one cycle a night, in the process that is already running —
  no cron, no second process, no new dependency. Unset means off, which is the
  default: a background process that writes to your graph without being asked is
  not something to enable by surprise. The run cannot overlap itself, a crash
  leaves a `failed` entry in the journal and tries again tomorrow, and shutdown
  never waits for a cycle to finish.
- **Spaces: a filter for reading, a target for writing.** A space is a node of
  builtin type `space`, so its whole lifecycle is an ordinary node's
  (`space-create` / `space-rename` / `space-archive`, each event-logged,
  versioned and undoable), and `space-list` reports each one's live node count
  and the agents granted on it. The two uses are deliberately separate
  controls: `--space` on a read **narrows** the view (default: every space in
  scope), `--space` on a write **targets** where the node lands (default:
  `main`). The read filter is a convenience, never a boundary — the boundary is
  the grant set, which is still applied underneath it, and a space a principal
  holds no grant on does not resolve at all, reading exactly like one that does
  not exist. Meta-space nodes (the type vocabulary, the spaces themselves) stay
  out of content reads unless `--include-meta` says otherwise, or the read is
  narrowed to `meta` by name. Four rules keep the vocabulary unambiguous, and
  all live in the service so every surface has them: **no two spaces may share
  a name** (a reference resolves as `id = ? OR title = ?`, compared exactly —
  and a space keeps its name when it is archived, so a retired name stays
  reserved and restoring the space can never collide);
  **`main` and `meta` cannot be archived** (every unnamed write still lands
  in `main` whatever state the row is in; `meta` holds the spaces themselves) —
  renaming either is fine, since a rename moves the title and the id is what
  everything structural depends on;
  **a space lives in `meta`** (one nested in ordinary territory would be listed
  and resolved as real while governed by its host's grants);
  and **archiving a space makes every grant on it inert** — cutting an agent
  off is what archiving is usually for, so while the space is archived a grant
  on it confers nothing at all, including on nodes reached by id. The grant rows
  are kept rather than deleted, so `grants` still lists them, `revoke` still
  removes them, and undoing the archive restores the delegation unchanged.
- **MCP server.** `nodum mcp serve` runs a stdio MCP server (the official
  Python SDK's FastMCP). The agent authenticates with its token in
  `NODUM_AGENT_TOKEN` (minted by `nodum agent create`, shown once, stored
  hashed) and is verified at startup; every write is confined to its grants.
  The registry is the design §8.1 read tier (`get_node`, `get_children`,
  `search`, `traverse`, `list_types`, `get_schema`, `find_path`, `history`,
  `diff`, `get_asset`, `get_download_url`) and additive tier (`create_node`,
  `update_node`, `link`, `propose_edges`, `ingest_file`, `ingest_url`,
  `request_upload_url`). Ingestion is **by reference** — the tool takes a path
  the server can read or a URL it can fetch, and no base64 ever crosses MCP; a
  host with no filesystem in common asks `request_upload_url` for somewhere to
  PUT instead. That is the entire registry: the review tools
  (`accept`/`reject`) and the curative tools (`merge_nodes`, `retype`,
  `supersede_edge`, `bulk_relink`, `consolidate`) are **never registered** —
  structural enforcement of §8.1/§8.2. The curative tier is built and in use;
  keeping it off MCP is a decision about a surface that exists, not a note about
  code that does not. One call there could merge two nodes or rewrite five
  hundred edges, and the only thing that takes those back is a human's rollback.
- **HTTP API + web UI.** `nodum serve` runs one process that answers the JSON
  API under `/api` and serves the built UI at `/`. It is the mirror image of
  the MCP server: that surface authenticates an agent by token, this one is
  the **human** surface and attributes every write to the session's human
  principal — and no request field, header, or query parameter can set an
  identity, because the adapter never reads one. A body that carries
  `{"actor": "agent:x"}` is ignored, not honoured and not rejected with a hint.
  Responses are the same envelope the CLI prints, byte for byte, and failures are
  `{"error": {"type", "message"}}` carrying the CLI's own one-line message
  (missing id → 404, bad value → 400, human-only → 403, impossible undo → 409,
  database busy → a retryable 503). The consolidation journal is on it as
  `GET /api/cycles`, `POST /api/cycles` (run one now, `dry_run` to rehearse),
  `GET /api/cycles/{id}` (the entry plus the events it wrote) and
  `POST /api/cycles/{id}/rollback` — where a graph that has moved on is a
  **409** carrying the conflicting rows, not a bare refusal. The curative tier
  is deliberately not there: it is the CLI's. With no UI bundle built, the API
  serves normally and `/` is a page telling you to run `make web-build`.
- **Origin control, which is not the same thing as auth.** Binding loopback is
  no defence against a *browser*: every page the user visits can reach
  `127.0.0.1`. So a state-changing request must prove it came from this origin
  — `Sec-Fetch-Site: same-origin`, or a matching `Origin`, or (for a
  non-browser client, which has neither) the explicit `X-Nodum-Client` header —
  every JSON route requires `Content-Type: application/json`, which a
  cross-origin page cannot send without a preflight this app never answers, and
  the `Host` header is checked against the names the server answers to, which
  is what stops DNS rebinding. Reads need none of it. `--allow-host` names an
  extra host for a reverse proxy.
- **Auth is password login + server-side sessions.** `POST /api/login` with a
  human name and password (argon2id, constant-time on failure) creates a
  30-day sliding session row and sets an `HttpOnly; SameSite=Strict` cookie;
  every `/api` route but login needs it — reads included — while `/healthz`
  (liveness and nothing else) and the static UI stay open. Origin control
  stops browsers; the password stops other processes on the machine. A
  non-loopback bind is allowed — login, not the bind, is the boundary — and
  marks the cookie `Secure` there. `nodum human passwd` sets the password;
  logout, expiry, and `human disable` kill the session at the next request.
  Account and grant administration is on the API too — `GET /api/me`,
  `/api/humans`, `/api/agents` (the show-once token comes back in the
  create/token-rotate body) and `/api/grants` mirror the CLI's
  `human`/`agent`/`grant`/`revoke`/`grants` commands.
- **Spaces are on the API as both controls.** `GET /api/nodes` and
  `GET /api/search` take `?space=` and `?include_meta=` (the read filter and
  the meta toggle, both off by default); `POST /api/nodes` takes `space` in the
  body (the write target, `main` when absent — a place, never an identity).
  The lifecycle mirrors the CLI: `POST /api/spaces`,
  `POST /api/spaces/{id}/rename`, `POST /api/spaces/{id}/archive`, and
  `GET /api/spaces` listing every active space with its live node count and
  grant holders.
- **Uploads are typed by their bytes, on both routes.** One policy, with the
  route's capability as its only parameter. `POST /api/assets` — the editor's
  drag-drop route — caps the request body before anything buffers it (32 MiB)
  and admits the raster images this install can render: it registers bytes and
  writes no describing
  node, so what describes them is the note that inlines a rendition of them, and
  it is the one route where the 40 MP rendition ceiling decides *admission*.
  `PUT /api/uploads/{token}` admits everything the sniffer can name — rasters,
  PDF, the common audio containers, and text — because it *ingests*: bytes in,
  reviewable subgraph out, under the principal who minted the grant and capped
  by that grant's own declared size as it streams. A 70 MP scan is an ordinary
  document there, so that route keeps the decompression-bomb guard and drops the
  rendition ceiling. Neither route reads the type
  off the filename or the client's `Content-Type`. Something neither takes — a
  `.docx`, a renamed binary, anything with no signature and NULs in it — goes
  in through `nodum ingest file`, where the operator owns the file. Both halves
  of that are heuristics rather than guarantees, and the sniffer's own docstring
  says exactly what each one checks: the text test is a window at each end of the
  file, and the displaced-PDF scan admits any non-text bytes carrying a versioned
  `%PDF-` header in the head window — so a zip whose first entry is a PDF gets
  in, and gets an honest "no text came out" for its trouble. There is
  no delete route, so what lands stays until it is managed out of band — which is
  why an upload that will be refused for a reason needing no bytes (an
  unresolvable target space) is refused before anything is stored.
- **Ingesting over HTTP.** `POST /api/ingest` takes exactly one of `path` and
  `url` (both or neither is a 400 rather than a precedence rule nobody
  remembers). Note what it hands the session's human: `path` is read *by the
  server*, so it reaches any file the server's user can, and `url` is fetched
  by the server, which blocks neither loopback nor private ranges. Both are
  properties of a human-only surface behind a password, which is exactly why
  this route sits inside the session gate while the two capability-URL routes
  do not — those carry no ambient credential to ride.
- **The ten views.** `/login` is the session gate: password login with
  argon2id. `/editor` is a CodeMirror-6 Markdown editor with slash commands,
  `[[` autocomplete, live Mermaid preview, drag-drop asset upload, and
  debounced autosave — a node's `type` is fixed at creation, so the type
  commands disappear once it is saved. `/search` is one box over hybrid search
  that renders the server's order and never re-ranks it, with the per-signal
  breakdown made legible. `/review` is the proposal queue: a reject always asks
  for a reason, an accept always shows what it will write. `/graph` renders
  `subgraph` in Cytoscape, with truncation and the confidence floor's exclusions
  stated on screen rather than in a footnote. `/assets` is the rendition grid
  and lightbox. `/spaces` is what territory exists: every active space with its
  live node count and the agents granted on it, plus create, rename and archive.
  `/admin` is accounts and grants: humans, agents with show-once
  tokens, and the grant grid. `/history/:nodeId` is the version timeline and
  side-by-side diff. The **dream journal** is the consolidation record: every
  cycle newest-first, and one entry showing what ran, the coherence metrics
  before and after, and the events it wrote — with a button to run a cycle (or
  rehearse one) and the rollback that takes a whole cycle back, which asks first
  and, when the graph has moved on, names both ends of every collision instead
  of a count. Every route is a real URL that survives a reload. Source
  and conventions: [`web/README.md`](web/README.md).
- **Spaces in the UI are a filter and a target, never a mode.** Search, the node
  graph and the review queue take a space *filter* that narrows and defaults to
  every space in scope; a single app-wide *write target* (sticky across sessions
  and across tabs) says where a new node lands, and is rendered on every surface
  that creates one — a persisted target the human cannot see is how work gets
  filed into a space nobody chose. Every search result names its space unless
  the filter already determined it, so the list a human scans is readable across
  spaces. The review queue groups proposals by space,
  then by agent, so a space that governs itself (an agent holds `edit`, its
  writes land `active` and never queue) is visible as *self-governing* rather
  than as silence; a cross-space edge proposal is filed under its source's space
  and **marked as a crossing**, because accepting one needs authority on both
  endpoints. Nothing in the UI ever reports a space as missing: the server
  refuses an unknown space and an ungranted one with identical words, and the
  interface keeps that ambiguity intact.
- **Assets and renditions.** `asset register` streams a file into the
  database keyed by its sha256 (dedup is free) and records its metadata row;
  the copy is re-hashed as it is written, so a file that changed since it was
  hashed (a rotating log, a partial download) is refused rather than stored
  under a key it does not match. One asset cannot exceed SQLite's blob limit
  (1 GB), which is reported up front. `asset rendition` derives small WebP
  images from it — `thumb` (≤256px) and `preview` (≤1024px, ≤300 KB) — lazily
  on first request, stored alongside, evictable with `asset purge`
  (everything regenerates). Binaries live in the database rather than a
  directory beside it, so **the database file is the whole system**: back it
  up and you have backed up the graph, its history, and its binaries. Over
  MCP, `get_asset` returns metadata plus a rendition image block: **LLMs never
  receive original binaries** (design §5.7). `--profile page:<n>` rasterises a
  1-based page of a PDF at 144 DPI — an ordinary rendition otherwise, with the
  same lazy generation, cache, and eviction — which is how an agent *looks at*
  a document whose layout, tables, or figures carry the meaning.
- **Ingestion turns bytes into a reviewable subgraph.** `nodum ingest file`
  takes one or more paths — a directory ingests the files inside it,
  `--recursive` goes deeper — and `nodum ingest url` fetches an `http`/`https`
  URL and does the same. Each document becomes an asset, an `asset_ref` node
  describing those bytes in one space, a `source` node whose content is the
  extracted text, a `derived_from` edge between them, and one `block` child per
  page of text. Every write goes through the ordinary service API, so the
  subgraph lands in the state the writer's grant earns. Ingestion is
  **idempotent per (hash, space)**: re-running the same folder returns
  `created: false` rather than a second copy, so a batch that failed halfway is
  simply run again. A batch never loses its successes — a file that fails
  prints its reason to stderr and the rest carry on — but the exit code is 1 if
  any file failed, so a non-zero exit means "read stderr for what is missing",
  not "nothing happened".
- **Extraction handlers degrade instead of failing.** Text, Markdown, JSON, and
  HTML are handled by the standard library and always work. PDF text
  (`pdf` extra), image OCR (`ocr`, which also needs the `tesseract` binary),
  and audio transcription (`audio`) are optional, and an absent one is a
  *reported result*, not an error: the asset is still registered, the nodes are
  still written, and the answer says plainly that no text came out. A corrupt
  file is treated the same way. `nodum ingest handlers` lists every handler,
  its MIME families, and — when it cannot run — what to install. Nothing is
  ever downloaded implicitly: as with the embedding model, the transcription
  model is confined to its local cache unless `NODUM_AUDIO_DOWNLOAD=1` says
  otherwise.
- **Capability URLs are the escape hatch, and they are logged.** An agent host
  that shares no filesystem with the graph can ask for a single-use,
  minutes-long URL to fetch an asset's original (`asset download-url`) or to
  PUT bytes exactly once (`asset upload-url`). The token is 256 random bits
  shown once and stored only as a sha-256, the row is the whole authority — so
  expiry, single use, and revocation are one update — and both the mint and the
  redemption are written to the event log. A download URL never widens the
  caller's reach: an asset they cannot read mints nothing.
- **Bounded reads for interactive clients.** `nodum subgraph` is `traverse`
  with the filters a graph view needs — edge type, edge state, confidence
  floor, edge author, node type — composed as one conjunction in SQL, plus a
  `--limit` on nodes that is enforced **during** the breadth-first walk rather
  than by slicing a materialized graph afterwards — tested before the far node
  is read, so a hub with ten thousand spokes costs `limit` reads, not ten
  thousand. The edge list is capped with it (`limit * SUBGRAPH_EDGE_FACTOR`),
  because a node cap bounds nodes only, and `--limit` is clamped to a server
  ceiling of 2000. A caller therefore cannot ask for an unbounded result in
  either dimension, and `truncated` says whether either cap cut the walk short
  instead of leaving a partial view to pass as the whole neighborhood. What
  does come back is closed over its own node list: two returned nodes are
  never drawn unconnected when the stored graph connects them. An edge whose
  far node the filters exclude is dropped with it, so the result never carries
  an edge pointing outside its own node list. `nodum suggest-links "Grap"` is
  the companion title-prefix lookup behind a `[[` autocomplete: it reads the
  node table directly, never a projector index, so it answers on a cold
  database — an empty list always means "no such title", never "the index has
  not run".
- **Event log + versions.** Every mutation appends an event (actor, op, full
  before/after JSON payload) and — for nodes — a version snapshot. `undo`
  reverses an event by restoring its `before` state. It never cascades beyond
  what the event created: undoing the create of a node that has since gained
  children, or of a row a later undo already deleted, is refused with a
  message instead of half-done.
- **Derived indexes are projectors.** The event log feeds checkpointed,
  independently rebuildable projectors (`nodum projector run/status/rebuild`).
  `fts` maintains an FTS5 index over node title + content, joining an
  ingested asset's full extracted text onto the `asset_ref` node that stands
  for its bytes — and onto that node only, so a word on page 3 does not match
  every other page just as strongly; `vec` maintains sqlite-vec embeddings of
  fixed-window text chunks (512 words, ~15% overlap, `model_id` recorded per
  chunk). `nodum search` fuses both — BM25 and vector lists merged by
  reciprocal rank fusion, then one-hop graph expansion along `active` edges —
  and every hit carries a per-signal `signals` breakdown plus the `space_id` it
  lives in, since a result list spans every space in scope unless `--space`
  narrowed it. With no embedding
  provider, the vector signal drops out and search stays BM25 + graph.

See [docs/architecture.md](docs/architecture.md) for the module map and
[AGENTS.md](AGENTS.md) for contributor/agent workflow rules.

## Development

```sh
make test      # pytest
make lint      # ruff check + format check
make format    # ruff auto-fix + format (run after every code change)
```

The frontend is a build-time dependency only — the wheel ships the built
bundle and the runtime is pure Python:

```sh
make web-install    # npm ci in web/ (once)
make web-test       # vitest over the pure modules in web/src
make web-build      # tsc --noEmit && vite build -> nodum/_web/
make web-dev        # Vite dev server on :5700, proxying /api to :8600
make web-clean      # drop the bundle; nodum serve falls back to the placeholder
```

`make web-test` and `make web-build` are both CI gates. The test run pins a
non-UTC `TZ`: the timestamp bug `web/src/lib/time.ts` fixes is invisible on a
UTC machine, which is what CI is.

`nodum/_web/` is gitignored whole (Vite wipes the directory on every build),
and hatchling re-includes it in the wheel as a declared artifact, so a release
must run `make web-build` before `uv build`.

The package version is derived from the git tag (`vX.Y.Z`) at build time by
hatch-vcs and is never committed.
