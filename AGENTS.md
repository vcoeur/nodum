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
history (Phase 5a adds the tenth, below).
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
Phase 5a (the gardener's spine) has landed — the deterministic half of design
§8.4/§8.5, cut at the LLM line: **consolidation cycles** (migration
`0014_cycles_and_gardener` gives `events.cycle_id` the table it has pointed at
since `0001` with nothing on the other end), the **internal agent**
(`builtin-gardener`, seeded by that migration with `read` on `meta` and `edit`
on `main`
as ordinary grant rows, minted in-process by `auth.internal_principal` with no
credential to present and none to steal — `read` on meta because resolving a
type is a read and no job ever writes the vocabulary (`_is_curatable` excludes
the meta space and the structural types outright), where `edit` bought latent
authority nothing shipped reaches: creating spaces, renaming `main`, and
archiving the `note` type, after which a **human** is blocked from writing a
note too — and the `builtin-` id **prefix** is
**reserved**, because `Principal.actor_string` renders every agent as
`agent:<id>`, so an external agent free to take that id would write events
indistinguishable from the gardener's, and the event log is this system's
answer to *who is answerable for this write*; `0014`'s upgrade guard therefore
refuses on `id LIKE 'builtin-%'` rather than on the one id, since `0010`
back-fills an `agents` row from every actor string in the log and a pre-0010
file merely *mentioning* `agent:builtin-librarian` would otherwise upgrade into
a live token-bearing account under the prefix), the **curative tier**
(`merge_nodes`, `retype`, `supersede_edge`, `bulk_relink` — §8.2), **cycle
rollback** (`service.rollback_cycle`, human-only and atomic), the **landing
seam** (`store.cap_landing` plus a keyword-only `landing=` on `create_edge` /
`propose_edges`: §8.3's grant-is-a-ceiling, which is what puts the gardener's
inferences in the review queue), the **consolidation runner**
(`nodum.consolidate` — four deterministic jobs and five coherence metrics,
running as a peer client over the public service API), the **nightly
scheduler** (`nodum.scheduler`, one asyncio task in `nodum serve`'s lifespan,
**off unless `NODUM_CONSOLIDATE_AT` is set**), and the **dream-journal view**
in the web UI, which Phase 3 deferred to Phase 5 precisely because it needed a
cycle to have something to show. Two columns reserved since `0001` and never
written by anything get their first writers here: `merge_redirects` (from
`merge_nodes`) and `edges.valid_to` (from `supersede_edge`). `edges.valid_from`
still has none — nothing in this system yet knows when an edge *started* being
true, and inventing a value at creation would be a guess in a column whose
whole purpose is a fact.
**Deliberately not built yet** (later phases — do not add): **claim
proposals**, which moved to Phase 5b deliberately rather than being forgotten —
deciding that a sentence *is* a claim is a judgement call and belongs to the
research agent in design §3, and splitting prose into sentences would fill the
review queue with noise instead of knowledge, so ingestion proposes sources and
structure and stops; the **LLM half of the gardener** (Phase 5b) — props
migration on a retype, deciding that an untouched claim has gone *stale*
rather than merely old, the abstraction job, and the two Q12 metrics that need
it — design Constraint 4 keeps the model out of validation, the state machine
and the projectors, and every line of `nodum.consolidate` runs on a machine
with no model present; **Markdown Mirror** and any whole-graph export (the only
export that exists is the thin per-node snapshot,
`GET /api/export/node/{id}?depth=`, which is `get_neighborhood` with a
`content-disposition` header — not a format, not a backup). Each lands as its
own append-only migration where it needs one.
A node's `type` is likewise **fixed at creation by design**, not by omission:
`service.update_node` takes `title`/`content`/`props` only, and retyping is a
curative operation (§8.2 `retype`, CLI `nodum retype`) that runs inside a
cycle — which is exactly why it is not a field on the update path. Do not add a
`type` field to `PATCH /api/nodes/{id}` — the editor withholds its type
commands on a saved node for exactly this reason.

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
  Four space rules are enforced **here** rather than on a screen, because a
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
  **A space lives in `meta`, and `create_node` enforces it**
  (`_require_space_lives_in_meta`). It is the model every adapter is already
  written against — `create_space` hardcodes it — but the generic path could
  put a `space`-typed node in ordinary territory, where `GET /api/spaces`
  listed it and `_resolve_space` resolved it as real while the grants governing
  it were the *host* space's. That is also what made the name check an
  existence oracle: the check is deliberately unscoped, on the premise that
  only a principal who can read `meta` reaches it (and such a principal can
  already list every space). The premise held for creates — resolving the
  `space` type needs READ on `meta` — but a rename is gated on `suggest` on the
  space the node **lives in**, so a space node in `main` let an agent holding
  nothing but `main` rename it onto a name and read a confirm/deny, plus the
  holder's id, for a space it cannot list. `update_node` therefore requires
  READ on `meta` before the name check as well, so a legacy or raw-SQL row
  cannot reopen it; the refusal is on the *grant*, identical for a taken and a
  free name. Migration `0013`'s index stays unscoped to meta on purpose — it is
  the backstop for writers that never pass through the service.
  **Archiving a space makes every grant on it inert** — enforced where grants
  become a principal (`auth._grant_set`), so reads, writes, proposals and
  review all inherit it rather than only the calls that spell the space's name.
  Cutting an agent off is *why* a human archives a space, and it used to be
  true only of those name-spelling calls: everything reachable by node id kept
  full live authority while `list_spaces` stopped showing the space or its
  grants — hidden authority, and unrevokable, since `grant`/`revoke` resolved
  active spaces only. The grant **rows** survive on purpose, so `list_grants`
  still shows them and `revoke` still reaches them (`_resolve_space_for_admin`,
  human-only, resolves a space in any state), and undoing the archive restores
  exactly the delegation that was there. Inert, not destroyed. `grant` on an
  archived space is refused and says why.
  **Consolidation cycles, the curative tier and rollback are here too** (design
  §8.2/§8.4). `open_cycle` / `close_cycle` / `abandon_cycle` / `get_cycle` /
  `list_cycles` own the
  `cycles` row — the dream journal's record of what ran, who asked, over what,
  and how it ended — and store **no diff**, because the diff is
  `list_events(cycle_id=…)` and a journal that kept its own copy could disagree
  with the log. `triggered_by` (who *asked*: a `human:<id>`, or the literal
  `scheduler`) is deliberately not the `actor` on the events inside (who
  *acted*: the gardener); an entry carrying only one of the two could not answer
  "I did not ask for this" **or** "who ran this at 04:00", and those are
  different questions. **"Who asked" is structurally one of those two**:
  `open_cycle` refuses a non-human principal on `trigger='manual'`, because that
  trigger *means* a human asked and it was previously only a convention —
  `consolidate.consolidate` takes `triggered_by` as a plain **string** and
  re-mints a principal from it, so nothing downstream had a `principal=` binding
  to check and a caller reaching it could write `agent:builtin-gardener` into
  the one column that answers "I did not ask for this". `scheduled` needs no
  check (it records the clock whatever it is handed) and `curative` genuinely
  records the principal running the operation, which may be an `edit`-granted
  agent by design. **`abandon_cycle` is the door out of an interrupted run**
  (human-only): a cycle that never closed cannot be rolled back (`_rollback_plan`
  refuses a `running` one) and `undo` refuses every event it stamped, so a run
  killed by `SIGKILL`, a power cut or `ConsolidationScheduler.stop()` cancelling
  a mid-cycle task left its writes irreversible on every surface, behind advice
  ("close it first") that nothing could carry out. It closes the row `failed`
  through `close_cycle` — one place leaves `running` — with a report naming who
  abandoned it; it refuses a cycle that is not `running`, because a cycle that
  has said how it ended is not abandoned and re-closing it would overwrite that
  record. It is reachable on both human surfaces — `nodum cycle-abandon <id>`
  and `POST /api/cycles/{id}/abandon` — because a door nothing opens is not a
  door; it stays off MCP with the rest of `HUMAN_ONLY_TOOLS`. **And every
  refusal a stranded cycle causes now names it, with the id filled in**: the
  rollback refusal on a `running` cycle, and `open_cycle`'s "a consolidation
  cycle is already running" — which matters more since the guard became a
  database index, because one interrupted run blocks every later run in every
  process rather than only the ones sharing its interpreter. A door exists once
  the thing that sends you to it says its name. The stamp itself is `in_cycle`, a `ContextVar` that
  `_emit` reads, so a cycle's writes go through the *ordinary* public functions
  and are stamped without any call site naming a cycle — a per-task variable
  rather than a module global, so the HTTP server handling a normal request
  while a cycle runs cannot be stamped by it, and it is reset in a `finally`
  because a leaked id would make ordinary later edits un-undoable on a graph
  whose only route back is rolling back a cycle they were never part of.
  **`undo` refuses a cycle-stamped event by name and points at rollback**, and
  that refusal is what makes rollback safe: a curative op writes several rows
  from one decision while `undo` reverses one row from one payload, so undoing
  one row of a merge would leave the other half standing. The curative
  operations are `merge_nodes` (soft, D9: tombstones keep `props.merged_into`,
  a `merge_redirects` row records where each went, incident edges are repointed
  keeping `props.merged_from` — or archived when repointing would self-loop or
  duplicate, each reported in `retired[]` carrying its own `reason` field and
  not only in the event payload, since the two rules read very differently and a
  list of bare edges says only that something disappeared; the read path is
  deliberately
  unchanged, since a redirect on the hottest read in the system buys nothing
  the props field does not already carry), `retype` (the one sanctioned
  exception to an immutable field; **no props are transformed**, because what a
  property *means* after a retype is judgement and judgement is 5b),
  `supersede_edge` (two facts, recorded as two: `valid_to` closed — *when* it
  stopped being true — **and** `archived` — *it is no longer live*; a
  replacement inherits every field it does not name, and the seeded
  `supersedes`/`superseded_by` pair is carried **in props, not as an edge**,
  since `edges.src_id`/`dst_id` reference `nodes` and one edge cannot point at
  another), and `bulk_relink` (an empty selector is refused rather than read as
  "everything", `MAX_RELINK_EDGES` caps one call, and `dry_run=True` opens no
  cycle and emits no event at all). **Every curative op runs inside a cycle,
  including one a human invokes directly** — `_curative_cycle` joins the
  ambient cycle when a runner set one and otherwise opens a one-op
  `trigger='curative'` cycle and closes it — so rollback is the single reverse
  for the whole tier and there is no second multi-row reversal mechanism to
  build and keep correct. The op **names** are not free either: `nodum.projectors`
  dispatches on `op.startswith("node.")` and indexes `payload["after"]`, so a
  curative op that changes a node's text or type must be `node.*` with one
  event per node or the search index silently desynchronises — which is worse
  than an index that is missing. `rollback_cycle` is human-only for a stronger
  version of `undo`'s own reason (it writes recorded payloads back verbatim,
  `state = 'active'` included, across spaces, for a whole cycle at once), is
  atomic, and **refuses rather than clobbers**: if anything outside the cycle
  has touched a row the cycle touched, nothing is written and `RollbackConflict`
  names the rows and both ends of each collision. "Touched" is a **fixpoint,
  not a set**: a reversal can itself be reversed (rolling a rollback back
  re-applies the original), so a cycle whose write was rolled back and then
  rolled back again is *live*, and counting its event as "already reversed" the
  moment anything named it let an older cycle's rollback write straight over it
  — reporting `conflicts: []` on the dry run first. A seq is reversed iff some
  reversal of it is not itself reversed, resolved by recursion (reversals
  strictly increase in seq, so it terminates). The **delete guards are modelled
  in the plan too**, as a second list: a conflict is the graph having *moved* a
  row the cycle wrote, a `blockers[]` entry is the graph having *grown something
  onto* a row the cycle created, and modelling only the first made the preflight
  a UI's confirm dialog calls disagree with the run. Every foreign key into
  `nodes(id)` is guarded — `nodes.parent_id`, `nodes.space_id`,
  `merge_redirects`, `grants.space_id` and `nodes.type_id` — because an
  unguarded one is a bare `IntegrityError`: a 500 over HTTP, and
  `database error: FOREIGN KEY constraint failed` on a CLI that promises to name
  what the graph has grown. A rollback is itself a cycle,
  so rolling *it* back re-applies the original — the reversal is an involution,
  and it holds at **every** depth, which is not free: the `merge_redirects`
  removal keys on what the payload *says happened* (`after.props` gained
  `merged_into` and `before` did not), never on the op name. Keying on
  `op == 'node.merge'` was right for the first two rollbacks and wrong from the
  third, because a rollback that re-applies a merge writes that same before/after
  pair under the name `node.rollback` — so reversing it restored the node and
  stranded the redirect, after which the tombstone's create was un-undoable
  for good and merging it again died on the redirect's primary key. **The
  journal's own `rolled_back` bookkeeping has to hold at every depth too**
  (`_restate_reversal_chain`), and the chain *alternates*: a rollback that is
  itself taken back stops standing, so the cycle it reversed stands again and
  its mark comes off — but that cycle may be a rollback in turn, and one that
  stands again is once more holding *its* target down, so that mark goes back
  on. Every step flips. Clearing exactly one hop was right at depth 2 and wrong
  from depth 3, where the journal ended up asserting the mirror of the
  invariant it exists to keep: a cycle reported `completed` with no
  `rolled_back_by` while its writes were reversed and standing that way. That
  is not only an entry a human misreads — `_rollback_plan` refuses an already
  `rolled_back` cycle by reading exactly that column, so a stale `completed`
  handed it a row it would cheerfully reverse a second time. Finally,
  the **landing seam**: `Store.cap_landing` and a
  keyword-only `landing=` on `create_edge`/`propose_edges` let a writer file
  below its own grant (§8.3 — a grant is a **ceiling, not a mandate**). It only
  ever lowers; asking to land *above* the grant is refused rather than quietly
  downgraded, because a caller that named a state and silently got another one
  has been told nothing.
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
  `edit` grant changes rather than promising `proposed`.
  **Anything an agent must be able to say has to be in a tool's signature
  here**, because the SDK discards a keyword this module does not declare
  instead of refusing it: `create_node` had no `space` parameter while
  `ingest_file`/`ingest_url`/`request_upload_url` did, so an agent asking for
  `research` got a 200-shaped response describing a node in `main` — no way to
  choose a space and no way to learn it had not got one. `create_node` now takes
  `space` (a space id or name, `main` by default, narrowed by the grant set like
  every other space reference, and refused in the non-oracle's identical words
  when the agent holds nothing on it). Making the *extra key* an error instead
  was the other option and is not reachable without mutating the SDK's
  `ArgModelBase.model_config`, a third-party base class every generated tool
  model inherits — and an agent that cannot name a space is not helped by being
  told its spelling was wrong. Every write result carries the `space_id` it
  actually landed in, which is the checkable half of the same rule.
  Auth is the agent token in `NODUM_AGENT_TOKEN` —
  an `ndm_…` token minted by `nodum agent create` / `token-rotate`, shown
  once and stored hashed — carried in the environment, never a flag (a flag
  leaks into `ps` and shell history). At startup it is verified against the
  `agents` table (an unknown or disabled agent is a startup error), the
  verified agent's principal is loaded with its grant set, and every read
  and write is confined to those grants. **Three tiers are never registered,
  and each one is a named absence**: the review tools
  (`accept`, `reject` — `REVIEW_TOOLS`, the §8.1 "write (human)" tier), the
  curative tools (`merge_nodes`, `retype`, `supersede_edge`, `bulk_relink`,
  `consolidate` — `CURATIVE_TOOLS`, §8.2), and **reversal plus the journal that
  records it** (`undo`, `rollback`, `abandon_cycle`, `get_cycle`, `list_cycles`
  — `HUMAN_ONLY_TOOLS`). Structural enforcement, not
  a runtime check. Phase 5a built the whole curative tier and **left this
  exactly as it was** — that absence is now a decision about a surface that
  exists, not a description of code that does not. It stays a decision: an
  agent reaching this tier could merge two nodes or rewrite five hundred edges
  from one call, and the only thing that takes those back is a human's
  rollback. The third list is newer, and it closed a real hole: `rollback_cycle`
  — the most destructive operation in the system, writing recorded payloads back
  verbatim across spaces for a whole cycle at once — was in **no** absence list
  at all, and neither were `undo`, `abandon_cycle` or the two journal reads, so
  the disjointness assertions would have watched a future tool expose any of
  them without a word. Reversal is human-only because no grant delegates writing
  `state = 'active'` back; the journal is human-only because an entry says what
  the gardener did across every space in the file, which is territory an agent
  holds no grant on. `UNREGISTERED_TOOLS` is the union, and what
  `tests/test_mcp_server.py` asserts the registry stays disjoint from; adding an
  operation to any of those tiers means adding its name to a list, never to the
  registry. Launched by `nodum mcp serve`.
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
  `sqlite3.OperationalError` → 503, `OverflowError` → 400,
  `urls.PayloadTooLarge` → 413 and `ClientDisconnect` → 499, which only a network
  surface meets. **Three of this package's exceptions sit in the `OSError`
  subtree**, because `PermissionError` derives from it: `auth.InvalidCredentials`
  → 401, `auth.PrincipalDisabled` → **403** (reached for real when a capability
  outlives the account that minted it), and `store.GrantNotPermitted` → **403**
  (reached for real by `POST /api/cycles`, since the runner writes as the
  *gardener* and `0014` grants it `main` and `meta` alone). Each needs a row of
  its own or it inherits `OSError`'s 500 — and each also needs `_failure_message`
  to leave its message alone, because that function rewrites an `OSError` as
  `storage error: <strerror>` so this surface never prints the operator's
  database path to a stranger. **That exemption used to be a literal tuple, and
  it was wrong twice.** `PrincipalDisabled` was added when a live pass caught
  `storage error: PrincipalDisabled` in a browser; `GrantNotPermitted` was
  missed, so the gardener's "the gardener holds no grant on space 'research' …
  Run: `nodum grant builtin-gardener research edit`" — a sentence written
  specifically for the one click that produces it — reached the journal's toast
  as `GrantNotPermitted: storage error: GrantNotPermitted`, with the space and
  the remedy both gone. Two misses out of three is the *list* failing, so the
  rule is inverted: `_is_domain_failure` asks whether the class was defined in
  this package, and only an `OSError` from somewhere else is rewritten. A domain
  exception added tomorrow is exempt the day it is written.
  `test_every_exception_cli_run_catches_is_mapped` reads `cli._run`'s own
  except clauses and asserts the claim instead of restating it, and
  `test_no_exception_this_package_defines_is_rewritten_as_a_storage_failure`
  **walks the package** for exception classes rather than listing them, so the
  fourth `PermissionError` subclass is audited before anyone notices it exists —
  it must render its own message *and* carry a status row that is not the
  `OSError` 500. Unmapped
  exceptions are a generic 500 with no traceback in the body.
  `RequestGuardMiddleware` is the origin control under all of it (see the
  HTTP contract below) — binding loopback keeps other machines out, not other
  *origins*, and a browser reaches `127.0.0.1` from any page.
  Phase 5a adds the **dream journal** (`GET /api/cycles`, `POST /api/cycles`,
  `GET /api/cycles/{id}`, `POST /api/cycles/{id}/abandon`,
  `POST /api/cycles/{id}/rollback`) and the **nightly
  scheduler**, owned by the app's lifespan and `None` unless configured — so an
  ordinary `nodum serve` creates no background writer at all. Three error rows
  came with them. `RollbackConflict` is **409** with the conflicts in the error
  body — the graph moved on, which is a conflict with current state and not a
  bad request — and it is the one failure whose body says more than `type` and
  `message`, rendered by `_rollback_conflict_handler` while its status stays the
  table's. `consolidate.CycleInProgress` is **409** for the same reason and not
  the 400 it inherited from `ValueError`: the request was fine and the graph was
  busy, which is what a client retries on — and the retry advice is real, since
  the class now comes from the `cycles` row a second opener could not insert
  rather than from a lock in this process, so a client that gives up on 400 and
  retries on 409 is being told the truth about a conflict another process
  created. It is a `nodum.service` class that `nodum.consolidate` re-exports; the
  table's row is on that one class either way. `auth.UnknownPrincipal` is **404**: it
  is a `LookupError`, so it
  inherited neither the `RecordNotFound` nor the `ValueError` row and escaped as
  a traceback and a generic 500 — a shape a cycle meets for real, since the
  runner re-mints whoever asked from stored state. The curative tier is
  **not** on this surface either: rollback is here because it is the human's
  undo for a cycle, `abandon` is here because a cycle left `running` by a
  `SIGKILL` or a shutdown mid-nightly-run makes its own writes irreversible
  until somebody closes it, and `POST /api/cycles` is here because a schedule
  that is off by default would otherwise leave the journal empty forever.
  **`POST /api/cycles` is the one handler that does not call the service
  inline** — it goes through `run_in_threadpool`. Every other handler here is a
  read or a single-row write, where inline is right; a cycle is every job over
  every node in scope (3.75 s measured on 450 nodes with no embeddings, minutes
  with them) and the event loop is single-threaded, so inline it froze
  `/healthz`, the SPA and every other tab for the length of the run —
  `nodum.scheduler`'s own docstring had made exactly this argument for the
  nightly half. What is handed to the thread is `_write`, so the principal is
  still bound in the one place this module binds one. **It frees the loop and
  not the database, and the difference was measured rather than assumed**: with
  the cycle on a worker thread a live pass had `/healthz` and the SPA answering
  throughout, while a concurrent `GET /api/nodes` against the same file took
  **1168 ms** where it takes **5 ms** on an idle server. SQLite has one writer,
  the cycle holds it in bursts, and a reader is behind it for as long as a burst
  lasts — so the honest claim is "the server keeps answering", never "a cycle
  costs other requests nothing". Do not describe this change as making a cycle
  free; the thread moved a total freeze to a slow read, which is the whole of
  what it bought.
