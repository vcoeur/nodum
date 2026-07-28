# nodum web UI

The human web UI for nodum (Phase 3, plus Phase 5's dream journal). React 19 +
TypeScript, built by Vite into
`../nodum/_web/`, which the Python process serves at `/` — one process, one
origin, no CORS.

Eleven views ship: **login** (`/login`), **editor** (`/editor`,
`/editor/:nodeId`), **search** (`/search`, also the landing view), **review**
(`/review`), **journal** (`/journal`) with one entry in full
(`/journal/:cycleId`), **graph** (`/graph`, `/graph/:rootId`), **assets**
(`/assets`), **spaces** (`/spaces`), **admin** (`/admin`), and **history**
(`/history/:nodeId`).

**A space is two independent controls, not a mode** (design decision D1), and
that shape runs through most of the tree: a *read filter* per view, defaulting
to every space in scope, and one app-wide sticky *write target*, defaulting to
`main`. Reading `research` while still filing into `main` is the ordinary case,
so a single switcher could not have served both. The shared pieces are
`components/SpaceFilter.tsx` (the read control), `components/useSpaces.ts` (the
one `GET /api/spaces` read behind every one of them) and `lib/writeTarget.ts`
(the write half). `/spaces` is where the lifecycle lives — create, rename,
archive, with each space's live node count and grant holders.

Reading a space and *seeing* one are different jobs, and the second has its own
rule: **a surface states the space unless the filter already determined it.**
Search names it per result under "Any space" and stays quiet under a narrowed
one (`views/search/resultSpace.ts`), exactly as the state badge does.

Naming one is the harder half, because `GET /api/spaces` is **active-only by
decision** — it is the vocabulary behind every picker, and a retired space
belongs in none of them. Plenty of surfaces still have to name a space that
listing will not carry: the review queue (a space archived while its proposals
waited), search, the editor's meta bar, the graph inspector, `/admin`'s grant
table (archiving makes a grant inert and keeps the row precisely so it can be
revoked), the write-target and space-filter pickers, **the journal** (a cycle's
`scope` is the *resolved space id* — `open_cycle` runs the reference through
`_resolve_space` before the row is written — so every scoped entry reports an id
and nothing else), and every sentence the
editor and search write about a write target or filter the server refused. They
all go through `components/spaceNaming.ts` over two lists — the shared active
one and `components/useArchivedSpaces.ts`, a lazy read of the archived space
nodes in meta that fires only when something on screen actually needs it. Two
sentences built on those answers live in the same module, because each has more
than one caller and both sit inside the never-say-it-does-not-exist rule:
`spaceNameNote`, and `writeTargetWouldNotResolve` — the *"the write target
`reading` has been archived …"* sentence that the editor's create path and the
assets drop-zone both write, and had already drifted into two wordings before it
was promoted.
`spaceLabel` is **not** that function: its fallback to the raw reference is a
picker rule (a `<select>` must be able to render its own value), it prints a
32-hex id anywhere else, and it is no longer exported from `components/` — its
one caller is `spaceOptions`, in the same file. The review queue additionally
has to admit that a cross-space edge filed under one space needs authority on
two — `grouping.edgeCrossing`.

**Naming an archived selection never makes archived spaces choosable.** That
is the line the picker walks, and the seam is built so it cannot be crossed by
accident: `spaceOptions` takes the active list plus, at most, *one
already-resolved `SpaceName` for the value it is already carrying*. It never
receives the archived list, so there is nothing inside it to offer. The one
option it adds beyond `spaces` is the current selection — because a controlled
`<select>` has to render its own value — marked `(archived)` when the archived
listing named it and `(unavailable)` when nothing did, and gone the instant the
human selects something else. Handing that module a list instead of a name
would let someone newly pick a space the server refuses to resolve, which is a
worse bug than the bare id it fixed.

## Two drops, two routes

Dropping a file has two meanings in this app, and the file's *type* is not what
tells them apart — what the human is doing is (design decision D1):

| Act | Route | Who describes the bytes |
|---|---|---|
| Put a picture in the prose I am writing | `POST /api/assets` | the note that carries it inline |
| Turn this document into knowledge | `POST /api/uploads` → `PUT /api/uploads/{token}` | ingestion, as `asset_ref` + `source` + `derived_from` + one `block` per page |

The **editor's** drop is the first: it inserts a rendition URL into the Markdown
and nothing else, and its own copy already routes everything else away. It keeps
`POST /api/assets`, which registers bytes and stops — and admits rasters alone,
because those bytes get inlined and rendered as an image.

The **assets page's** drop-zone is the second, and it is the whole of
`client.ts`'s `ingestUpload`: mint a single-use grant, then spend it. Bytes with
no describing node are readable by humans and by nobody else and carry no
`node_fts` row, so a document registered through the other route is invisible to
agents and to search — which is why widening that route was the wrong fix and
the capability flow is the right one. Images go through it too, deliberately: an
image with no OCR handler yields a description and no text, exactly as
`nodum ingest file <image>` does, and branching on the type inside one drop-zone
would rebuild the split one level up where the human cannot see which of the two
things happened.

Three rules that are easy to get wrong there, all pinned by tests:

- **redeem on our own origin.** The grant's `url` is absolute and built from
  `NODUM_PUBLIC_URL`, which exists for a foreign host and may name another
  machine; the browser uses `grant.token` against `/api/uploads/{token}` here.
  The client owns its origin, the grant carries only the capability;
- **declare no `sha256`.** A declared hash the store already holds is answered
  with the existing asset and no grant at all, and that shortcut proves the
  *bytes* exist rather than that anything *describes* them — a file registered
  earlier through the editor is exactly the undescribed case, so declaring it
  would silently skip the ingestion the human asked for;
- **statuses come from the server.** `created: true` is *ingested*,
  `created: false` is *already ingested*; there is no client-side hash
  bookkeeping, and `views/assets/uploadOutcome.ts` is where that reading and the
  detail-line wording live. Two slots on that line belong to one thing each: the
  landing phrase reports a *filing*, so the already-ingested branch says
  "Already described in `research`" rather than claiming a write nothing made,
  and the "why no text" slot belongs to the **extraction** — on the
  already-ingested branch the server puts its own idempotency note in
  `extraction.detail`, and letting it through had the second drop of an OCR-less
  PNG read *"no text extracted — already ingested into this space"*: a causal
  claim that is false, in the slot where *"install the `ocr` extra"* had been.

Three more, about the queue rather than the wire:

- **one batch at a time.** Ingestion holds the single SQLite writer for a
  registration, an extraction and one `create_node` per page, and a second
  concurrent batch would contend for it while the first to finish cleared `busy`
  and refreshed the grid under the other. A drop arriving mid-batch is refused
  and **said so** — files disappearing on a drop is worse than the loops it
  prevents;
- **an abort stops the readout, and only sometimes the write.** Between the two
  requests it leaves a minted, unspent grant and nothing else. After the last
  chunk has left it stops nothing server-side: `urls.consume` has spent the
  token, and the refusal check and `ingest_upload` then run synchronously with no
  disconnect check, so the bytes and the whole subgraph land while the queue has
  stopped listening — the human is left with no row, no verdict and no link to
  something that exists. Closing that would mean owning the queue above the view
  so a batch survives navigation, which has not been done;
- **the bookkeeping is a plain module.** `views/assets/uploadQueue.ts` holds what
  a drop becomes (`nextBatch`, which freezes the write target per batch), what
  each status is called (`statusLabel`, total over the status union by
  construction), what a refused drop says, and the one per-batch announcement a
  live region over the rows was drowning.

Because the drop-zone creates nodes, it is also a D1a surface: it **shows** the
write target, files into it, and names the space the server actually filed into.
A refused target goes through `describeUploadFailure`, never through
`describeFailure` — which would print *"The server has no record of …"* about a
space. And when the target it is showing is one the server will already refuse,
it **warns before the drop** in the same register as the editor's meta bar —
*"Every drop will be refused until another space is chosen"* — with a `<Link>` to
`/editor`, where the picker lives. A badge and nothing else let a human drop a
whole batch into a target this panel already knew would fail every row.

## Running it

```bash
make web-install          # once — npm ci in web/
make web-dev              # Vite dev server on http://127.0.0.1:5700
uv run nodum serve        # the API on http://127.0.0.1:8600, in another shell
```

The dev server proxies `/api` and `/healthz` to `127.0.0.1:8600`, so develop
against `http://127.0.0.1:5700` and let the proxy reach the Python process.
The header shows a `connected` / `no server` pill so you can tell at a glance
whether the backend is answering.

For the packaged path:

```bash
make web-build            # tsc --noEmit && vite build -> ../nodum/_web/
uv run nodum serve        # now serves the real bundle at /
make web-clean            # drop the bundle, restore the placeholder
```

`npm run build` type-checks first, so a type error fails the build. CI runs the
same thing in the `Frontend build` job.

## Testing

```bash
make web-test             # or: cd web && npx vitest run
cd web && npx vitest      # watch mode while working
```

[Vitest](https://vitest.dev) over the **pure modules** in `src/` — no component
rendering, no `@testing-library`. A test lives beside the module it covers
(`src/lib/time.test.ts`), and `vitest.config.ts` is kept separate from
`vite.config.ts` so a test setting can never change what ships in the wheel.

The default environment is `node`. One suite opts out —
`views/editor/markdownRender.test.ts` sets `// @vitest-environment jsdom` in its
own docblock, because the preview's sanitising policy *is* a parse and asserting
it against strings would assert against the wrong thing. The opt-out is per
file; the global config stays `node`.

Covered today: `api/client.ts` (the unknown-space normalisation and the
two-request capability upload — the only logic in the file that is not a URL and
a verb), `lib/time.ts`,
`lib/failure.ts`, `lib/session.ts`, `lib/paging.ts`, `lib/writeTarget.ts`,
`components/spaceOptions.ts`, `components/spaceNaming.ts`,
`views/failureRouting.ts`,
`views/graph/filters.ts`, `views/graph/graphElements.ts`,
`views/graph/truncation.ts`, `views/history/unifiedDiff.ts`,
`views/search/signals.ts`, `views/search/searchState.ts`,
`views/search/spaceFailure.ts`, `views/review/grouping.ts`,
`views/spaces/spaces.ts`, `views/admin/grants.ts`,
`views/search/resultSpace.ts`, `views/login/credentials.ts`,
`views/editor/createOutcome.ts`, `views/journal/journal.ts`,
`views/assets/uploadOutcome.ts`, `views/assets/uploadQueue.ts`,
`views/editor/markdownRender.ts` + `views/editor/mermaidRender.ts` (the
sanitising policies), and `views/editor/leftoverBuffer.ts`.

`components/useSpaces.ts`, `components/useArchivedSpaces.ts` and
`views/journal/useNodeTitles.ts` are deliberately
**not** in that list: they are hooks, and the harness renders nothing, so there
is no honest way to drive them here. Their behaviour is verified by
type-checking and in a browser, like every component — but the *rule* that
decides what each of them fetches is a plain function with a test
(`unresolvedSpaceIds`, `referencedNodeIds`, `verdictNodeIds`), precisely because
getting it wrong is invisible until you watch the network panel. `useNodeTitles`
has two callers for that reason: the event diff asks for the endpoints on the
page it is drawing, and the rollback dialog asks for the endpoints and
dependants of the rows its verdict names — a clean verdict names none and fetches
nothing.

**The run pins `TZ` to `Asia/Kathmandu`, and this matters.** SQLite's
`datetime('now')` is UTC with no zone marker, so the bug `lib/time.ts` fixes —
`new Date(s)` reading it as local time — produces *the same instant* on a UTC
machine. Every CI runner is UTC, so a test that trusted the ambient zone would
pass while the code was broken. The pin makes the bug visible; `time.test.ts`
asserts the pin took effect, so dropping it fails the suite rather than quietly
turning it into a tautology. Measured: with the normalisation removed from
`parseTimestamp`, 12 of the 20 timestamp tests fail under the pin and only 4
under UTC.

Kathmandu specifically because it is UTC+05:45 with no DST — a non-integer
offset catches an hours-only assumption as well as a zone-less one, and the
expected instants do not move between summer and winter.

Since the harness is unit-only, anything React renders is still verified by
type-checking it and driving it in a browser.

## Module map

| Path | What lives there |
|---|---|
| `src/main.tsx`, `src/App.tsx`, `src/router.tsx` | entry, app shell (header, nav, toasts, crash boundary, health pill), route table |
| `src/api/client.ts`, `src/api/types.ts` | the only `fetch` in the app, and the types mirroring `nodum/models.py` |
| `src/lib/` | cross-view plain functions: timestamp parsing (`time.ts`), failure classification (`failure.ts`), the 401 broadcast (`session.ts`), which slice of a long list to render (`paging.ts` — the journal's event diff and the review queue), and the sticky write target (`writeTarget.ts`, the one module here that also exports a hook) |
| `src/components/` | shared React components: `NodeBadge`, `Toast`, `Spinner`, `EmptyState`, `ErrorBoundary`, `Modal`, plus the whole space vocabulary — `SpaceFilter.tsx` with `spaceOptions.ts` (what a picker offers, which is the active list and never more) and `useSpaces.ts` (the `GET /api/spaces` read every space surface shares), and `spaceNaming.ts` with `useArchivedSpaces.ts` (what a surface that *displays* a space calls it, including one the active listing does not carry — and what names an archived value a picker is already holding) |
| `src/styles/` | `tokens.css`, `base.css`, `primitives.css`, `app.css` |
| `src/views/editor/` | CodeMirror-6 Markdown source editor, slash commands, `[[` autocomplete, live Mermaid preview, autosave, the write-target picker and the landing/refusal copy (`createOutcome.ts`) |
| `src/views/search/` | query box, ranked hits, per-signal breakdown, signal grouping, the space filter + show-meta toggle in the URL (`searchState.ts`), refused-filter copy (`spaceFailure.ts`), when a row names its space (`resultSpace.ts`) |
| `src/views/review/` | proposal queue grouped space → agent, per-kind cards, proposed-version diffs, self-governing space sections, cross-space edge marking (`grouping.edgeCrossing`), and the pager over it (`grouping.sectionOrder` / `restrictToPage`, over `lib/pageWindow`) with the honest count above it (`grouping.queueCount`) |
| `src/views/journal/` | the dream journal: cycles as sentences, one entry with its job report, the five coherence metrics before/after, the events it wrote as a paged diff, the run-now control, the abandon confirm for a cycle a crash left `running`, and the dry-run-then-confirm rollback — whose verdict is **two** lists, `conflicts` and `blockers`, and is clean only when both are empty (`journal.ts` owns every sentence and every reading of the untyped `report` blob; `useNodeTitles.ts` names the nodes an edge event points at) |
| `src/views/graph/` | Cytoscape subgraph render, filters, cross-space edge styling and far-endpoint dimming, path panel |
| `src/views/assets/` | rendition grid, lightbox, the ingesting drop-zone with its queue readout (`uploadOutcome.ts`) and its bookkeeping (`uploadQueue.ts` — batches, status labels, the refused second drop, the per-batch announcement), thin JSON export |
| `src/views/login/` | password login against `POST /api/login` |
| `src/views/spaces/` | the space lifecycle: list with node counts and grant holders, create, rename, archive — and `spaces.ts`'s `archiveConsequences`, the one place the archive dialog's promises are written, every line of which has to be something the server actually delivers |
| `src/views/admin/` | accounts and grants administration, show-once token dialog |
| `src/views/history/` | per-node version timeline and side-by-side diff |
| `package.json`, `vite.config.ts`, `vitest.config.ts`, `tsconfig.json` | toolchain |
| `**/*.test.ts` | Vitest unit tests, beside the module each covers |

Conventions that hold across the tree:

- **A view's entry component keeps a default export.** Routes are lazily loaded,
  which is what keeps CodeMirror, Mermaid, and Cytoscape out of the initial
  bundle; `lazy()` needs the default export.
- **View-local components, hooks, and CSS live in the view's own directory**,
  e.g. `src/views/graph/GraphToolbar.tsx` and `src/views/graph/graph.css`.
  Import the CSS from the component, not from `src/styles/index.css`.
- **Views never import each other.** They link by URL. `router.tsx` is where
  those paths are defined and is the only place they are written down twice —
  grep for the path string before renaming one.
- **Promote to `src/lib/` or `src/components/` when a second view needs it**,
  not before. Both are inherited by every view, so a change there is a change
  everywhere.
- **Timestamps go through `src/lib/time.ts`.** SQLite's `datetime('now')` is UTC
  with no zone marker, so `new Date(row.created_at)` reads it as *local* time
  and prints the wrong clock. `parseTimestamp` normalises it; nothing else may
  call `new Date()` on a server string. (`new Date()` on a client-side epoch
  number — "saved at", "checked at" — is fine.)
- **Failures go through `src/lib/failure.ts`.** `describeFailure` is the one
  place that tells *the API refused this* apart from *nothing was listening*,
  which is not a single test: same-origin an unreachable server is a `fetch`
  `TypeError`, but behind the dev proxy it is a **502** and therefore an
  `ApiError`. Views map its `kind` onto their own panels; they do not re-derive
  it.
- **A refused space is `isUnknownSpace`, and only that.** `api/client.ts`
  normalises the refusal on every call that names a space — the two filtered
  reads, the write target on `createNode`, all three lifecycle calls, both
  halves of the upload, and a cycle's `scope` on `runCycle` — into one
  `UnknownSpaceError`. Two views once kept their
  own `^unknown space:` match because the write path was not wrapped; both are
  gone, and a third would be the bug, not the belt. If a call ever throws a bare
  `ApiError` carrying that message, fix the client. Facts a view needs *beyond*
  space-ness ride on a subclass rather than on a second test:
  `UnknownUploadSpaceError.phase` says which of the upload's two requests refused,
  and `isUnknownSpace` answers true for it too.
  One refusal never arrives as a caught response at all: a cycle **records** its
  failures as strings (`f"{type(failure).__name__}: {failure}"`), and the journal
  renders them hours later. `recordedUnknownSpace` reads those, and it is the
  *same* regex rather than a second copy — which is exactly why it lives in
  `api/client.ts` beside `isUnknownSpace` and not in the view that needs it.
  `recordedUngrantedScope` is its sibling and reads the **second** shape that
  names a space: `consolidate._require_gardener_scope`'s *"the gardener holds no
  grant on space '…'"*, which is the refusal a default install meets on the first
  click of the journal's scope picker (migration `0014` grants the gardener
  `main` and `meta`; the picker offers everything) and which echoes the caller's
  reference — a 32-hex id for the one caller that arrives by clicking. It matches
  a **live** `ApiError.message` too, because `http_api._failure_message` exempts
  this package's own exceptions from the storage rewrite.
- **No server text reaches a screen carrying a raw id, known shape or not.**
  Two message shapes have now been printed verbatim, so `journal.ts` does not
  keep a list of rewrites to extend: the two refusals whose *wording* is a
  decision get named copy, and every other server sentence the journal renders —
  a recorded cycle failure, a job's own `error`, a delete guard's `reason` —
  goes through `journal.nameIdsIn`, which replaces each `[0-9a-f]{32}` with the
  page's own name for that row or its shortened form. A third shape can carry an
  id; it cannot put one on the screen.
- **Never say a space does not exist.** Nothing user-facing may render "no such
  space", "does not exist", "unknown/missing/nonexistent space", or "not found"
  for a space failure — including by handing an `UnknownSpaceError` to
  `describeFailure` **or to `toast.showError`**, which formats an `ApiError` as
  `type: message` and so renders the server's own *"UnknownSpace: unknown
  space: research"* verbatim. That second trap is the easy one to miss: an
  editor path that reports through a toast rather than an inline panel looks
  nothing like a copy decision. `describeFailure`'s 404 body is *"The server
  has no record of …"*. The
  server answers a space that was never created and a space the caller holds no
  grant on with **the same words on purpose**; copy that resolved the ambiguity
  would turn the filter into an existence oracle over spaces the reader cannot
  read. Say what changed instead — a space stops resolving once it is archived,
  and a renamed one stops answering to its old name. `views/search/spaceFailure.ts`,
  `views/editor/createOutcome.ts`, `views/spaces/spaces.ts` and
  `views/journal/journal.ts`'s `describeRunFailure` **and
  `describeRecordedFailure`** — a cycle's `scope` names a space, both when the run
  is refused now and when the report says it was refused then — own that copy and
  pin it with tests. The journal's headline (`cycleWork`) is the harder half of
  the same rule and is solved by construction: it is built from counts and
  registered names and **quotes no server text at all**, the reason moving to
  `cycleFailures` one line below, where the copy rules reach it. `journal.test.ts`'s
  `FORBIDDEN` guard therefore runs over every branch of `cycleWork` rather than
  over one function's happy path — it used to cover `describeRunFailure` alone,
  while the sentence that actually broke the rule was three functions away.
  The one refusal that *does* name a space — creating one
  whose name an **archived** space still holds (a space title is reserved for
  good) — is not an exception to this: it is the server's message, shown
  verbatim, and creating a space means writing `meta`, which is exactly the
  grant that already lists every space node there. The server pins that premise
  as its own test.
- **The write target is shown wherever it is used** (design decision D1a). It is
  app-wide, sticky across sessions, and synchronised across tabs
  (`lib/writeTarget.ts` listens for `storage`) — all of which make it a way to
  file work into a space nobody chose if it is ever read without being
  displayed. `useWriteTarget()` is the subscription; anything calling
  `getWriteTarget()` without rendering the answer is the failure the module
  exists to prevent. `clearWriteTarget()` is the one reset, with two callers:
  `/spaces` when the human archives the space they are filing into, and the
  shell on **log out** — the value is persisted per browser, not per session,
  so leaving it would hand the next account a target it never chose.
- **One space list.** `components/useSpaces.ts` is the shared
  `GET /api/spaces` read: the filter's vocabulary, the write-target picker, the
  grant picker, the review queue's self-governing sections, and `/spaces` all
  take it from there. It drops its list on failure rather than keeping a stale
  one, and exposes `error` for the one view that escalates (`/admin` cannot
  offer a grant without spaces to grant over) rather than degrades.
  **It stays active-only.** A surface needing to name a retired space takes the
  lazy `components/useArchivedSpaces.ts` instead; widening this endpoint would
  put archived spaces into six pickers to fix a label on one screen. Pass
  `spaces` to `nameSpace` / `unresolvedSpaceIds` / `describeWriteFailure` /
  `describeSpaceFilterFailure` **null and all** rather than `?? []` — null means
  *not answered yet*, and reading it as "empty" is what makes a screen claim
  nothing names a live space and fires the archived read on a healthy file.
  A surface derives `needed` for the archived read from **everything it names**,
  the write target and the space filter included: gating only on rendered rows
  left the two controls that most often point at a retired space unable to name
  one.
- **Logic worth testing lives in a plain module, not in a component.** The
  harness is unit-only, so a rule that matters (a URL codec, a diff parser, a
  grant grid) goes in its own `.ts` with a `.test.ts` beside it, and the
  component consumes it.
- **A dialog locks body scroll and hands focus somewhere real.** `review/Modal`
  and `assets/AssetLightbox` both set `body.style.overflow` on open and restore
  it on close. On close each returns focus to its opener *only if the opener is
  still in the document*; when it is not — the usual case after a confirm that
  emptied the selection — the view places focus instead, because focusing a
  detached node lands the user on `<body>`.

## What is already installed

| Package | For |
|---|---|
| `react`, `react-dom` (19), `react-router-dom` (7) | shell and routing |
| `@codemirror/state`, `@codemirror/view`, `@codemirror/lang-markdown`, `@codemirror/commands`, `@codemirror/autocomplete`, `@codemirror/language`, `@codemirror/theme-one-dark` | editor slice |
| `mermaid` | live diagram preview in the editor |
| `marked` | Markdown preview rendering |
| `dompurify` | sanitising the preview's HTML and mermaid's SVG before either reaches `innerHTML` |
| `cytoscape`, `@types/cytoscape`, `cytoscape-fcose` | graph slice |
| `vite`, `@vitejs/plugin-react`, `typescript`, `@types/react`, `@types/react-dom` | toolchain |
| `vitest` | the unit harness — no component-testing stack |
| `jsdom` | the DOM environment the sanitiser suite alone opts into |

`cytoscape-fcose` ships no types; a minimal declaration lives in
`types/cytoscape-fcose.d.ts`.

## The API client

`src/api/client.ts` is the only place that calls `fetch`. It covers the whole
Phase-3 endpoint surface plus the five consolidation-cycle routes, typed, and
every route it names is served by `nodum.http_api`.

What it handles for you:

- covers the **five** consolidation-cycle routes, `POST /api/cycles/{id}/abandon`
  included — the door out of a cycle a crash left `running`, and the
  precondition for the rollback route rather than a tidier journal: rollback
  refuses a cycle that has not closed and `undo` refuses every event a cycle
  stamped, so until the row is closed the run's writes are irreversible on every
  surface;
- prefixes `/api` (`getHealth` is the one exception — `/healthz` sits outside);
- unwraps the `{"<plural>": [...], "count": n}` list envelope, so list calls
  return a plain array;
- raises `ApiError` (carrying `status`, `type`, `message`) on any non-2xx, with
  `isNotFound` / `isForbidden` / `isRetryable` helpers;
- raises `RollbackConflictError` (test it with `isRollbackConflict`) for the one
  failure whose body carries more than `type` and `message`: a refused rollback
  names the rows standing in its way, and those rows exist nowhere else once the
  response is parsed. That is why the branch sits in `toApiError` rather than in
  the calling route, unlike the unknown-space normalisation, which only re-reads
  a message the caller already has. In practice a view rarely sees one — the
  confirm dialog calls the same route with `dry_run: true` first, which answers
  the same list under a 200 — so a 409 means the graph moved between the check
  and the commit. **The dry run answers a second list too, `blockers`**: the
  delete guards, which refuse a rollback for a different reason (something now
  depends on a row the cycle created, so the delete that reverses that create
  would have to cascade). A verdict is clean only when both lists are empty, and
  a caller reading one of them offers a confirm button for a rollback that will
  fail. Only `conflicts` has a 409 body to come back in — a guard met for real
  raises `UndoNotPossible`, one sentence and no list;
- raises `UnknownSpaceError` (test it with `isUnknownSpace`) whenever a call
  that named a space could not resolve it. The wire is inconsistent here by
  accretion — the node listing refuses with a **404** (the service's
  `TypeNotFound`) and search with a **400** (a bare `ValueError`, since
  `nodum.search` does not import the service's exception vocabulary) — so the
  class absorbs it: `status` is normalised to 404 so `describeFailure` gives one
  answer, `wireStatus` keeps what the server said, and `space` names the
  reference. It is keyed on the message, because neither status is specific
  enough alone;
- composes the capability upload: `ingestUpload(file, { space })` mints the grant
  and spends it, and **both** halves normalise a refused space onto
  `UnknownSpaceError` — the pipeline resolves the target again on the far side of
  the PUT, so the second request can refuse it too. Each half does its **own**
  normalising, because each is exported: unnormalised, a direct caller of
  `redeemUploadGrant` has no sanctioned way to recognise the refusal and the only
  thing left is `describeFailure`, which renders a 404 as *"The server has no
  record of …"* alongside the server's own *"unknown space: sp-old"* — two
  forbidden phrasings in one sentence. The redemption reads the reference out of
  the server's message, since it is handed a token and the target lives in the
  token row; `ingestUpload` then re-labels it, because that message names the
  *resolved* 32-hex id rather than the reference the human typed. What the two
  refusals do **not** collapse is *which request said no*:
  `UnknownUploadSpaceError` carries a `phase`, read with `uploadRefusalPhase`,
  and it is a subclass so that `isUnknownSpace` stays the only test for
  space-ness. The phase is load-bearing copy: a refused mint sent nothing, while
  a refused redemption spent the grant and streamed the whole file. Its raw body
  is the client's one non-JSON write: the two capability routes sit outside the
  server's content-type gate precisely so an upload need not claim to be JSON,
  and that is the third branch in `rawRequest`;
- takes an optional `AbortSignal` on every call — use it in `useEffect` cleanup.

```ts
import { api, ApiError } from "../../api/client";
import { useToast } from "../../components";

const toast = useToast();
try {
  const nodes = await api.listNodes({ type: "note", state: "active" });
} catch (error) {
  toast.showError(error); // formats ApiError's type and message
}
```

**Never send an identity.** The HTTP surface is the human surface: the server
attributes every write to the session's human principal, and a client-supplied
identity is ignored. The client has no parameter for one, deliberately.

`src/api/types.ts` mirrors `nodum/models.py` field for field. If you change a
pydantic model, change it here in the same commit.

## Styling

Plain CSS, no framework. `src/styles/tokens.css` holds the custom properties;
use them rather than literal values.

Two conventions carry the whole system:

- **The accent (brass) means "you can act on this"** — focus rings, links,
  primary actions, selection. It never encodes anything about the data.
- **The state ramp encodes the service-layer state machine**: `proposed` violet,
  `active` sea-green, `archived` deliberately the lowest-contrast colour in the
  system. Use `<NodeBadge type state />` rather than rolling your own pill.

Anything outside those two axes needs a hue of its own, kept view-local until a
second view names it. Exactly one has: `--nd-crossing` (magenta) means *this
edge's endpoints live in two different spaces* — neither an affordance nor a
state. It started in the graph (design decision D5) and moved into `tokens.css`
when the review queue had to mark the same fact on a cross-space edge proposal.

Every form control carries an `id` or a `name`. There is no `<form>` submit
anywhere here, so the value never travels; a control with neither is one the
browser cannot address, which is what DevTools flags and what autofill and
assistive tooling are left guessing at. `SpaceFilter` takes `name` as a prop
(default `space`).

Class names are prefixed `nd-` because Mermaid and Cytoscape inject global
stylesheets using names like `.node`, `.label`, and `.edge`. Keep the prefix.

Primitives available: `.nd-button` (`--primary`, `--danger`, `--ghost`,
`--small`), `.nd-input` / `.nd-textarea` / `.nd-select` / `.nd-field`,
`.nd-card`, `.nd-badge`, `.nd-empty`, `.nd-spinner`, `.nd-modal`. Utilities:
`.nd-mono`, `.nd-label`, `.nd-meta`, `.nd-truncate`, `.nd-stack`, `.nd-row`,
`.nd-sr-only`. Layout: wrap your view in `.nd-view` (or `.nd-view--wide` if you
manage your own space, as the graph and editor will).

Reduced motion is honoured globally and `:focus-visible` is styled once, for
everything — do not remove an outline anywhere.

## What the server does for the frontend

Both settled; noted here because breaking either is invisible in dev.

1. **The SPA catch-all.** Client routes like `/graph/:rootId` and
   `/editor/:nodeId` are real URLs a user reloads and bookmarks.
   `StaticFiles(html=True)` serves `index.html` for `/` but 404s on those, so
   `create_app` returns `index.html` for any non-`/api`, non-`/healthz` GET
   instead. The Vite dev server does the same thing, so a regression here would
   only show up in the packaged app.
   **`/favicon.ico` is exempted.** A browser asks for it on its own and it is
   not a client route, so the catch-all answering it with `index.html` under a
   200 told the browser it had received an icon. It now gets the bundle's
   `favicon.ico` if one exists and a **204** otherwise — this page declares its
   icon as an inline data URI, so normally there is no file to serve and the
   honest answer is "nothing here".
2. **The port.** `nodum serve` defaults to `127.0.0.1:8600` and the dev proxy in
   `vite.config.ts` targets the same. Change one and change the other.
3. **`nodum/_web/` is gitignored whole.** Vite's `emptyOutDir` wipes it on every
   build, so nothing tracked can live there. When the bundle is absent
   `create_app` serves `nodum/_web_placeholder.html` — a tracked, packaged "UI
   not built" page that sits outside the output directory.
