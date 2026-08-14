# AGENTS.md — nodum

Agent-facing instructions for working in this repository. Read this before
editing anything here.

## Read this first

`nodum` is a **DB-native knowledge graph**: a typed graph of Markdown-content
nodes and typed edges in one SQLite file (WAL mode), behind a deterministic,
LLM-free service layer. Every mutation is validated, state-machine-checked,
logged in an append-only event log with full before/after payloads, versioned
(nodes), and reversible (`undo`). `[[wikilinks]]` in content are materialized
as `mentions` edges on write; the Typer CLI emits exactly one JSON object per
command; hybrid search fuses BM25 + vector lists by reciprocal rank fusion with
a per-signal `signals` breakdown. Four surfaces share the one store — the CLI,
the HTTP API + web UI, the MCP server for external agents, and an internal
gardener that runs consolidation cycles and files its inferences in the review
queue. The HTTP API and the CLI render through one envelope, so their JSON is
byte-identical.

**How to use this file.** The sections below are the contract: Non-negotiables,
Workflow rules, and one contract per surface (CLI, HTTP, MCP, Frontend). The
measured how-and-why behind the rules lives in `docs/decisions.md`; the
module-by-module architecture in `docs/architecture.md`; the route reference in
`docs/http-api.md`. Contract sections state rules, not rationale — where a rule
was born of a measurement, it links the decision entry instead.

## Non-negotiables

The invariants that must never be broken, whatever the section:

- **uv for everything** — never raw `pip`/`venv`; commit `uv.lock`; `.venv/`
  stays gitignored. Python ≥ 3.12.
- **`make format` after every code change** — CI runs `make lint` and `make
  test` on Python 3.12 and 3.13.
- **Never assert on Typer's rendered error or help panel.** That is Rich
  output, wrapped to the terminal's width and styled by its colour support —
  the stable surface is the exit code and the state the command did not change.
  A flag's presence is read from `nodum schema-dump`, the CLI's own
  self-description.
- **Version comes from the git tag** (`vX.Y.Z` via hatch-vcs) — never bump a
  version in code.
- **Keep adapters thin.** A service operation you add or change is exposed
  through the CLI in the same change, and `README.md`, `docs/architecture.md`
  and this file are updated in the same commit. Adapters add no behaviour the
  service lacks, and no request field the domain cannot represent.
- **Docstrings on public APIs**: one-line summary plus args/returns where
  applicable. Comment the *why*, not the *what*. `cli.py` is exempt — its
  docstrings *are* Typer's `--help` surface.