- **`nodum.consolidate`** — the consolidation runner (design §8.4/§8.5), and
  everything on the near side of the LLM line: four deterministic jobs and five
  coherence metrics, with no provider, no generation and no judgement anywhere
  in the module. **It is a peer client, not an insider** (§8.4 rule 1): every
  read and write goes through a public `nodum.service` function exactly as the
  MCP server's do — it opens no connection, imports no service private, and
  touches no table — which is what makes the gardener an agent with grants
  rather than a back door with a name, and `tests/test_consolidate.py` asserts
  it over this file's **AST** so a refactor cannot quietly forget it. The jobs:
  `duplicate_candidates` (normalised-title equality, near-equality at 0.95, and
  embedding cosine where a provider exists — it writes a `proposed`
  `duplicate_of` edge and *never merges*, because D9 says a merge is always
  human-approved and a proposed edge is already a queue item with a diff and an
  accept button, so entity resolution needed no new proposal kind),
  `link_maintenance` (two prunings a machine can be *right* about — an exact
  duplicate edge and an edge incident to an archived node, both on `active`
  edges only, since retiring a `proposed` edge is a review decision that belongs
  to the human — then `relates_to` inference from embedding proximity and
  co-citation), `housekeeping` (D3's position rebalance, which is a **correct
  no-op**: `create_node` is the only writer of `position` and writes
  `max + 1.0`, so no sibling set can converge on float precision until a
  reorder operation exists — the gap check is live, not decorative — plus D6
  embedding catch-up by running the existing `vec` projector rather than growing
  a second embedding path that could disagree with search), and `neglect_report`
  (names active nodes untouched past 90 days and **writes nothing**, because
  age is arithmetic while *stale* is judgement). Every edge a job suggests is
  filed `proposed` through the landing seam **whatever the gardener's grant
  allows** — the inferences are the uncertain half by construction, and the
  grant is left alone because it is what lets the pruning half archive an edge
  outright. A **dry run opens a cycle flagged `dry_run` and emits zero events**,
  so `events --cycle <id>` on it is empty, which is the machine-checkable form
  of "it changed nothing" — deliberately unlike `bulk_relink`'s dry run, which
  opens no cycle because it is a diff a human is reading right now rather than a
  rehearsal of the nightly run. One job's failure never loses the others: its
  outcome carries the error, the rest still run, the after-metrics are still
  computed, and the cycle closes `failed` with all of it. Determinism is a
  rule here: no randomness, one clock captured when the cycle opens, and every
  pair, group and list ordered before it is written.
  Three rules guard the run itself. **One cycle at a time, in the whole file —
  and the guard is a row, not a lock.** Migration `0014` carries a partial
  unique index over `cycles(status)` where `status = 'running' AND trigger IN
  ('manual','scheduled')`, so at most one consolidation row can be open at a
  time and `service.open_cycle` refuses the second opener **on the INSERT**
  (`CycleInProgress`, a `ValueError`, now defined in `nodum.service` beside the
  guard and re-exported as `consolidate.CycleInProgress` — the *same* class, so
  every existing `except` and the 409 row still match). The class of bug is a
  read-then-write with no transaction spanning it: every job's "leave what is
  already there alone" is one, so two concurrent runs propose every duplicate
  pair twice. `SELECT` then `INSERT` would have reproduced that shape in the
  guard itself, which is why the index does the deciding. **The first cut was a
  module-level lock and it guarded the wrong half.** It covered the surfaces
  sharing one interpreter — the HTTP route, the nightly task, an in-process
  caller — and covered a `nodum consolidate` typed at a terminal while `nodum
  serve` ran one **not at all**: both completed, and the measured result was
  1580 `duplicate_of` edges over 790 pairs and two journal rows for one human
  intention, on the review queue, which is the human's. The lock is **gone**
  rather than kept underneath: it stated the same rule one layer up with a
  second sentence for the identical condition and no way to name the cycle in
  the way, and it was also too *wide* — two runs against two different database
  files in one process are not a conflict and it refused them anyway. Refusing
  rather than waiting is still the point: a blocking wait would hang a request
  thread for the length of a cycle and then run a second cycle over a graph the
  first had just changed. And the refusal **names the cycle holding the file and
  `nodum cycle-abandon <id>`**, because a run a `SIGKILL` ended never closes
  itself, now blocks every later run in every process, and "try again when it
  has finished" would be advice about a run that will never finish. `curative`
  and `rollback` cycles are deliberately **outside** the index: each is one
  short human-driven operation, neither is what proposes a duplicate pair twice,
  and blocking them for the length of a nightly sweep would take the curative
  tier offline every night. `db._cycles_problems` checks the index exists on any
  file recording `0014`, because `0014` was amended in place while unreleased and
  `init_db` skips a migration whose name it already has. **A scoped cycle checks
  the gardener's own grant** right after
  `open_cycle` and raises `GrantNotPermitted` naming `nodum grant
  builtin-gardener <space> edit` — every space created after `0014` is invisible
  to the gardener, so the first click on the UI's scope picker used to reach
  `list_nodes(space=…, principal=gardener)` and fail with the Q13 non-oracle
  `unknown space: <32-hex id>`: the right sentence for a caller who lacks the
  grant, and the wrong one when the caller can see the space in a picker and it
  is the *gardener* that cannot — and it landed in a permanent journal row the
  dream journal splices into an entry's headline. The check runs **after**
  `open_cycle` on purpose, so a scope the *caller* cannot see is still refused
  by the non-oracle rule first, and the message echoes the reference the caller
  supplied so a name is never answered with a raw id. **The guard catches
  `BaseException`**: Ctrl-C during `nodum consolidate` raises
  `KeyboardInterrupt`, which is not an `Exception` and used to escape with the
  cycle row still `running` — and a `running` cycle cannot be rolled back while
  `undo` refuses every event it stamped, so its writes were irreversible on
  every surface. The per-job handler stays `Exception` deliberately: one job
  falling over must not lose the others, but an interrupt is a request to stop
  the run. Finally, the gardener's principal is minted **once per run**, so **a
  revoked grant bites at the next cycle, not mid-flight** — the same window
  `disable_agent` documents for the MCP server's process-lifetime principal,
  stated in the module docstring because the archive dialog promises an agent
  can do nothing the moment a space is archived.
