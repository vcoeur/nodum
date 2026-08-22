# Decision log

One entry per decision or measured finding, **dated, append-only — never
rewrite an entry; add a new one that supersedes it.** This log was created on
2026-08-04 by the documentation split (review finding M40): `AGENTS.md`'s
measured and dated prose moved here so the contract file states rules only
(the 04-documentation review's rule 3). Entries preserve the numbers the
original prose stated, verbatim; entries whose prose carried no date are dated
with the day they were recorded into this log (2026-08-04), and the original
measurement dates are kept where the prose had them.

## 2026-08-04 — Phase 1 and 2: core and agent-native land

Phase 1 (core) and Phase 2 (agent-native) landed: **event-log projectors**
(`nodum.projectors`) with per-projector checkpoints and rebuild mechanics, the
**`fts` projector** (FTS5 over node title + content), the **`vec` projector**
(sqlite-vec chunk embeddings, local in-process fastembed model — migration
0006), **hybrid search** (`nodum.search`, CLI `search`): BM25 + vector lists
fused by reciprocal rank fusion, then one-hop graph-expansion re-ranking, with
a per-signal `signals` breakdown, **principals, spaces and grants** (Q13:
`humans`/`agents`/`grants` tables, a scope-bound store, `read`/`suggest`/`edit`
per (agent, space) — no policies, no auto-accept anywhere), the
**review/accept API** (proposal listing with reviewer context, where every
referenced node is reported as `{id, title, space_id}` so the human UI's queue
can group by space without chasing ids, plus batch accept/reject by id or
filter — a human, or `edit` on the item's space; `undo` stays human-only),
**proposed updates** (agent `update_node` stages a `proposed` version recording
which fields it named; accept applies exactly those, reject archives it —
migrations 0005/0008), the **MCP server** (`nodum.mcp_server`, stdio, read +
additive tiers only; review and curative tools are never registered), and
**assets + image renditions** (`nodum.assets` — migration 0007): thin
content-addressed asset registration (a metadata row + an in-database blob +
sha256) and lazily generated, stored, evictable `thumb`/`preview` WebP
renditions (design §5.7), exposed over MCP as `get_asset` (metadata + rendition
image block — never the original).

## 2026-08-04 — Phase 3: the human UI lands

Phase 3 (human UI) landed: the **HTTP API** (`nodum.http_api`, `nodum serve`)
is the human surface — a Starlette app serving the JSON API under `/api` and
the built web UI at `/`, gated on password-login sessions with every write
attributed to the session's human and no request field able to say otherwise —
the shared **envelope** module (`nodum.envelope`) both the CLI and the API
render through, and the **web UI** itself (`web/`, React 19 + TypeScript, built
into `nodum/_web/` by `make web-build`; gitignored, shipped in the wheel as a
hatchling artifact): nine views — login, Markdown editor, hybrid search, review
queue, graph, assets, a spaces screen, an accounts-and-grants admin, per-node
version history. Phase 5a adds the tenth (the dream journal).

## 2026-08-04 — Phase 4: ingestion lands

Phase 4 (ingestion) landed: **text extraction** (`nodum.extract` — a registry
of optional handlers keyed by MIME family, where an absent dependency is a
returned result and never an exception), the **ingestion pipeline**
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
`ingest_url`/`request_upload_url`/`get_download_url`, and HTTP
`POST /api/ingest`, `POST /api/assets/{id}/download-url`, `POST /api/uploads`,
`GET /api/download/{token}`, `PUT /api/uploads/{token}`.

## 2026-08-04 — Phase 5a: the gardener's spine lands

Phase 5a (the gardener's spine) landed — the deterministic half of design
§8.4/§8.5, cut at the LLM line: **consolidation cycles** (migration
`0014_cycles_and_gardener` gives `events.cycle_id` the table it has pointed at
since `0001` with nothing on the other end), the **internal agent**
(`builtin-gardener`, seeded by that migration with `read` on `meta` and `edit`
on `main` as ordinary grant rows, minted in-process by `auth.internal_principal`
with no credential to present and none to steal — `read` on meta because
resolving a type is a read and no job ever writes the vocabulary
(`_is_curatable` excludes the meta space and the structural types outright),
where `edit` bought latent authority nothing shipped reaches: creating spaces,
renaming `main`, and archiving the `note` type, after which a **human** is
blocked from writing a note too — and the `builtin-` id **prefix** is
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
`propose_edges` / `create_node`: §8.3's grant-is-a-ceiling, which is what puts
the gardener's inferences in the review queue), the **consolidation runner**
(`nodum.consolidate` — five deterministic jobs including **queue curation**
(§L1–§L4: proposers' acceptance rates over their proposals — row state measures
the outcomes, the event log classifies which rows were proposals — recorded as
convention notes in the `conventions` space plus one `annotations` row per
queue item — statistics and the record, never the judgement: nothing
auto-accepts and nothing gates a write on the proposer's own `confidence`),
running as a peer client over the public service API), the **nightly
scheduler** (`nodum.scheduler`, one asyncio task in `nodum serve`'s lifespan,
**off unless `NODUM_CONSOLIDATE_AT` is set**), and the **dream-journal view**
in the web UI, which Phase 3 deferred to Phase 5 precisely because it needed a
cycle to have something to show.

Two columns reserved since `0001` get their full temporality here (D2):
`merge_redirects` gets its first writer (from `merge_nodes`), and
`edges.valid_from`/`valid_to` become a real capability — **`valid_from`** is
written at creation by an edge that lands `active` (an active edge IS true at
creation, so the value is a fact, not a guess) and by the accept transition
(`proposed` → `active`, only when still NULL — the edge became true when
accepted); **`valid_to`** is written by every `active` → `archived` retirement —
the shared edge-state writer (`_set_edge_state`) records it for plain archives,
wikilink/synthesis retirement, merge and `supersede_edge` alike, so archival
and validity closure are the same instant by construction, and a rejected
proposal (never true) closes no window. The read paths pair with the writers:
`list_edges`, `subgraph`, `traverse`/`get_neighborhood` and search's `--expand`
gain an `as_of` instant — the default read stays the live graph, and an as-of
read returns exactly the edges whose validity window covered the instant
(`valid_from` unset or `<= t`, and `valid_to` unset on a live row or `> t`),
with pre-D2 NULL rows read as "valid since the beginning" (active) or "closed
at an unknown time, so not placeable" (archived).

## 2026-08-04 — Phase 5b: the LLM jobs land

Phase 5b's LLM work is the deliberate exception to the no-model rule and ships
in two halves. 5b-i — the internal agent runtime (`nodum.llm` +
`nodum.agent`) — ships first: the provider, the accounting, the budgets and the
kill switch, so the thing being observed arrives after the observability and
can be judged rather than trusted. 5b-ii lands the jobs on top.

**The abstraction job is the first of them and has landed.** Its selection is
fully deterministic — dense, sized, not already synthesized, all computed
before any model call — and the model writes the synthesis text and nothing
else: it never decides *whether* to synthesize, only what the text says. The
write files a `concept` node `proposed` with `props.synthesized` and one
`derived_from` edge per member, through the same landing seam as every other
inference; a synthesis is decided together with its members — accepting the
concept activates its `derived_from` edges, rejecting it archives them — and
the run's cost rides the cycle report under `report["llm"]`. Design Constraint
4 is unchanged and now structurally enforced — the model stays out of
validation, the state machine and the projectors (`tests/test_llm.py` proves
those modules cannot reach `nodum.llm` at all), and the five deterministic jobs
of `nodum.consolidate` still run on a machine with no model present; the
abstraction job is the deliberate exception, gated on the cycle budget
(`NODUM_LLM_CYCLE_BUDGET`, off by default) and on a configured provider.

**Learned queue curation is also built, and it is a deterministic job, not one
of the LLM jobs**: the curation job (§L1–§L4) computes each proposer's
acceptance rate from **its proposals** — `active`/`archived` rows whose
creation op was `propose` (row state measures the outcomes; the event log only
classifies which rows were proposals, so a direct `edit` write, a materialised
wikilink or an ingest subgraph never counts) and `applied`/`archived` versions
over the last `CURATION_WINDOW_DAYS` (90) — and records the result twice: a
convention note per `(proposer, edge type)` in the `conventions` space (the
gardener's own, migration `0016`, where it holds `edit` alone), and one
`annotations` row per queue item whose proposer has history on its type,
written through `service.annotate` and read back on `ProposalOut.annotation`.
It never accepts and never rejects — statistics and the record, not the
judgement — and **nothing gates a write on the proposer's own `confidence`**.
Auto-accept exists as a real interface and stays OFF at `null`: the job reads
the well-known `auto_accept_above` props field off `conventions`-space notes,
and even when a human sets one the accept direction stays conservative and
unimplemented (the measured evidence put its misses there); the report says so
and names what would turn it on. The window is measured from row `created_at`
because row state records no decision time — the recorded deferral is a
`reviewed_at` timestamp on the row, deliberately not added, and the `policies`
table (dropped by migration 0010) stays dead.

## 2026-08-04 — deliberately not built, and why

**Claim proposals** moved to Phase 5b deliberately rather than being
forgotten — deciding that a sentence *is* a claim is a judgement call and
belongs to the research agent in design §3, and splitting prose into sentences
would fill the review queue with noise instead of knowledge, so ingestion
proposes sources and structure and stops. The **remaining LLM *jobs* of the
gardener** — props migration on a retype, deciding that an untouched claim has
gone *stale* rather than merely old, and the two Q12 metrics that need a model —
are 5b-ii follow-ups. **Markdown Mirror** and any whole-graph export are not
built (the only export that exists is the thin per-node snapshot,
`GET /api/export/node/{id}?depth=`, which is `get_neighborhood` with a
`content-disposition` header — not a format, not a backup). Each lands as its
own append-only migration where it needs one.

## 2026-08-04 — a module-level lock guarded the wrong half

The first cut of the one-cycle-at-a-time guard was a module-level lock, and it
guarded the wrong half. It covered the surfaces sharing one interpreter — the
HTTP route, the nightly task, an in-process caller — and covered a `nodum
consolidate` typed at a terminal while `nodum serve` ran one **not at all**:
both completed, and the measured result was **1580 `duplicate_of` edges over
790 pairs and two journal rows** for one human intention, on the review queue,
which is the human's. The lock is **gone** rather than kept underneath: it
stated the same rule one layer up with a second sentence for the identical
condition and no way to name the cycle in the way, and it was also too *wide* —
two runs against two different database files in one process are not a conflict
and it refused them anyway. The guard is now a row: migration `0014` carries a
partial unique index over `cycles(status)` where `status = 'running' AND
trigger IN ('manual','scheduled')`, so at most one consolidation row can be
open at a time and `service.open_cycle` refuses the second opener **on the
INSERT** (`CycleInProgress`, a `ValueError`, now defined in `nodum.service`
beside the guard and re-exported as `consolidate.CycleInProgress` — the *same*
class, so every existing `except` and the 409 row still match). The class of
bug is a read-then-write with no transaction spanning it: every job's "leave
what is already there alone" is one, so two concurrent runs propose every
duplicate pair twice. `SELECT` then `INSERT` would have reproduced that shape
in the guard itself, which is why the index does the deciding. Refusing rather
than waiting is still the point: a blocking wait would hang a request thread
for the length of a cycle and then run a second cycle over a graph the first
had just changed. And the refusal **names the cycle holding the file and
`nodum cycle-abandon <id>`**, because a run a `SIGKILL` ended never closes
itself, now blocks every later run in every process, and "try again when it
has finished" would be advice about a run that will never finish. `curative`
and `rollback` cycles are deliberately **outside** the index: each is one short
human-driven operation, neither is what proposes a duplicate pair twice, and
blocking them for the length of a nightly sweep would take the curative tier
offline every night.

## 2026-08-04 — the rollback preflight under-reported, and a dry run once lied

The rollback delete-guard modelling once covered only conflicts and not
blockers, and the outcome was the same shape and was fixed the same way: a dry
run returned `restored_nodes`, `restored_edges`, `restored_versions`,
`deleted_nodes`, `deleted_edges` and `redirects_removed` all empty whatever the
rollback was about to do, so the preflight answered *"reversing 17 446 events"*
about a reversal that was going to delete 17 446 edges — **measured on a live
graph**. One accounting (`_RollbackEffects`) now answers both paths:
`_apply_rollback` fills it as it reverses and `_planned_effects` fills it from
the same plan without writing, and the test asserts *the lists are equal*
rather than pinning expected values, because hand-written expectations are
exactly how two implementations start to drift. The one live read in it —
whether a `merge_redirects` row is there to remove — is taken before the
reversal touches anything, which is when the run takes it too. What still
under-reports is the web journal's rollback **toast**, which counts nodes and
edges only; itemising versions there is the follow-up `RollbackOut.restored_versions`
already carries the data for.

## 2026-08-04 — the reversal chain needs a second record

The journal's own `rolled_back` bookkeeping has to hold at every depth
(`_restate_reversal_chain`), and the chain *alternates*: a rollback that is
itself taken back stops standing, so the cycle it reversed stands again and its
mark comes off — but that cycle may be a rollback in turn, and one that stands
again is once more holding *its* target down, so that mark goes back on. Every
step flips. Clearing exactly one hop was right at depth 2 and wrong from depth
3, where the journal ended up asserting the mirror of the invariant it exists
to keep: a cycle reported `completed` with no `rolled_back_by` while its
writes were reversed and standing that way. That is not only an entry a human
misreads — `_rollback_plan` refuses an already `rolled_back` cycle by reading
exactly that column, so a stale `completed` handed it a row it would cheerfully
reverse a second time. What the walk follows is a *record*, and it needs two of
them (`_reversal_record`). It cannot be `cycles.rolled_back_by`: that mark is
what the walk rewrites, so it cannot also be the thread. The rollback's
**report** was the only other one, and it is written by the `close_cycle` at
the end of `rollback_cycle` — which a rollback whose process died between
`_apply_rollback`'s commit and that line never reaches. `abandon_cycle` is the
door out of exactly that state and it replaces the report wholesale
(`{abandoned, abandoned_by, detail}`, naming no cycle), so a report-only walk
stopped dead at the one rollback a human had to close by hand and left every
cycle below it marked `rolled_back` by a cycle that had itself been taken back
— writes standing while both the journal and `rollback`'s own refusal ("roll
*that* cycle back", itself refused) said otherwise. The second record is the
rollback's own `cycle.rollback` **summary event**: emitted inside the
transaction that applies the reversal, so it exists whenever the reversal does;
never rewritten, because nothing rewrites an event; and carrying
`previous_status` as well, which `rolled_back_by` cannot — a `failed` cycle put
back into force is `failed` again, and a fallback that only knew *which* cycle
would have had to guess `completed`.

A rollback is itself a cycle, so rolling *it* back re-applies the original —
the reversal is an involution, and it holds at **every** depth, which is not
free: the `merge_redirects` removal keys on what the payload *says happened*
(`after.props` gained `merged_into` and `before` did not), never on the op
name. Keying on `op == 'node.merge'` was right for the first two rollbacks and
wrong from the third, because a rollback that re-applies a merge writes that
same before/after pair under the name `node.rollback` — so reversing it
restored the node and stranded the redirect, after which the tombstone's create
was un-undoable for good and merging it again died on the redirect's primary
key.

## 2026-08-04 — a version review was the fourth instance of the two-sided reversal class

A review changes two rows from one decision and only one of them is a graph
record, so both halves sat outside both reversal verbs by two different
mechanisms. Accepting emitted an ordinary `node.update`, which reversed
correctly, and *also* flipped `versions.state` to `applied` through no event of
its own — the `merge_redirects` shape exactly. Rejecting did emit
`version.reject` carrying the version rows, and that event was skipped by
`_rollback_plan` (its op prefix was not in `_TABLE_KIND`) and excluded from
`undo` (`_UNDOABLE_OPS` matched `node.%`/`edge.%` only) — so the one review
outcome that *had* recorded itself properly was the one nothing could read.
Neither was hypothetical: `_transition_row` gates a version through
`store.require_review`, which passes for an `edit`-granted agent, and `0014`
gives the gardener `edit` on `main` — the authority is live in the shipped
release and only the call site is missing. And the accept half needed no agent
at all: a human accepting their own queue item and pressing `undo` reached it,
leaving the proposal marked `applied` over content that had gone back, which
strands it for good because a version leaves `proposed` exactly once. The fix
is the one `merge_redirects` already established rather than a fifth
mechanism: the accept's `node.update` payload carries the version row's own
before/after under `VERSION_STATE_KEY`, and `_restore_version_state` writes the
recorded row back and returns the **mirrored** record for the reversal's own
payload — so rolling a rollback back re-applies the accept, at every depth,
with no inverse code path. The reject needed no new payload: `version.`
simply joins the reversible kinds (`_REVERSIBLE_TABLES`, which is *not*
`_TABLE_KIND` — a version row is reversible but carries no conflict, since
`_transition_row` is its only writer and moves it out of `proposed` once), so
the rollback plan reverses it and emits `version.rollback`. That op is
deliberately **outside** the `node.`/`edge.` namespaces the projectors dispatch
on — the mirror of the rule the curative ops follow: an op that changes node
text must be `node.*` or the index desynchronises, and an op that changes
*only* a `versions` row must stay out of it or the index reprojects a node
nothing touched. `undo` reaches both halves too, and `version.%` in
`_UNDOABLE_OPS` fixed a second thing on the way: a bare `nodum undo` after a
rejection used to reach *past* it to the node's own create — a proposal emits
`version.propose`, so the create was the last `node.` event — and delete the
node, taking the rejected proposal's row with it. `RollbackOut.restored_versions`
and `UndoResult.restored_version` are the reported half; a reversal that moved
a row its own result did not mention is the smell this whole class is made of.
Both are additive with defaults, so no adapter had to change — and the web
journal's rollback toast (`rollbackOutcome`) still counts nodes and edges only,
which understates a review-only rollback rather than misstating it: it leads
with "N events reversed", which stays true. Itemising versions there is the
follow-up, and it is a `web/` change rather than a service one.

**Events written before this fix are not covered**, deliberately: the recorded
move is what the reversal reads, an accept that predates the key recorded none,
and a branch inferring one from `applied_version_id` would be a second path no
test can reach honestly. A pre-fix accept-then-reverse leaves its version on
`applied`; putting it back is a `versions` UPDATE, not a mechanism.

## 2026-08-04 — the stop read is no longer an existence oracle

`stop_requested` used to be an existence oracle. The two answers it gave a
principal it turned away — `RecordNotFound` for an id naming nothing,
`GrantNotPermitted` for a cycle it may not watch — told those cases apart, so
anything holding one grant could probe a cycle id and learn whether a cycle
wearing it exists. Cycle ids are `uuid4` and both journal reads are human-only,
which bounds the damage and does not close the class. The ordering trick the
space-name check uses — ask the grant first, so the existence question is never
reached — is unavailable here, because the grant to ask for is recorded *on the
row*; so it takes `_resolve_space`'s shape instead, the Q13 non-oracle one:
**one refusal for both cases**, the not-found one, echoing back nothing but the
id the caller supplied. `_no_such_cycle` owns that sentence so the two answers
cannot drift apart again, and humans — unfiltered here as everywhere — are
never told a cycle they can see does not exist.

**Closed here and nowhere else, which is the whole claim.** It is not the
*class* of cycle-id oracles: `close_cycle` takes a cycle id, is not human-only,
and still answers `GrantNotPermitted` for a cycle the caller may not close
against `RecordNotFound` for an id that names nothing — the only one of the
seven cycle-id surfaces that still tells them apart (`get_cycle`,
`request_stop`, `abandon_cycle`, `rollback_cycle` and `list_events` all refuse
before they read the row, so none of them can). That is deliberate.
`close_cycle`'s refusal is the *same* `require_review` `open_cycle` raises, on
the same spaces, and `open_cycle` cannot be an oracle at all because it takes a
scope and not an id; collapsing one half of that pair would leave a principal
that opened a cycle and cannot close it reading *this cycle does not exist*
about a row it is holding open, and would falsify the symmetry
`Store.require_review` documents.

**What the collapse costs, said once.** The refusal a principal outside the run
now meets is `consolidation cycle not found`, and that sentence reaches a
*legitimate* runner too. Grants go inert at **mint** time (`auth._grant_set`
drops grants on archived spaces), not at check time, so a run whose scope is
archived under it keeps reading its switch — `_run_cycle` mints the gardener
once and holds it, the module's own "revocation bites at the next cycle, not
mid-flight" — while the **next** principal minted for that cycle, in a later
process or after a restart, is told its own cycle does not exist. It used to be
told it needed `edit` on the item's space, which at least pointed at the cause.
Both refusals are wrong for that reader; this one is quieter about the thing an
outsider must not learn, which is the trade that was taken. Reading the
*recorded* scope rather than re-resolving it is what keeps the lookup working
at all (`_resolve_space` matches active spaces only) — it does not, and cannot,
keep an archived space's cycle readable by a re-minted runner.

## 2026-08-04 — `request_stop` is three verbs' worth of distinction

`request_stop` is **human-only**: it stamps `0015`'s two columns on a `running`
cycle and does nothing else — the row stays `running`, no event is emitted, no
write is touched — and the run notices at its next check and closes *its own*
entry `failed`. It refuses a cycle that is not `running` (nothing is left to
obey it, and the stamp would name a run that never saw it), and **asking twice
is a no-op that keeps the first asker**: a switch that raised because the run
was already stopping would make a human hitting it twice doubt whether it
worked, which is the one moment that must not be ambiguous. It does not roll
back — stopping and undoing are two decisions, and a switch that also reverted
would make "stop, look at what it did, then decide" impossible. Building it on
`abandon_cycle` would erase the distinction the journal exists to keep: a
repair closes somebody else's dead process from outside, an instruction is
obeyed by a live run. `stop_requested(cycle_id, *, principal) -> bool` is the
read the runner obeys, and it is **deliberately not human-only** unlike
`get_cycle`/`list_cycles`: those are, because a journal entry says what the
gardener did across every space in the file, while this is one boolean about a
run that discloses no territory — and a runner that cannot ask whether it was
told to stop cannot obey. What bounds it instead is the rule that would admit
the caller to run this cycle's territory (`_may_watch_a_cycle`): a scoped cycle
asks for the grant that resolves its scope, which is the check
`consolidate._require_gardener_scope` already makes before the run starts, and
an unscoped one asks what `open_cycle` asks of an unscoped cycle — `edit`
somewhere, since no grant confers the whole file.

**Caller-relative, not run-relative, and the difference is a schema gap rather
than a wording choice**: `cycles` records `triggered_by`, who *asked*, and has
no column at all for who is *running*, so "exactly what admitted this run" is
not a question the row can answer. The width that buys is real and named rather
than papered over — any agent holding *any* grant on space S now reads the
switch on every cycle ever scoped to S, including a human's and another
agent's. Concretely: `create_agent`'s own minimum grant set, `read` on `meta`,
watches every `meta`-scoped cycle, and the parity pair `0010` backfilled onto
the agents that predate it (`meta: read`, `main: suggest`) watches every cycle
over `main` too. `require_review` refused both, since neither level reaches
`edit`. It is one boolean per cycle id, no surface hands an agent a cycle id,
and stating a narrower rule than the code enforces is how a later reader grants
themselves the narrower one. It asked `require_review` over
`_cycle_authority_spaces` for both, on the argument that *obeying a stop is
closing the cycle, so a principal that could not close this one has no use for
the answer*, and **that argument is unsound on both triggers, in two different
ways**. On a `manual` run the gardener never closes the cycle at all:
`_run_cycle` closes as the *opener* and `_opener` resolves a human-triggered
run's opener to the human, so the check demanded of the gardener an authority
the gardener does not exercise. On a `scheduled` run the gardener **is** the
opener and does close its own cycle — but `open_cycle` had then already
required `edit` on the scope before the run could start, so the check was
re-asking a question the door had answered `yes`, and could refuse nothing a
scheduled run would ever meet. Either way it only ever bit where it was wrong:
a gardener holding `read` on a space is entitled to consolidate it and was then
refused the switch over its own run — a night dying at its first provider call
on `GrantNotPermitted`, which is a kill switch killing the run by being
unreadable. A check on the far side of a door must not be stricter than the
door. Nothing caches it — a check answering from a value read at the top of the
run would be a switch that cannot be hit after the run starts, which is the
only time anyone hits one. The stamps outlive the run, so the journal goes on
saying who stopped that night. Neither verb is on MCP (`HUMAN_ONLY_TOOLS`).

The stamp itself is `in_cycle`, a `ContextVar` that `_emit` reads, so a cycle's
writes go through the *ordinary* public functions and are stamped without any
call site naming a cycle — a per-task variable rather than a module global, so
the HTTP server handling a normal request while a cycle runs cannot be stamped
by it, and it is reset in a `finally` because a leaked id would make ordinary
later edits un-undoable on a graph whose only route back is rolling back a
cycle they were never part of.

## 2026-08-04 — restoring `edit` on `meta` was rejected

Migration `0016` adds the write seam's other half: the **conventions space** —
the gardener's own workspace (§L2), where convention notes are ordinary `note`
nodes written by the cycle, with the gardener holding `edit` on it **alone**, as
an ordinary revocable grant row — and the **annotations table** (§L1): one row
per queue item saying what a proposer's acceptance signal judged and at what
rate, an **exclusive arc** (three typed nullable `ON DELETE CASCADE` target
columns, a CHECK that exactly one is non-null) with **no direct read surface**
— written only through `service.annotate` (the learned-curation cycle's writer,
gated like a review by `Store.require_review`, resolving the target through the
principal's read scope so it is no existence oracle, and replacing rather than
accumulating per target: the partial unique indexes hold one annotation per
item, and a later cycle's annotation supersedes the earlier one), read only by
`list_proposals`, which attaches it to a `ProposalOut` the store has already
grant-filtered. Restoring `edit` on `meta` was rejected because 5a's live pass
proved it buys renaming `main` and archiving the `note` type, after which a
human cannot write a note.

## 2026-08-04 — `create_node` grew a `space` parameter because the SDK discards what it does not declare

The MCP SDK discards a keyword this module does not declare instead of refusing
it: `create_node` had no `space` parameter while the ingestion tools and
`request_upload_url` did, so an agent asking for `research` got a 200-shaped
response describing a node in `main` — no way to choose a space and no way to
learn it had not got one. `create_node` now takes `space` (a space id or name,
`main` by default, narrowed by the grant set like every other space reference,
and refused in the non-oracle's identical words when the agent holds nothing on
it). Making the *extra key* an error instead was the other option and is not
reachable without mutating the SDK's `ArgModelBase.model_config`, a third-party
base class every generated tool model inherits — and an agent that cannot name
a space is not helped by being told its spelling was wrong. Every write result
carries the `space_id` it actually landed in, which is the checkable half of
the same rule.

## 2026-08-04 — the third absence list closed a real hole

The MCP absence lists started as two — the review tools (`accept`, `reject` —
`REVIEW_TOOLS`, gated by `Store.require_review` — a human, or `edit` on the
item's space — but not an agent-surface tool) and the curative tools
(`merge_nodes`, `retype`, `supersede_edge`, `bulk_relink`, `consolidate` —
`CURATIVE_TOOLS`, §8.2). The third is newer, and it closed a real hole:
`rollback_cycle` — the most destructive operation in the system, writing
recorded payloads back verbatim across spaces for a whole cycle at once — was
in **no** absence list at all, and neither were `undo`, `abandon_cycle` or the
two journal reads, so the disjointness assertions would have watched a future
tool expose any of them without a word. Reversal is human-only because no grant
delegates writing `state = 'active'` back; the journal is human-only because an
entry says what the gardener did across every space in the file, which is
territory an agent holds no grant on. `UNREGISTERED_TOOLS` is the union
(`REVIEW_TOOLS ∪ CURATIVE_TOOLS ∪ HUMAN_ONLY_TOOLS`), and what
`tests/test_mcp_server.py` asserts the registry stays disjoint from; adding an
operation to any of those tiers means adding its name to a list, never to the
registry. Phase 5a built the whole curative tier and **left the MCP surface
exactly as it was** — that absence is now a decision about a surface that
exists, not a description of code that does not. It stays a decision: an agent
reaching this tier could merge two nodes or rewrite five hundred edges from one
call, and the only thing that takes those back is a human's rollback.

## 2026-08-04 — the `OSError` exemption tuple was wrong twice

`_failure_message` rewrites an `OSError` as `storage error: <strerror>` so the
HTTP surface never prints the operator's database path to a stranger — but four
of the package's own exceptions sit in the `OSError` subtree, because
`PermissionError` derives from it: `auth.InvalidCredentials` → 401,
`auth.PrincipalDisabled` → **403** (reached for real when a capability outlives
the account that minted it), `store.GrantNotPermitted` → **403** (reached for
real by `POST /api/cycles`, since the runner writes as the *gardener* and `0014`
grants it `main` and `meta` alone), and `auth.LoginLocked` → **429** (M5: a name
refused by the failed-login lockout). The exemption used to be a literal tuple,
and it was wrong twice. `PrincipalDisabled` was added when a live pass caught
`storage error: PrincipalDisabled` in a browser; `GrantNotPermitted` was
missed, so the gardener's "the gardener holds no grant on space 'research' …
Run: `nodum grant builtin-gardener research edit`" — a sentence written
specifically for the one click that produces it — reached the journal's toast
as `GrantNotPermitted: storage error: GrantNotPermitted`, with the space and
the remedy both gone. Two misses out of three is the *list* failing, so the
rule is inverted: `_is_domain_failure` asks whether the class was defined in
this package, and only an `OSError` from somewhere else is rewritten. A domain
exception added tomorrow is exempt the day it is written.
`test_every_exception_cli_run_catches_is_mapped` reads `cli._run`'s own except
clauses and asserts the claim instead of restating it, and
`test_no_exception_this_package_defines_is_rewritten_as_a_storage_failure`
**walks the package** for exception classes rather than listing them, so the
fourth `PermissionError` subclass is audited before anyone notices it exists —
it must render its own message *and* carry a status row that is not the
`OSError` 500.

## 2026-08-04 — the read-heavy handlers moved off the event loop

The handlers that do not call the service inline are the read-heavy routes and
the blocking writes — `GET /api/search` (both branches; one 400-term query
measured holding the loop **126 ms**), `POST /api/ask` and `POST /api/summarize`
(model calls), `POST /api/assets` and `PUT /api/uploads/{token}` (registration
streams up to a 1 GB blob), `POST /api/ingest` (a fetch, then
register/extract/describe — one 20.8 MB PDF measured **20.8 s and 680 MB of
RSS** holding the loop), the rendition route (Pillow decode, or pypdfium2
rasterisation on a miss), the download route's spool, and `POST /api/cycles` —
all through `run_in_threadpool`. Every other handler here is a read of a row or
a single-row write, where inline is right; a cycle is every job over every node
in scope (**3.75 s measured on 450 nodes with no embeddings, minutes with
them**) and the event loop is single-threaded, so inline it froze `/healthz`,
the SPA and every other tab for the length of the run — `nodum.scheduler`'s own
docstring had made exactly this argument for the nightly half. What is handed
to the thread is `_write`, so the principal is still bound in the one place
this module binds one. **It frees the loop and not the database, and the
difference was measured rather than assumed**: with the cycle on a worker
thread a live pass had `/healthz` and the SPA answering throughout, while a
concurrent `GET /api/nodes` against the same file took **1168 ms** where it
takes **5 ms** on an idle server. SQLite has one writer, the cycle holds it in
bursts, and a reader is behind it for as long as a burst lasts — so the honest
claim is "the server keeps answering", never "a cycle costs other requests
nothing". Do not describe this change as making a cycle free; the thread moved
a total freeze to a slow read, which is the whole of what it bought.

## 2026-08-02 — the cosine bars are measured, and the measurement set both

The embedding cosine bars stand at **0.93** (duplicate) and **0.60**
(`relates_to`), chosen from `scripts/measure_kasten_calibration.py`'s tables
over a real corpus, measured 2026-08-02 on 426 kasten prose notes (`note/` +
`literature/`, frontmatter and wikilinks stripped), sampled 200, scored for
volume and precision rather than for separation — the fixture-derived
0.72/0.38 pair separated `tests/fixtures/embedding_calibration.json`'s bands
cleanly and still failed on real content, because a set written to demonstrate
a separation cannot measure a false-positive rate. At 0.80 the link bar
measured **dead**: 0.04 `relates_to` per node, the gate's "5 at 0.80"
reproduced. The reverted 0.38 measured as a **flood**: 5.9–6.4 per node, the
gate's 1 175/200 reproduced. At 0.60 it fires at **~1.1–1.2 `relates_to` per
node with ~6–10 % precision** against the vault's own wikilinks as ground truth
— the precision swings with which linked pairs land in the 200-note sample, and
it under-counts, since the 0.907 'Software architecture for developers' ↔ 'A
Philosophy of Software Design' pair is clearly same-area and not wikilinked —
and the above-bar pairs are genuinely same-area by inspection. The duplicate
bar cannot do better on this content: at calibration time real duplicate
candidates — the same-normalised-title pairs — scored 0.28–0.55, overlapping
the related band completely, so only exact copies reach 0.93 and the
title-normalisation signal is the real duplicate detector (the script now
prints that band over the whole corpus, and the honest table is how the claim
is re-verified). What the cosine pair still cannot express — a near-duplicate
worded differently clears the link bar and arrives as `relates_to`, and the
queue cannot tell "not merely related" from "not a duplicate" — is the
learned-curation cycle's job (§L1 annotations), not a bar. The vault is a live
corpus, so the numbers above drift (423 prose notes today): the committed
script reproduces the method, and re-running it is how a drift is detected and
a re-tuning starts — a re-run is required after any change of model, of
fastembed's pooling, or of `CHUNK_WORDS`.

## 2026-08-04 — `_cycle_stop_problems` was checking half of `0015`'s guarantee

`0015` records a stop as two nullable stamps *and one cross-column CHECK*, and
the constraint is the whole reason the pair is honest — without it a file can
hold a time with no requester or a requester with no time, which is precisely
the state the migration chose two columns over a boolean to make unstorable,
and the earlier three-column cut a drifted file comes from is the one that
leaned on the boolean instead. `PRAGMA table_info` cannot see a constraint, so
the check reads the stored schema and looks for `CYCLE_STOP_CHECK_NAME` in it —
**word for word, not whitespace for whitespace** (`_CYCLE_STOP_CHECK_NAME_RE`).
By name is a deliberate narrowing and it stays one: an unnamed CHECK enforcing
the same rule is still reported, because SQLite prints the name verbatim as
`CHECK constraint failed: <name>` and that sentence is half of what `0015`
guarantees. What the narrowing must not do is refuse a file whose owner *did*
run the repair, and it did: the name is 45 characters of prose with spaces in
it, the refusal ships through rich, and a terminal narrower than the line broke
the quoted identifier across two lines — **60 of the 141 widths between 60 and
200**, including every one from 61 to 79. That paste ran without error, kept
every row and installed a **working** constraint under a name spelled with a
newline, and `init` went on refusing with the identical message forever. Width
80 is safe and a non-tty pipe reports 80, which is why nothing in CI could see
it. Both halves are fixed: every statement a refusal prints is written
pre-wrapped to `db._SQL_WIDTH` (58 columns, narrower than any terminal anyone
runs, so nothing re-wraps it), and the search accepts a name a renderer already
broke, because those databases exist either way.

**Its repairs are its own and they are different repairs**: a missing column is
added by the migration's own `ALTER`, which carries the CHECK with it — so a
missing constraint is only reported once both columns are there, and no file is
ever handed both cures at once — while putting a constraint under a column that
already exists is the one thing `ALTER TABLE` cannot do, making that repair the
documented create-copy-drop-rename rebuild (`CYCLE_STOP_CHECK_REBUILD_SQL`),
which carries every row across and recreates both indexes. Neither is "delete
the database file and re-run `nodum init`".

## 2026-08-04 — the rebuild carries no statement that can lose a row

The `cycles` rebuild used to be `DROP TABLE cycles` with nothing but the
transaction between a failed copy and an empty journal — and a transaction is a
guarantee about *one* execution model. `executescript` abandons the script at
the first error; an interactive console, a database GUI and a notebook cell
report the error and read the next statement, and on a file holding a
half-stop they ran the `DROP`, the `RENAME` and the `COMMIT` after the copy had
already failed. Four cycles to none, `events` left pointing at cycles that no
longer existed, `init_db` returning `[]` afterwards so nothing ever noticed.
Nothing weaker than "destroy nothing" reaches that property: SQLite has no
conditional DDL and no `RAISE` outside a trigger, so no statement in a pasted
script can stop the one after it from running. So the first thing the rebuild
does is copy every row into `db.CYCLES_PARKED_TABLE` (`cycles_before_repair`),
a table with no constraints of its own to fail on, and every destructive
statement after that is destroying a copy. The park is **left behind on
purpose**, including when the repair works perfectly: `_cycle_stop_problems`
reports it and prints the one statement that removes it, so a file does not
pass `init` until a human has been told it is there — which is what turns "the
rebuild stopped in the middle" from an empty table that reads as an empty
journal into a refusal that names the stranded rows and the two statements that
put them back.

**And it refuses to print the rebuild at all in the three states where running
it would fail.** A file already holding a row the CHECK forbids gets the row
ids and `CYCLE_STOP_CLEAR_HALF_STOP_SQL` instead — `CYCLE_STOP_HALF_STOP_SQL`
is a query, the check can run it, and knowing in advance beats
`_CREATE_THE_CYCLES_INDEX`'s "if it fails, …" idiom, which was the shape used
here before. A `cycles` carrying a column `CYCLES_COLUMNS` does not list gets
the column named rather than a rebuild that would drop it *with its data* — the
pin on that list compares it against a freshly migrated database, which is
precisely the one schema the repair never runs against. A `cycles` missing a
column the copy selects gets the same treatment. And `DROP TABLE IF EXISTS
cycles_rebuilt` leads the script because a failed attempt a human then commits —
which is exactly what the "clear the half-stops, then re-run" advice asks them
to do — used to leave that scratch table behind and wedge every later attempt
on `table cycles_rebuilt already exists`, with no next step named.

## 2026-08-04 — the scoped-cycle grant check and the interrupt guard

A scoped cycle checks the gardener's own grant right after `open_cycle` and
raises `GrantNotPermitted` naming `nodum grant builtin-gardener <space> edit` —
every space created after `0014` is invisible to the gardener, so the first
click on the UI's scope picker used to reach `list_nodes(space=…,
principal=gardener)` and fail with the Q13 non-oracle `unknown space: <32-hex
id>`: the right sentence for a caller who lacks the grant, and the wrong one
when the caller can see the space in a picker and it is the *gardener* that
cannot — and it landed in a permanent journal row the dream journal splices
into an entry's headline. The check runs **after** `open_cycle` on purpose, so
a scope the *caller* cannot see is still refused by the non-oracle rule first,
and the message echoes the reference the caller supplied so a name is never
answered with a raw id.

**The guard catches `BaseException`**: Ctrl-C during `nodum consolidate` raises
`KeyboardInterrupt`, which is not an `Exception` and used to escape with the
cycle row still `running` — and a `running` cycle cannot be rolled back while
`undo` refuses every event it stamped, so its writes were irreversible on every
surface. The per-job handler stays `Exception` deliberately: one job falling
over must not lose the others, but an interrupt is a request to stop the run.
Finally, the gardener's principal is minted **once per run**, so **a revoked
grant bites at the next cycle, not mid-flight** — the same window
`disable_agent` documents for the MCP server's process-lifetime principal,
stated in the module docstring because the archive dialog promises an agent can
do nothing the moment a space is archived.

## 2026-08-04 — the schedule ran twice on the autumn fall-back

`NODUM_CONSOLIDATE_AT` is a *wall clock* time and a wall clock does not advance
uniformly, so `seconds_until` does its arithmetic in **aware local time**
(`datetime.astimezone`, which reads a naive value as local and attaches the
offset in force at that instant). Subtracting two naive datetimes measures the
calendar rather than elapsed time: driven over a real `Europe/Paris` timeline
that ran the schedule **twice on the autumn fall-back** (waking an hour early,
then again at the hour it was asked for) and **an hour late on the
spring-forward**. Neither crashed and neither overlapped — the loop is
sequential — but the property is the property. The two pathological wall-clock
times are answered rather than special-cased: one that occurs twice resolves to
its first occurrence, one that does not occur at all resolves an hour later,
and each runs exactly once on the right date.

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
of it.

## 2026-08-04 — the model cache must not default to a temp directory

The embedding cache is nodum's, and `cache_dir` is always passed explicitly.
`DEFAULT_CACHE_PATH` is `~/.local/share/nodum/models` — beside the database and
resolved the same way — overridable with `NODUM_EMBED_CACHE`. Leaving the
argument out is not a neutral default: fastembed falls back to
`<tempdir>/fastembed_cache`, and a system whose `systemd-tmpfiles` entry for
`/tmp` is type `D` empties it **on boot**, deleting the **241 MB** download.
After that `get_provider()` returns `None`, the vector signal drops out of
search *and* consolidation with no error, and nothing re-fetches it because the
download is deliberately gated — it stays degraded until a human notices.
`~/.local/share` is spelled out rather than read from `XDG_DATA_HOME` because
`db.py` spells it out too; honouring the variable in one place only would split
the graph and the model that indexes it across different roots.
`unavailable_reason()` distinguishes a cache that was **never populated** from
one that held files and no longer does, because the second means something
deleted them and needs a different response.

**One definition of a node's chunks, one reduction to a node vector.**
`node_chunks(node)` (= `chunk_text(node_text(node))`) is the single spelling of
how a node is split, and both consumers start there: the `vec` projector stores
one vector per chunk because search retrieves the *best chunk*, while
`node_vectors(provider, nodes)` reduces the same chunks to the one vector a
pairwise comparison needs — the **L2-normalised mean** of the chunk vectors,
which is the model's own mean pooling applied one level up, and the identity up
to scale for a node that fits one window. The node-level vector is therefore a
pure function of the projector's rows rather than a second, independently
produced embedding. The consolidation cycle used to embed each node's whole
text in one call: it chunked nothing, so a node past the model's 512-token
window was silently truncated and compared on its opening pages, and the same
node had one vector there and a different set in the projector. A node with no
text at all reduces to the zero vector, which is at cosine 0.0 from everything
— two empty nodes are not each other's duplicates.

## 2026-08-04 — a base URL `urllib` cannot POST to is no provider with a reason

`urllib.request.Request` parses the URL **in its constructor**, so
`api.deepseek.com/v1` is `ValueError: unknown url type` and `http://[bad/v1` is
`ValueError: Invalid IPv6 URL` — both raised *outside* `_post`'s `try` and so
escaped `LLMError` entirely, exactly as `IncompleteRead` once did, reaching a
CLI traceback and the 400 kept for a malformed request. The construction moved
inside the `try` **and** the same check runs at resolve time (`base_url_problem`).
A missing scheme is **never guessed back in**: choosing `http` or `https`
decides whether the API key crosses the network in clear text, and that is not
a decision to make from four characters that are not there. A **wrong** scheme
is refused the same way, like a scheme-less one: urllib accepts
`localhost:11434/v1` (urlsplit reads `localhost` as a scheme) and the
registered `ftp`/`file` types, so once the constructor succeeds the parsed
scheme must still be `http` or `https` — anything else is no provider with a
reason at resolution. `profile_for`'s `_hostname` stays forgiving, deliberately
— it reads a host out of a spelling nothing can POST to so the refusal can name
the endpoint.

## 2026-08-04 — a model name may not move a credential

`_post` attached `NODUM_LLM_API_KEY` unconditionally, so a hosted model id
nobody profiled (a typo, a newer id) resolved to `DEFAULT_BASE_URL` and sent
the vendor's bearer token to `http://localhost:11434/v1` — driven end to end,
the prompt correctly stayed local and the credential arrived at a throwaway
listener as `Authorization: Bearer …`. The rule: **the key travels only to an
endpoint somebody named**, either the operator through `NODUM_LLM_BASE_URL` or
the model id through an exact profile match. Otherwise it is dropped at
resolution time — the provider never holds it — and `key_withheld_reason()`
says so in `nodum llm status` (`api_key_withheld`). Withheld rather than
refused, because the local default needs no key and a stale variable must not
break a working install; and a **self-hosted gateway that requires a key keeps
it**, since naming it in `NODUM_LLM_BASE_URL` is the operator saying the key
belongs there. "No key to localhost" would have been the wrong rule for exactly
that reason.

## 2026-08-04 — capability negotiation, not retry

Two things about the "one wire" are not universal, and each was found by a live
400 rather than by reading a spec. `response_format: {"type": "json_schema"}`
— ollama honours it, `deepseek-v4-flash` answers `HTTP 400 "This
response_format type is unavailable now"` and takes `{"type": "json_object"}`.
And `reasoning_effort` — DeepSeek takes a graded level, **ollama takes only
`none`**: `low` is `HTTP 400 "llama3.2:1b" does not support thinking` on a
model without thinking and `HTTP 400 think value "low" is not supported for
this model` on `qwen3:8b`, which *does* think. A graded level sent
unconditionally is therefore a 400 on every ollama call, i.e. the whole local
half. Both are handled by **capability negotiation, not retry**: the request
carries the strongest form the provider still believes in, a **400** whose body
names the field downgrades that belief *on the instance*, and the call is
re-sent once under the weaker form, inside the **same** per-call timeout.
Bounded at three downgrades; only a 400 counts (a 5xx or a timeout says nothing
about capabilities and would otherwise cripple a healthy provider
permanently); matching is on the server's sentence, which is brittle
**one-sidedly** — a false positive is a weaker request every endpoint accepts,
named in `llm status`, and a false negative is exactly today's
`ProviderUnavailable`.

The **one sharpening** of that matcher is reason words, not the punctuation
that used to stand in for them: `Invalid schema for` means the server *parsed*
`response_format` and is validating what is inside it, which proves it serves
the field and that the fault is nodum's own schema — never downgrade there.
`is unavailable` / `not supported` naming `response_format` mean a genuine
capability rejection, which does downgrade; a dotted or bracketed field path
alone decides neither way. A message naming both a schema fault and a rejection
word is read as a validation error — the conservative side, where a false
negative is exactly today's `ProviderUnavailable` and a false positive weakens
every request for the life of the process. `_THINKING_REJECTIONS` deliberately
keeps plain substring matching, because two of its three markers are sentences
a server says (`does not support thinking`) rather than field names, and the
guard would break them over a full stop.

The reasoning field is a **three-rung ladder**, not a boolean: `graded` →
`off-only` → `absent`. The third rung is not decoration — a server that rejects
unknown fields refuses `reasoning_effort: "none"` too, and a two-state version
sent `none` unconditionally and left such a server permanently unusable. That
was found by making `llm status` drive a real 400, not by reasoning about it.
Rungs are dropped **one at a time**, because "a graded level was refused" is
not evidence that `none` was, and `none` is the documented cure for `qwen3:8b`
returning an empty body.

## 2026-08-04 — `json_object` is a real reduction in what a caller may assume, so it is never silent

Under `json_schema` the server's constrained decoding makes a string the schema
forbids *unrepresentable* — which is what `nodum.answers`' citation `pattern`
rests on. Under `json_object` the schema is a **sentence in the prompt** and
every constraint inside it is advice. 5b-i already recorded that *schema
validity was never truth*; this is weaker still. So the mode is on
`Completion.structured_mode`, on `AgentRun.structured_mode`, in `LLMReport`, and
in `nodum llm status`'s `structured_output`. The injected instruction's **word
"json" is load-bearing**: the endpoint refuses `json_object` outright with
`HTTP 400 "Prompt must contain the word 'json' in some form"` unless the prompt
says it. It is inserted **after the caller's leading system messages** and the
schema is rendered with sorted keys, both for prefix caching. And
`estimate_prompt_tokens` takes the schema, because under the fallback the
schema costs prompt tokens and an estimate that excluded them would under-count
— the one failure it may not have.

## 2026-08-04 — `NODUM_LLM_THINKING` levels are validated, and `high` is the cheapest of the graded three

`NODUM_LLM_THINKING` is `none`/`low`/`medium`/`high`, default `high`, and a
value outside that set is refused — no provider, with a reason naming the set,
exactly as an unparseable `NODUM_LLM_CONTEXT_TOKENS` already is. This
deliberately parts company with `nodum.agent`'s "an unparseable value falls
back" rule: that rule is right for a *number*, where the fallback is a smaller
ceiling and less work. A level is a **name**, and the live API validates the
enum strictly, so a value passed through either 400s the request or runs under
a setting nobody chose while the report names the one that was asked for.

**The level name does not predict what a call costs, and `high` is the cheapest
of the graded three.** Measured on two five-note synthesis fixtures, two
samples each: `low` spent 743/905 and 2 177/797 reasoning tokens where `high`
spent 349/101 and 60/110 — eight points, both fixtures, same direction — and
`high` was also the fastest (5.4 s against `low`'s 26.9 s). On a one-word
prompt, `low` 639 against `high` 47. So **nothing may size an output ceiling
from the configured level**, and only `none` is predictable (measured exactly
0, every time). The field is **always sent** where the endpoint takes one,
because unset is not neutral: it measured 1 492 reasoning tokens on a fixture
where `none` measured 0. The quality evidence for preferring a graded level is
**thin and recorded as thin** — of two hard fixtures only one discriminated.

## 2026-08-04 — the reasoning level is per call site over a global default

The call sites do not want the same thing. `/ask` and `/summarize` take the
human's level, since deciding whether the retrieved context answers the
question is where a confident wrong answer is the recorded danger. The **query
rewrite is pinned to `none`**: measured over twelve samples at the default it
spent 44–1 174 reasoning tokens — 57 % of its own ceiling, a 26x spread — for
terms that did not change, where `none` was byte-identical per question and
3.4x faster. The **reachability probe is pinned to `none`** for a blunter
reason: at any graded level it returns an **empty body**, every output token
going to thinking. Do not "fix" either inconsistency.

## 2026-08-04 — the output ceiling is per call site too

`/ask` reserves `ASK_OUTPUT_TOKENS` (2 048) rather than the blanket 4 096:
measured on the real `ASK_TEMPLATE` over **24 samples** — `deepseek-v4-flash`
at `high` (8, output 60–343), the same at `low` (12, 67–**528**, reasoning up
to 442) and `qwen3:8b` on ollama with the level withheld (4, 26–76) — the worst
output was 528, and 2 048 is 3.9x that. It is not sized closer, for two
reasons: 2 048 is the number recorded below as the cure for `qwen3:8b`'s empty
body, and on ollama's 4 096-token window it is **exactly what `/ask` already
got**, since `OUTPUT_RESERVATION_FRACTION` clamps the blanket ceiling there
anyway. So it can regress no local install, and what it buys is elsewhere: on a
wider window the prompt gains the difference, and on DeepSeek it halves what
each `/ask` charges the 8 000-token request budget. **The "halved prompt room"
on a 4 096-token window is the fraction's doing, not the ceiling's** — no
per-call number above 2 048 can change it. `/summarize` keeps the blanket
ceiling, deliberately: that number was sized against a *synthesis* worst case
and this is the synthesis-shaped call, so a constant here would be a copy free
to drift, and an operator who raises `NODUM_LLM_MAX_OUTPUT_TOKENS` for a
six-sentence summary should get it.

## 2026-08-04 — a call is charged the provider's own two numbers, and neither used to be

`AgentRun.chat` estimated the prompt **without the schema it was about to send**
— under `json_object` the schema is stated as a system message and costs **330
prompt tokens for `ASK_SCHEMA`**, measured — so at
`NODUM_LLM_CONTEXT_TOKENS=8192` `answers._fit_prompt` sized a prompt at 4 068
against a 4 096 ceiling and `chat` refused the same prompt at 4 398: `/ask`
failing on a prompt its own fitter had just built to fit. And it charged the
**unclamped** ceiling rather than `output_reservation(ceiling)`, which is what
will really be sent as `max_tokens` — 4 096 against 2 048 on a 4 096-token
window, so `BudgetExhausted` could fire up to 2 048 tokens per call early.
`_fit_prompt` now takes both the schema and the call site's ceiling, with no
defaults, so a new call site cannot forget either.

## 2026-08-04 — a `usage` block that contradicts itself is clamped and logged, never believed

`reasoning_tokens` is documented as a *share* of `completion_tokens` and
nothing enforced it: a wire reporting 5 000 inside 50 makes `content_tokens` 0
and prints a report where the thinking is larger than the output it is part of.
No budget moves (reasoning is never summed into the spend), so it is clamped.
And `usage.total_tokens` was never read at all — overriding it is right, since
`Completion.total_tokens` is what budgets are denominated in, but a provider
disagreeing about the bill should be noticed. Both go to
`logging.getLogger("nodum.llm")` at WARNING, the only thing in that module that
is logged rather than raised or returned: they are facts about the provider,
actionable by whoever operates it and useless to the caller who wanted an
answer.

## 2026-08-04 — a default belongs on a field whose zero means *unknown* — and nowhere else

In `LLMReport` and `JobCost`, `reasoning_tokens` and the cache counters keep
their defaults (absent from every ollama response, and from DeepSeek's own at
`reasoning_effort: "none"`). `elapsed_seconds`, `exhausted`, `stopped`,
`stop_switch` and `Generation.latency_ms` are **required**: their zero values
are assertions, not absences, and `STOP_SWITCH_NONE` is the worst of them — an
affirmative claim ("this run is not a cycle and has no stop row") that a future
construction site omitting the field on a *cycle* run would file as the
opposite of the truth. Pydantic imposes no defaults-after-defaults ordering, so
nothing ever forced them.

## 2026-08-04 — the output reservation is a share of the window, not a flat subtraction

`OUTPUT_RESERVATION_FRACTION` (0.5 — the rule `answers._rewrite_ceiling`
already used) reserves a share of the window, not an absolute. The window holds
prompt *and* answer on a server that shares one KV cache, so the reservation is
load-bearing on ollama; it stopped being expressible as an absolute once the
ceiling was sized for a reasoning model, because 4 096 out of a 4 096-token
window leaves the prompt nothing and the same 4 096 out of 1 000 000 is a
rounding error. **The clamped number is also what is sent as `max_tokens`** —
reserving less than the server is told it may generate is the overrun the
reservation exists to stop. Consequence for the local half: `/ask` on ollama
now fits its prompt into 2 048 tokens rather than 3 584. There is exactly
**one** place this arithmetic lives (`AgentRun.output_reservation`);
`answers._fit_prompt` calls it rather than recomputing, because a second copy
disagreed the moment the default rose and refused every question as "too long
for the window".

## 2026-08-04 — out of the box means a model name and a key

`profile_for` matches an **exact model id** against a tiny table of endpoints
this ships knowing about, supplying the base URL, the served window, the
structured mode and whether graded thinking is accepted.
`NODUM_LLM_MODEL=deepseek-v4-flash` plus `NODUM_LLM_API_KEY` is therefore a
working install: `https://api.deepseek.com/v1`, a 1 000 000-token window,
`json_object` with no rejected round trip, graded thinking on. Matching on the
model name and not only the URL is what makes that possible — with nothing else
set there *is* no base URL to match. Every field is a **default**: an
explicitly set variable always wins, so a profile decides nothing an operator
has decided. A profile earns its place only by being an endpoint whose defaults
are otherwise wrong, it is an **optimisation and never a gate** (an unprofiled
provider starts optimistic and negotiates down), and the ollama default is
handled by the base URL rather than a row: the local endpoint starts out
disbelieving graded levels instead of paying a 400 to learn it.

## 2026-08-04 — a model name may not move a call to another company's server

The profile match is exact and a set `NODUM_LLM_BASE_URL` takes the *whole*
decision. Both looser rules had teeth. A `deepseek-` **prefix** also matches
`deepseek-r1`, `deepseek-coder`, `deepseek-coder-v2`, `deepseek-llm`,
`deepseek-v2`, `deepseek-v3` and `deepseek-v3.1` — **ollama library models**,
pulled and served locally — so `NODUM_LLM_MODEL=deepseek-r1:8b` with nothing
else set resolved to `https://api.deepseek.com/v1`: a configuration whose only
statement about where to run was "the default endpoint" POSTed private graph
text to a vendor whenever `NODUM_LLM_API_KEY` was also set, and 401'd against a
host nobody configured when it was not. And an explicit
`NODUM_LLM_BASE_URL=http://localhost:11434/v1` won the **URL** while the
profile still supplied `context_tokens`, carrying a 1 000 000-token belief
against a server serving 4 096 — the refusal is computed against that belief,
so nothing was refused and the silent truncation this module is mostly about
came back through the table written to close it. Host matching is a **parsed
hostname** against a set, never `key in url`: a proxy at `deepseek-gw.lan`, and
a lookalike at `api.deepseek.com.evil.example`, are not DeepSeek. The asymmetry
that settles the whole rule: a hosted id guessed wrong costs egress nobody
sees, and a local one guessed wrong costs a 404 the operator reads.

## 2026-08-04 — `prompt_truncated` weighs only the bytes really sent

`estimate_content_tokens` counts the bytes really sent, not the ~52 tokens of
chat-template overhead the *refusal's* estimate adds. On a long prompt the
difference is noise; on a 33-byte one the overhead is 60 % of the estimate, and
the check fired on a completion the server had read whole — measured, `llm
status` reported `failed_calls: 1` on a healthy install, which is the one
command that must not manufacture a failure. Found by driving the live probe,
not by inspection.

## 2026-08-04 — an over-long prompt is refused before the call

Measured: 16 000 characters report 2 932 prompt tokens while 64 000 and 70 000
both report **4 096** — the window filled, the rest was dropped, and nothing in
the response says so. `finish_reason` describes the *output* only, so it reads
`stop` on a prompt truncated from 70 000 characters; every signal there is
lives in `usage`, and it arrives after the call is paid for. So `chat` counts
first and raises `PromptTooLong` without sending.

## 2026-08-04 — the window is the server's, not the model's

`NODUM_LLM_CONTEXT_TOKENS` is the window the *server* serves, not the one the
model has, and getting that backwards is how the hole re-opens from the other
side: ollama applies `num_ctx` (4096 unless `OLLAMA_CONTEXT_LENGTH` raises it)
to every model it serves, while `llama3.2:1b` really has 128 k — so "raise it
for a model that has the room" is advice that produces silent truncation.
Measured at `NODUM_LLM_CONTEXT_TOKENS=32768` against that server: a 30
000-character prompt is **not** refused, `prompt_tokens` comes back 4096,
`finish_reason` is `stop`, and a whole answer is returned from a prefix.

## 2026-08-04 — the after-the-fact defence is two signals, and they see different failures

`Completion.context_filled` compares the report against the *configured* window
and is structurally blind to the case above (4096 is nowhere near 32768) — it
catches the server whose window really is the configured one.
`Completion.prompt_truncated` compares it against the prompt's own bytes: the
pre-send estimate rides back on the completion (`Completion.prompt_estimate`,
recorded by the provider, which is the only thing that has it — a caller
recomputing it would be a second estimator free to disagree with the one that
decided), and a report below `estimate / MAX_BYTES_PER_TOKEN` is a server that
read less than it was sent. The constant is **6**, against a measured worst
case of **4.55 bytes per token** (Arabic; English 4.49, 32-hex ids 1.18), so it
catches a truncation that lost roughly a quarter of an English prompt or more
and **cannot see a narrower one** — there is no per-call signal for that, since
the estimate cannot tell an efficient tokeniser from a server that read less.
The error is one-sided by the same argument the estimate itself rests on: a
false alarm is a visible, itemised refusal, a miss is an answer from a prefix
nobody can tell from a good one. Either signal makes the call a
`ContextOverflow` in `nodum.agent`: charged, body discarded. **The estimate is
UTF-8 bytes**, because a byte-level BPE token decodes to at least one byte and
that makes it a *bound* rather than a heuristic; a `chars/4` estimate
under-counts emoji by twelve times and a run of accented Latin by four, and an
under-count is the one failure this may not have. The price is refusing about
four times too eagerly on English prose, which is the trade an over-refusal
(visible, itemisable) versus an under-refusal (an answer nobody can tell from a
good one) forces.

## 2026-08-04 — a token ceiling gives unparseable JSON, not a short object

Measured `'{\n  "title": "Kafka'` at `finish_reason: "length"` — so a `length`
finish is no result. **A JSON schema fixes the envelope and nothing else**:
asked a question its context could not answer, under a schema, the model
returned `{"answer": "n0", "cited_ids": ["n0"], "answered": false}`. Never read
a schema-valid object as a true one; validating what the model said against
what was actually retrieved is the caller's job.

## 2026-08-04 — `IncompleteRead` had to be added by hand

`response.read()` raises `http.client.IncompleteRead` when a body stops short
of its `Content-Length`, and that class derives from `HTTPException`, **not**
`OSError` — so the clause catching a refused connection missed it, and it
escaped `LLMError`, escaped `answers.ask`'s handler and escaped `cli._run`,
reproducing as a Rich traceback with exit 1 on the CLI and an HTTP 500 on
`POST /api/ask`. It is the shape a killed provider, a proxy timeout and a
dropped load-balancer connection all make, which is to say the ordinary way a
long call dies; `_post` catches `http.client.HTTPException` for it, and a test
drives six death shapes and asserts each lands on `LLMError` rather than on a
message. Resolution reads configuration and makes **no network call** — unlike
the embedding seam, whose construction loads a model — because "configured" and
"reachable" are different facts and a probe would cache one instant's answer
for the process.

## 2026-08-04 — the import rail gained a test per hole

Design Constraint 4 is held structurally: `tests/test_llm.py` walks the
package's own import graph and asserts that `nodum.service`, `nodum.projectors`,
`nodum.store` and `nodum.migrations` cannot reach `nodum.llm` under any
spelling (aliased, relative, `importlib.import_module` / `__import__`
**positionally or by keyword**, an attribute chain) **or any number of hops**,
and that `nodum.agent` is the only module that imports it at all. Two of those
words were claims rather than facts until this wave. The extractor walked
`node.args` only, so `import_module(name="nodum.llm")` — a constant string,
spelled the way an editor's signature help suggests it — was invisible; and the
module glob skipped `__init__.py`, so the `nodum` node had *inbound* edges from
most of the package and no outbound ones at all, which made a one-line
re-export placed there invisible to **both** properties. The rail now carries a
test per hole, and the second one injects the re-export into the real graph
because the real `__init__` correctly has none.

## 2026-08-04 — a reasoning model's thinking is metered beside the spend, never inside it

`usage.completion_tokens_details.reasoning_tokens` is a *share* of
`completion_tokens` — measured, `total_tokens` is `prompt + completion` on
every call and reasoning never exceeds completion — so adding it to the total
would report a night as costing up to twice the bill. Omitting it is the other
failure: a job that spent 1 420 of 1 520 output tokens thinking reads exactly
like one that wrote 1 520 tokens of proposal, and only the first is one sample
from a `length` finish, which here is *no result*. So `reasoning_tokens` is on
`Completion`, `Generation`, `JobCost` and `LLMReport`, beside
`cache_hit_tokens`/`cache_miss_tokens` — the prefix cache is priced ~50x below
a miss, so a token total is not a cost without them. **At `reasoning_effort:
"none"` the whole `completion_tokens_details` block is absent from `usage`**
rather than reporting zero, so those three counters are read leniently while
`prompt_tokens`/`completion_tokens` stay strict: the first three are
decompositions of numbers already read, and losing one loses detail rather
than money. This is **not** `chunks.model_id`'s mechanism (A3): embeddings are
derived and a model change is `projector rebuild vec`, while generated text
cannot be regenerated by replaying the log — so do not later "unify" them into
a `model_id` column on `nodes`. `prompt_version` (A2) is a short hash of the
prompt template, because two cycles a month apart can name the same model and
differ only in the prompt.

## 2026-08-04 — the budgets: per-call ⊂ per-job ⊂ per-cycle, with a wall clock beside them

The budget unit is tokens metered from `usage`, and an **independent
wall-clock ceiling** sits beside it because tokens do not bound a night — **2
395 prompt tokens cost 47 s locally**. Charging a job charges the cycle and the
remainder is the minimum down the chain; a job budget shares the run's clock,
so no ceiling is ever infinite (`cycles.report` is `json.dumps`, which writes a
bare `Infinity` that is not JSON — and that is enforced where the number is
*read*, since `float("inf")`, `"Infinity"` and `"1e999"` all parse:
`_positive_float` requires `math.isfinite`, measured after
`NODUM_LLM_REQUEST_SECONDS=inf` put `"budget_seconds": Infinity` on a 200 from
`POST /api/ask` and made the browser's `JSON.parse` throw. `nan` is refused by
the same line for a quieter reason: every comparison against it is false, so
the wall-clock check silently stops existing).

**The wall clock starts at the first provider call, not at construction** —
`for_cycle` is built when the cycle opens and the LLM jobs run last, so a clock
started in the constructor charged the five deterministic jobs' minutes to the
LLM's ceiling and reported time the model never had. **The per-call timeout is
clamped to what is left of it** (`min(call_timeout, ledger.remaining_seconds)`):
the clock was checked before a call and never again, so a 2 s ceiling with the
shipped 120 s call timeout measured `elapsed 3.0`, and a night could overrun by
two minutes.

**Every share of a budget is measured against the same ceiling, across calls.**
`AgentRun.job` is one `split` per job, so a guard reading only its own argument
let three jobs at `share=0.6` each hold 600 of a 1000-token cycle — 180 % in
`LLMReport.per_job`. Spending stayed bounded (the remainder is the minimum down
the chain); the *report* lied, about the one number a human checks a night
against. `Budget` therefore accumulates the shares it has handed out, and a
**repeated job name is refused** rather than replacing the first —
`AgentRun._jobs` is keyed by name, so the replacement took the displaced job's
calls and tokens out of the report with it.

**The refusal names the variable that funds *this* run** (`AgentRun.budget_env`),
because `for_request` reads the request variable and `NODUM_LLM_REQUEST_BUDGET=0
nodum ask` used to answer with "set `NODUM_LLM_CYCLE_BUDGET`", which does
nothing for a request — and "turn the LLM jobs on" is cycle vocabulary for
something a human asked for by hand. A *funded* run whose job share rounds to 0
gets a third sentence naming the share, since the report says `enabled: true`
and the variable is already set.

## 2026-08-04 — exhaustion is a different report shape from the degraded path

A provider absence is `available: false` + `unavailable_reason` (a stable fact
about this install, and 5a's `notes` vocabulary); exhaustion is `exhausted:
true` + an itemised `skipped` (a fact about *this* run, false again tomorrow) —
`LLMReport` has no `notes` field at all, and a test pins that. `exhausted`
means *a spending ceiling stopped work*, not that a counter reached zero: a
budget with 10 tokens left can afford nothing, and a flag read off the counter
would report a truncated night as a complete one. A budget that was never
turned on is refused as `kind="off"` and is **not** reported exhausted. A
failed call — `length` finish, filled context or a prompt the server truncated
— is charged, because the tokens were really spent, **and counted in
`failed_calls`**, which means *calls that produced no usable result* rather
than *calls that never reached the wire*: three discarded answers reported
`calls 3, failed_calls 0` before, which is a night of three successes. A
`PromptTooLong` costs nothing, because nothing was sent, and is neither a call
nor a failure — but it **records a skip**, because an item was left unexamined
and three refusals used to report `calls 0, failed_calls 0, skipped []`:
byte-identical to a night with no work.

## 2026-08-04 — the gate under the stop switch is gone, and so is `STOP_SWITCH_PENDING`

`0015`'s docstring proposed a `stop_requested INTEGER NOT NULL DEFAULT 0`
beside the two stamps; checked against the table, that is one column too many —
`cycles` writes every fact that arrives *after* the INSERT as a nullable column
whose presence is the flag (`finished_at`, `report`, `rolled_back_by`) and
carries a boolean only for `dry_run`, which is fixed at insert and never
transitions. A flag beside the stamps would be a fourth instance of *state a
later reader has to reconcile with the record next to it*, and `ALTER TABLE`
cannot add the table-level CHECK that would forbid `stop_requested = 1` with no
requester. `CycleOut.stop_requested` is `stop_requested_at IS NOT NULL`,
computed on every read and stored nowhere. The one disagreement two columns can
still have — a requester with no time, or a time with no requester — is closed
by a **cross-column CHECK, which `ADD COLUMN` does accept**, named so SQLite
prints the name as the message. Both columns are pure additions with no
back-fill: every row that predates them is a cycle nobody asked to stop, which
is what two NULLs say.

The gate under it is gone, and so is `STOP_SWITCH_PENDING`. That branch was the
only way to reach the armed path before `0015`, and afterwards it was three
kinds of stale at once: the string named a column (`cycles.stop_requested`) the
migration never created, the gate keyed on the *service function* rather than
on the column, and no build carrying this module could reach it. The field
still has two values, because the distinction it exists for is still real — a
cycle has a row anybody can stamp (`STOP_SWITCH_ARMED`), and a human's request
has none (`STOP_SWITCH_NONE`, `for_request`'s posture, previously reported as a
migration that had already landed). Whether a *database* can store a stop is
the question with two live answers, and it belongs to `db._cycle_stop_problems`
at `init_db` where the answer comes with the statements that repair it — never
to a string in a report written after the write already failed.

## 2026-08-04 — 512 was below the floor; 4 096 is sized for a reasoning model

`NODUM_LLM_MAX_OUTPUT_TOKENS`'s default is now 4 096, sized for a reasoning
model. 512 was measured *below the floor at which anything works*: a ceiling
sweep on one synthesis prompt against `deepseek-v4-flash` was perfectly
bimodal — 300/400/500 gave `length` and an unparseable body every run, 650 and
above parsed every run — so the shipped default made every call on that
provider a B3 failure. 4 096 is not "650 plus margin": the floor is not the
number that matters, because thinking comes out of the same ceiling and
**cannot be predicted from anything configured** (worst cases measured: 2 177
reasoning tokens on a synthesis, 1 174 on a one-line query rewrite, 1 277 total
output for the single word "ping", a 26x spread on repeats). It costs nothing
where it is large — DeepSeek's own max output is 384 000 — and against
ollama's shared 4 096-token window the proportional reservation lands it at
2 048, the number recorded as the measured cure for `qwen3:8b`. The output
ceiling also reached the provider as `ValueError: max_output_tokens must be at
least 1`, which this stack renders as a **400**, the client-error voice, on
every `POST /api/ask` for a request that was perfectly well formed — so the
reader takes a `minimum=`, and the output ceiling passes 1.

## 2026-08-04 — `AgentRun` does not hand out the provider object

`_provider` is private. P3's rail checks *imports*, and a module holding
`run.provider` imports nothing: demonstrated, a call through
`run.provider.chat(...)` succeeded with the budget at 0 and a stop firing, and
the run reported `calls 0, total_tokens 0, stopped True` — unmetered,
unstoppable, unattributed. What a prompt builder actually needs is two numbers,
and the run answers both: `context_tokens` (`None` with no provider, like
`model_id` and `provider_id`) and `estimate_prompt_tokens(messages)` (which
raises `ProviderUnavailable` with none, because there is no honest number). The
estimate must be the provider's own, so that what a caller fits is exactly what
the provider would refuse.

## 2026-08-04 — `answered` is computed, never read from the model

Every id the model cites is resolved against the notes *this request*
retrieved; ids outside that set are dropped into `unresolved`, and **zero
surviving citations means `answered: false` and the answer text is not
returned**. The schema carries no `answered` field at all — the measured
failure is a schema-valid `{"answer": "false", …, "answered": true}`, and a
field nobody may read is a field the next reader wires up.

## 2026-08-04 — citation resolvability is not groundedness, and `answered: true` does not claim otherwise

E2's rule defends against an invented *id*; it says nothing about invented
*content citing a real id*, which is the failure a model actually has. Live:
asked which cloud provider hosts the production Kubernetes cluster,
`llama3.2:1b` answered **AWS**, `answered: true`, citing a 28 100-character
Kafka textbook containing **zero** occurrences of AWS, cloud, Kubernetes, k8s,
Azure, GCP or provider — on a graph that says elsewhere the cluster is k3s on
three on-prem nodes. **The endpoint had the signal that would have caught it
and threw it away**: the model also cited marker `2` when exactly one note had
been offered, which is proof it was not reading the context, and the response
filed that in `unresolved` while standing behind the other citation.

**A citation that resolves beside one that does not, on its own, is not an
answer** (`_ungrounded`). It is deliberately *not* widened to "any unresolved
citation voids the answer": two surviving citations beside one spurious marker
is a different picture — a model that placed two real notes has demonstrably
read them — and voiding it would refuse answers the graph really contains.

**A number the sent text does not contain is not an answer**
(`_unsupported_numbers`). It compares the answer's digit runs against the
**excerpts as sent** plus their titles plus the question, and reports what is
in neither; runs rather than substrings, because `2024` does not contain `24`
and a substring test would read a year as corroboration for a duration. The
prompt's own markers are excluded — they are this module's numbers, not the
graph's, and counting them would make every single-digit claim
self-supporting. Measured: a source saying an escalation deadline is *fourteen
minutes*, of which the model was shown a 1 213-character prefix stopping well
before that sentence, answered "…is 24 hours". A false positive is the trade
taken on purpose — a model rendering *fourteen* as `14` is refused with the
number named — because that is `nodum.llm`'s own rule for its token estimate:
an over-refusal is visible and itemised, an under-refusal is an answer nobody
can tell from a good one.

**Numbers are checked and language is not, and that is a boundary rather than
an oversight.** Deciding whether a sentence is supported by a paragraph is a
judgement, and a judgement is a second model call — 5b-ii's job, not this
wave's. **An answer can pass every check here and be false.** The envelope is
built so a reader can see that for themselves rather than being asked to trust
a boolean, which is also why 5b-i ships **no Ask view in the browser**: a CLI
reader seeing `unresolved`, `truncated_notes` and `dropped` in JSON is far
better equipped to catch a confident wrong answer than a browser reader seeing
prose.

## 2026-08-04 — what may be *sent* is narrower than what may be *read*

`SENDABLE_STATES` narrows what reaches the provider. `service.subgraph` filters
*edges* by state and never filters nodes at all, so the walk hands `/summarize`
archived, proposed and meta-space nodes — and it used to put every one of them
in front of the provider while `/ask`, which searches `state="active",
include_meta=False`, could not reach any of them at any `k`. Neither is a grant
violation: the caller is a human who may read all of it. What was wrong is that
**two endpoints on one install disagreed about what leaves the machine**, and
only one of them agreed with what a human means by archiving a note —
"circulation" has to include the one path that puts a note's text on somebody
else's machine. They are named in `withheld` rather than being silently absent.

## 2026-08-04 — a bound that is not reported is a lie the caller cannot detect

This one went unreported for the *ordinary* case rather than the edge case —
Phase 4's whole output is `source` nodes carrying whole documents, and
`MAX_CONTEXT_CHARS` is 1 200. Measured: a **6 832-character source whose
answer sat at character 3 433 was sent as 1 213 characters that did not contain
it**, and `/ask` returned `answered: true`, a confabulated number, that node in
`considered`, an empty `dropped` and no `refusal` — a wrong answer inside a
clean provenance envelope, produced by the module's own bound and not by any
attacker or any weak retrieval. `/summarize` was worse the same way: it narrows
to `MIN_CONTEXT_CHARS` (240) and still reported `truncated: false`, because the
only `truncated` it had belonged to the subgraph *walk*.

So **four lists say four different things and none is a synonym for another**:
`considered` is what reached the model, `truncated_notes` is what reached it
**in part**, `dropped` is what the window refused outright, and `withheld`
(summarize only) is what this module declined to send. Every `Citation` carries
`truncated` too, because without it a citation says "the answer came from this
note" and means "the answer came from *some prefix of* this note" — a human who
opens the node and finds the sentence there has confirmed nothing, since the
model may never have been shown that line. **`considered` is empty on every
path where no call was made**, and `used.calls` is its corroboration: listing
node ids beside `calls: 0` said notes reached a model that was never called.

## 2026-08-04 — three prompt findings, measured on `llama3.2:1b` and each pinned by a test

The first version scored **1/6** on a six-question battery and every failure
was an unparseable citation (`"]"`, `"/1"`, `"space main"`, a chat template's
`<|start_header_id|>`) — the validation working perfectly and the endpoint
useless. Three changes took it to **6/6**: the citation format is a **`pattern`
on the schema** (enforced by the server's constrained decoding — verified
against ollama — so bad strings are unrepresentable rather than discouraged); a
note is identified by a **small integer and nothing else**, because with the
32-hex node id printed beside the marker the model cited `"116"` and `"749"`,
mining the id for digits; and the instructions contain **no number the model
can copy**, because a worked example (`write exactly: ["1", "3"]`) came back as
`"3"` on every call — it scored *better* that way (5/6) and was still wrong,
since on a graph returning three hits that copied number resolves to a real
note the answer never came from. The general rule: **every number in the prompt
is a candidate citation.**

## 2026-08-04 — the marker boundary twin: `[n]` at the start of a line

`_context_block` renders `[n] title` followed by the note's text, so a `source`
node — the shape `ingest url` produces — carrying the line `[1] Retention
window` opened a second note inside another note's body. Measured against both
local models: two honest notes saying a retention window is thirty days plus
one forged correction, and **both answered 9999**. `qwen3:8b` was the worse of
the two — it repeated the forged claim while citing **only the honest notes**,
so a human auditing the citations opens *Retention window*, reads "thirty
days", and the answer said otherwise. The defusing keeps the digits and the
line's width (`[12]` → `(12)`), so the note reads the same to a human and the
excerpt bound above it is unchanged, and it fires only on a line that would
otherwise have opened a note.

**The rule is about the prompt, not about the graph, so it holds at both ends
of the template.** `ASK_TEMPLATE` prints the question *underneath* the notes,
so a question carrying a line `[3] …` opened one more note than the retrieval
offered — measured, `llama3.2:1b` came back citing `2` and `3` on a one-note
graph, having read the block as notes. That is the caller's own text and no
grant boundary is crossed; what is crossed is the invariant `citations` rests
on, that **a note boundary is something this module wrote**. Notes and question
are both defused before they are fitted, and the envelope still echoes the
question as typed — the neutralised question is what is *sent*, exactly as
`excerpt` is what is sent of a node.

**The question is defused as grammar and counted as evidence in the same call,
and that pair is a position rather than an oversight.** Measured on identical
graphs and an identical model reply: `ledger retention window` refuses with
`unsupported_numbers: ['9999']`, and `ledger retention window 9999` answers,
citing two notes that say *thirty days*. Four typed characters switch off the
only groundedness check the module has, so which of the two claims is about the
human matters. Only one is. The defusing says nothing about the caller — `[n]`
at the start of a line is **this module's grammar**, and every string
interpolated into the prompt is subject to the prompt's grammar whoever wrote
it; defusing the question is the same rule the notes get and not an accusation.
The corroboration is the claim that rests on the human, and it rests on a fact
rather than on goodwill: `ask` is reachable from `nodum ask` and from
`POST /api/ask` behind a session the middleware resolved to an **enabled
human**, and from nowhere else — no MCP tool (`READ_TOOLS`/`ADDITIVE_TOOLS`
carry neither verb), no job, no endpoint calling another. So the question is
the human's own text, and a human who types a number is asking about that
number; refusing the answer that repeats it would be refusing the question.
That makes **reachability load-bearing rather than incidental**, so a test pins
the caller set over the package's AST instead of a comment claiming it: a third
caller reddens it, and a caller that *composes* a question rather than typing
one changes the answer — the question stops being evidence, and only what a
human supplied should reach `_unsupported_numbers`.

**Escaping is not a defence against a model and does not pretend to be** —
"ignore previous instructions" in a note works on the 1B and nothing here
stops it. What this restores is the narrower thing `citations` claims: a cited
note is where the sentence was printed. Minting a per-request nonce into the
marker was the alternative and was rejected for a measured reason: the markers
are the only numbers in the prompt on purpose, and hex in front of every note
is exactly what took the citation format from 6/6 back to 4/6.

## 2026-08-04 — a line start is whatever a reader takes for one

The first version of the line rule asked `re.MULTILINE` for the line and
`[ \t]` for the indent: `^` matches at position 0 and after `\n` and after
nothing else — not `\r`, `\v`, `\f`, the file/group/record separators, U+0085,
U+2028 or U+2029, every one of which `str.splitlines` treats as a line and a
model reads as one — and the indent covered space and tab, so not NBSP, not the
em/en/ideographic spaces, and not the zero-width family, which is not
whitespace at all. **16 of 21 candidate line-starts survived, including every
one that renders identically to a defused one.** Measured live on
`llama3.2:1b` at temperature 0, 3 of 3 identical: one zero-width space in
front of a forged `[9]` on a two-note graph came back
`{"answer": "Ledger records are kept for 9999 days.", "cited": ["1","2","9"]}`
— `answered: true`, `unresolved: ['9']`, no `unsupported_numbers`, no refusal,
citations pointing at two notes that say *thirty days*. Verbatim the failure
the defusing exists to prevent, restored by a character with no glyph, and
reachable through the very path the rule was written for: `extract.HtmlHandler`
unescapes `&#8203;`/`&#65279;`/`&#8288;` and passes them through verbatim (NBSP
*is* removed by the line-stripping there; the zero-width family is not,
because it is not whitespace), and `ingest._source_content` hands that to
`create_node` unchanged. So the line is now `str.splitlines`'s, and the indent
is written out rather than described: **Unicode whitespace, plus the
`Cc`/`Cf`/`Cn`/`Co`/`Cs`/`Mn`/`Me` general categories, plus five named
blank-rendering characters** (the four Hangul fillers and U+2800 BRAILLE
PATTERN BLANK).

## 2026-08-04 — "draws nothing" is not a predicate

"Anything that puts no glyph on the page" was false in exactly the direction
the fix was made in: the class was whitespace plus two categories, and **six
other glyphless classes still carried an ASCII `[9]` to all three prompt
surfaces** — U+3164 HANGUL FILLER (`Lo`), U+FE0F VARIATION SELECTOR-16 (`Mn`),
U+2065 (`Cn`, an unassigned hole *inside* U+2060..U+206F whose assigned
neighbours the fix did close), U+2800 (`So`), U+E000 (`Co`) and U+0300 (`Mn`).
All six reach a `source` node verbatim through the same path as the zero-width
family: `extract.HtmlHandler` unescapes `&#12644;` like any other numeric
reference and its per-line `str.strip()` removes only whitespace. Measured live
on `llama3.2:1b` at temperature 0 before the widening, on a three-node graph:
HANGUL FILLER `unresolved: ['3','4','5','6','7','8','9']`, U+2065 `['3']`,
U+E000 `['9']`, U+0300 `['3']`, against `[]` for the defused baseline and for
the two the previous round had closed. After the widening every one of those
arms returns `unresolved: []`, identical to the baseline. **"Draws nothing" is
not a predicate** — `Lo` holds every CJK ideograph and `Mn` holds marks that
visibly draw — so the class is stated as what it is and the pin is exhaustive:
a test enumerates the sentence above over all 0x110000 codepoints and asserts
`_line_opening` agrees, so prose and code cannot move apart again. The widening
costs nothing measurable: over 2.8 MB of this repo's prose and 200 KB of real
`ingest url` output (PEP 8, RFC 8259, three Wikipedia articles, arXiv,
`docs.python.org`) the old class and the new one rewrite **the same 7
markers**. After the fix, same payload, same model, same temperature:
`cited: ["1","2"]`, 3 of 3, `unresolved: []`.

## 2026-08-04 — defused, not normalised, and the shield survives in the text

Stripping the zero-width characters and folding the exotic line breaks first
would make the defence's notion of a line and the model's coincide by
construction, and it is the wrong trade: **every deletion changes a width**,
and width is what the excerpt bound is measured in. `excerpt` claims to be
*what was sent*, `_unsupported_numbers` checks the answer against exactly that
string, and `…[truncated]` claims the cut fell at `MAX_CONTEXT_CHARS` —
normalising makes all three approximate, and does it to *every* note rather
than to the one carrying a forgery. Rewriting two brackets in place keeps them
exact, and leaves the shield visible to anyone who looks instead of silently
editing the caller's own note. Keeping the digits has a measured cost and it is
the cheap direction: `(9)` is still a number, and *every number in the prompt
is a candidate citation* — live on `qwen3:8b`, the fixed prompt came back
`cited: ["9"]`, mining the defused marker exactly as it once mined a node id
for `"116"`. It resolves to nothing, so the envelope is `answered: false` with
the answer withheld. **On `qwen3:8b` a forged number costs a refusal where it
used to buy `answered: true` beside citations that said the opposite** — and
that sentence is scoped to the model it was measured on, because the other
local model contradicts it. On `llama3.2:1b` at temperature 0, **with this
defence working and nothing shielding anything**, the marker-reuse payload
returns `answered: true`, `cited: ["1","2"]`, `unresolved: []` and the answer
*"records are kept for 9999 days, not thirty"* — 3 of 3 — while note 1 says
thirty days. That is verbatim the failure described two entries above,
occurring with the defence correct. The defusing closes the prompt's grammar;
it does not make a 1B model read, which is the next entry's point and the
reason this one may not be generalised.

## 2026-08-04 — the residual is named rather than left to be discovered

It is three things. (1) **A prefix that draws.** List and quote furniture — `- `,
`* `, `+ `, `> `, `# `, `1. `, `| `, `• ` — is a line start to any reader, is
not walked, and never will be. `- [9] Retention window` reaches the prompt
undefused and takes `llama3.2:1b` to `unresolved: ['3'..'9']`, exactly like an
invisible shield; closing it would rewrite every ordinary markdown list item
and every reference-link definition `[1]: https://…` in every ingested page,
which is a cost nothing measured justifies. It is the residual most likely to
arrive by accident rather than by design, so the suite **asserts it open**:
eight furniture prefixes are parametrized with `closed=False`, and anyone who
later closes one has to move the row and say why. (2) **A confusable rendering
of the grammar** — `［9］` fullwidth, `[٣]` Arabic-Indic, `[ 9 ]` spaced — is
*not* rewritten; it cannot forge what `citations` claims, because
`resolve_citation` takes ASCII digits and nothing else, so no such marker
resolves, and what it could still do is persuade a weak model that a line is a
boundary, which is the same "escaping is not a defence against a model" limit
already drawn and not a new one. (3) **A character outside the five named
blanks that a particular font happens to render blank** — a substituted missing
glyph, an unnamed `Lo` or `So` codepoint. There is no offline oracle for what a
font draws, so the class is five named characters and not a claim about
rendering. The test suite's audit sees all three **on purpose** — the furniture
and the confusable grammars are both matched there — so if one ever reaches a
prompt the suite says so instead of the question being re-reasoned.

## 2026-08-04 — the defusing has to run last, and `/summarize` is where that was found

`_excerpt`'s own `str.strip()` is Unicode-aware where the indent class was
`[ \t]`, so a leading NBSP shielded a marker from the defusing and was then
*deleted* by the strip — putting a bare `[9]` at column 0 of the excerpt after
the defence had already run. Deterministic, no argument about what a model
treats as a line required: the module's own strict regex read `['1','2','9']`
off the prompt `/summarize` really sent on a two-node region. `/ask` escaped
it by accident — `_offered_hit` builds `text=node.content.strip()`, so the
strip had already happened — while `summarize` builds `text=node.content`
unstripped, and **no marker test in the suite had ever reached `summarize`**;
the endpoint with only *one* end of the template was the one left untested and
broken. Two things hold it now, because one of them being enough is how it came
back: the indent class covers everything `str.strip` removes (asserted over the
whole of Unicode, not over the four characters that were measured), *and*
`_narrowed` excerpts first and defuses second, so the defusing runs on the
exact string that goes into the message. The second is the one that does not
depend on two character sets continuing to agree, and it is pinned over the
source rather than over an input — there is no payload left that distinguishes
the two orders, so a test built from one would pass under the order that was
wrong. `_context_block` defuses the excerpt again at the point it writes the
grammar, which costs one idempotent width-preserving scan and buys the property
that no caller's ordering can be wrong; that also makes its docstring true,
which it was not — it claimed to defuse the excerpt when only `_narrowed` had,
and an `Offered` built by hand with `excerpt=` set reached the prompt unread.

**An audit that shares a grammar with the code it audits tests that the grammar
equals itself**: the suite's own marker audit was `_LINE_MARKER` character for
character, so it could not have detected the gap, and it is now deliberately
looser on every axis the defence could narrow on, with the containment pinned
**exhaustively over all 0x110000 codepoints on the character axis and by a
seeded 20 000-string fuzz on the marker axis** (there is no `hypothesis` in
this repo and the check was never property-based; a seven-item corpus pins
seven items) so a later simplification back towards the module's regex fails
instead of quietly restoring the blind spot.

## 2026-08-04 — the same mistake reappeared one round later in the *fixtures*

Every payload the widening was tested with was drawn from `str.isspace() ∪ Cc ∪
Cf`, the implementation's own predicate written out as a parametrize list, so
the six classes it missed could not be expressed — and for three of them the
non-vacuity guard fired first and reported *"this case carries no forgery, so
it tests nothing"*, which is an instruction to delete the row that finds a real
bypass. The corpus is now authored from the character database with the Unicode
or markdown fact recorded beside every row, the rows the defence does **not**
close sit in the same list marked `closed=False`, and **every non-vacuity guard
is structural** — it asserts the fixture was built as stated, never that the
function under test changed something. Where a guard does consult the audit it
reports a failure as a claim about the audit ("widen `_carries_no_glyph`, and
do not delete this row"), never as a claim about the payload.

## 2026-08-04 — the weak model was never the binding constraint on this surface

The comparison against `qwen3:8b` says the same thing from the other side: it
makes the *identical* citation-format errors under the first prompt and costs
65–113 s a question against the 1B's 3–8 s, so the weak local model was never
the binding constraint on this surface — the prompt was. On the fixed prompt
both score **6/6**, the 1B in 25 s of wall clock and the 8B in 535 s; the 8B's
citations are cleaner (0 unresolved against the 1B's 3 spurious markers across
six questions, all correctly dropped), which is the only measured quality
difference between them here.

## 2026-08-04 — a reasoning model spends its thinking tokens out of `max_output_tokens`

`ollama` charges `<think>` to `completion_tokens` and strips it from `content`,
so `qwen3:8b` answers a rewrite with an **empty body at `finish_reason:
"length"`** — B3 then correctly discards it, and the feature is off on that
model with a message about a ceiling nobody chose. The query rewrite therefore
sets **no per-call output ceiling of its own**; there is one knob and it is the
human's (`NODUM_LLM_MAX_OUTPUT_TOKENS`). A tight per-call number is not a
saving, it is a model-compatibility setting in disguise. **`NODUM_LLM_MAX_OUTPUT_TOKENS=2048`
is the verified cure** — the 8B rewrite then returns `["compaction", "topic",
"state store"]` and finds the note. The degradation without it is graceful
either way: the rewrite reports `applied: false` with the exact reason and the
search runs the human's own words, which found the right note in 5 of 5.

## 2026-08-04 — the stored-MIME rule: a signature is definite evidence, the text heuristic is weak evidence

`_stored_mime` decides what is recorded: a signature always beats the name,
whatever family it guessed — PDF bytes called `scan.txt` *or* `report.json`
land as `application/pdf`, which is what `page:<n>` rasters and extraction
dispatch on (the same-family case was the hole: an `application/*` name kept
its answer against an `application/*` signature, so a real PDF called
`report.json` was stored as JSON and its bytes decoded as garbage text, finding
M25) — and the text heuristic may only **fill in** where the name guessed
nothing. That last clause is load-bearing: an uncompressed PDF whose `%PDF-`
sits one byte in sniffs as text, and letting that win cost the document its
handler, its page rasters, and put raw PDF bytes into the FTS index. It is also
why `image/svg+xml`, `application/json` and `application/xhtml+xml` keep their
own names with no list of exceptions to maintain.

## 2026-08-04 — a displaced `%PDF-` header is definite evidence too

`pypdf` and PDFium both *scan* for the marker rather than requiring it at
offset 0, so a real PDF behind a stray byte extracts, paginates and rasterises
— and since every PDF a human actually drops carries compressed streams, it
does not sniff as text either, so before this it matched nothing and the upload
route **refused it outright**. Order matters and is the safety argument: the
scan runs only for bytes the text test rejected, so prose quoting `%PDF-1.4` —
which this repository's own `docs/architecture.md` does — can never reach it,
where a bounded scan would only have made the misfire rarer. That refusal was
found by a live end-to-end pass and not by the suite: the test for the
mis-typing above drives this very route, but with a hand-assembled uncompressed
fixture that takes the text branch, so it stayed green while a real PDF was
turned away. **A fixture that cannot reach the branch is not coverage of it.**

**Text is a windowed heuristic and is documented as one, not as a guarantee**:
a NUL or any other C0 control byte means binary, checked over a 4 KiB window at
*each* end of the file (the tail is what catches a zip behind 4 KiB of ASCII,
since its central directory is at the end); a UTF-16/UTF-32 BOM exempts a file
from the NUL rule *only*, and the window is then decoded in that encoding and
still has to be control-free; a UTF-8 BOM proves nothing and is not honoured,
because UTF-8 text passes the byte test unaided and the exemption only ever
bought a bypass; and an empty file is not text. A NUL-free, control-free binary
format is still admitted as text — stated, bounded, and never called a
guarantee. New registrations decide their own MIME; a **dedup hit** keeps the
stored one except where a definite signature contradicts its family, which is
repaired with an `UPDATE` (`_repaired_mime`) — `assets` is content-addressed
base state maintained exactly that way already (`set_extracted_text`), and a
row registered under an older rule otherwise poisons every later reader of it.

## 2026-08-04 — the bomb guard and the 40 MP ceiling answer two different questions

`check_image_pixel_budget` takes its ceiling as an argument and `limit=None` is
a real posture, not a bypass: what Pillow itself calls a decompression bomb —
and bytes Pillow cannot read at all — is about danger and applies wherever an
image arrives; 40 MP is about what this server can *render*, so it gates
admission only on the route whose purpose is a rendition. Both refusals also
take a `name`, because the spool path is the operator's on a terminal and a
stranger's over a socket. Note that "cannot read" is `OSError`, not
`UnidentifiedImageError`: that class is one of its subclasses, and a plugin
whose `accept()` matched before the parse failed raises the bare class, which
used to escape as an unmapped 500. Pillow reads originals through `_BlobReader`,
which restores the file-style tolerant seeks that `sqlite3.Blob` refuses and
Pillow's format probing depends on.

## 2026-08-04 — `pypdfium2` won on licence, and `page:<n>` shares the rendition path

`pypdfium2` won on licence: PyMuPDF renders at least as well and is AGPL, which
would reach anything embedding nodum, while PDFium ships permissive wheels
needing no system package. `page:<n>` is the third profile shape
(`resolve_profile`): a 1-based page of a PDF rasterised by `pypdfium2` at
`PAGE_DPI` (144 — exactly 2× the PDF canvas unit, so a text page is legible
without a resample), then encoded down the *same* WebP path, so a page and a
photograph share their quality stepping, their id scheme, their cache, and
their eviction. The import is lazy and the dependency sits behind the `pdf`
extra, so an install without it still serves image renditions and answers a
page request with an `UnsupportedRendition` naming the extra rather than an
`ImportError` at startup. A raster has no image header to read, so its pixel
budget is arithmetic (page geometry × the DPI scale) — PDF permits a 200×200
inch page, which is 829 MP at 144 DPI.

## 2026-08-04 — nothing irreversible happens before the refusals that need no bytes

The target space is resolved *and the write grant and the `asset_ref`/`source`
types probed* before `register_asset`, because registration is the irreversible
half (there is no delete route). A grant minted against a space archived inside
its five-minute TTL otherwise stored up to 32 MiB with no describing node, no
FTS row, and no way to reclaim them (review F13), and a read-only agent's
refused ingest committed the same bytes with the same permanence, because the
write grant was only demanded by the node write afterwards (review B6). The
probes ask the same questions the write itself asks (`Store.landing_state` and
type resolution), so a refusal here is exactly the refusal the node write would
have given, moved before any byte is stored.

## 2026-08-04 — the keyword half matches on a quorum, not a conjunction

A node is a candidate when the query terms it carries are worth at least
**half** the query's total inverse-document-frequency weight. Weighting by IDF
is the whole of why the rule works rather than being a knob — a term
discriminates in proportion to its rarity, so a document qualifies by carrying
enough of the query's *discriminating power* rather than enough of its *words*,
and the eight function words of a twelve-word question cost it nothing.

The conjunctive rule it replaces was Phase 2's carried "BM25 goes silent"
finding, and the numbers are the argument: on a 312-node corpus with 40
question-shaped queries, **85 % returned no hits at all** (recall 0.06,
precision-over-returned 0.15) against 3 % after (recall 0.74, precision 0.63);
on 16 short keyword queries recall 0.79 → 0.92 with precision 1.00 → 0.72; and
on those same keyword queries plus **one invented term**, recall 0.00 → 0.92 —
which is the E3 prerequisite, since a query rewrite laid over a conjunctive
index can be zeroed by a single hallucinated token. A **bare OR** was measured
as the third arm and rejected on precision: recall 0.94 on questions but
precision-over-returned 0.24, and 1.00 → 0.32 on keyword queries. `0.5` itself
is measured rather than picked, and was re-measured under the function-word
list: 0.6 scores better on question precision (0.83 against 0.77 on 312 rows)
and **costs keyword recall at every size** (0.92 → 0.89 on 312, 0.96 → 0.92 on
47, 0.75 → 0.50 on 26), while 0.4 buys keyword recall and spends question
precision (0.77 → 0.65 on 312). A graph is small before it is large, so the
constant is chosen where it still works on a young one.

## 2026-08-04 — the df drop is a cost rule that is very nearly free

**Three** kinds of term are dropped before the quorum is computed, all because
they separate nothing: one the index has **never seen** (`df = 0` — BM25
already scores it zero, and requiring it is exactly how a hallucinated term
used to empty a result set), one in **more than half the indexed rows** — that
second is a *cost* rule and not a relevance one, since such a term's weight is
near zero either way but leaving it in the expression makes FTS5 walk a
doclist the size of the graph — and one on a fixed **English function-word
list** (`_QUERY_STOPWORDS`). Measured on 312 nodes, a question-shaped query
costs **32 ms without the df drop and 14 ms with it**, for 0.03 of recall and
no measurable precision (0.766/0.622 against 0.737/0.632) — a cost rule that is
very nearly free, not a free one.

## 2026-08-04 — the function-word list exists because the df rule is an estimator of it and a small graph breaks the estimator

A 47-row graph of short claims holds *what* in 7 rows and *does* in 8, both
far under any ceiling worth setting, so on *"What does min.insync.replicas
protect against?"* — where the target carries the query's only `df = 1` term —
the bar came out at 4.84 and the target collected 3.47, short by exactly the
weight of the two question words it does not contain. Dropping the two question
words by hand answered with the right node. The same mechanism zeroed *"What
did I write about how exactly-once semantics work in Kafka?"* — the design
note's own motivating query — and, worse than silence, returned the **draft**
near-duplicate first while excluding the canonical claim on *"How does
compaction let a topic work as a state store?"*, because `let` (df 2)
outweighs `compaction` (df 4). The 312-node measurement corpus was large enough
to hide all of it. A list does not move with corpus size, which is the property
the defect asks for; it holds no word that carries topic meaning here (`state`,
`store`, `key`, `value`, `log`, `set`, `order`, `time`, `point`, `case`, `long`,
`work`, `mean`, `group` and `number` are all deliberately absent — the list is
`_QUERY_STOPWORDS`' own docstring, and the two had already drifted apart once),
and **a function word never decides a search on its own**, which is what the
fallbacks are ordered by.

## 2026-08-04 — the ubiquity cut is given up first

The ubiquity cut is the only one of the three that is about cost rather than
meaning: a young graph is usually *about* something, so its subject sits over
the ceiling, and dropping it left *"What is kafka?"* matching every note that
says "what" and **not one that says "kafka"** — the exact inverse of the
answer, on the most ordinary question there is, measured at 20/30/60 subject
rows and wrong at all three. **That hands the `32 ms → 14 ms` saving back on
precisely the shape the cut was written for**, and the number belongs here
rather than in a comment: measured on a single-subject graph at 80/170/320
rows, median of 31 interleaved pairs, *"What is kafka?"* costs **+19 % to +29 %
with ~9 KB documents** (where the doclist walk is real) and **−1 % to +6 % with
short ones**. A cost rule given up on the one graph it was paying for is still
the right trade — the old plan was cheaper because it was answering a different
question — but it is not free, and the earlier `32 ms / 14 ms` figure now
describes a case this ordering rarely reaches. Only a query with **no content
word at all** ("what is it") falls back to its function words. A query whose
content words the graph has simply **never seen** answers with the nothing
those words alone answer with: `zarquon` returns nothing from the keyword arm,
so *"What does zarquon protect against?"* must not return three prose notes
that share its phrasing. That was the same conversion the JSON-schema finding
names — a visibly empty result turned into a confidently wrong one — reached
this time by wrapping an unknown word in a question.

## 2026-08-04 — the vector arm has a similarity floor

`_search_vector` carries a similarity floor (`_VECTOR_MIN_SIMILARITY`, cosine
0.5 — a chunk below it never enters the ANN list), so the vector arm no longer
answers the query the keyword arm just refused `k` rows deep from the nearest
unrelated chunks. Measured with the repo's own `HashEmbedder` on a 20-row
graph: `zarquon` and *"What does zarquon protect against?"* both come back
with **no hits at all**, vector signal included — the closest prose note sits
at distance 0.63 (cosine 0.37), below the bar. On the default install — no
provider — the refusal is the whole answer the same way; `tests/test_search.py`
asserts it on the keyword arm, `tests/test_hybrid_search.py` asserts the floor
and the empty result beside it, and `tests/test_answers.py` pins the `/ask`
gate: an absent term with embeddings present is refused, not answered. The
floor is a tunable constant, chosen so the test provider's genuine seeded
matches (cosine 0.5–0.71) pass and its disjoint ones (0.0) drop; the derivation
and units live in the constant's docstring. Finding M20.

## 2026-08-04 — six of the twelve invented-subject questions are silent now

Measured across five corpus sizes, questions whose content is invented
returned **4.8 / 5.1 / 5.9 / 7.9 mean hits and were never silent** before —
measured on the claim graph of `tests/test_search.py`, twelve questions built
around an invented subject — and **six of the twelve are silent now** (mean
hits 0.7 / 1.2 / 2.2 / 4.2). The count was previously reported as "stable at
40, 72, 136 and 264 rows"; that claim is **withdrawn**, because those sizes
were reached by repeating the one fixture and duplication cannot move the
number: the gate reads only whether a content word has `df > 0`, and copying
every row preserves that for every word while scaling `df` and the ceiling
together. It varied corpus *size* while holding corpus *composition* fixed, and
composition is the only input the gate has. Six is a measurement on that corpus
and nothing here claims more; a test asserts it exactly, so this sentence
cannot drift from the code.

**This rule closes half of that shape, not all of it.** What the gate tests is
"no content word *known*", not "no content word that *discriminates*": ordinary
English nouns and verbs stay on the content side on purpose, so the six that
still answer are the six whose *other* content words — `fail`, `safe`, `store`,
`long`, `node`, `space`, `first` — the graph genuinely holds. A test asserts
that six exactly, so this sentence cannot drift from the code.

**Widening the gate to "no content word at or under the ubiquity ceiling" was
measured and rejected**, because it collides head-on with the ubiquity-first
relaxation above. On a single-subject graph at 38, 56, 120 and 308 rows it
takes *"What is kafka?"* from **30 hits to 0 at every size**. The surgical
variant — refuse only when an unknown content word sits beside an
over-ceiling one — keeps that question but takes `kafka concretoid` from 30
hits to 0, which is the E3 guarantee that a hallucinated term must not empty a
query, and it fires on `apple zarquon` inside a six-row read set too. Neither
variant closes **one extra** invented-subject question at any of those sizes
(silence stays 0.83 for all three gates at all four sizes). The two rules
cannot both be maximised; the ubiquity relaxation is worth more, because a
graph that is about one subject is the graph every graph starts as.

Measured across five corpus sizes, question-shaped queries (recall, precision
over the returned list): **47 rows 0.74/0.65 → 0.87/0.73** (and the zero-hit
rate 0.19 → 0.00), 26 rows 0.79/0.63 → 0.88/0.65, 52 rows 0.73/0.57 → 0.89/0.69,
78 rows 0.70/0.52 → 0.81/0.63, **312 rows 0.74/0.63 → 0.86/0.77**. The keyword,
two-term and hallucinated-term suites are **byte-identical** before and after at
every size — a question's phrasing was the only thing paying for the bar — and
identical again after the fallback re-ordering, which is reachable only on the
two shapes those four suites do not contain: a query the graph knows no content
word of, and a graph whose subject is over the ubiquity ceiling. Alternatives
measured and rejected: an absolute IDF floor (a df-fraction cut in disguise —
it drops `Kafka` at 8 rows of 47), a tighter df ceiling (0.25 recovers 0.00 of
the 47-row recall), and a quorum over the query's N heaviest terms (no better
than the list on any corpus, and worse on the 312-node one).

## 2026-08-04 — two terms of equal document frequency compare strictly

`>` rather than `>=`: equal df is equal weight, each term is then exactly half,
and `>=` admits either alone — the quorum silently becomes the bare OR it was
chosen over. Measured on a 40-row graph with `kafka` and `postgres` both at df
6: 10 hits at precision 0.100 against 1 hit at 1.000. The strictness is **gated
on the two-term case**, because with four equal terms a blanket `>` moves the
bar from two-of-four to three-of-four; gated, it is byte-identical to `>=` on
every suite at every corpus size, since two real terms are rarely exactly equal
— *rarely*, not never, which is what the carried claim got wrong.

## 2026-08-04 — a repeat wearing punctuation is still a repeat

The dedup folded the raw token while FTS5 (`porter unicode61`) tokenizes
`kafka,` and `kafka` identically, so the same word arrived as two terms
carrying one word's document frequency twice — enough to clear a bar half of
itself. Measured on the 40-row equal-df fixture: `kafka postgres` answered with
the one node carrying both and `kafka, kafka postgres` with **six**, the bare
disjunction the quorum was chosen over, restored by a comma. `_query_terms` and
`_is_function_word` now share one fold (`_bare_word`) — they had disagreed
about what "the same word" is, one stripping edge punctuation and the other
not. Pre-existing; the fallback re-ordering made it load-bearing.

**The fold is the tokenizer's rule, not a list of characters.** A first fix
trimmed fifteen ASCII characters off each end, which closed the comma and left
the same bypass open on everything outside the list: measured on the same
fixture, `kafka- kafka postgres`, `“kafka” kafka postgres`, `#kafka kafka
postgres` and `**kafka** kafka postgres` each answered with **six** again, and
the same hole re-opened the refusal — `**What** does zarquon protect against?`
and `“What” …` answered with **8** prose notes where the undecorated question
correctly answered with **0**. `_bare_word` is now every maximal run of
alphanumeric characters, lowercased — `unicode61`'s own rule — and is
**incomplete, and sound on every script SQLite's case table has caught up
with**: what it merges the tokenizer merges (zero counterexamples over a
465-token probe, 107 880 pairs), and what the tokenizer merges it may not.

The soundness half is **not** an absolute, and the exception is a version skew
rather than a corner case: SQLite's fold table is frozen at the Unicode version
`fts5_unicode2.c` was written against, so a simple case mapping added since
folds in Python and does not fold in FTS5. Swept exhaustively over every
alphanumeric codepoint below U+30000 (133 808 of them) against a live table:
**417** fold groups Python merges and SQLite splits — Cherokee, Old Hungarian,
Vithkuqi, Georgian Mtavruli, Adlam, Garay, Osage, Warang Citi, Medefaidrin,
Glagolitic, and a tail of Latin, Greek and Cyrillic letters — and
`search("ᲓᲦᲔ დღე")` really does reach only the Mtavruli row, the lowercase
term the caller typed having been dropped as its duplicate. Deseret (cased
since Unicode 3.1) folds correctly on both sides, so the axis is the table's
vintage and not "non-ASCII" or "non-BMP". Blast radius on this corpus is nil —
nothing in ASCII, Latin-1 or any Western European language is affected — so the
fold is deliberately **not** changed: merging a case pair SQLite refuses to
merge is the more correct behaviour linguistically, and it was the stated
invariant that was wrong, not the code. One pair per block is pinned as
`_FOLD_UNSOUND` in `tests/test_search.py`, with a sound control beside it, so
SQLite catching up fails the test rather than making this paragraph quietly
stale. **Both counts are a reading of two moving tables and neither is a
constant** — `_bare_word` calls `str.lower()`, so the Python side moves with
the interpreter's Unicode version just as the SQLite side moves with its build.
The figures above are CPython 3.14 against SQLite 3.50.4; on CPython 3.12 and
Unicode 15.0, **which is what CI runs**, the identical sweep gives **390 of
128 804**. The pinned pairs are Unicode 7-to-11 mappings and hold identically
on both, which is why the pin is a test and the counts are only prose — a
paragraph that replaced one unqualified absolute should not quietly introduce
two more.

## 2026-08-04 — the stemmer half of the residue, and the rejected alternatives

`porter` **stems**, and `_is_function_word` looks a *character* fold up in
`_QUERY_STOPWORDS` — so any word that stems onto a stopword arrives at
`_compile_match` as a **content** word while matching the stopword's rows in
the index, which **re-opens the refusal** exactly as a decorated stopword used
to: `known_content` is non-empty, `kept` is non-empty, and the early return
never fires. This is not exotic input — **167 of the 63 875 lowercase words in
`/usr/share/dict/american-english` collide this way**, and the worst is the
commonest verb a technical question carries, since `use`, `used`, `using`,
`uses` and `useful` all stem to `us`, which is in the list. Measured: on a
corpus saying the pronoun *us* in six rows, all five spellings answer *"How is
zarquon used?"* with those six rows and nothing else, and the shipped claim
fixture needs no help at all — *"What does zarquon doe?"* answers with **8**
hits, every one of them prose sharing only the question's phrasing, where the
undecorated question correctly answers with **0**. The cheaper half is the
dedup: `retain retains postgres` still answers with **six** and still buys one
word two shares of the quorum's weight. `unicode61` folds **diacritics**
(`café`/`cafe`) besides. All of it is pinned by tests that fail if any of it is
closed without rewriting this paragraph. Closing the stemmer half needs a
Python porter stemmer that agrees with SQLite's on every word, and one that
*disagrees* is worse than none; stemming `_QUERY_STOPWORDS` at import would fix
the lookup without a whole stemmer but carries the same risk with the sign
flipped — an over-merge there demotes a real content word to a function word
and refuses a query that should have answered. A different error, not obviously
a cheaper one, so both halves stay open and both stay measured. `casefold()`
was rejected for `lower()` (it merges `straße`/`strasse` and `ﬁle`/`file`, which
the tokenizer keeps apart — unsound, and unsound loses a term the caller
typed), and NFD diacritic-stripping was rejected too (SQLite's default
`remove_diacritics=1` leaves *precomposed* multi-diacritic codepoints alone, so
stripping merges `ộ` with `o` where the tokenizer does not: twelve unsound
pairs to buy back eleven incomplete ones).

On the harness's four query suites the two folds are **identical at every
corpus size** (26 / 47 / 52 / 78 / 312 rows, 75 queries each, 0 diffs). They
differ only on a fifth, decorated suite, and every difference is a
restoration: `“kafka” kafka read` goes from 4–7 hits to 3, and `**What** does
clickhouse do when the disk fills up?` goes from 0 hits to 2, because the
decorated `What` stops being counted as a content word.

## 2026-08-04 — document frequencies are counted through the search's own filters

Counting the whole index made the bar depend on rows the caller cannot read:
with `zarquon` planted in a private space, an agent holding `read` on one
public space got **0 hits for `apple zarquon` and 6 without the planting** — a
one-bit existence oracle over every space in the file, and repeating it with
words planted at chosen frequencies brackets a private term's df. `search` is
in `mcp_server.READ_TOOLS`, so an external agent has it. Scoping also makes the
weight *right*: rarity is a property of the corpus being searched.

## 2026-08-04 — a query carries at most `_MAX_QUERY_TERMS` = 64 distinct terms

More is a `ValueError` — a 400, not a 503. Above 500 usable terms the quorum's
`UNION ALL` hits `SQLITE_LIMIT_COMPOUND_SELECT` and SQLite raises: measured, a
4 508-byte query answered **503 "database error: too many terms in compound
SELECT"** from `GET /api/search` and `POST /api/ask` and exit 1 from the CLI,
three storage-voice failures for an oversized request — and 503 is this API's
*retryable* status, so a client retries it forever. The cap is far above
anything this system produces (the model's rewrite is capped at 8 terms, the
longest measured question is 11). A query left with one term compiles **no
quorum at all**, so a one-word search runs the statement it always ran. The
restriction is a CTE in the `WHERE`, never a filter over the ranked rows —
ranking first and filtering after would drop good rows off the end of `LIMIT k`
before anything looked at them — and it changes *which rows are candidates* and
nothing else: the BM25 weights, the `k` cap, RRF's rank arithmetic and the
post-fusion graph expansion are all untouched.

## 2026-08-04 — "source nodes outrank claim nodes" does not reproduce as recorded

Phase 2's carried finding blamed BM25 length normalisation for not offsetting a
`source` node carrying a whole document's text. Normalisation offsets it and
then some: with term coverage held fixed and only length varying, the same
sentence scores **−14.6 at 112 characters and −0.5 at 60 KB** (more negative is
better) — a 28× penalty for length alone — and a one-sentence `claim` beats a
20 KB `source` carrying that same sentence under the same title.
`asset_ref` text in the `extracted_text` column — the design pass's named
suspect if it *had* reproduced — does not change that either. What is real is
the observation, and its cause is the **conjunction**: a whole-document node is
the only node in a graph that contains every word of a question, so under the
AND rule it was the only node that could match one. Measured on the 312-node
corpus: of the six question-shaped queries the conjunctive matcher answered at
all, **all six put a `source` node first**; after the quorum, `source` holds 7
of 40 first places and `claim` 15. The two carried findings were one defect
seen from two sides, and fixing the first closes the second — which is why
**no ranking weight was touched**. Where a source still outranks a claim (11 of
37 comparable queries), it does so having matched strictly more of the query's
terms in *every* case, which is BM25 being right. Do not retune `_BM25_WEIGHTS`
against this finding; `tests/test_search.py` pins the normalisation property so
a later change cannot quietly make the old explanation true.

## 2026-08-04 — a migration runs with `foreign_keys=OFF` and is checked before its commit

Deferring the constraints instead cannot work for a table rebuild, because
dropping a populated parent leaves a deferred-violation counter the rename does
not clear — 0009 could not upgrade a database holding a single node and its
version row. The schema-consistency check runs **before** the apply loop, so a
database whose only cure is deletion never gets a new (possibly irreversible)
migration committed onto it first.

## 2026-08-04 — `0014` refuses the `builtin-` prefix, not the one id

`0014` refuses the upgrade on a database that already holds an agent id under
the reserved `builtin-` prefix rather than resolving the collision: taking the
id would attribute that agent's whole history — every `agent:builtin-gardener`
in `events.actor`, `versions.actor` and both `created_by` columns — to the
gardener, and renaming the impostor would detach that same history from the
account it names, since actor strings are immutable log entries and not
references anything can follow. Both corrupt the one question the event log
exists to answer, so the operator renames or removes the account by hand and
re-runs. The guard is `id LIKE 'builtin-%'` and not the single id: `0010`
back-fills an `agents` row from every actor string in the log, so a pre-0010
file whose events merely *mention* `agent:builtin-librarian` upgraded clean and
left a live, token-bearing external account under the prefix — the collision
this guard exists to refuse, pre-installed for the day 5b seeds a second
`builtin-*` agent. `RAISE()` is trigger-only in SQLite, so the abort is a
`CHECK` constraint whose **name** carries the message — SQLite reports it
verbatim — over a scratch table that gets a row only when something under the
prefix exists. The name cannot name the offenders: SQLite takes an expression
as `RAISE()`'s second argument only from 3.47.1, newer than most distributions
ship, and a migration that fails to *parse* is a worse failure than one whose
message carries the `LIKE` pattern to look them up with.

## 2026-08-04 — `0015` records a stop as two columns and a cross-column CHECK, not a boolean

`cycles.stop_requested_at` and `cycles.stop_requested_by`, **two columns and no
boolean flag** — the story is in the "gate under the stop switch" entry above.
`db._cycle_stop_problems` asserts both exist on any file recording `0015`, for
the reason `_cycles_problems` asserts 0014's index — `init_db` skips a
migration whose name it already holds, and nothing in the runtime catches the
drift first: `LLMReport.stop_switch` reports the posture a *run* had rather
than what the file can store, so a cycle over such a database reads `armed`
right up to the failed write. Its remedy is `0014`'s kind, not the first four's:
the refusal prints the `ALTER TABLE` for each column it **found missing**, in
dependency order, because `ADD COLUMN` has no `IF NOT EXISTS` and a remedy that
always printed both would die on `duplicate column name`. And
`db._cycles_problems` checks the index exists on any file recording `0014`,
because `0014` was amended in place while unreleased and `init_db` skips a
migration whose name it already has — and **its remedy is its own**, not the
one the four checks beside it share. `_verify_schema_consistency` used to end
every refusal with "delete the database file and re-run `nodum init`", which is
true of a missing table and wildly wrong for a missing index: the index
constrains rows the file already has, so `db.CYCLES_RUNNING_INDEX_SQL` repairs
it in place and the refusal prints that statement. A refusal that reads as
*your graph is unrecoverable* over one `CREATE UNIQUE INDEX` costs a human
every node they own.

## 2026-08-04 — the probe waits exactly as long as the envelope says it will

`llm status`'s reachability probe used to hold its own 30-second constant that
`NODUM_LLM_CALL_TIMEOUT` did not reach, so a slow install printed `did not
answer within 30s` three lines under `"call_timeout": 600.0` and raising the
knob changed nothing. There is one per-call ceiling and it is the run's. And
what it spends is reported, in `used` — **34 tokens a probe, measured**. This
was the one provider call in the phase that reported none, which made `llm
status` the single place in this system where something is spent and the caller
cannot see it. `--no-probe` reports the configuration, spends nothing, and says
`calls: 0` to prove it.

**The probe asks a bounded question at `reasoning_effort: "none"`, and both
halves of that are measured.** `"ping"` was not bounded — this model answers it
with a paragraph, in Chinese — so the probe hit its own ceiling on every call
and reported `failed_calls: 1` on a healthy install; and at any graded level it
returns an **empty body**, every output token spent thinking. `"Reply with
exactly one word: pong"` at `none` costs **2** output tokens and stops on its
own, six times out of six. `PROBE_OUTPUT_TOKENS` is 32 rather than 8 for the
same reason: 8 was below what any answer costs here, so the truncated path was
guaranteed rather than exceptional. The `OutputTruncated → reachable: true`
handler stays as the backstop for a chattier model.

**`llm status` is also where a downgrade becomes visible**: it reports
`structured_output` (`json_schema` or `json_object`), `thinking` with
`thinking_applied` beside it (`false` is a knob doing nothing — the ollama
case), and `effective_max_output_tokens`, which is what the configured ceiling
is really worth once the window's share caps it. It re-reads **both** negotiated
beliefs after the probe. `thinking_applied` because the probe *sends*
`reasoning_effort` and is the one call that can discover a refusal;
`structured_output` because the argument for leaving it stale — "the probe
sends no schema, so that branch is unreachable" — was about the **request**
while `_negotiate` decides on the **response**, and never asks whether a schema
was sent. Any 400 whose body names `response_format` **and a reason word**
(`is unavailable` / `not supported` — the field name alone does not downgrade)
demotes the belief, a gateway can answer that to a schema-less request, and the
demotion then lasts the life of the process because the provider is cached.
The visible failure was one status payload reporting `structured_output:
"json_schema"` directly above a `used.structured_mode` of `"json_object"`, with
every later `/ask` in that `nodum serve` running under the weaker envelope.
**An unreachable-branch claim is a claim about the code that decides, not about
the code that asks.**

## 2026-08-04 — a limit below 1 was three different wrong answers

`service.require_positive_limit` — the one *public* helper in a file of private
ones, because `nodum.search` imports it too — is now every capped read's error.
`subgraph` stated the rule, `list_cycles` followed it, and the rest took the
number straight through — so `events --limit -3` and `node list --limit -3`
handed back the whole log and the whole listing, the opposite of what was asked
for. It now covers `list_nodes`, `list_edges`, `list_events`, `list_proposals`,
`suggest_links`, `subgraph`, `list_cycles` and `search` (which spells it `k`,
hence the helper's `name=` argument: a message about `limit` would name a flag
that does not exist). **The bug is not the same bug everywhere, which is why
"one message" matters more than it looks**: where the number reaches SQL a
negative cap is *unbounded*, but where the cap is a Python slice
(`list_proposals`, `suggest_links`) a negative one silently drops that many
rows off the **end** and answers normally — on the review queue that is a
proposal that vanishes with nothing to say it did. Three different wrong
answers from one typo, and now one refusal. Any new capped read calls the
helper; do not restate the check.

## 2026-08-04 — `retype` and `bulk-relink` reported success to the one thing a script reads

`retype` used to print its `failed[]` and exit **0**, so `nodum retype main
--type note` — which accomplishes nothing, opens a curative cycle and closes it
`completed` with zero events — reported success to the one thing a script
reads. Now the envelope is on stdout as before (the successes are the point of
not aborting), each skipped id is named on stderr as `  failed <id>: <reason>`,
and the exit code is 1 if any item was skipped. **`bulk-relink` follows it too
now, and it took a shape change to get there.** Its exemption rested on
`skipped[]` mixing two things: "nothing would change on this edge" sat beside
real refusals under one field called `error`, so a script could tell them apart
only by matching the sentence and an exit code derived from the list would have
been wrong more often than right. That mixture is **gone** — `BulkRelinkOut`
reports `unchanged` (bare edge ids the change would not alter: a diff
annotation) apart from `skipped` (the refusals, each with a reason: a self-loop,
a duplicate the graph already carries, or a space the caller may not edit) — so
`skipped` *is* a failure list, and the exit code is derived from it. Without
that, a run which could not relink three edges for want of `edit` on their
space reported success to the one thing a script reads, which is precisely the
`retype` bug above.

## 2026-08-04 — the SPA catch-all gets an exemption list, starting with `/favicon.ico`

The catch-all's premise is "an unknown non-API path is a client route", and
that is true of everything a *user* can type. It is false of the paths a
browser requests on its own: `/favicon.ico` was answered with `index.html`
under a 200 and `text/html`, which a client asking for an image cannot detect
as a non-answer. It is now routed ahead of the catch-all and serves the
bundle's icon if one exists, **204 otherwise** — the page declares its icon as
an inline SVG data URI, so normally there is no file and "nothing here" is the
true answer. A 404 would be equally honest; 204 was chosen because it is not an
error and produces no console noise. The general rule the entry records: a path
the browser invents belongs in the exemption list, not in the catch-all.

## 2026-08-04 — the stored timestamp is UTC and does not say so

Every `created_at` / `updated_at` is SQLite's `datetime('now')` —
`YYYY-MM-DD HH:MM:SS`, UTC, with no zone marker — and `new Date("2026-07-24
21:49:13")` parses that as *local* time. Every view printing a timestamp was
therefore wrong by the reader's UTC offset, silently and identically. The fix
is one parser (`web/src/lib/time.ts`), which normalises a zone-less stored
string to UTC before constructing the `Date`; every formatter in the app goes
through it, and `new Date()` on a server string is banned by convention. The
alternative — writing offsets into the column — would be a migration over every
row and a change to what the CLI prints, to fix a bug that only ever existed in
one client. The Vitest run pins `TZ` to a non-UTC zone (`web/vitest.config.ts`)
and `time.test.ts` asserts the pin took: measured with the normalisation
removed from `parseTimestamp`, **12 of 20 timestamp tests fail under the pin, 4
under UTC**.

## 2026-08-04 — there is no Ask view, and that is a decision

`POST /api/ask` is a read-only surface a client could call in an afternoon, and
5b-i deliberately does not build a browser view. `/ask` can return a
**confident, well-cited, wrong answer** — it was measured answering "AWS" with
`answered: true`, citing a Kafka textbook containing no occurrence of AWS, cloud
or Kubernetes, against a graph that says k3s on three on-prem nodes — because
citation *resolvability* is not groundedness: E2 defends against an invented
**id**, and that answer invented **content** and hung it on a real one. What
catches it is the envelope, and the envelope survives one surface and not the
other: a CLI reader gets `unresolved`, `considered` and `dropped` as JSON
beside the answer and is already looking at them, while a browser reader gets
prose, and a screen that has just answered the question in a paragraph is a
screen whose lists nobody reads. So the surface stays where its reader is
equipped for it. **It moves to the browser once groundedness is real** — a
deterministic check that the answer's claims are in the excerpts the request
retrieved, rather than that its citations resolve. Until then an Ask view, an
"ask about this node" button, and an answer panel bolted onto search are all
the same decision taken by accident; `/summarize` is the same call and the same
rule.

## 2026-08-04 — the journal copy was right in one place out of four

What checks the kill switch today is a provider call; the deterministic jobs
make none, so a run of those finishes even after a stop. The *confirm* said so,
and the button's tooltip offered to "ask this run to wind down and close its
own entry", `RUNNING_ACTIONS_HINT` said the run "closes its own entry when it
notices", and the toast a human reads immediately after pressing promised "the
entry closes when the run notices". The code was right and three of the four
surfaces were wrong, which is this defect class exactly: the fix is the
sentence, never a stop check wired into the deterministic jobs. That caveat is
now one exported constant (`STOP_IS_NOTICED_AT_A_MODEL_CALL`) carried by every
surface — a caveat repeated in four voices is a caveat that stops being true in
three of them — and the tooltip moved out of the JSX into `journal.ts` with it,
because the harness renders no components and a claim inside one is a claim
nothing checks, which is how that one stayed wrong. `tests/test_consolidate.py`
still fails the day 5b-ii wires a check in. **Verified against the race the
review drove**: a consolidation stopped mid-run through
`POST /api/cycles/{id}/stop` ran to `completed` with the stop kept on its
entry, and the journal entry for it reads *"a stop was asked for on this run
and it completed anyway"*.

## 2026-08-05 — the MCP surface takes no path on the server's disk

`ingest_file` accepted a path this server could read, and the grant model had
nothing to say about it: grants scope the *graph*, and reading a file is not a
graph read. An agent holding the minimal write grant — `suggest` on one space,
what a new agent is given — named a path, the pipeline wrote the extraction to
`assets.extracted_text`, a `proposed` describing node was enough to reach it,
and `get_asset` returned it. Two calls, both auto-approved by a host
(`destructiveHint=False`, then `readOnlyHint=True`), and `name` picks the
extraction handler, so an extensionless secret reads as text by asking for it.

**The first fix was the wrong one and is worth recording as such.** A review
reported the capability through the path it noticed — the tool *echoed* the
text in its own result — so the result stopped carrying it, and
`_ingest_result` got a docstring naming the harm: "echoing them would hand any
token-bearing agent the contents of any file this server can read". Two
sentences later the same docstring documented the next call. The delivery
vector closed; the capability never moved. Its own docstring now says it is a
payload-size decision and not a boundary, and the test that pins it asserts the
second call *does* return the text, so nobody reads the first half as a defence
again.

What bounds the surface is that nothing here names a file
(`mcp_server.FILESYSTEM_TOOLS`, the fourth named absence beside the review,
curative and human-only tiers). Nothing is lost: ingestion by reference (§5.7
rule 2) keeps `ingest_url` for anything the server can fetch and
`request_upload_url` for bytes the caller holds — which are the two doors a
server the agent does not share a machine with needs anyway. `nodum ingest`
still takes a path, on the CLI, where local access is already the trust
boundary.

## 2026-08-05 — `edge_scope` computes the node rule instead of restating it

`node_scope_clause` scopes a `space`-typed node to its **own id**: space nodes
live in meta, every agent reads meta for the type vocabulary, and filtering one
on `space_id` alone hands every space in the file to any meta reader. That was
M3. `edge_scope` kept the pre-M3 rule — `space_id IN (…)` for both endpoints —
and `service._walk` loads both endpoints of every edge it follows, trusting
that clause. So a `mentions` edge onto a space node, which wikilink
materialisation writes whenever a readable note links a space by name, carried
that space node's id *and its title* to an agent `get_node` refuses.

Two copies of one rule, and the second was a year behind the first. `edge_scope`
now calls `node_scope_clause` per endpoint, and `_walk` re-checks each endpoint
row against `Store.node_visible` — the clause is the filter, the row check is
the second layer, and a node read in the service that skips it is the shape of
this defect. Both docstrings asserted the guarantee that did not hold; that is
the tell, not the SQL.

## 2026-08-05 — a failed login is nobody, and its refusal is not an event

`events.actor` answers *who did this*. `human.login_failed` put the attempted
name there, reasoning that a failure has no verified principal so the column
should record what the attempt claimed — on the one `/api` route outside the
session gate. `{"name": "human:owner"}` therefore wrote rows attributed to the
seeded owner, with no credential presented, and `nodum events --actor
human:owner` listed them beside the real owner's. The name is data about the
attempt and lives in the payload; the actor is `UNAUTHENTICATED_ACTOR`, which
carries neither identity prefix and so cannot be resolved to an account.

A refusal by the lockout no longer writes an event either. It did, so that a
guesser who kept trying kept the window fresh — true, and two defects: an
unauthenticated request became an unbounded append to the append-only log, and
any local process could hold the real human out forever by re-arming the window
every quarter-hour. Sixty attempts on one name wrote sixty rows; they now write
five, which are the failures that earned the lockout. Its refusals are a rate
limit, and a rate limit that logs is a rate limit that can be turned around.

Two things under it. `login` runs through `run_in_threadpool` like every other
blocking route: argon2id is ~100 ms, spent on names that do not exist too, and
this is the only route an unauthenticated caller reaches — inline, ten requests
a second was a stopped server. And `0018` indexes `events(op, created_at)`,
because `login_failure_count` runs on every attempt and was a full scan of the
log with a `json_extract` per row: the check got slower with exactly the
traffic it throttles. `EXPLAIN QUERY PLAN` reads `SEARCH events USING INDEX
idx_events_op_created`, where it read `SCAN events`.

**The residual, stated rather than defended**: the lockout is per name because
it must not be an existence oracle, so an attacker cycling distinct names still
appends a row per name — the same-name amplification is what closed, not the
append. A global limit would fix that by handing the same attacker a way to
lock the human out of their own graph, which is worse. The row's *size* is
bounded separately: the login name and password are capped where they are read
and where they are written, after an uncapped name wrote a 200 KB event row per
unauthenticated request.

**And the fix's own first cut traded one denial of service for another
silently.** Moving argon2id off the event loop was right and was shipped with
no bound, so anyio's default limiter ran 40 hashes at 64 MiB each: 64
concurrent unauthenticated logins took **+2573 MiB** of RSS where the inline
version took +64 MiB. A dedicated `CapacityLimiter(2)` puts that at +131 MiB
and — the property that matters — stops it scaling with load at all. What
replaces it is a latency cost the excess pays in a FIFO queue: 8 concurrent
logins settle in 0.53 s, 256 in 13.78 s. That is the honest shape of the
trade. It is the better half — a slow login recovers the moment the flood
stops, while 2.5 GiB of RSS does not — and it is written down here because the
first version of the comment justifying the queue described a property the
code does not have.

## 2026-08-05 — the tag gate runs what the PR gate runs, and a test says so

`release.yml`'s comment read "the matrix mirrors ci.yml's so the tag gate is
not weaker than the PR gate". True of the matrix, false of the gate: ruff,
pyright, the highest-resolution resolution leg and the whole frontend suite ran
on pull requests only, so the artifact users install was gated more weakly than
the branch it came from. Usually a tag names a commit that already passed
ci.yml — and "usually" is what a release gate exists not to depend on. This
project has already pushed a tag at a tree that was never the merged one
(`v0.12.0` published a pre-merge state).

The missing jobs are in `release.yml` and in `build-and-publish`'s `needs`,
and `tests/test_docs.py` asserts the cover: the claim is checkable now rather
than written down, and a job added to a PR-gating workflow fails the suite
until somebody decides whether a release needs it. The highest-resolution leg
matters most of them — it is the leg that exists for an unbounded dependency
major breaking a fresh resolution, which is a thing a *release* ships and a PR
does not.

**The first cut of that test asserted less than its own docstring claimed**,
which is the defect it exists to prevent, arriving inside the fix for it. Four
ways: it read `ci.yml` alone, so `docs.yml` — a third `pull_request`-triggered
workflow, and the one that catches a broken internal link — was invisible, and
a tag at a tree with a dead link published green; its job-id regex was
`[a-z0-9-]+`, so a job named `type_check` was silently *not seen* rather than
reported missing; it took the first `needs:` in the file and assumed it was
`build-and-publish`'s; and it compared job *names*, while `release.yml`'s `web`
job was in fact missing `make web-build` — the frontend typecheck — that
`ci.yml`'s ran. All four are closed: workflows are discovered by their
triggers, an unparseable job id is a loud failure rather than a skip, `needs:`
is read out of the named job in both YAML spellings, and same-named jobs are
compared by their `run:` steps with the release side required to be a superset.
Two exemptions are named and load-bearing rather than decorative — `npm audit`
(an advisory published overnight must not block a tag) and `docs.yml`'s deploy
job (never a PR check).

## 2026-08-05 — the workflow gate stopped hand-parsing YAML, after three silent mis-reads

`tests/test_docs.py` asserts a tag push runs every check a pull request runs. It
read `.github/workflows/*.yml` with regexes, to avoid a dependency. Three
consecutive adversarial reviews found it **silently mis-reading** the files it
audits:

- `run: |` matched the regex and recorded the command as the literal string
  `"|"`, so two entirely different block scalars compared equal;
- then nine more at once — a flow-mapping step dropped whole, a `uses:`-only job
  read as running nothing, anchors and aliases compared as literal text, a
  truncated quoted scalar, `run: |2`, a `with: run:` action input counted as a
  shell command, a duplicate job id, two top-level `jobs:` keys;
- then a plain multi-line `run:` truncated to its first line, which is the one
  that decided it: `uv run --locked ruff check` continued with `.` on the next
  line compared **equal** to a release side weakened to `--exit-zero`. The gate
  reported parity while the tag ran a check the pull request did not.

Each round the parser was made stricter and each round it was still wrong
somewhere else, in the one test whose entire purpose is catching silent drift.
That is the argument for the dependency, and it is worth more than the
dependency costs: `pyyaml` is in the `dev` group — never `[project]
.dependencies`, verified by reading `Requires-Dist` out of the built wheel — and
the file is 228 lines lighter (615 → 387), the parsing layer about 180.

The switch also *widened* what the gate accepts. The hand parser refused flow
mappings, anchors, a `run:` under `with:` and a quoted `"on":` key outright;
those are legal workflow YAML and now pass. One shape does not: the strict
loader rejects a `<<:` merge key, because it resolves every key before
`flatten_mapping` runs. That is the commonest reason to reach for an anchor at
all, so the widening is narrower than it sounds — it is a loud refusal rather
than a mis-read, and GitHub Actions does not support merge keys either, so the
file it refuses is one Actions would refuse too. Two behaviours are deliberate rather
than inherited: PyYAML is YAML 1.1, so a bare `on:` key parses as the boolean
`True` and the trigger reader accepts both spellings while refusing a file
carrying both; and the loader **raises on a duplicate mapping key** instead of
taking YAML's last-wins, because either reading means the file says something
other than what it runs.

What is compared is job presence and the `run:` commands of same-named jobs,
release required to be a superset. What is **not**: `shell:`,
`working-directory:`, `env:`, `if:`, step names, `uses:` steps, matrix legs and
step order. A release job could run byte-identical commands behind `if: false`
and still count — stated in the test's own docstring rather than left for the
next review to find.

## 2026-08-05 — stdio was removed rather than kept beside HTTP

The MCP surface moved onto the server `nodum serve` already runs: `POST /mcp`,
streamable HTTP, the same origin and the same process as `/api` and the web UI.
The stdio serve command and the whole mcp command group went with the transport
they existed for. They are named in plain prose here, without backticks, and
that is the convention this log now follows for anything it records the removal
of: `test_every_command_the_docs_name_exists` reads code spans across the docs
and refuses a command that no longer resolves. The gate is right to — a reader
who copies one gets "No such command" — and an append-only decision log is
exactly where removed names accumulate, so the log spells them the one way that
is not an invitation to type them.

Keeping stdio alongside was the obvious cautious answer and it was the wrong
one. The cost of two transports is not the second adapter, it is that **every
invariant this surface holds has to be held twice** and can be true on the one
under test while false on the one in use — the registry-disjointness assertions
most of all. Removing it left one code path for the principal, one surface for
the tier-absence tests, and no launch-time branch to keep in step with the
per-request one.

The removal was outright, not a deprecation stub. The command ships on PyPI at
`v0.12.1` and sat in ten launcher configs, but the only consumer of those
configs is the workspace that repointed them in the same change, so there was
no third-party user to strand and a stub would have been dead code carrying a
migration message nobody would read.

What it costs, stated rather than discovered later: the endpoint is only alive
while a server is running. Under stdio the *client* started the process on
demand, so a laptop workflow had no daemon to keep up and now does. And the
token stopped being a server-side environment variable — it is purely a
client-side credential presented as `Authorization: Bearer`, which means it
crosses a socket on every call rather than sitting in a subprocess environment.
Remotely that socket is TLS-terminated at the proxy; locally it is loopback.

Three consequences fell out of one process serving many agents:

- **Identity is per request, never cached.** A principal held between calls
  would be somebody else's. `_principal` re-reads the header off the SDK's
  per-request context every call, which also keeps revocation
  verification-time.
- **The credential is checked twice on purpose.** `BearerGuard` refuses the
  *request* with a 401 before `initialize` or `tools/list` answers, so an
  unauthenticated peer cannot enumerate the surface; `_principal` decides who
  is speaking for the *call*. The guard is the door, not the identity.
- **`RequestGuardMiddleware` exempts `/mcp` from exactly the two checks it
  exempts capability URLs from**, and for the same reason: a bearer token is
  not an ambient credential, so there is nothing for a cross-origin page to
  ride. The `Host` check and the body ceiling still apply. The exemption is one
  constant path, not "requests carrying an `Authorization` header" — that shape
  would let any route opt out of CSRF protection.

Two smaller calls, both made because the alternative fails silently rather than
loudly. `stateless_http` is **on**: a deployed instance is restarted and
redeployed under short-lived agent processes, and each request carries its own
credential already, so a session id that has to survive between calls is state
both ends would have to keep. And the SDK's DNS-rebinding host list is
**derived from nodum's own** `resolve_allowed_hosts` rather than configured
separately — FastMCP defaults to loopback-only, which refuses every request on
a deployed host and looks like a broken deployment rather than a policy. One
policy, two enforcement points.

The trap worth recording: `streamable_http_app()` returns a Starlette app whose
lifespan starts the session manager, and **Starlette does not run a
sub-application's lifespan**. A route wired without it answers 500 on every
call while the route table looks perfectly correct. `http_surface()` returns
the route *and* the lifespan for that reason, and the test helpers enter the
app's lifespan explicitly — without it every negative assertion in those tests
would pass against a route that is simply broken.

## 2026-08-19 — configuration became a ladder, and the file layer is the new rung

Every `NODUM_*` value was read from `os.environ` at the moment it was needed.
That is the right answer for a deployment secret and the wrong one for a knob an
operator turns: changing a model, a budget or the nightly schedule meant editing
a compose file and restarting the container. `nodum.settings` adds one layer —
`settings.env`, beside the database — and the ladder is
`default < settings.env < environment`.

**The environment wins, and empty is not set at any layer.** Both halves are
forced by the deployment that exists: `origin/main`'s compose file renders six
of its variables as `${VAR:-}` pass-throughs and one as a bare interpolation,
all of which produce keys that are **present and empty** in the container's
environment when the host `.env` says nothing. Ten of the nineteen names are
present there and seven of those are empty, so a precedence keyed on *presence*
would have pinned all seven to the empty string and made the file unreachable on
the one instance it was built for. Presence is not the signal; a non-empty value
is.

**The path is threaded in, not re-derived.** `settings.bind(db_path)` takes the
path the caller already resolved. The global `nodum --db` sets `NODUM_DB`, but
`nodum serve --db PATH` does **not**, so a module that read the environment for
itself would have served one graph while reading configuration beside another —
and the frontend's own e2e fixture spawns exactly that shape, which would have
written a 0600 file carrying an API key into the developer's real
`~/.local/share/nodum/`. `:memory:` has no directory and is refused rather than
resolved to the process's working directory.

**The write path is the whole of the file's integrity.** Validation runs
*before* the write, so the file never holds a value the runtime would read back
and discard — the accepted-but-inert edit was reachable through three different
doors, because this codebase has two incompatible bad-value postures already
(`nodum.agent` falls back to the default, `nodum.llm` refuses and kills the
provider) and the scheduler has a third (announce and ignore). The registry
records which of the three applies per key, and `nodum config list` reports it.
Control characters and newlines are refused outright: written verbatim into a
`KEY=value` file, a newline is a second setting chosen by whoever supplied the
value. The bytes land through an `O_EXCL` 0600 temp file **in the same
directory** (a rename across a mount boundary is `EXDEV`, and the deployment
bind-mounts `/data`), `fsync`, `os.replace`, then `fsync` on the directory —
without the last one the rename itself can be lost and the *old* file, carrying
the old key, comes back. Concurrency is `fcntl.flock` on a sibling lockfile
**plus** an in-process `threading.Lock`, held across the whole
read-merge-render-replace span: flock does not serialise two threads of one
process and a threading lock does not serialise two processes. The kernel drops
an flock when its holder dies, which is why there is no pid-and-age apparatus —
and a pid inside a container's namespace is not one anybody outside it can
check.

**The cache is keyed on the file's identity, compared with `!=`.** The stamp is
`(st_dev, st_ino, st_mtime_ns, st_size)`, taken by `fstat` on the same
descriptor the bytes were read from, so the stamp and the bytes are the same
inode by construction. `>` would have been wrong rather than merely narrow:
`os.replace` installs a new inode whose mtime can be *older* than the one it
replaced — a file restored from a backup, a coarse-granularity mount — and a
`>` comparison calls that "unchanged" forever. A missing file is a state that
bumps the generation, not a silence. The comparison happens **before** the read
rather than after it, which is what makes the cost claim true: an unchanged
resolution is one `open` and one `fstat`, no `read` and no bytes. Stamping and
then reading the whole file before comparing — the first cut — was two reads and
the file's contents per resolution, under a docstring promising one `stat`. And
the generation the comparison feeds is **process-wide**: per store, each one
started at 0, so a process that rebound from one graph to another produced two
counters that both reached 1, and a cache comparing a bare integer went on
serving the first graph's provider while every other surface had followed the
rebind.

**A reader must never wait on a writer.** The first cut used one lock for the
cache and the write span, so every `resolve` waited on whatever process held the
file — measured at 1.8 s against a child holding the `flock`. The readers are not
incidental: the scheduler reads on the event loop every slice, and
`llm.resolution()` reads while holding its own lock, so one stuck writer stalled
the loop and every path to the model behind it. The locks are now split — the
write lock across read-merge-render-replace, a separate short-lived cache lock
only for the swap, always taken in that order.

**Splitting them turned the cached view into a shape problem, and the third
pass over it changed the shape rather than patching it again.** Under one lock
the whole read-parse-publish span was atomic; under two, a refresh can be
overtaken between its read and its publish — and a publish that checked only
the file's stamp against the one the cache held wrote a *superseded* reading
back over a newer one, taking the higher generation as it went, so the cache
named an older file with a newer number. Four findings across two review rounds
were one defect wearing different clothes: a reader pairing one reading's values
with another's generation, a report refreshing once per field and reading a
third straight off the store, a slow parse published over a fresh one, and the
write path retiring the stamp outside the lock altogether. The view is now a
single frozen record — stamp, values, unknown keys, unreadable reason,
generation — held by one reference and published by **compare-and-swap** against
the record the refresh started from. A reader loads that reference once and
every field it reads is one moment by construction; the thread that loses the
race discards its reading. The invariant a later edit cannot forget is the one
the type enforces, not the one a docstring asks for.

**A descriptor closed twice is a descriptor some other thread now owns.**
Putting the temp file behind a buffered writer — a bare `os.write` may take
fewer bytes than it was given, and the short write would have been fsynced and
renamed into place as a truncated settings file — moved the `os.close` ahead of
`os.replace` while the `except BaseException` handler still closed it too. Any
failure of the replace (`EXDEV` across a mount boundary, `EROFS`, a sticky
parent, a swept temp file) or an interrupt between the two closed the same
number twice, and `contextlib.suppress(OSError)` hid the `EBADF` without
stopping the call: whatever another thread had opened in the meantime and been
handed that number was closed under it. The close now lives in its own `finally`
and runs exactly once on every path — the shape the backup copy in `cli.py`
already had.

**The provider caches were not serialised by the event loop, and the first
liveness mechanism did not work.** `run_in_threadpool` puts search, ask,
summarize and cycles on worker threads, so the generation check is a genuine
multi-threaded read-modify-write. Stamping the generation *after* resolving —
the natural writing — consumes a write that lands during resolution and pins the
stale provider permanently, with no self-heal. The generation is therefore
sampled **before** `_resolve_default` and that sample is what gets stored, the
whole compare-resolve-publish runs under one lock, and the four `nodum.llm`
globals became one frozen `Resolution` object rebound in one assignment, because
a caller reading three of four globals could otherwise get one resolution's
provider with another's reason. `set_provider` **pins**: the suite's autouse
guards are `set_provider(None)` calls made precisely so no test reaches a
developer's local ollama or a paid API, and a generation check that ignored the
pin would have discarded them at the first settings write in any test.
`nodum.embeddings` got the same shape with one deliberate difference — it
invalidates on a snapshot of its own three values rather than on the file's
generation, because *constructing* that provider loads a model, and a rule keyed
on the file would let a budget write trigger a fastembed load on whatever thread
asked next.

**A malformed line is named, never quoted.** The parse refusal carried the
whole offending line, and both consumers published it: the refresh logs it at
ERROR into the container's unrotated log, and the write path raises it through
the CLI's error boundary onto the operator's terminal. The likeliest malformed
line in this file is `export NODUM_LLM_API_KEY=sk-…` — a habit carried from a
shell profile, whose space breaks the key shape — so the shape that fails most
often is exactly the one holding the credential. The message now names the line
number, and the key only when the whole of the text before the first `=` is a
key (optionally behind `export`); everything from the `=` onwards is dropped
unread. Naming the last token of that prefix instead would have been more
generous and wrong: a pasted `Bearer sk_abc=…` would have had `sk_abc` named.

**The scheduler holds its target instant instead of re-deriving it.** Slicing
the sleep introduced a way to lose a night outright: a slice that overruns — a
stalled event loop, a suspended host, a long `to_thread` hop — wakes past the
scheduled minute, `seconds_until` answers about tomorrow, and the cycle silently
does not run. Measured with virtual time: a 90 s stall at 02:58 against an 03:00
schedule skipped the night, where the single long sleep it replaced ran it late.
The instant is computed once, held across slices, and compared against the
clock; it is **aware**, because `seconds_until` measures real elapsed seconds
and adding those to a naive local datetime lands an hour out on the two nights a
year the local day is not 24 hours long. A run that starts more than a slice
late says so in the log.

**The scheduler stopped being rebuilt and started being re-read.**
`NODUM_CONSOLIDATE_AT` was read once, at app construction, and by nothing
afterwards, so a schedule written from anywhere applied at the next restart.
The obvious fix — stop the scheduler, build another, start it — was measured
into two live schedulers over one database under two concurrent writes, with the
loser orphaned and unreachable from the app that would have to stop it, and the
write that returned 200 not being the one that won. The loop now sleeps in
slices of at most 60 s and re-reads the schedule on each one; the task is always
created, and an unset schedule is an idle loop rather than a missing object. Two
things fall out that no longer need special-casing: there is nothing to rebuild,
so there is no race, and "no scheduler exists because none was configured" stops
being a state. The cost is one wakeup a minute. The last slice before a run is
the exact remainder, so the DST arithmetic is untouched.

**The never-print rule for the API key was introduced here, not extended.** The
design cited an existing one at `llm.py:1205-1208`; those lines are a
`provider_id` property returning a base URL, and a grep for `never print` or
`redact` across the package, the docs and `AGENTS.md` finds one hit, about the
database path. So the invariant is structural rather than inherited: a
`SECRET_KEYS` frozenset, a `Change.event_payload()` that is the only way to
build an audit payload, and a sweep test over the real CLI's stdout, stderr and
the event log.

**What `nodum backup` covers.** The design cited backup posture as a reason
*for* a file beside the database, and backup was `VACUUM INTO` on the database
alone — so a by-the-book restore ("replace the database file") would silently
revert every stored setting including the key. `backup` now copies
`settings.env` to `<dest>.settings.env` at 0600 and names it in the result, and
`docs/deploy.md` says restore is two files.

Four names refuse storage and resolve from the environment alone, each for its
own reason: `NODUM_DB` (read before the graph, and therefore before its settings
file, is open), `NODUM_LLM_BASE_URL` (the endpoint an API key may travel to is a
deployment decision), `NODUM_EMBED_CACHE` (a path on the server's own disk), and
`NODUM_PUBLIC_URL` (every capability URL is minted from it, so a stored value
would redirect them). `NODUM_EMBED_MODEL` and `NODUM_EMBED_DOWNLOAD` are not on
the surface at all yet — changing the model invalidates every stored vector.

## 2026-08-22 — the settings surface reached HTTP, and a pin became a 409

The file layer shipped with one door, the CLI. A browser needs its own, and
with it three decisions that were not obvious from the CLI's shape.

**A pin is a conflict, not a bad request.** `config set` refuses an
environment-pinned key with a sentence; over HTTP that sentence has to travel
with a status a client can act on, and "your request was malformed" (400) is
the wrong advice — the request is well formed, the file is simply not the layer
in force. So `SettingPinned` subclasses `SettingRefused`, `EXCEPTION_STATUS`
gives it a 409 row resolved by MRO, and the CLI boundary is untouched because
it reads only the `ValueError` text.

**A multi-key write is all or nothing.** One-key-at-a-time `set_value` calls
can land the first key of a batch and refuse the second — a half-applied form.
`settings.apply` validates every key (registry, pin, empty, control characters,
validator) before anything moves, then renders all of them inside the single
write-lock + flock span. The one sanctioned waiver of the pin check inside
`apply` is adopt-env: its whole job is to store keys the environment currently
pins, which is exactly what every other caller must be refused.

**Adopt does not move provenance.** It writes environment values into
`settings.env` and stops there. Unsetting the variable host-side is what flips
the ladder to the file layer, and that stays a deliberate deployment step —
otherwise a cutover verb and a configuration change would be the same button
press. What adopt changes immediately is only `stored`, so the operator can see
the value is now backed by the file before they touch the environment.