- **The core is deterministic and LLM-free.** `nodum.service`, the projectors,
  the store and the migrations must never reach `nodum.llm`; `nodum.agent` is
  the one door to the model (enforced over the package's import graph).
- **A node's `type` is fixed at creation by design.** Retyping is a curative
  operation (`nodum retype`), not a field on the update path. **Do not add a
  `type` field to `PATCH /api/nodes/{id}`** — the editor withholds its type
  commands on a saved node for exactly this reason.
- **Deliberately not built — do not add opportunistically**: claim proposals
  (ingestion proposes sources and structure and stops), Markdown Mirror, and
  any whole-graph export (the only export is the per-node snapshot,
  `GET /api/export/node/{id}?depth=`). Each lands as its own append-only
  migration where it needs one.
- **One fact, one home.** Rules live in this file, architecture in
  `docs/architecture.md`, measured decisions in `docs/decisions.md`. Do not
  restate a number the decision log records.

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
- **Never assert on Typer's rendered error or help panel.** Those are Rich
  output: wrapped to the terminal's width and styled by its colour support, so
  an assertion reading them tests the runner's environment rather than the
  behaviour. `test_agent_create_has_no_kind_flag_at_all` asserted `"--kind" in
  refused.output` and was green locally and **red on both CI matrix jobs** —
  same code, same lock file, different terminal. What a usage error offers that
  is stable is the **exit code** (2) plus the state the command did not change;
  what a flag's presence is really readable from is `nodum schema-dump`, this
  CLI's own self-description. `cli._run`'s failure line is a different thing and
  is safe to assert on: it is one `typer.echo` that neither wraps nor styles.
  This is the same failure family as the release lesson below — a gate that
  passes on a path the real run never takes — and this one would otherwise have
  surfaced at tag time instead of PR time.
- **Version** comes from the git tag (`vX.Y.Z`) via hatch-vcs at build time;
  never bump a version in code.
- **Releasing.** Land the change on `main`, then push an annotated `vX.Y.Z`
  tag on a commit reachable from `origin/main`. That triggers
  `.github/workflows/release.yml`: **every check a pull request passes** —
  lint (ruff + pyright), the test matrix, the highest-resolution resolution
  leg, the frontend suite, the docs build and the clean-install smoke — gates a
  `uv build`, which publishes to PyPI over OIDC trusted publishing (no API
  token). Tag pushes do **not** trigger `ci.yml` or `docs.yml`, which is why
  the release workflow re-runs their checks itself; four of them ran on pull
  requests only until 2026-08-05, so a tag was gated more weakly than the
  branch it came from. That parity is no longer a promise in this paragraph:
  `tests/test_docs.py` reads the workflow files and asserts it, jobs and
  `run:` steps both, with every exemption named. The publish step sets `skip-existing:
  true`, so re-pushing a tag onto an already-released version is a no-op rather
  than a `400 File already exists` failure, and pins the publish action to an
  exact tag because that job holds OIDC publish rights.
- **Docstrings on public APIs**: one-line summary plus args/returns where
  applicable. Comment the *why*, not the *what*. Don't annotate code you
  didn't change. ``cli.py`` is exempt: its docstrings *are* Typer's ``--help``
  surface, and its parameters are documented with ``help=`` strings — an
  ``Args:`` section there would leak into user-facing output.
- **Keep adapters thin.** When you add or change a service operation, expose it
  through the CLI in the same change, and update `README.md`,
  `docs/architecture.md`, and this file in the same commit.
- **Line length 100**; ruff rules `E, F, I, UP, B, SIM`.
- **Frontend**: `make web-install` once, then `make web-build` (which runs
  `tsc --noEmit` first, so the build is the type gate) or `make web-dev` for
  the Vite server on 5700 proxying to `nodum serve` on 8600. Three gates, all
  in CI: `tsc --noEmit` over the whole tree, **`make web-test`** — Vitest over
  the pure modules in `web/src` (`*.test.ts` beside the module it covers) —
  and **`make web-lint`** — ESLint over `web/src`, where
  `react-hooks/exhaustive-deps` at error catches stale hook closures. `web/`
  parses TypeScript with Babel's parser rather than typescript-eslint because
  the pinned typescript@7 (the native compiler) has no JS API for
  typescript-eslint's peer range. There is no component/DOM harness, so
  anything React renders is verified by type-checking it and **running it** —
  `make web-e2e`, the fourth gate (see the frontend contract's browser rule).
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
  push to `main` that touches those paths, and built (not deployed) on any
  pull request that touches them. The build runs `--strict`; `mkdocs.yml`'s
  `validation` block raises the orphan-page and broken-anchor checks from
  INFO to WARN, which is what makes a broken internal link or a page missing
  from `nav` **fail CI** — check a docs change locally with `make docs`
  (`make docs-serve` for live reload), which runs the same `mkdocs build
  --strict` against the pinned `docs` dependency group.
  `docs/CNAME` carries the custom domain and must survive any docs
  reorganisation. **`docs/llms.txt`** is the agent-facing summary published at
  `/llms.txt` (mkdocs copies non-Markdown files through verbatim); it states the
  CLI contract, the actor/privilege split, and the MCP tier boundary, so a
  change to any of those belongs in it as well as in this file. `docs/architecture.md` is both the in-repo architecture doc
  and a site page, so links out of it must be absolute URLs — a relative link
  to something outside `docs/` resolves in the repo but breaks the site build.
  `docs/http-api.md` is **generated** from the live HTTP route table
  (`api_routes` in `nodum.http_api`) by `scripts/gen-http-api-docs.py` and
  never hand-edited — regenerate it (`uv run python scripts/gen-http-api-docs.py`)
  after any route-table change and commit the result; `tests/test_docs.py`
  fails if the committed page is not the generator's output.

## CLI contract (for agents driving the CLI)

- Every command prints **one JSON object** on stdout and nothing else on the
  success path. A list returns a named key plus a `count`
  (`{"nodes": [...], "count": 2}`); keep new list commands to that shape.
- DB path resolution: `--db` flag → `NODUM_DB` env var →
  `~/.local/share/nodum/nodum.db`.

### Identity and authority

- **The CLI is human-only, and every command that touches the graph names its
  human** with a required `--as human:<id>` (or the bare id) — reads included.
  There is no `--actor`: agents drive MCP, never the CLI. A human write lands
  `active`; an agent write (over MCP) lands per its grants: `suggest` →
  `proposed`, `edit` → `active`. An agent `node update` with `suggest` stages
  a `proposed` *version*; `accept` applies **only those fields** it named,
  `reject` archives it; an agent `[[wikilink]]` materialises a `proposed`
  `mentions` edge.
- **Review authority is a human, or `edit` on the item's space** (Q13):
  `accept`, `reject`, every `review` subcommand, the curative tier
  (`merge-nodes`, `retype`, `supersede-edge`, `bulk-relink`) and `consolidate`
  ask the identical question through `Store.require_review`. `archive` is not
  in that set — it retires live state — so it is the human tier with `undo`.
  `undo` and `rollback` stay human-only. Both reject spellings require
  `--reason`.

### Reversal

- **`undo` and `rollback` split on one line: does the event carry a `cycle_id`?**
  An event with none is reversed by `undo`; one with a cycle id is reversed by
  `rollback <cycle-id>`, and `undo` refuses it by name. The no-`seq` search
  **finds** cycle-stamped events and gets that same refusal, naming
  `nodum rollback <cycle-id>`; it does **not** step over them. (Why: that
  refusal's second sentence came and went — `docs/decisions.md`.)
- Errors are always one line on stderr with exit 1, never a traceback. **A
  command that reads a file reads it *through* `_run`**, never beside it; new
  file-reading options follow that rule.

### Errors and exit codes

- **Widening `_run`'s except list is not how a new error gets a message.** The
  list is the set of things a *caller* can provoke; a class that also covers
  defects would turn every future bug into a friendly sentence and exit 1.
  Translate new domain errors at the argument they belong to instead.
- `--set key=value` is repeatable; values parse as JSON with a raw-string
  fallback.
- `--version` prints `nodum <version>` and exits 0; `schema-dump` prints the
  CLI's whole command tree as JSON. Both short-circuit without touching a
  database. Note `schema-dump` (the CLI adapter's own surface) is a different
  thing from `schema <type>` (one node/edge type's catalog entry).

### Spaces, search and the smart verbs

- **A space is two independent controls, not a mode** (D1): reads take an
  optional `--space` **filter** (default: every space in scope); writes take a
  `--space` **target** (default `main`). The filter **narrows** and never
  widens: a space the principal holds no grant on does not resolve, and reads
  identically to a nonexistent one. `--include-meta` is the other read-side
  control, off by default; `--space meta` is the same opt-in said precisely.
- Surface: `init`, `node create/get/update/list/children`, `edge
  create/list/create-batch`, `accept <id>` / `reject <id> --reason` /
  `archive <id>` (each takes a node, edge, or proposed-version id), `undo [seq]`,
  `history <node-id>`, `events [--cycle <id>]`, `types`, `schema <type>`,
  `schema-dump`, `search <query>`, `traverse`, `subgraph <root-id>`,
  `suggest-links <prefix>`, `resolve-titles <title>… [--space]` (the exact,
  case-insensitive sibling — each title answers `resolved`/`ambiguous`/`not-found`),
  `find-path`, `diff`, `projector run/status/rebuild`,
  `review queue/accept/reject/accept-all/reject-all`,
  `asset register/get/list/rendition/purge/download-url/upload-url`
  (everything except `register`/`purge` takes `--as`),
  `ingest file <path>… / url <url> / handlers` (`handlers` takes no `--as`),
  `human create/list/passwd/disable/enable` (the last enabled human cannot be
  disabled),
  `agent create/list/token-rotate/disable/enable` (create and rotate print the
  show-once `ndm_…` token to stderr; only the hash is stored. Every account is
  **external — `--kind` is gone**),
  `grant <agent> <space> <level>` / `revoke <agent> <space>` / `grants [--agent]`
  (`read`/`suggest`/`edit`, event-logged; `revoke` reaches an **archived**
  space by id or name, while `grant` refuses one),
  `space-create`/`space-list`/`space-rename`/`space-archive` (a space is a
  node of builtin type `space` in the meta space — event-logged, versioned and
  undoable like any other write; `space-list` carries each space's **live node
  count** and its **agents granted**, human-only like `grants`),
  `consolidate [--scope] [--job …] [--dry-run]` (the gardener's cycle: `--as`
  names who *asked*; the writes are the gardener's),
  `cycle-list [--limit]` / `cycle-get <id>` / `cycle-abandon <id>` (the dream
  journal, human-only for the reason `events` is. `cycle-get` returns the
  cycle row and nothing else; the entry's *diff* is `events --cycle <id>`,
  deliberately a second command — the row stores no diff. `--limit` below 1 is
  an **error**; `events --cycle <id>` on an id naming no cycle is a **not
  found**, not an empty list — an empty list is what a *dry run* looks like,
  the machine-checkable proof a rehearsal changed nothing. **`cycle-abandon`
  is the door out of an interrupted run**: a cycle left `running` makes its
  own writes irreversible, so this closes it `failed`, naming who abandoned
  it.
  **`cycle-stop` is the kill switch, not a softer `cycle-abandon`**: it stamps
  who asked and when on a `running` cycle and changes nothing else — the entry
  stays `running`, no event, no write — and the run closes its *own* entry at
  its next check. A stop is an instruction a live run obeys; abandoning is a
  repair on somebody else's dead process. Both end `failed`; neither reverses
  anything — `rollback` is the only verb that does, and it runs *after* the
  entry closes. **What obeys a stop is `AgentRun.chat`, before a provider
  call**; the deterministic
  jobs make none (the abstraction job is the exception)),
  `rollback <cycle-id> [--dry-run]`,
  `merge-nodes <ids…> --into <id>`, `retype <ids…> --type <t>`,
  `supersede-edge <edge-id> [--src --dst --type --confidence --set]` (every
  option describes the **replacement**; unnamed fields are inherited),
  `bulk-relink [--src --dst --type --state] [--to-type --to-dst] [--dry-run]`,
  `ask <question> [--k] [--space]` / `summarize <node-id> [--depth]` /
  `search … [--nl]` / `llm status [--probe/--no-probe]` (the read-only smart
  surface — see `nodum.answers`; none of the four writes anything),
  `serve [--host 127.0.0.1] [--port 8600] [--allow-host NAME]
  [--db PATH] [--behind-tls]`. **There is no `mcp` command group**: the
  agent surface is a route on `serve` (`/mcp`), not a process a client
  launches. `serve` prints the database path on stderr and translates
  uvicorn's own startup failure into the contract's exit 1. A non-loopback
  bind is allowed (password login, not the bind, is the boundary), marks the
  session cookie `Secure` there, and warns that uvicorn speaks plain HTTP.
  **The nightly consolidation cycle is configured by `NODUM_CONSOLIDATE_AT`
  (`HH:MM`, local wall clock) and by nothing else** — no `--consolidate-at`
  flag, because unset means off. A schedule that is on says so in the banner.
- **The four smart verbs never fail because the model did.** `ask`,
  `summarize`, `search --nl` and `llm status` exit **0** whatever the provider
  did — a question nothing answered is `answered: false` with a `refusal`; no
  provider is a perfectly good install with the smart features off. Exit 1
  stays for the caller's error (a blank question, a node that does not
  resolve, `--k 0`). **`llm status` takes `--as` although it reads no graph** —
  the one exception to the "reports the install, not the graph" rule: the
  reachability probe is a real model call, metered like every other. `--no-probe`
  spends nothing. `reachable` is **tri-state**, `null` = not established. The
  probe waits exactly as long as the envelope says — one per-call ceiling —
  and what it spends is reported in `used`; it asks a bounded question at
  `reasoning_effort: "none"`. `llm status` also reports the negotiated state
  (`structured_output`, `thinking`/`thinking_applied`,
  `effective_max_output_tokens`). (The measurements: `docs/decisions.md`.)

### Rehearsals and batches

- **A `--dry-run` answers "what would happen", and each one is precise about
  what it costs.** `consolidate --dry-run` still writes its journal entry,
  flagged — and emits **no** event, so `events --cycle <id>` on it is empty.
  `bulk-relink --dry-run` writes nothing at all, not even a cycle. `rollback
  --dry-run` opens no cycle and returns the conflicts in `conflicts` **and the
  delete guards in `blockers`** instead of raising — a rollback fails for
  either reason. Each `blockers[]` entry names the cycle's own create event,
  the row it made, the `dependants` in the way and the `reason` the run would
  refuse with. Both empty is the only clean verdict.
- **A refused `rollback` is this CLI's one structured error.** A rollback
  conflict is a *list* — for each row in the way, which cycle event wrote it
  and which later event moved it, plus that event's actor and cycle — so the
  command prints `{"error": {"type", "message", "conflicts"}}` as its one JSON
  object, and the message still goes to stderr with exit 1.
- Reads are not state-filtered by default beyond edge traversal: `node get`,
  `node children`, `node list`, and `history` return `proposed` rows, and
  `search --state any` includes them. Only *traversals* (`node get --depth`,
  `traverse`, `subgraph`, `find-path`, `search --expand`) are restricted to
  `active` edges — proposed structure is inert, not hidden. `subgraph
  --edge-state proposed` is the one way to walk it. `suggest-links` never
  suggests `archived` titles.
- `subgraph` is the bounded read, bounded twice: `--limit` is a hard node cap
  applied while walking (tested before the far node is read), and the edge
  list has its own cap at `limit * SUBGRAPH_EDGE_FACTOR`; `--limit` is clamped
  to `MAX_SUBGRAPH_LIMIT` (2000). `truncated` is true when **either** cap bit.
  A limit below 1 is an error — `service.require_positive_limit` is the one
  *public* helper in a file of private ones (because `nodum.search` imports it
  too), covering every capped read including `search` (which spells it `k`).
  Any new capped read calls the helper. The edge list is *closed* over the
  node list: an edge between two returned nodes comes back even when the walk
  never traversed it.
- Asset images reach agents only as renditions: `asset rendition` prints
  rendition metadata alone — WebP bytes stay in the database (`--out <file>`
  extracts them); MCP `get_asset` returns metadata + a WebP image block;
  originals are never served over MCP (design §5.7). `--profile` takes
  `thumb`/`preview` for an image and `page:<n>` for a 1-based page of a PDF
  (needs the `pdf` extra).
- **`ingest file` takes one or more paths; a directory argument ingests the
  files directly inside it** (`--recursive` walks deeper). Dot-names and
  non-regular files are skipped; the rest are ingested in sorted order. One
  path naming a *file* prints a single JSON object; anything else is a batch
  printing `{"ingestions": [...], "count": n}`. `--name`/`--title` describe
  one document and are refused for a batch; `--space` applies to all.
- **A batch never loses its successes, and the exit code is 1 if any file
  failed** — `retype` and `bulk-relink` follow the same rule (each skipped id
  named on stderr as `  failed <id>: <reason>`; `bulk-relink` via `BulkRelinkOut`'s
  `unchanged`/`skipped` split, so `skipped` *is* a failure list). Re-running
  the same batch is safe: ingestion is idempotent per `(hash, space)`, so what
  already landed comes back with `created: false`. (Why both verbs used to
  exit 0: `docs/decisions.md`.)
- **The rule `bulk-relink` follows is "non-empty `skipped` *and* not a dry
  run".** Every check a real run makes runs on the rehearsal too, but nothing
  was attempted there, so exit 1 would announce a failure that has not
  happened. The CLI reads `result.dry_run` off the answer rather than its own
  flag.
- `ingest url` fetches `http`/`https` only, once, with a timeout and a size
  ceiling, and refuses a redirect that leaves those two schemes. It does *not*
  block loopback or private ranges — this is itself a loopback service.
- `ingest handlers` lists every extraction handler with its MIME families,
  `available`, and — when a handler cannot run — a `detail` naming the extra
  to install. It needs no principal and no database.
- The capability-URL commands are the escape hatch for a host that shares no
  filesystem with the graph (design §5.7 rule 4). `asset download-url <id>`
  and `asset upload-url --name --mime --size` mint a short-lived, single-use
  URL, print the token **once** (only its sha256 is stored), and log both the
  mint and the later redemption. `--ttl` is bounded (1 s to 1 h). An
  `upload-url` whose `--sha256` this graph already holds answers with the
  existing `asset` and **no** `grant`. Set `NODUM_PUBLIC_URL` when the server
  is not on the default address.
## HTTP contract (for agents touching `nodum serve`)

The complete route reference is the generated `docs/http-api.md` — every
route, its handler, its auth class, and one line on what it does, derived
from the live route table (`uv run python scripts/gen-http-api-docs.py`
after a route-table change; `tests/test_docs.py` holds the lock). The rules
below are the parts that do not fit a route table.

### Identity, and why it is structural

- **The HTTP surface is the human's.** Every write is attributed to the
  session's human principal; the identity is never read from a request. Do not
  add an "actor" parameter, header, or override "for testing" — the MCP
  surface is where agent identity lives.
- Route handlers are thin delegates: one service/search/assets/ingest/urls
  call each. Writes go through `_write(service.fn, …)` — including
  `ingest.ingest_file`/`ingest_url` and `urls.mint_*` — the only place the
  principal is bound for a write. **Never import a service function that takes
  a `principal` into `http_api`**, and never splat request data into a call:
  `**` may only unpack a dict an allowlisting helper built.
- **The test that actually holds the boundary is the runtime sweep**
  (`test_writes_are_attributed_to_the_sessions_human_and_nothing_else`): it
  drives every state-changing method of every route in `app.routes` with
  actor-carrying bodies, query strings and headers, then asserts nothing
  written is attributed to anything but the session's human.

### Origin control, and what it is not

- **A state-changing request must prove it is same-origin**
  (`RequestGuardMiddleware`), because `nodum serve` binds loopback and loopback
  is reachable from every page the user visits. The rule: `Sec-Fetch-Site` in
  `{same-origin, none}`, **or** an `Origin` whose host is allowed, **or** the
  `X-Nodum-Client` header (how a non-browser client declares itself). A
  cross-site `Sec-Fetch-Site` or a mismatched `Origin` is refused outright.
  Reads are unencumbered.
- **Every JSON route requires `Content-Type: application/json`, bodyless ones
  included.** That is not pedantry: `application/json` is not a CORS-simple
  content type, so a cross-origin page cannot send it without a preflight this
  app never answers. `POST /api/assets` is the one exception — multipart *is*
  simple — so it rests entirely on the same-origin proof above. A new upload
  route goes in `MULTIPART_ROUTES` or it inherits the JSON rule.
- **The `Host` header is validated** against `resolve_allowed_hosts(host,
  --allow-host)`. This is the DNS-rebinding defence and the only check that
  protects *reads*. Host names are compared without ports (the `make web-dev`
  proxy sends `Host: localhost:5700`).

### The session gate and the capability hatch

- **The session gate is one rule: every `/api` route `_needs_a_session` claims
  needs a valid session, reads included.** The cookie is `HttpOnly;
  SameSite=Strict` over a server-side row with a 30-day sliding expiry; logout,
  expiry, and `human disable` all kill it at the next request. Any local
  process can satisfy every origin check with three curl headers, so the
  human's password is the heart of the defence, throttled only by the
  failed-login lockout (five misses per name per quarter-hour, then a 429).
  The predicate has exactly two exemptions — `/api/login` and the two
  capability-URL routes below. **Add an exemption to the predicate, never to a
  call site.**
- **The two capability-URL routes are the one thing here that is not a
  session.** `GET /api/download/{token}` streams an asset's original bytes and
  `PUT /api/uploads/{token}` stores a raw body; both are redeemed by an agent
  host that has no filesystem in common with this server and no account here.
  They sit outside the session gate **and** outside the origin/content-type
  gate: those gates exist because a browser attaches the session cookie by
  itself (which is what CSRF rides), and a capability URL carries no ambient
  credential — the token in the path *is* the authorisation. Both exemptions
  key on one predicate, `_is_capability_path`. **What is *not* exempt**: the
  `Host` check and the body ceiling (`urls.MAX_UPLOAD_BYTES` is deliberately
  equal to `MAX_REQUEST_BYTES`). Neither route may call `_session_principal`,
  and neither writes to the graph; the redemption is attributed inside
  `urls.consume`, to the token row's own `created_by`.
- **A downloaded original is served as `application/octet-stream`, never as
  its stored MIME**, with `nosniff`, `attachment`, `no-store` and a filename
  built from the content hash — serving a stranger's `text/html` back from
  this origin is stored XSS. The bytes stream out of the blob in 1 MiB chunks;
  never read an original into memory to send it.

### Ingestion and assets

- **`PUT /api/uploads/{token}` ingests: bytes in, reviewable subgraph out.**
  It answers with the whole ingestion — asset, `asset_ref`, `source`,
  `derived_from`, one `block` per page — via `ingest.ingest_upload`, which
  re-mints the principal from the token row's own `created_by` (a grant whose
  account has since been disabled fails there). What the route itself owes is
  the grant's `max_bytes` enforced *while* the body streams, and the type
  policy below. A refusal **spends the token**, so a client retries by
  re-minting.
- **`POST /api/ingest` takes exactly one of `path` and `url`** (plus optional
  `name`/`space`/`title`); both or neither is a 400. `path` is read *by the
  server* and `url` is fetched *by the server*, which is exactly why this
  route is inside the session gate and the two token routes are not.

### Spaces, accounts and the smart routes

- **Spaces reach the human over HTTP as a filter, a target, and a lifecycle.**
  `GET /api/nodes` and `GET /api/search` take `?space=` (narrow to one space)
  and `?include_meta=` (off by default). `POST /api/nodes` takes `space` in
  the body: the **write target**, optional, `main` when absent. A space names
  *where a node goes*, never *who wrote it*. The lifecycle is `POST /api/spaces`
  (create), `POST /api/spaces/{id}/rename` and `POST /api/spaces/{id}/archive`,
  in the `/api/nodes/{id}/archive` verb-POST style; `{id}` is a space id *or
  name* and resolves as a **space**. `GET /api/spaces` carries per space the
  live node count and the agents holding grants on it, byte-identical to
  `nodum space-list` — **active spaces only**. The space rules are the
  service's, so both archive routes answer 400 for `main` and `meta`; both
  writers answer **409 `SpaceNameTaken`** for a name any space already holds;
  and `POST /api/nodes` answers 400 for `{"type": "space"}` aimed anywhere but
  `meta`. Archiving a space makes every grant on it inert. Do not re-implement
  any of it in a handler or in the UI: the refusal is the server's.
- **Account and grant administration is on the API too.** `GET /api/me`
  returns the session's human; `/api/humans`, `/api/agents` and `/api/grants`
  mirror the CLI's `human`/`agent`/`grant`/`revoke`/`grants` commands, with
  disable/enable and password/rotate as verb-POSTs (`/api/humans/{id}/password`,
  `/api/agents/{id}/token-rotate`, …). Agent creation over HTTP is
  external-kind; the show-once token comes back in the create and token-rotate
  response bodies.
- **The smart surface is three routes and all three are reads.** `POST /api/ask`
  (`question`, optional `space` and `k`; answers with `answered`, `answer`,
  `citations[]`, `considered[]`, `truncated_notes[]`, `dropped[]`,
  `unresolved[]`, `unsupported_numbers[]`, `refusal`, `used`),
  `POST /api/summarize` (`node_id`, optional `depth`; the same shape with
  `summarized`/`summary`, plus `withheld[]` and the separate `truncated` that
  is the *walk* stopping at its cap), and `GET /api/search?nl=1`, which adds a
  `rewrite` object to the ordinary result. All three go through
  `run_in_threadpool` and delegate to `nodum.answers`, where the rules live.
  **`?nl=1` is additive**: a search without it is byte-identical to
  `nodum search`. **`/summarize` has no `propose` flag**: the smart surface
  ends exactly where a model call causes a write. **A client rendering
  `/api/ask` must not stop at the boolean**: `answered: true` is four
  deterministic checks and none of them says the answer is *true* — a note can
  have reached the model **in part** (`truncated_notes`, and `truncated` on
  every citation), notes can be missing (`dropped`), and `considered` is empty
  whenever no call was made. **This is why there is no Ask view in `web/`**.
  **A provider failure is a 200, not a 5xx**: no provider, an unreachable one,
  a `length` finish, a filled context, an exhausted budget are all
  `answered: false` with a `refusal`; a malformed *request* is the ordinary
  400/404 through `EXCEPTION_STATUS`. `/summarize` reads the subgraph
  **before** the provider: a missing node is a 404 with or without a model.
- **The dream journal is six routes, and the curative tier is none of them.**
  `GET /api/cycles` (newest first — byte-identical to `nodum cycle-list`),
  `POST /api/cycles` (run one now — `scope` and `dry_run` are the runner's own
  parameters), `POST /api/cycles/{id}/abandon` (close an interrupted run
  `failed`; 400 on a cycle that is not `running`), **`POST /api/cycles/{id}/stop`**
  (the kill switch: stamp who asked and when on a `running` cycle and close
  nothing — the run closes its own entry when it notices; 400 on a
  non-`running` cycle, and **200 on a second stop**, keeping the first asker;
  deliberately not `/abandon`), `GET /api/cycles/{id}` (the row, its metrics,
  and `list_events(cycle_id=…)` in one round trip), and
  `POST /api/cycles/{id}/rollback`. `POST /api/cycles` exists because the
  schedule is off unless configured. The runner takes *who asked* as a string
  from the verified session (the scheduler calls it with no principal at
  all). **A rollback conflict
  is 409**, not 400 — the graph moved on — and it is the only failure whose
  body carries more than `type` and `message`. **`consolidate.CycleInProgress`
  is 409 for the same reason**: the request was well-formed and the graph was
  busy, which is what a client retries on. **`POST /api/cycles` runs the cycle
  off the event loop** (`run_in_threadpool`), as do `GET /api/search` (both
  branches), `POST /api/ask`, `POST /api/summarize` and the blocking writes
  (`POST /api/assets`, `POST /api/ingest`, `PUT /api/uploads/{token}`, the
  rendition route, the download spool); `_write` is what goes to the thread.
  **The caveat is the write lock, and it is measured**: the loop is free, but
  a `GET /api/nodes` issued while a cycle runs waits for the burst holding
  SQLite's single writer. (The measurements: `docs/decisions.md`.) Do not add
  `merge_nodes`, `retype`, `supersede_edge` or `bulk_relink` here: they are
  the curative tier and they belong to the CLI, and `PATCH /api/nodes/{id}`
  still cannot retype a node.

### Failures, limits and the shape of a request

- **A wrong verb on a real route is a 405 with an `Allow` header**, not the
  catch-all's 404 — `api_not_found` asks the real routes what they would have
  matched.
- **`/healthz` reports liveness only.** It sits outside auth, so anything it
  says is said to everyone.
- **`POST /api/assets` is bounded before it buffers**: `MAX_REQUEST_BYTES` is
  checked against `Content-Length` and enforced mid-stream. It registers bytes
  and writes no describing node, so what describes them is the note that
  inlines `![alt](/api/assets/<hash>/rendition/preview)` — which is why it
  admits `INLINE_IMAGE_MIMES`, the rasters this Pillow build can actually
  render, and nothing else, and why the 40 MP rendition ceiling is an
  admission rule *here*. A document belongs on the capability route, which
  ingests it. **There is no delete route** — a known gap, not an oversight,
  and the reason both routes refuse before they store rather than after.
- **One type policy over both upload routes, with the route's capability as its
  only parameter.** `_refuse_unsupported_upload(spooled, name, admits=…,
  pixel_limit=…, cli_hint=…)` sniffs the *bytes* (`assets.sniff_mime`), never
  the filename or the client's `Content-Type`, then refuses anything outside
  `admits`: `INLINE_IMAGE_MIMES` on `POST /api/assets`, `INGESTIBLE_MIMES` on
  `PUT /api/uploads/{token}`. The second **is** `assets.RECOGNISED_MIMES`
  rather than a copy of it, so the policy cannot drift from what the sniffer
  knows; widening either route means adding the type to the sniffer. Every
  other difference is a **named argument**: the decompression-bomb guard runs
  on both, `pixel_limit` (40 MP, what this server can *render*) on
  `/api/assets` alone, `cli_hint` naming `nodum ingest file` when the refusal
  points there. What it gives up: a PDF is refused by `/api/assets` and
  ingested by the capability route; a renamed binary, a `.docx`, anything with
  NULs and no signature are refused by both, the refusal naming
  `nodum ingest file` as the way in. **That refusal is a heuristic, not a
  guarantee**, with exactly two documented ways through, both degrading
  cleanly: a **NUL-free, control-free** binary format is admitted as text (see
  the sniffer's windowed rule under `nodum.assets`), and **non-text bytes
  carrying a versioned `%PDF-` header in the head window** are admitted as PDF
  (the downstream answer is an extraction `detail` or a mapped 400, never a
  500). Availability is *not* part of it — an install without the `pdf` extra
  still admits a PDF. A new upload route names its admitted set and calls this
  helper.
- **A refusal on the capability route is indistinguishable from a re-drop in
  the audit log.** A spent `asset.upload` with no `asset.ingest` after it means
  *type-refused* **or** *over the grant's size* **or** *already ingested into
  that space* — three outcomes, one silence.
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
  query keys; an unknown `/api` path is a JSON 404 while unknown non-API paths
  fall through to the SPA entry point (or the "UI not built" placeholder).
  **`/favicon.ico` is the one exemption**: a browser asks for it unprompted,
  so it is answered with the bundle's icon if there is one and a 204 otherwise
  — never an HTML document under a 200. Any other path a browser requests on
  its own belongs in that same exemption list, not in the catch-all.
- Renditions are WebP bytes at `/api/assets/{id}/rendition/{profile}`, where
  `{profile}` is `thumb`, `preview`, or `page:<n>` for a PDF page raster (the
  colon needs no routing change, since Starlette's default path convertor is
  `[^/]+`). Originals are served on **one** route only,
  `GET /api/download/{token}`, and only against a capability URL minted
  through `POST /api/assets/{id}/download-url` (design §5.7).
## MCP contract (for agents touching the `/mcp` surface)

The MCP server (`nodum.mcp_server`) is the **external-agent** surface, served
at `POST /mcp` by `nodum serve` over streamable HTTP — the same origin and the
same process as `/api` and the web UI. It registers exactly two tiers and
nothing else.

- **HTTP is the only transport.** The stdio server and the mcp command group
  that launched it were both removed: a subprocess reaches only the launching
  machine's database, and a deployable agent surface is the point. `http_surface()`
  returns the route to register and the lifespan to run — **both**, because
  Starlette does not run a sub-app's lifespan and a route wired without it
  answers 500 while looking correct.

- **The registry is the read + additive tiers, structurally.** The design §8.1
  read tier (`get_node`, `get_children`, `search`, `traverse`, `list_types`,
  `get_schema`, `find_path`, `history`, `diff`, `get_asset`,
  `get_download_url`) and additive tier (`create_node`, `update_node`, `link`,
  `propose_edges`, `ingest_url`, `request_upload_url`) — every
  tool a thin delegate to one service/search/assets/ingest/urls function.
- **Four tiers are never registered, and each one is a named absence**: the
  review tools (`accept`, `reject` — `REVIEW_TOOLS`, gated by
  `Store.require_review` — a human, or `edit` on the item's space), the
  curative tools (`merge_nodes`, `retype`, `supersede_edge`, `bulk_relink`,
  `consolidate` — `CURATIVE_TOOLS`, §8.2), **reversal plus the journal
  that records it** (`undo`, `rollback`, `abandon_cycle`, `request_stop`,
  `get_cycle`, `list_cycles` — `HUMAN_ONLY_TOOLS`), and **anything that names a
  path on the server's own disk** (`FILESYSTEM_TOOLS`). `UNREGISTERED_TOOLS` is
  the union, and what `tests/test_mcp_server.py` asserts the registry stays
  disjoint from; adding an operation to any of those tiers means adding its
  name to a list, never to the registry. This is **structural enforcement, not
  a runtime check** — the tools are absent from the tool list, not refused at
  call time.
- **Anything an agent must be able to say has to be in a tool's signature**,
  because the SDK discards a keyword this module does not declare instead of
  refusing it. `create_node` takes `space` (a space id or name, `main` by
  default, narrowed by the grant set like every other space reference). Every
  write result carries the `space_id` it actually landed in — check it rather
  than assuming, because a misspelt argument is silent.
- **Tool annotations state each tool's worst case.** Reads are `readOnlyHint`
  — `get_download_url` included, since it writes an expiring capability row and
  an audit entry but no node, edge, or version; the additive tools are
  `destructiveHint=False` (they only ever add state); `update_node` is
  `destructiveHint=True` because under an `edit` grant it overwrites the node
  in place — MCP hosts auto-approve on that flag, so it must not lie. Every
  write tool's description says what an `edit` grant changes rather than
  promising `proposed`.

### Authentication

- **Auth is a per-request bearer token** — `Authorization: Bearer ndm_…`, minted
  by `nodum agent create` / `token-rotate`, shown once and stored hashed. It is
  never an environment variable on the server and never a flag: the server is
  not bound to an agent at all.
- **It is checked twice, on purpose.** `BearerGuard` refuses the *request* with
  a 401 unless it presents an enabled agent's token — before `initialize` or
  `tools/list` answers, so an unauthenticated peer cannot enumerate the
  surface. `_principal()` then re-reads the same header off the SDK's
  per-request context and re-mints the principal for the *call*. The guard is
  the door; `_principal` is the identity.
- **Nothing about a caller survives between calls.** One process serves many
  agents, so a cached principal would be someone else's. Reading the token from
  the live request each time is also what makes revocation verification-time:
  disabling the agent or its owner, or archiving a space it holds a grant on,
  bites at the next call rather than the next restart.
- **The DNS-rebinding host list is nodum's, translated.** The SDK defaults to
  loopback-only, which refuses every request on a deployed host; `http_surface`
  derives its `TransportSecuritySettings` from `resolve_allowed_hosts` so one
  policy feeds both enforcement points.
- **Ingestion is by reference** (§5.7 rule 2), and by **URL or upload only**:
  `ingest_url` takes something this server can fetch, `request_upload_url`
  hands back somewhere to PUT bytes the caller holds. **No base64 ever crosses
  MCP, and no path does either.** `ingest_file` took one until finding B1: an
  agent holding nothing but `suggest` named a server path, the pipeline wrote
  the extracted text to `assets.extracted_text`, a `proposed` describing node
  was enough to reach it, and `get_asset` returned it — two calls a host
  auto-approves. Withholding the text from the first call was tried and was not
  the fix, because the second call was never the reported path. Grants scope
  the *graph*; a filesystem read is not a graph read, so nothing in the grant
  model could bound it. `nodum ingest` on the CLI still takes a path — there,
  local access is already the trust boundary.
- **`get_asset` never serves originals** (§5.7): metadata (carrying the
  asset's extracted text, capped, with the real length and a truncation flag)
  plus a `preview`/`thumb`/`page:<n>` WebP image block; an unknown profile is
  refused. The one documented exception is `get_download_url` — a single-use,
  minutes-long URL built on `NODUM_PUBLIC_URL`, with the mint and the
  redemption both in the event log.
## Frontend contract (for agents touching `web/`)

Full conventions: `web/README.md`. The rules below are the ones that bind.

- **One `fetch`.** Everything goes through `src/api/client.ts`. It has no
  identity parameter and must never grow one — the server binds the principal,
  and the client being unable to express one is the second layer under that.
  It sends `Content-Type: application/json` on every non-GET request to a JSON
  route, bodyless ones included; `form` bodies (`POST /api/assets`, multipart)
  set no content type; and `rawRequest`'s raw-body branch
  (`PUT /api/uploads/{token}`) sends none at all — the capability routes are
  deliberately outside the content-type gate (`_is_capability_path`). Auth is
  the `HttpOnly` session cookie the browser attaches itself; a 401 from any
   route but login is broadcast through `src/lib/session.ts` to a `/login`
   redirect.
- **Recent reads are scoped by verified human identity.** `src/lib/recents.ts`
  receives its scope only from the app shell after `GET /api/me` returns the
  stable human id. Until then it exposes and records an empty list, including
  when a non-401 identity failure leaves failure-capable views mounted. Its
   localStorage keys and `storage` events are identity-scoped; a session
   transition invalidates same-origin tabs. Logout clearing is defense in depth,
   never the authority boundary.
- **Never call `new Date()` on a server string.** SQLite writes
  `datetime('now')` — UTC, no zone marker — which every browser reads as *local*
  time. Parse through `parseTimestamp` (`src/lib/time.ts`); `new Date()` on a
  client-side epoch number is the only exception.
- **Never re-derive a failure's meaning.** `describeFailure`
  (`src/lib/failure.ts`) is the one place that tells *the API refused this*
  apart from *nothing was listening* (a `fetch` `TypeError` same-origin, a 502
  behind the dev proxy). Map its `kind` onto your own panel. The same rule
  covers a refused space: `isUnknownSpace` (`src/api/client.ts`) is the **only**
  discriminator, and the client normalises every call that names a space into
  one `UnknownSpaceError` — the upload pair raises the
  `UnknownUploadSpaceError` subclass, which adds which request refused (a
  refused mint sent nothing; a refused redemption already spent the grant).
  It is keyed on the message (`unknown space: …`), because no status is
  specific enough. If a bare `ApiError` with that message ever reaches a view,
  wrap the call in the client.
- **Nothing user-facing may say a space does not exist** — not "no such
  space", not "does not exist", not "unknown/missing/nonexistent space", not
  "not found", and not via `describeFailure` or `toast.showError` either. The
  server answers a space that was never created and a space the caller holds
  no grant on with **word-for-word identical text on purpose** (Q13 review
  S3): a refusal that told them apart would be an existence oracle over every
  space in the file. Say what changed instead — a space stops resolving once
  it is archived, and a renamed one no longer answers to its old name.
  `views/search/spaceFailure.ts`, `views/editor/createOutcome.ts` and
  `views/spaces/spaces.ts` own that copy and pin it with tests; the refused
  **write target** sentence belongs to `components/spaceNaming.ts`
  (`writeTargetWouldNotResolve`).
- **The space surfaces are shared, and there is one of each.** The read filter
  is `components/SpaceFilter.tsx` (controlled and presentational); its option
  vocabulary is `components/spaceOptions.ts` (a space reference is an id *or*
  a name everywhere); the `GET /api/spaces` read behind all of them is
  `components/useSpaces.ts`. Do not add a seventh copy of that fetch or a
  second `spaceLabel`. `GET /api/spaces` is **active-only and stays that way**.
  Naming a space that listing cannot goes through `components/spaceNaming.ts`
  and its lazy `components/useArchivedSpaces.ts`. `nameSpace` has four
  answers, and `pending` is not `unknown`: a space list still in flight is not
  an unresolvable space, so `?? []` at a call site is the bug — pass the
  `null` through.
- **An archived space is *nameable* everywhere and *selectable* nowhere**, and
  those are two rules, not one. `spaceLabel` is **no longer exported from
  `components/`**: its one caller is `spaceOptions`, in the same module. The
  picker names an archived *selection* by being handed
  `spaceOptions(spaces, selected, selectedName)`; it is never handed the
  archived **list**, so nothing inside it can put an archived space among the
  choices; the option it adds is the current value, marked `(archived)` rather
  than `(unavailable)`. Widening that seam to a list would let someone newly
  choose a space the server refuses to resolve, which is exactly what D1a
  exists to prevent.
- **Every surface that displays a node says which space it is in** — the rule
  for how loudly: **a row states a dimension the filter has not already
  determined.** A concrete space filter is ANDed onto both ranked lists and
  onto graph expansion, so under one every hit provably lives there; under
  *any space* it is the fact the scan needs. `views/search/resultSpace.ts`
  owns that rule, beside `ResultRow.knownState` for the state filter.
- **Where the review queue simplifies, it says so.** A cross-space edge
  proposal is filed under **one** space (its source's) while accepting it needs
  `edit` on **both** endpoints. `grouping.edgeCrossing` carries the honesty:
  the card is marked `cross-space`, the Inspect panel names each endpoint's
  space and states the both-ends rule, and the section header counts how many
  of its proposals leave it (`SpaceSection.crossings`).
- **The archive confirmation states consequences the server actually
  delivers.** `views/spaces/spaces.ts`'s `archiveConsequences` is the one
  place that copy lives, and every line in it has to be a fact: the space
  leaves every picker and stops resolving; its nodes keep their `space_id` and
  stay readable to the human; its **name stays reserved**; and **every grant
  on it goes inert** — an agent granted there can read, write, propose and
  review nothing until the archive is undone, though the grant row survives on
  `/admin` so it can be revoked for good. Do not soften it back.
- **The write target is app-wide, sticky, and must be visible** (D1a).
  `src/lib/writeTarget.ts` owns it: one module-level value, persisted in
  `localStorage`, synchronised across tabs through the `storage` event, and
  **never changed without the human being told** — a target naming a space
  archived from somewhere else survives and fails at the write, because filing
  a node somewhere the human did not choose is worse than a refusal they can
  read. The one reset is `clearWriteTarget()` (called by `/spaces` when the
  human archives the very space they are filing into, and by logout). The rule
  is about *silence*, not immutability. A surface that creates a node
  **shows** the current target, and the post-create confirmation names the
  space the server actually filed it in.
- **A view owns its directory and links to other views by URL.** No view
  imports another. Route paths live in `src/router.tsx`; grep for the path
  string before renaming one. A view's entry component keeps a **default
  export** — the routes are lazily loaded and `lazy()` needs it.
- **Promote to `src/lib/` or `src/components/` on the second user, not the
  first.** `src/lib/` is the plain-function tier; a hook or a shared fetch
  belongs beside the component it serves, in `src/components/`. `writeTarget.ts`
  is the one hook in `lib/`, only because the state it owns has no component.
- **Do not render a control for something the service cannot do.** A node's
  `type` is immutable on the update path — retyping is a curative operation
  with no HTTP route at all — so the editor drops the type commands on a saved
  node. The curative tier is CLI-only, so nothing in this UI may offer a merge,
  a supersede or a bulk relink; what it *does* offer is their reverse, because
  rollback is the human's undo for a cycle.
- **There is no Ask view, and that is a decision.** `/ask` can return a
  **confident, well-cited, wrong answer** — it was measured answering "AWS"
  with `answered: true`, citing a Kafka textbook containing no occurrence of
  AWS, cloud or Kubernetes, against a graph that says k3s on three on-prem
  nodes — because citation *resolvability* is not groundedness. What catches
  it is the envelope, and the envelope survives one surface and not the other:
  a CLI reader gets `unresolved`, `considered` and `dropped` as JSON beside
  the answer, while a browser reader gets prose. So the surface stays where
  its reader is equipped for it; **it moves to the browser once groundedness
  is real**. Until then an Ask view, an "ask about this node" button, and an
  answer panel bolted onto search are all the same decision taken by accident.
- **The journal shows the two records apart, and never merges them.** A cycle's
  `report` says what each job examined, proposed, applied and skipped; the
  events say what actually changed. They are two records on purpose, so a view
  renders both and summarises neither into the other. A **dry-run** entry has
  a report and no events at all — the checkable form of "it changed nothing".
  **Three actions, three situations, and the copy is what keeps them apart.**
  `stop` asks a live run to wind down (the entry stays `running`), `abandon`
  closes the entry of a run nothing is going to finish, and `rollback` is the
  only one of the three that reverses a write — and it only works once the
  entry has closed. A stopped run and a crashed one both close `failed`, so
  the *record* rather than the status is what a reader has to be given. Each
  action names itself in **one exported constant** (`STOP_ACTION_LABEL`,
  `ABANDON_ACTION_LABEL`); `RUNNING_ACTIONS_HINT` renders **only while both
  buttons are on screen**; each confirm's copy is an exported array
  (`STOP_CONFIRM`, `ABANDON_CONFIRM`) rather than JSX, because the harness
  renders no components and a claim inside one is a claim nothing checks.
  **Every line of that copy has to be something the system delivers**,
  including the awkward one: what checks the kill switch today is a provider
  call, the deterministic jobs make none, so a run of those finishes even
  after a stop — that caveat is one exported constant
  (`STOP_IS_NOTICED_AT_A_MODEL_CALL`) carried by every surface, and
  `tests/test_consolidate.py` fails the day a stop check is wired into the
  deterministic jobs. A **rollback confirm** has one hard rule: a 409 carries
  a `conflicts` list, and each conflict names *both* ends of a collision —
  render both, and the `blockers` list's reasons and dependants too. A verdict
  is clean only when **both** lists are empty. (How the copy was right in one
  place out of four: `docs/decisions.md`.)
- **The design system has two colour axes and both are taken**: the brass
  accent means "you can act on this", the state ramp means the service-layer
  state machine (`proposed` violet, `active` sea-green, `archived`
  lowest-contrast). Anything else needs its own hue, kept view-local until a
  second view names it. Exactly one has: `--nd-crossing` (magenta) means *this
  edge's endpoints are in two different spaces* (moved into
  `styles/tokens.css` on the second user). Class names are `nd-`-prefixed
  because Mermaid and Cytoscape inject global stylesheets on `.node`,
  `.label`, and `.edge`.
- **A form control carries an `id` or a `name`** — a field with neither is one
  a browser cannot address. `SpaceFilter` takes `name` as a prop (default
  `space`).
- **Anything React renders is verified by running it** (`make web-e2e`,
  Playwright over `web/e2e/`). There is no component harness, so the browser
  *is* the harness. The fixture (`web/e2e/serve-fixture.mjs`) builds the bundle,
  seeds a throwaway graph in a temp directory, gives the migration-seeded
  `owner` a password and starts `nodum serve` — **it builds first on purpose**,
  because `serve` serves `nodum/_web/` and a run without that build tests the
  code as it was before the change. Locally it uses the system Chrome; CI
  installs chromium (`playwright.config.ts` switches on `CI`).
  **A transient overlay owes four checks**, and they are not a general
  checklist — they are the four things that actually failed across nine review
  rounds on `ContextMenu`: focus lands where you claim on open, focus returns
  where you claim on close, Escape *and* an outside press both dismiss, and a
  document-level shortcut still reaches the surface behind. A new interactive
  surface ships with its spec; verify the spec red before keeping it (comment
  out the behaviour and watch it fail), because a browser test that cannot fail
  is the most expensive kind of green.
- **A pure module gets a `*.test.ts` beside it** (`make web-test`, Vitest). The
  harness is unit-only by design — no component rendering — so pull the logic
  worth testing out of the component and test it there. Assert the *semantics*
  the module encodes (a `min_confidence` of 0 is a filter, not a no-op), not
  its line coverage. The global environment is `node`; a suite that genuinely
  needs a DOM says so in **its own** docblock
  (`// @vitest-environment jsdom`).
  **Unit-only forbids rendering components; it does not forbid the DOM.** That
  misreading is expensive — it once concluded that thirty-nine review findings
  "could not have been a test" and argued for a component harness nobody needs.
  Logic that touches real DOM but needs no React — focus management, listener
  wiring, selection, measurement — goes in a plain-DOM `lib/` module that takes
  the element as an argument, with a jsdom suite beside it.
  `lib/dismissWatchers.ts` and `lib/programmaticFocus.ts` are the pattern. What
  is left for `make web-e2e` is then only what jsdom genuinely cannot answer:
  engine event ordering, touch, and synthesized clicks.
- **A transient overlay that owns focus is the most expensive thing on this
  surface.** Before writing a third one, decide build-versus-adopt **explicitly**
  and record the decision — this repo already ships cytoscape, mermaid,
  CodeMirror, marked and DOMPurify, so "we don't take dependencies" is not the
  reason. If you build it, the dismissal logic is `lib/dismissWatchers.ts`
  (extend it; do not write a second one) and the component keeps only the
  rendering. **Two components holding the same private flag is a missing
  module, not a coincidence** — `NodePeek` and `ContextMenu` each grew their own
  "this focus move was mine" flag, and the bug that took five rounds to find was
  one of them focusing into the other. The promote-on-the-second-user rule
  applies to behaviour, not just CSS.
- **Nothing reaches `innerHTML` without going through DOMPurify.** The preview
  renders Markdown that *agents* wrote, in the origin that may write to the API,
  so `markdownRender.ts` reduces it to an allowlist with **no SVG and no
  MathML** — that namespace is where `<animate>` retargets an anchor's `href`
  to `javascript:` and where a lowercase `<style>` slips past any check keyed
  on `tagName`. `mermaidRender.ts` runs a second, SVG-shaped policy. Both are
  covered by `markdownRender.test.ts`; a new sink means a new policy, not a
  new exception. `nodum.http_api.CONTENT_SECURITY_POLICY` is the runtime
  backstop under both — `script-src 'self'`, no `'unsafe-inline'`.
- **The peek card is one shared component and its preview is plain text.**
  `components/NodePeek.tsx` is the hover/focus quick preview — `NodePeek`
  wraps a trigger element (search titles, the graph panel title), `NodePeekScope`
  delegates `mouseover`/`focusin` on a rendered-Markdown container so the
  `a.nd-wikilink` anchors inside sanitised `innerHTML` peek too, exactly the
  shape of the wikilink click interceptor. Its excerpt is the node's opening
  prose, whitespace-collapsed, from `lib/peek.ts` — **never** rendered HTML, so
  a transient hover pays nothing for sanitisation or mermaid. Everything
  testable lives there with its `peek.test.ts`: excerpt capping, in/out
  edge-count derivation from the depth-1 read, the 300 ms intent state machine,
  and the per-session `getNode` cache. The card sits below `--nd-z-toast` and
  `--nd-z-modal` in the layer stack (`--nd-z-peek`).
- **The create-link dialog is one shared component and the first caller of
  `createEdge`.** `components/LinkDialog.tsx` is the one place a typed edge is
  made, reachable from the reading view's header, the graph panel's actions,
  and the editor's `/link` slash command. Its edge-type chips come from the
  live `GET /api/types` catalog, never a hardcoded list; direction is a
   first-class toggle that swaps the selected type for its catalog inverse
   (`supports` ↔ `supported_by`, so outgoing and incoming describe the same
   fact), and is locked with a reason when the selected type declares no
   inverse (a user-created directed type has no flipped form) — picking such
   a chip while flipped to incoming resets the direction to outgoing, so a
   directed type is never stranded under `in` with both toggle buttons
   disabled; the target
  search reuses `suggestLinks` (title-prefix) and falls back
  to a full `search` when the prefix matches nothing; confidence is optional,
  unset by default, and refused client-side outside `[0, 1]` exactly as the
  service refuses it. The pure model — the pairing, the fallback, the
  confidence parse, the search debounce — lives in `lib/linkDialog.ts` with
  its `linkDialog.test.ts`; a cross-space target is marked `crossing` in the
  results, and the dialog names spaces through the shared vocabulary. A
  human-created edge lands `active` over the HTTP surface, so the dialog has
  no review-queue affordance. On success the host refetches what it holds (the
  reading-view rail, the graph panel's subgraph); the editor inserts the
  target's `[[Title]]` at the caret — or `[[id]]` when the title carries a `|`
  or a bracket the wikilink grammar cannot (`lib/wikilinks.ts`
  `wikilinkInsertion`).
- **There is one context menu, and a view contributes items rather than
  building one.** `components/ContextMenu.tsx` is the only contextual-action
  surface; `useContextMenu` opens it, and `MenuButton` is its twin — a surface
  that offers a right-click **offers the button too**, because a right-click
  does not exist on touch and is invisible to anyone who has not tried one on a
  web app. An action the server would refuse is rendered **disabled carrying its
  reason** (`archiveRefusal`, and the same shape for anything after it), never
  hidden: the refusal is worth reading once, and a 400 after a click is where it
  is otherwise met. Nothing destructive happens in the menu — a `danger` item
  opens a confirm — which is what lets the menu act on Enter while the `Modal`
  contract's "nothing confirms on a keypress" still holds. The placement and
  keyboard rules are pure in `lib/contextMenu.ts`; the component is wiring.
  Four behaviours in that wiring are load-bearing and were each a bug first.
  **The menu owns `MENU_KEYS` and nothing else** — the set is pure, in
  `lib/contextMenu.ts`, with tests, because both of its edges are regressions
  a component-less harness could otherwise not see. It needs a *wide* set
  because a portal bubbles through the React tree and not the DOM one: the
  search list's roving `onKeyDown` sits above the panel and ran beside it, so
  ArrowDown moved the results *and* scrolled (firing the scroll-closes
  listener), and an ArrowRight the menu ignores navigated away with the menu
  open — which is why arrows the menu does not act on are in the set too, and
  why `Escape` and `Tab` stay in it (handing `Escape` back would let one
  keypress close the menu and the dialog behind it). It must own nothing
  *beyond* the set because React's `stopPropagation` forwards to the native
  event and the panel is portalled into `document.body`: stopping every key
  killed every `document`/`window` shortcut in the app, search's `/` and Ctrl-K
  among them; Space is the exception inside the set — on a focused `menuitem` its
  default action *is* the activation ARIA requires, so it is prevented only on
  the panel itself. **Focus leaving the panel closes it, and that takes two
  watchers**, because neither DOM event says it alone. `focusin` on `document`
  fires when something *else* takes focus — which is what a shortcut outside
  the set does, and a panel left painted over the page had no keyboard route
  out — but it is silent when focus falls back to `<body>`, which is what a
  focused menu item going `disabled` under a refetch produces. The panel's own
  `focusout` covers exactly that gap, acting on a **null `relatedTarget` while
  `document.hasFocus()`**: null alone is also what a window losing focus
  reports, and acting on it dismissed a menu whenever the reader alt-tabbed
  away. **`MenuButton` opens; it does not toggle, and no
  dismissal is exempt for it.** This is the rule that cost the most to find:
  four attempts at a toggling button produced eleven confirmed defects, every
  one of them the same shape — whether the click should open or close depends
  on whether the menu survived until the click, and that depends on an ordering
  (document capture `pointerdown` → `mousedown` and the focus it moves →
  React's synchronous flush → `click`) that varies with how the menu was
  opened, with whether the press produced a click at all, and with the engine.
  Exempting the opener from the focus watchers stranded a press-and-drag-off
  with the panel open and focus outside the portal; deciding at `pointerdown`
  removed the ordering and cost the focus default, the touch-scroll
  cancellation and every environment without Pointer Events; snapshotting the
  flag earlier only moved the race. Opening unconditionally has no ordering to
  get wrong — the press dismisses through the ordinary watchers, the click
  opens — and Escape, a selection, an outside click, a scroll and a focus move
  all still dismiss. **Focus a surface moves itself goes through
  `lib/programmaticFocus.ts`.** A DOM focus event does not say who caused it,
  and watchers act on *user* focus — the peek card arms on focus with no intent
  delay, the menu treats focus outside its panel as a dismissal. So a closing
  menu handing focus back to a search row's title, which is a peek trigger,
  pinned a preview card open over the results. Both components had grown the
  same private flag for their *own* re-arm; shared, it covers the case neither
  private one could. It counts rather than latches, because two hand-backs
  overlap. **Focus is handed back only if the panel still holds it**, and with
  `preventScroll` — unlike a modal this overlay closes *on* scroll, so an
  unconditional restore both drags the viewport back and steals focus from
  wherever a shortcut deliberately put it.
- **An undo affordance names one `seq`, never "the latest".** `POST /api/undo`
  with no `seq` reverses whatever the log head is, and four surfaces write to
  this store — an agent holding `edit` can land a write between a human's
  archive and their reach for its undo, and the bare call would then reverse
  *that*, under a label naming the human's own action. `lib/undoTarget.ts` is
  the one place that decides: the head has to carry the same op, name the same
  row, and carry no `cycle_id` (`service.undo` refuses a cycle-stamped event and
  points at `rollback`). When it does not, the confirmation appears **with no
  Undo on it** — that is the designed outcome, not a fallback.
- **A retirement confirm states consequences the service actually delivers**,
  the same rule `archiveConsequences` carries for a space, one scale down.
  `components/nodeArchive.ts` owns the node copy, and its load-bearing line is
  the counter-intuitive one: **archiving a node archives none of its edges**.
  `_transition_row` settles synthesis edges on `accept`/`reject` only, and
  `_walk` filters on *edge* state, so an archived node stays in the graph and in
  every neighbour's rail. Search is the thing that stops finding it
  (`search.search` defaults to `state = 'active'`). Do not soften either line.
  Two corollaries. It states **the node's own counts, and only when they are
  facts** — a menu archiving a neighbour has not read that neighbour's
  neighbourhood, and a truncated walk's count is the cap rather than the size
  (which is why the rail states it as a floor), so both pass `edgeCount: null`
  and get the uncounted sentence. And it **promises neither the Undo button nor
  a condition for it**, only that the archive is one reversible event: the
  toast withholds the button whenever it cannot *prove* the log head is this
  write, which includes the event-log read simply failing — so even "while
  nothing else has landed" would be a condition the next screen falsifies.
- **A space is not an ordinary node to a surface offering `archiveNode`.** A
  space is a node of type `space` in `meta`, and `POST /api/nodes/{id}/archive`
  reaches the same row the space route does — `_transition_row` says so. The
  server would perform it; what it costs is `archive_space`'s list, not the
  node one — every grant on it goes inert, it stops resolving, its name stays
  reserved. `archiveRefusal` therefore refuses a space **on the surface's own
  authority, not the server's**, and points at `/spaces`, which is the screen
  that can count those consequences off the space's row — but only *after* the
  already-archived check, because `GET /api/spaces` is active-only and a
  pointer there for an archived space names a screen that cannot show it.
- **A write that must land after a save awaits the save.** The editor's buffer
  flush is detached (`flushLeftover`), so archiving with unsaved text put the
  `node.update` on the wire *after* the archive: it landed on an archived row
  and became the event-log head, which is exactly the condition that costs the
  archive its undo. `useNodeDocument.persistNow()` is the awaitable save —
  `saveNow` fires and forgets, which is right for a shortcut and wrong here —
  and a save that did not land stops the write with the dialog standing. What
  makes it awaitable at all is `runPersist` **handing back the outstanding
  write's own handle** when `persist` re-enters mid-save: `persist`
  short-circuits on `savingRef` and resolves immediately, so storing *that*
  promise dropped the handle on the write actually in flight and then cleared
  the ref while it was still unresolved — and everything waiting on it carried
  on as though the wire were clear.
- **A dialog locks body scroll and hands focus somewhere real.** Both the
  review `Modal` and the assets lightbox set `body.style.overflow` on open and
  restore it on close. On close, focus returns to the opener *only if it is
  still in the document* — after a successful confirm it usually is not, and
  focusing a detached node silently drops the user on `<body>`. The view
  places focus in that case.

## Where everything else lives

- `docs/architecture.md` — the one architecture reference: the module map, the
  design-section mapping, and the module-by-module architecture (`###` per
  module).
- `docs/decisions.md` — the decision log: every measured finding, dated and
  append-only. Contract rules link to it; never restate its numbers.
- `docs/http-api.md` — the generated route reference (every route, handler,
  auth class, and one line; regenerate after a route-table change).
- `docs/commands.md` — the CLI command reference; `docs/concepts.md` — the
  concepts behind the surfaces.
- `docs/llms.txt` — the agent-facing summary published at `/llms.txt`; a change
  to the CLI contract, the actor/privilege split, or the MCP tier boundary
  belongs in it as well as here.
- `web/README.md` — the frontend conventions in full.