- **`nodum.scheduler`** — the nightly schedule (decision J1): one asyncio task
  in `nodum serve`'s lifespan, no `cron` file this repo does not ship, no second
  process, no new dependency. Six properties, each a decision. It **cannot
  overlap itself** — the next wait is computed only after the run it follows
  has returned, so no timer can fire into a cycle still in progress, which
  against a single-writer database would be a lock fight at 3am nobody is awake
  to read. A **crash neither takes the server down nor stops the schedule**: the
  runner already closes a failing cycle `failed`, anything escaping it is logged
  and the loop waits for tomorrow. **A night the runner *refused* is a skip, not
  a failure**: cycles are serialised and a second caller is refused
  (`CycleInProgress`) rather than queued, so a human running a cycle across the
  schedule's fire time makes the timer bounce off it — which is not rare and is
  getting less rare, since `POST /api/cycles` moved off the event loop precisely
  so a human-triggered cycle may take minutes. `CycleInProgress` is a
  `ValueError`, so it landed on the generic `except Exception` and the night was
  reported as `scheduled consolidation cycle failed` at **ERROR with a full
  traceback** — a fault report for a night on which the graph was being
  consolidated exactly as intended, by somebody who asked for it. It is caught
  ahead of that handler and logged at WARNING with the runner's own reason and
  no traceback. **A skipped night is visible in the server log and deliberately
  nowhere else**: the obvious alternative is a journal row, and it is wrong three
  times over — `cycles` records runs that *happened*, a row for a non-event would
  carry no events, which is exactly the shape a `dry_run` entry has and this file
  leans on that shape as the machine-checkable proof a rehearsal changed nothing,
  and writing one would mean opening a cycle while the guard it just bounced off
  is still held. The journal already answers the question the human would ask: a
  cycle *did* run that night, listed with whoever triggered it. **"One cycle a
  night" holds on the two nights
  a year that are not 24 hours long**: `NODUM_CONSOLIDATE_AT` is a *wall clock*
  time and a wall clock does not advance uniformly, so `seconds_until` does its
  arithmetic in **aware local time** (`datetime.astimezone`, which reads a naive
  value as local and attaches the offset in force at that instant). Subtracting
  two naive datetimes measures the calendar rather than elapsed time: driven
  over a real `Europe/Paris` timeline that ran the schedule **twice on the
  autumn fall-back** (waking an hour early, then again at the hour it was asked
  for) and **an hour late on the spring-forward**. Neither crashed and neither
  overlapped — the loop is sequential — but the property is the property. The
  two pathological wall-clock times are answered rather than special-cased: one
  that occurs twice resolves to its first occurrence, one that does not occur at
  all resolves an hour later, and each runs exactly once on the right date.
  **The suite could not see that bug, and the reason is a rule for every
  clock-driven test here.** The fast harness (`_VirtualTime`) is a naive clock
  and a sleep that agree — sleeping *is* how time passes, `now += delay` — so a
  wall clock that repeats or skips an hour is not merely untested in it, it is
  **unrepresentable**: the harness could only ever confirm the arithmetic the
  code was already doing. The DST tests hold an aware **UTC instant** (what a
  real `asyncio.sleep` advances) and render it as `datetime.now()` would on a
  machine in `Europe/Paris` — plus `TZ` set for real via `tzset`, since the
  scheduler reads the *process's* local zone and cannot be driven by handing it
  aware values. A test whose fixture cannot express the failure is not coverage
  of it. It
  is **off unless configured** —
  `NODUM_CONSOLIDATE_AT` (`HH:MM`, local wall clock) is unset by default and
  unset means no task is created at all, because a background process writing
  to the human's graph unasked is not something to enable by surprise; a value
  that is set but unparseable is **announced and ignored**, since a server that
  will not boot over a stray character in an optional setting is worse than one
  that says what it skipped. And **shutdown does not wait for it**: `stop()`
  cancels and gives the task `SHUTDOWN_GRACE_SECONDS` to unwind, then returns
  regardless. The cycle runs through `asyncio.to_thread` — the one call on this
  server nobody is waiting for, where running inline would stall every request
  for the length of the cycle. The clock, the sleep and the runner are all
  injectable, which is what lets the tests drive a year of nights without
  sleeping through one.
- **`nodum.envelope`** — the JSON envelope both the CLI and the HTTP API emit:
  `envelope()`, `list_envelope()` (the `{"<plural>": [...], "count": n}`
  convention), and `render_json()`. Extracted so the surfaces cannot drift;
  `GET /api/nodes/{id}` is byte-identical to `nodum node get <id>` on stdout.
  New list output goes through `list_envelope`, never a hand-built dict.
- **`web/`** — the human UI (React 19 + TypeScript + Vite), built into
  `nodum/_web/` by `make web-build` and served by `nodum serve`. Ten views,
  each lazily loaded so CodeMirror, Mermaid, and Cytoscape stay
  out of the initial bundle. The tenth is Phase 5a's **dream journal**: the
  cycle list (`GET /api/cycles`), one entry with its metrics and the events it
  wrote (`GET /api/cycles/{id}`), a run button (`POST /api/cycles`, with the
  dry run beside it), the **abandon** confirm
  (`POST /api/cycles/{id}/abandon`, offered only on a `running` entry — the
  browser half of "a door nothing opens is not a door", and the thing that turns
  the rollback button's refusal from a dead end into a route) and the rollback
  confirm — which is the only place a human
  meets a 409 with a `conflicts` list, so it has to render *both* ends of each
  collision rather than a count. It reads the journal; it does not summarise
  it, because the cycle report and the event list are two different records and
  neither is a substitute for the other. **No sentence on either journal screen
  carries a raw id or a server refusal it has not read**: the two refusals that
  name a space (`unknown space:` and the gardener's ungranted scope) get copy of
  the view's own, and every other server string it renders has its 32-hex ids
  replaced by the page's name for that row — so a message shape nobody
  anticipated cannot put one on the screen. Full rules: `web/README.md`. `src/api/client.ts` is the only `fetch` in the
  app and has **no identity parameter anywhere** — the server's structural
  rule, mirrored in the client. It sends `Content-Type: application/json` on every
  non-GET request that goes to a JSON route, bodyless ones included, because the
  server requires it there — and on exactly one kind of request it deliberately
  sends **no content type at all**: the raw-bytes `PUT /api/uploads/{token}`,
  which is its own `rawRequest` branch. That is coherent rather than an
  exception to be tidied away: the capability routes sit outside the
  content-type gate on purpose (`_is_capability_path`), because that gate exists
  to stop a cross-origin browser write riding an ambient cookie and a capability
  URL carries no ambient credential — so demanding `application/json` on a body
  that is not JSON would only make the client lie about its bytes.
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
  lock for its whole duration. **There is one sniffer, `sniff_mime`, and how
  strongly it knows decides whether it can overrule the filename.** It names a
  type from the *bytes* over `RECOGNISED_MIMES` — the rasters this Pillow build
  reads (PNG, JPEG, GIF, WebP, BMP, TIFF including BigTIFF, JPEG 2000, AVIF,
  ICO), `application/pdf`, the audio containers, and `text/plain` — a vocabulary
  derived from the two places that already decide what this system can act on:
  the rendition path and `nodum.extract`'s registry, and `RECOGNISED_MIMES` is
  asserted to be covered by `extract.handler_for` rather than merely claimed to
  be. **A signature is definite evidence and the text heuristic is weak
  evidence**, which is the whole of the stored-MIME rule (`_stored_mime`): a
  signature may overrule a name from another family — PDF bytes called
  `scan.txt` land as `application/pdf`, which is what `page:<n>` rasters and
  extraction dispatch on — while the name keeps its specificity *within* a
  family, and the text heuristic may only **fill in** where the name guessed
  nothing. That last clause is load-bearing: an uncompressed PDF whose `%PDF-`
  sits one byte in sniffs as text, and letting that win cost the document its
  handler, its page rasters, and put raw PDF bytes into the FTS index. It is also
  why `image/svg+xml`, `application/json` and `application/xhtml+xml` keep their
  own names with no list of exceptions to maintain. **A displaced `%PDF-` header
  is definite evidence too** (`_sniff_displaced_pdf`): `pypdf` and PDFium both
  *scan* for the marker rather than requiring it at offset 0, so a real PDF
  behind a stray byte extracts, paginates and rasterises — and since every PDF a
  human actually drops carries compressed streams, it does not sniff as text
  either, so before this it matched nothing and the upload route **refused it
  outright**. Order matters and is the safety argument: the scan runs only for
  bytes the text test rejected, so prose quoting `%PDF-1.4` — which this repo's
  own `docs/architecture.md` and this file both do — can never reach it, where a
  bounded scan would only have made the misfire rarer. That refusal was found by
  a live end-to-end pass and not by the suite: the test for the mis-typing above
  drives this very route, but with a hand-assembled uncompressed fixture that
  takes the text branch, so it stayed green while a real PDF was turned away.
  **A fixture that cannot reach the branch is not coverage of it.** **Text is a windowed heuristic
  and is documented as one, not as a guarantee**: a NUL or any other C0 control
  byte means binary, checked over a 4 KiB window at *each* end of the file (the
  tail is what catches a zip behind 4 KiB of ASCII, since its central directory
  is at the end); a UTF-16/UTF-32 BOM exempts a file from the NUL rule *only*,
  and the window is then decoded in that encoding and still has to be
  control-free; a UTF-8 BOM proves nothing and is not honoured, because UTF-8
  text passes the byte test unaided and the exemption only ever bought a bypass;
  and an empty file is not text. A NUL-free, control-free binary format is still
  admitted as text — stated, bounded, and never called a guarantee. New
  registrations decide their own MIME; a **dedup hit** keeps the stored one
  except where a definite signature contradicts its family, which is repaired
  with an `UPDATE` (`_repaired_mime`) — `assets` is content-addressed base state
  maintained exactly that way already (`set_extracted_text`), and a row
  registered under an older rule otherwise poisons every later reader of it.
  Registration itself **refuses nothing on type** — it takes no principal,
  and the CLI's tolerance for arbitrary operator-owned files is deliberate; a
  type policy belongs to the HTTP surfaces that take bytes from a stranger.
  Renditions (`thumb` ≤256px WebP
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
  `check_image_pixel_budget` takes its ceiling as an argument and `limit=None` is
  a real posture, not a bypass: **the bomb guard and the 40 MP ceiling answer two
  different questions.** What Pillow itself calls a decompression bomb — and
  bytes Pillow cannot read at all — is about danger and applies wherever an
  image arrives; 40 MP is about what this server can *render*, so it gates
  admission only on the route whose purpose is a rendition. Both refusals also
  take a `name`, because the spool path is the operator's on a terminal and a
  stranger's over a socket. Note that "cannot read" is `OSError`, not
  `UnidentifiedImageError`: that class is one of its subclasses, and a plugin
  whose `accept()` matched before the parse failed raises the bare class, which
  used to escape as an unmapped 500. Pillow reads originals through
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
  between the two node writes is repaired by running it again. That second branch
  answers the *same* question as the first: `pages` is the source's **`block`
  children in a non-archived state** and not every child it happens to have (a
  `source` is an ordinary node, so a note filed under it by any `parent_id` write
  used to be reported as a page of the document), and `pages_truncated` is
  inferred from whether the count reached `MAX_PAGE_BLOCKS` rather than
  hard-coded false — a re-drop of a 900-page scan must not deny the cap the first
  drop reported. Blank pages are
  skipped (a scanned PDF with no OCR handler would otherwise propose a hundred
  empty nodes) while the page number stays in props, so numbering is honestly
  sparse rather than quietly renumbered, and `MAX_PAGE_BLOCKS` (100) stops a
  900-page scan from becoming a 900-item review queue — the overflow is
  reported through `pages_truncated`, never dropped silently.
  **Nothing irreversible happens before a refusal that needs no bytes**: the
  target space is resolved *before* `register_asset`, because registration is the
  irreversible half (there is no delete route) and a grant minted against a space
  archived inside its five-minute TTL otherwise stored up to 32 MiB with no
  describing node, no FTS row, and no way to reclaim them — while the client was
  told the upload failed. `ingest_url` is
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
  revocation. **Claim extraction is deliberately absent** — Phase 5b, the LLM
  half; Phase 5a's gardener is the deterministic one and proposes no claims
  either.
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
  because a domain module has no business importing an adapter. A declared size
  above it raises **`PayloadTooLarge`**, which lives here for the same reason —
  both ends of that ceiling are here, and the adapter imports the class and maps
  it to 413, the direction `TokenInvalid` already runs in. It derives from
  `ValueError` so the CLI reports it as one line; the explicit 413 row wins over
  the inherited 400 because status lookup walks the MRO. A *negative* size stays
  an ordinary `ValueError` — that is a malformed request, not an oversized one.
  `TokenInvalid`
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
  `0014_cycles_and_gardener`). Never edit a shipped migration; append a
  new one. A migration must never leave data readable only through a store a
  later migration replaces: introduce a table where its bytes already belong
  (this is why asset bytes are part of `0007` and there is no `path` column
  anywhere). `0014` adds the `cycles` table, seeds the `builtin-gardener`
  internal agent with its two ordinary grant rows (`read` on `meta`, `edit` on
  `main`), and **refuses the upgrade**
  on a database that already holds an agent id under the reserved `builtin-`
  prefix rather than resolving
  the collision: taking the id would attribute that agent's whole history —
  every `agent:builtin-gardener` in `events.actor`, `versions.actor` and both
  `created_by` columns — to the gardener, and renaming the impostor would
  detach that same history from the account it names, since actor strings are
  immutable log entries and not references anything can follow. Both corrupt
  the one question the event log exists to answer, so the operator renames or
  removes the account by hand and re-runs. The guard is `id LIKE 'builtin-%'`
  and not the single id: `0010` back-fills an `agents` row from every actor
  string in the log, so a pre-0010 file whose events merely *mention*
  `agent:builtin-librarian` upgraded clean and left a live, token-bearing
  external account under the prefix — the collision this guard exists to refuse,
  pre-installed for the day 5b seeds a second `builtin-*` agent.
  `RAISE()` is trigger-only in SQLite,
  so the abort is a `CHECK` constraint whose **name** carries the message —
  SQLite reports it verbatim — over a scratch table that gets a row only when
  something under the prefix exists. The name cannot name the offenders: SQLite
  takes an expression as `RAISE()`'s second argument only from 3.47.1, newer
  than most distributions ship, and a migration that fails to *parse* is a worse
  failure than one whose message carries the `LIKE` pattern to look them up
  with.
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
  `accept`, `reject`, `archive`, every `review` subcommand, and — since Phase
  5a — the curative tier (`merge-nodes`, `retype`, `supersede-edge`,
  `bulk-relink`) and `consolidate`, which ask the identical question through the
  identical check (`Store.require_review`) and add no new permission concept.
  `undo` stays human-only — restoring an event's payload can write
  `state = 'active'` back, and no grant delegates that — and `rollback` is
  human-only for a stronger version of the same reason: it does that for a whole
  cycle at once, across spaces, and an operation strictly more powerful than
  `undo` cannot be gated more weakly than `undo`.
  Both spellings of a reject — single-item `reject <id> --reason` and batch
  `review reject … --reason` — require the reason and record it in the reject
  event's payload: one operation, one audit guarantee.
- **`undo` and `rollback` split on one line: does the event carry a `cycle_id`?**
  An event with none is reversed by `undo`; an event with one is reversed by
  `rollback <cycle-id>`, and `undo` refuses it by name rather than reversing one
  row of a multi-row decision and leaving the other half standing. This is not a
  gap in `undo`: a curative operation always writes several rows from one
  decision, and a merge's tombstone, its redirect row and its repointed edges
  are one act. The no-`seq` search **finds** cycle-stamped events and gets that
  same refusal, naming `nodum rollback <cycle-id>`; it does **not** step over
  them. It used to, and the reading was backwards: `nodum undo` means *take back
  the last thing that happened*, so right after a merge it reversed an unrelated
  older event instead — deleting the edge the merge had just relinked, which
  nobody had named — and that undo then counted as work outside the cycle
  touching a row the cycle owned, so `rollback <merge cycle>` was refused as a
  conflict. Both reversal verbs spent, an edge gone, the merge permanently
  unrollbackable. The only thing still skipped there is an event a previous
  `undo` already reversed: that one has a reversal, while a cycle is simply the
  most recent thing that happened.
  **That refusal names a verb that actually works** (`_cycle_stamped_refusal`).
  Pointing only at `nodum rollback <cycle-id>` was a loop with no exit written
  in it: a rollback is *itself* a cycle and its own events are stamped, so a
  human who follows the advice and then types `nodum undo` again lands on the
  identical sentence — and following it twice re-applies exactly what they just
  took back. There is no state in which a bare `nodum undo` recovers, so the
  message says that and names **the last write carrying no cycle at all**
  (`nodum undo <seq>`), found by the module's own search rather than by a
  second one that could disagree with it. The merge sentence is now
  **conditional on the cycle having written more than one row**: it exists to
  explain why a multi-row decision cannot come apart one event at a time, and a
  cycle carrying a lone `edge.propose` has no other half to leave standing — so
  on that one it was justifying the refusal with something that never happened.
  The refusal itself is unchanged; a cycle is taken back whole either way.
- Errors are always one line on stderr with exit 1, never a traceback — that
  includes a missing file (`asset register /missing.png`), a database another
  writer holds (`database error: database is locked`), and an undo the graph
  has grown past (a created node that now has children). **A command that reads
  a file reads it *through* `_run`**, never beside it: a helper called in the
  argument list of the command's own `_run(...)` is evaluated before `_run` is
  entered, which is how `edge create-batch /missing.json` and `node
  create|update --content-file /missing.md` printed a full Rich traceback while
  `asset register` and `ingest file` did not. `_principal` was moved inside
  `_run` for the identical reason; new file-reading options follow both.
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
  `history <node-id>`, `events [--cycle <id>]`, `types`, `schema <type>`,
  `schema-dump`,
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
  the show-once `ndm_…` token to stderr; only the hash is stored. Every account
  created here is **external and there is no flag for anything else**:
  `service.create_agent` refuses
  `kind="internal"`, because `auth.internal_principal` selects the gardener by
  being the only row with that kind and refuses to choose between two — so a
  second one does not add a gardener, it takes the existing one away and every
  consolidation path dies. `disable_agent` is no cure (the count precedes the
  `disabled` check) and no surface deletes an agent, so the install would only
  be recoverable by hand-editing the database. `--kind` is therefore **gone**:
  its one non-default value became a permanent refusal, which is not a choice,
  and HTTP already hardcoded `external`),
  `grant <agent> <space> <level>` / `revoke <agent> <space>` / `grants [--agent]`
  (`read`/`suggest`/`edit`, event-logged; `revoke` reaches an **archived**
  space by id or name, since archiving makes a grant inert but leaves the row
  for the human to take away, while `grant` refuses one and says why),
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
  `consolidate [--scope] [--job …] [--dry-run]` (the gardener's cycle: `--as`
  names who *asked*, and the writes are the gardener's because the gardener made
  them),
  `cycle-list [--limit]` / `cycle-get <id>` / `cycle-abandon <id>` (the dream
  journal, human-only for
  the reason `events` is — a journal entry says what the gardener did across
  every space in the file, and an agent reading it would learn the shape of
  territory it holds no grant on. **`cycle-get` returns the cycle row and
  nothing else** — what ran, who asked, over what, how it ended — and the
  entry's *diff* is `events --cycle <id>`, deliberately a second command: the
  row stores no diff, because a journal keeping its own copy could disagree with
  the log. `GET /api/cycles/{id}` composes the two (plus the metrics and a
  truncation flag) into one round trip because a browser paints one screen from
  one request; the divergence between the two surfaces is intentional and this
  is where it is written down. `--limit` below 1 is an **error**, the rule
  `subgraph` states and `events` and `node list` now follow: SQLite reads a
  negative `LIMIT` as unbounded, so
  `--limit -3` used to answer with the whole journal — a caller asking for less
  got everything. `events --cycle <id>` on an id naming no cycle is a **not
  found**, not an empty list: an empty list is what a *dry run* looks like, and
  this file leans on exactly that as the machine-checkable proof a rehearsal
  changed nothing. **`cycle-abandon` is the door out of an interrupted run**: a
  cycle left `running` by a `SIGKILL`, a power cut or a server shutdown
  cancelling a mid-nightly-run task makes its own writes irreversible on every
  surface — `rollback` refuses a running cycle and `undo` refuses every
  cycle-stamped event — so this closes it `failed`, with a report naming who
  abandoned it, and `rollback` then works. It refuses a cycle that already said
  how it ended, because re-closing it would overwrite that record),
  `rollback <cycle-id> [--dry-run]`,
  `merge-nodes <ids…> --into <id>`, `retype <ids…> --type <t>`,
  `supersede-edge <edge-id> [--src --dst --type --confidence --set]` (every
  option describes the **replacement**, and every field it does not name is
  inherited from the edge being replaced, so naming none of them retires the
  edge with no successor), `bulk-relink [--src --dst --type --state]
  [--to-type --to-dst] [--dry-run]`,
  `mcp serve` (the agent token comes from `NODUM_AGENT_TOKEN`, never a flag),
  `serve [--host 127.0.0.1] [--port 8600] [--allow-host NAME]
  [--db PATH]`. `serve` prints the database path on stderr and translates
  uvicorn's own startup failure (a port already in use) into the contract's
  exit 1 — it used to escape as uvicorn's exit 3. A non-loopback bind is
  allowed (password login, not the bind, is the boundary), marks the session
  cookie `Secure` there, and warns on stderr that uvicorn speaks plain HTTP —
  the cookie fails closed without TLS, but the login body has already crossed
  the network by then. **The nightly consolidation cycle is configured by
  `NODUM_CONSOLIDATE_AT` (`HH:MM`, local wall clock) and by nothing else** —
  there is deliberately no `--consolidate-at` flag, because unset means off and
  a flag would put "start a background writer on the human's graph" one
  keystroke from an ordinary `serve`. **A schedule that is on says so in the
  banner**, beside the database path and the auth posture: an unparseable value
  was announced and a *valid* one produced nothing at all, which is the wrong
  way round for the one setting that starts a background writer on the human's
  graph. The unparseable case is still announced exactly once, by `create_app`,
  which is also what decides to start without a schedule.
- **A `--dry-run` here answers "what would happen", and each one is precise
  about what it costs.** `consolidate --dry-run` still writes its journal entry,
  flagged, because the journal has to say which it was — and emits **no** event,
  so `events --cycle <id>` on it is empty. `bulk-relink --dry-run` writes
  nothing at all, not even a cycle, because it is a diff a human is reading
  right now. `rollback --dry-run` opens no cycle and returns the conflicts in
  `conflicts` **and the delete guards in `blockers`** instead of raising, which
  is the "would this succeed?" a confirm dialog needs — and it needs both,
  because a rollback fails for either reason and a preflight modelling only
  `conflicts` answered "clean" for a created node that has since gained a child
  or a created space that has since been granted on. Each `blockers[]` entry
  names the cycle's own create event, the row it made, the `dependants` in the
  way and the `reason` the run would refuse with. A dry run reporting either
  list is a rollback that would fail; both empty is the only clean verdict.
- **A refused `rollback` is this CLI's one structured error.** Every other
  failure is one line on stderr because one line is all there is to say; a
  rollback conflict is a *list* — for each row in the way, which event of the
  cycle wrote it and which later event moved it, plus that event's actor and
  cycle — and `RollbackConflict`'s message names only the first few and drops
  the actor and the cycle entirely. So the command prints
  `{"error": {"type", "message", "conflicts"}}` as its one JSON object, and the
  message still goes to stderr with exit 1 exactly as every other refusal does.
  The contract is unbroken: one JSON object on stdout, one line on stderr.
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
  also return. **A limit below 1 is now every capped read's error**
  (`service.require_positive_limit` — the one *public* helper in a file of
  private ones, because `nodum.search` imports it too). `subgraph` stated the
  rule, `list_cycles` followed it, and the rest took the number straight through
  — so `events --limit -3` and `node list --limit -3` handed back the whole log
  and the whole listing, the opposite of what was asked for. It now covers
  `list_nodes`, `list_edges`, `list_events`, `list_proposals`, `suggest_links`,
  `subgraph`, `list_cycles` and `search` (which spells it `k`, hence the helper's
  `name=` argument: a message about `limit` would name a flag that does not
  exist). **The bug is not the same bug everywhere, which is why "one message"
  matters more than it looks**: where the number reaches SQL a negative cap is
  *unbounded*, but where the cap is a Python slice (`list_proposals`,
  `suggest_links`) a negative one silently drops that many rows off the **end**
  and answers normally — on the review queue that is a proposal that vanishes
  with nothing to say it did. Three different wrong answers from one typo, and
  now one refusal. Any new capped read calls the helper; do not restate the
  check. The edge list is also *closed* over the node list: an edge
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
- **That is the rule for every batch verb, and `retype` follows it.** It used to
  print its `failed[]` and exit **0**, so `nodum retype main --type note` —
  which accomplishes nothing, opens a curative cycle and closes it `completed`
  with zero events — reported success to the one thing a script reads. Now the
  envelope is on stdout as before (the successes are the point of not aborting),
  each skipped id is named on stderr as `  failed <id>: <reason>`, and the exit
  code is 1 if any item was skipped. **`bulk-relink` follows it too now, and it
  took a shape change to get there.** Its exemption rested on `skipped[]` mixing
  two things: "nothing would change on this edge" sat beside real refusals under
  one field called `error`, so a script could tell them apart only by matching
  the sentence and an exit code derived from the list would have been wrong more
  often than right. That mixture is **gone** — `BulkRelinkOut` reports
  `unchanged` (bare edge ids the change would not alter: a diff annotation)
  apart from `skipped` (the refusals, each with a reason: a self-loop, a
  duplicate the graph already carries, or a space the caller may not edit) — so
  `skipped` *is* a failure list, and the exit code is derived from it. Without
  that, a run which could not relink three edges for want of `edit` on their
  space reported success to the one thing a script reads, which is precisely the
  `retype` bug above.
- **The rule `bulk-relink` follows is "non-empty `skipped` *and* not a dry run",
  and the second clause is not a courtesy.** Every check a real run makes runs
  on the rehearsal too — that is what makes the diff worth reading — so
  `--dry-run` predicts its refusals accurately and reports them in the same
  field. But nothing was attempted there and nothing was lost, so exit 1 would
  announce a failure that has not happened, on the one command whose entire job
  is to be read before it is run. A rehearsal names nothing on stderr either:
  `  failed <id>` says an attempt was made. The CLI reads `result.dry_run` off
  the answer rather than its own flag, because the service decides which posture
  the run had. This is the only place a batch verb departs from the flat rule,
  and it departs because a dry run is the only batch that never touched
  anything.
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
- **`PUT /api/uploads/{token}` ingests: bytes in, reviewable subgraph out.**
  This bullet used to say the route "registers bytes and stops there" and
  prescribed closing that gap in the domain layer, where the token row's
  principal can legitimately be loaded. Phase 4 did exactly that, in
  `ingest.ingest_upload`, and nobody updated the bullet. The route now answers
  with the whole ingestion — asset, `asset_ref`, `source`, `derived_from`, one
  `block` per page — and the adapter still invents no identity: it hands the
  spooled file and the token row to the domain, which re-mints the principal
  from the row's own `created_by`, so a grant whose account has since been
  disabled fails there and not here. What the route itself owes is what a
  network surface always owes — the grant's `max_bytes` enforced *while* the
  body streams, and the type policy below — and nothing more. A refusal
  **spends the token**, since `urls.consume` runs before the bytes are read, so
  a client retries by re-minting; nothing may offer to resume a spent grant.
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
  explain itself in words. The space rules are the service's, so both
  archive routes (`/api/spaces/{id}/archive` and `/api/nodes/{id}/archive`)
  answer 400 for `main` and `meta`; both writers answer **409
  `SpaceNameTaken`** for a name any space already holds — including an archived
  one, whose message says so, since this listing does not carry it; and
  `POST /api/nodes` answers 400 for `{"type": "space"}` aimed anywhere but
  `meta` (it used to answer 200, and `space` is in the editor's type picker, so
  a human could nest a space inside ordinary territory with one click).
  Archiving a space through either route makes every grant on it inert, which is
  what the archive confirm has to say, and `/api/grants` can still revoke one
  afterwards. Do not re-implement any of it in a handler or in the UI: the
  screen may say *why* before the click, but the refusal is the server's.
- **Account and grant administration is on the API too.** `GET /api/me`
  returns the session's human; `/api/humans`, `/api/agents` and `/api/grants`
  mirror the CLI's `human`/`agent`/`grant`/`revoke`/`grants` commands — thin
  delegates over the service's human-only admin surface, with disable/enable
  and password/rotate as verb-POSTs (`/api/humans/{id}/password`,
  `/api/agents/{id}/token-rotate`, …) in the `/api/nodes/{id}/archive` style.
  Agent creation over HTTP is external-kind and owned by the session's human;
  the show-once token comes back in the create and token-rotate response
  bodies, since HTTP has no stderr to print it to the way the CLI does.
- **The dream journal is five routes, and the curative tier is none of them.**
  `GET /api/cycles` (newest first — byte-identical to `nodum cycle-list`, as
  every list endpoint is to its command), `POST /api/cycles` (run one now —
  `scope` and `dry_run` are the runner's own parameters and this route invents
  neither), `POST /api/cycles/{id}/abandon` (close an interrupted run `failed`,
  which is what makes its writes rollback-able at all; 400 on a cycle that is
  not `running`), `GET /api/cycles/{id}` (the row, its metrics, and
  `list_events(cycle_id=…)` composed into one round trip, bounded by `?limit=`
  with `events_truncated` when it bit), and `POST /api/cycles/{id}/rollback`.
  `POST /api/cycles` exists because the schedule is off unless configured: a
  journal that could only fill itself overnight, on an install that never opted
  into overnight, shows an empty table forever. The runner is the one domain
  entry point this surface reaches that takes *who asked* as a **string** rather
  than a `Principal`, and that is the runner's shape and not a convenience here
  — the scheduler calls the same function with no principal at all, because
  nobody asked, the clock did. Nothing about that round trip weakens the
  boundary: the string comes from the principal `SessionMiddleware` verified
  into the scope, and the runner re-mints it from *stored* state, so a session
  whose account was disabled since login cannot start a cycle. The writes are
  the in-process gardener's; the journal row records the human beside them.
  **A rollback conflict is 409**, not 400 — the graph moved on, which is a
  conflict with current state — and it is the only failure on this surface whose
  body carries more than `type` and `message`: `_rollback_conflict_handler`
  replaces the *rendering* while the status stays `EXCEPTION_STATUS`'s.
  **`consolidate.CycleInProgress` is 409 for the same reason**: it derives from
  `ValueError` and so rendered as a clean 400 carrying the right sentence, but
  the request was well-formed and the graph was busy, which is what a client
  retries on and what 409 means. **And `POST /api/cycles` runs the cycle off the
  event loop** (`run_in_threadpool`) — the one handler here that does not call
  the service inline, because a cycle is minutes of work on a real graph and the
  loop is single-threaded, so inline it stalled `/healthz`, the SPA and every
  other request for the whole run. `_write` is what goes to the thread, so the
  principal boundary is unchanged. **The caveat is the write lock, and it is
  measured**: the loop is free, but a `GET /api/nodes` issued while a cycle runs
  waited **1168 ms** against **5 ms** on an idle server, because SQLite has one
  writer and a reader queues behind the burst holding it. A client that times
  out reads at a second will see those timeouts during a cycle; the fix bought a
  responsive server, not a free one. Do not
  add `merge_nodes`, `retype`, `supersede_edge` or `bulk_relink` here: they are
  the curative tier and they belong to the CLI, and `PATCH /api/nodes/{id}`
  still cannot retype a node.
- **A wrong verb on a real route is a 405 with an `Allow` header**, not the
  catch-all's 404. The catch-all claims every method so a `fetch` never gets
  HTML, which also means it out-matches a real route's 405 unless it asks the
  real routes what they would have matched — which `api_not_found` does.
- **`/healthz` reports liveness only.** It sits outside auth, so anything it
  says is said to everyone; it used to say the absolute database path.
- **`POST /api/assets` is bounded before it buffers**: `MAX_REQUEST_BYTES` is
  checked against `Content-Length` and then enforced mid-stream (the header is
  client-supplied and cannot be the only guard). It registers bytes and writes
  no describing node, so what describes them is the note that inlines
  `![alt](/api/assets/<hash>/rendition/preview)` — which is why it admits
  `INLINE_IMAGE_MIMES`, the rasters this Pillow build can actually render, and
  nothing else, and why the 40 MP rendition ceiling is an admission rule *here*.
  A document belongs on
  the capability route, which ingests it. **There is no delete route**, so
  anything that does land is only reclaimable out of band — a known gap, not an
  oversight, and the reason both routes refuse before they store rather than
  after.
- **One type policy over both upload routes, with the route's capability as its
  only parameter.** `_refuse_unsupported_upload(spooled, name, admits=…,
  pixel_limit=…, cli_hint=…)` sniffs
  the *bytes* (`assets.sniff_mime`) and never the filename or the client's
  `Content-Type` — the sender chose both — then refuses anything outside
  `admits`: `INLINE_IMAGE_MIMES` on `POST /api/assets`, `INGESTIBLE_MIMES` on
  `PUT /api/uploads/{token}`. The second **is** `assets.RECOGNISED_MIMES` rather
  than a copy of it, so the policy cannot drift from what the sniffer knows;
  widening either route means adding the type to the sniffer, where the whole
  system sees it. Every other difference between the two routes is a **named
  argument, never a set comparison**: the decompression-bomb guard runs on both
  (it was unguarded on the capability route), while `pixel_limit` carries
  `assets.MAX_IMAGE_PIXELS` on `/api/assets` alone — 40 MP is what this server
  can *render*, and a 600 dpi A3 scan is ~70 MP of ordinary document, so making
  it an admission rule on the ingestion route would be capability gating
  admission — and `cli_hint` says whether the refusal points at
  `nodum ingest file`, which is a fact about being the widest *network* route and
  not something to infer from `admits == INGESTIBLE_MIMES`.
  What the policy gives up, deliberately: a PDF is refused by `/api/assets` and
  ingested by the capability route; a renamed binary, a `.docx`, and anything
  else with NULs and no signature are refused by both, and that refusal names
  `nodum ingest file` as the way in, because the pipeline's tolerance for a file
  no handler claims is unchanged and the CLI is where an operator registers a
  file they already own. **That refusal is a heuristic, not a guarantee**, and it
  has exactly two documented ways through, both of which degrade cleanly rather
  than reaching anything they should not. A **NUL-free, control-free** binary
  format is admitted as text — see the sniffer's windowed rule under
  `nodum.assets` — bounded by what the download route serves it back as. And
  **non-text bytes carrying a versioned `%PDF-` header in the head window** are
  admitted as PDF, so a zip whose first entry is a PDF passes: that is the
  displaced-header scan being broader than "is a PDF", it grants nothing the same
  bytes at offset 0 did not already grant as a leading signature, and the
  downstream answer is an extraction `detail` or a mapped 400, never a 500 or a
  bad rendition. Availability is *not* part of it
  — an install without the `pdf` extra still admits a PDF and reports in `detail`
  that no text came out, since refusing at the door is a worse answer than the
  honest empty one.
  A new upload route names its admitted set and calls this helper; it does not
  grow a check of its own.
- **A refusal on the capability route is indistinguishable from a re-drop in the
  audit log.** The escape hatches log both ends (`asset.upload_url` on the mint,
  `asset.upload` on the redemption) and ingestion logs `asset.ingest`, but a spent
  `asset.upload` with no `asset.ingest` after it now means *type-refused* **or**
  *over the grant's size* **or** *already ingested into that space* — three
  outcomes, one silence. Nothing reads it wrongly today; it is a readability cost
  on the one surface whose rule is that an escape hatch logs both ends, and it is
  written down here rather than left to be rediscovered.
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
  It also owns `Content-Type: application/json` on every non-GET request to a
  JSON route, bodyless ones included, because the server requires it there. It
  has **three** request shapes, not two, and the third one sends no content type
  at all: `rawRequest`'s raw-body branch, for `PUT /api/uploads/{token}`. Do not
  "fix" it by adding a header — the capability routes are deliberately outside
  the content-type gate (`_is_capability_path`): that gate stops a cross-origin
  browser write riding the session cookie, and a capability URL carries no
  ambient credential to ride, so requiring `application/json` on a body of raw
  bytes would only be the client lying about what it sends. A `form` body
  (`POST /api/assets`, multipart) is the second shape and sets no content type
  either, because the browser writes its own boundary. Auth is the
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
  `search`, `createNode`, `createSpace`, `renameSpace`, `archiveSpace`, and the
  upload flow's two halves, `requestUploadUrl` (`POST /api/uploads`) and
  `redeemUploadGrant` (`PUT /api/uploads/{token}`) — into
  one `UnknownSpaceError`. The upload pair raises the `UnknownUploadSpaceError`
  **subclass**, which is a second *fact* rather than a second discriminator:
  `isUnknownSpace` answers true for it exactly as before, and what it adds is
  which request refused, because a refused mint sent nothing while a refused
  redemption already spent the grant and streamed the body. Copy that cannot tell
  those apart claims "nothing was uploaded" about a request that uploaded the
  whole file. It is keyed on the message (`unknown space: …`),
  because no status is specific enough: the node listing answers 404 and search
  answers 400 for the same event, while a 404 from `POST /api/nodes` is equally
  an unknown node *type*. Two views once carried their own copy of that match,
  and a second copy of a discriminator is how the two drift apart — if a bare
  `ApiError` with that message ever reaches a view, wrap the call in the client.
- **Nothing user-facing may say a space does not exist.** Not "no such space",
  not "does not exist", not "unknown/missing/nonexistent space", not "not
  found" — and not by handing an `UnknownSpaceError` to `describeFailure`,
  whose 404 body is *"The server has no record of …"*, nor to `toast.showError`,
  which renders `` `${type}: ${message}` `` and so emits *"UnknownSpace: unknown
  space: research"* verbatim. That second trap is the one that actually bit, on
  the detached editor saves, because reaching for the error toast reads as a
  reporting choice rather than a copy decision. It is a copy decision. The
  server answers a
  space that was never created and a space the caller holds no grant on with
  **word-for-word identical text on purpose** (Q13 review S3): a refusal that
  told them apart would be an existence oracle over every space in the file, and
  the space filter would leak the shape of what an agent cannot read. Say what
  changed instead — a space stops resolving once it is archived, and a renamed
  one no longer answers to its old name. `views/search/spaceFailure.ts`,
  `views/editor/createOutcome.ts` and `views/spaces/spaces.ts` own that copy and
  pin it with tests; new copy goes through one of them. The one sentence about a
  refused **write target** — the space a create or an upload was filing into —
  belongs to `components/spaceNaming.ts` instead (`writeTargetWouldNotResolve`):
  the uploader is that sentence's
  second user, and the rule here is to promote on the second user rather than let
  a second view keep its own copy. `components/` is also where it can be reached
  by every node-creating surface, which is what makes it the shared tier.
  The refusal that names
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
  `unlistedMark` — a space reference is an id *or* a name everywhere, so resolve
  before comparing); the `GET /api/spaces` read behind all of them is
  `components/useSpaces.ts`. Do not add a seventh copy of that fetch or a second
  `spaceLabel`. `GET /api/spaces` is **active-only and stays that way** — it is
  the vocabulary behind every picker, and a retired space belongs in none of
  them. Naming a space that listing cannot (one archived while its proposals
  waited, holding a node you are reading, or left behind in a write target or a
  filter) goes through `components/spaceNaming.ts` and its lazy
  `components/useArchivedSpaces.ts` — review, search, editor, graph, the grant
  table, both pickers, and every sentence the editor and search write about a
  refused space. That resolution was once view-local to the review queue; when
  the rest of the app needed it the answer was to **promote the read, not widen
  the endpoint**, and that stays the answer. `nameSpace` has four answers, and
  `pending` is not `unknown`: a space list still in flight is not an
  unresolvable space, so `?? []` at a call site is the bug — pass the `null`
  through.
- **An archived space is *nameable* everywhere and *selectable* nowhere**, and
  those are two rules, not one. `spaceLabel`'s `?? spaceRef` fallback is the
  picker's own — a controlled `<select>` whose `value` matches no option renders
  blank and is silently rewritten by the next change event — and it is a bare
  32-hex id anywhere else, which is why it is **no longer exported from
  `components/`**: its one caller is `spaceOptions`, in the same module, and
  every surface that reached for it instead of `nameSpace` was an id on a
  screen. The picker names an archived *selection* by being handed
  `spaceOptions(spaces, selected, selectedName)` — one already-resolved
  `SpaceName` for the value it is already carrying. It is never handed the
  archived **list**, so nothing inside it can put an archived space among the
  choices; the option it adds is the current value, marked `(archived)` rather
  than `(unavailable)`, and it is gone the moment the human picks something
  else. Widening that seam to a list would let someone newly choose a space the
  server refuses to resolve, which is worse than the id it fixed and is exactly
  what D1a exists to prevent.
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
- **The archive confirmation states consequences the server actually delivers.**
  `views/spaces/spaces.ts`'s `archiveConsequences` is the one place that copy
  lives, and every line in it has to be a fact: the space leaves every picker
  and stops resolving; its nodes keep their `space_id` and stay readable to the
  human; its **name stays reserved**, so no new space may take it; and **every
  grant on it goes inert** — an agent granted there can read, write, propose and
  review nothing until the archive is undone, though the grant row survives on
  `/admin` so it can be revoked for good. That last line was copy before it was
  behaviour: the service kept the agent's authority over everything reachable by
  node id, so the dialog promised the opposite of what happened. Do not soften
  it back — a human archiving a space to cut an agent off now gets exactly that.
- **The write target is app-wide, sticky, and must be visible** (design decision
  D1a). `src/lib/writeTarget.ts` owns it: one module-level value, persisted in
  `localStorage`, synchronised across tabs through the `storage` event, and
  **never changed without the human being told** — a target naming a space
  archived from somewhere else (the CLI, another session) survives and fails at
  the write, because filing a node somewhere the human did not choose is worse
  than a refusal they can read. The one reset is `clearWriteTarget()`, and it has
  two callers. `/spaces` calls it when the human archives the very space they are
  filing into: that is the second half of an act they just performed, not a
  correction behind their back, and it is announced in both the archive
  confirmation (before) and the toast (after). Logout calls it too — the value is
  persisted per browser, not per session, so a second human signing in on the
  same machine would otherwise inherit the first one's target. The rule is about
  *silence*, not about immutability. `useWriteTarget()` is the subscription;
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
  `type` is immutable on the update path — retyping is a curative operation
  with no HTTP route at all — so the editor drops the type commands on a saved
  node rather than offering one that silently no-ops. Same rule as the
  HTTP contract's "do not invent request fields", one layer up. The curative
  tier as a whole is CLI-only, so nothing in this UI may offer a merge, a
  supersede or a bulk relink; what it *does* offer is the reverse of all of
  them, because rollback is the human's undo for a cycle.
- **The journal shows the two records apart, and never merges them.** A cycle's
  `report` says what each job examined, proposed, applied and skipped; the
  events say what actually changed. They are two records on purpose — a journal
  that folded them together could disagree with the log, which is the one thing
  the log exists to prevent — so a view renders both and summarises neither into
  the other. A **dry-run** entry has a report and no events at all, and that is
  the point rather than an empty state to hide: it is the checkable form of "it
  changed nothing", and copy that says "no changes recorded" reads as a failure.
  A **rollback confirm** has one hard rule: a 409 carries a `conflicts` list,
  and each conflict names *both* ends of a collision — the cycle's event and the
  later one that moved the row. Render both. A count, or the server's message
  alone, tells a human that something is in the way without telling them what,
  and the only action available to them is to go and look. The preflight now
  answers with a second list, **`blockers`**, and a confirm that renders only
  `conflicts` still says "clean" for a rollback that will fail: a blocker is the
  graph having *grown something onto* a row the cycle created (a child node,
  an occupant, a grant, a type in use, a merge redirect) rather than having
  moved it. Each entry carries `row_id`, `cycle_event_seq`/`cycle_event_op`, the
  `dependants` in the way and the `reason` the run refuses with — render the
  reason and the dependants. A verdict is clean only when **both** lists are
  empty.
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
