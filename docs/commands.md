---
title: Commands · nodum
description: The full nodum CLI surface — every command, its purpose, and the conventions every command shares.
---

# Commands

Every command prints **one JSON object** on stdout and nothing else on the
success path, so you can parse stdout directly. There is no `--json` flag —
JSON is the only output format. Human-facing and error messages go to stderr.

```sh
nodum node get <id> | jq .title
```

List-returning commands wrap their rows in a named key plus a `count`:

```json
{ "nodes": [ … ], "count": 2 }
```

Errors are always one line on stderr with exit 1 — never a traceback. That
includes a missing file, whichever command reads one: `asset register
/missing.png`, `ingest file /missing.pdf`, `edge create-batch /missing.json`,
and `node create|update --content-file /missing.md`. It covers the directory
arguments too — a folder this process may not read, and a `~someone` whose home
directory does not resolve, are each one line from `ingest file`.

It also covers who you say you are: an account `--as` names that does not exist,
or exists and is disabled, is the same one line and the same exit 1, on every
command that takes the option.

## Conventions

**Database path** — `--db` flag → `NODUM_DB` environment variable →
`~/.local/share/nodum/nodum.db`.

**Attribution** — the CLI is human-only and every command that touches the
graph requires `--as human:<id>` (or the bare id) — reads included, since
reads are grant-scoped like writes. A human's write lands `active`. Agents
write over
MCP, never the CLI, and land per their grants (`suggest` → `proposed`, `edit` →
`active`).

**Human-only operations** — `accept`, `reject`, `archive`, `undo`, `rollback`,
`cycle-abandon`, `cycle-list`, `cycle-get`, every
`review` subcommand, and all account/grant administration (`human`, `agent`,
`grant`, `revoke`, `space-*` commands) require a human principal. Review
(`accept`/`reject`/`archive`) and the curative tier (`merge-nodes`, `retype`,
`supersede-edge`, `bulk-relink`, `consolidate`) can also be exercised by an
agent holding `edit` on the spaces involved — over the service API, not the
CLI. `undo` and `rollback` stay human-only: both write a recorded payload back
verbatim, `state = 'active'` included, and `rollback` does it for a whole cycle
at once.

**Rejections need a reason** — both `reject <id> --reason` and `review reject
… --reason` require it and record it in the reject event's payload.

**`--set key=value`** is repeatable. Values are parsed as JSON with a
raw-string fallback, so `--set year=1815` yields an integer and `--set
venue=Nature` a string.

**A row cap below 1 is an error** — on every command that takes one: `node
list`, `edge list`, `events`, `review queue`, `suggest-links`, `subgraph`,
`cycle-list`, and `search`'s `-k`. Asking for fewer rows than exist used to
give you *more*: SQLite reads a negative `LIMIT` as "unbounded", so `nodum
events --limit -3` printed the whole log. Where the cap is applied in Python
instead the same typo did something different and quieter — it dropped that many
rows off the **end** of the list and answered normally, so a `review queue
--limit -1` was a proposal you never saw. One refusal now covers all of them,
and it names the option you actually typed (`k` for `search`, `limit`
elsewhere).

## Self-description

Two commands work without a database, which makes them safe to run against a
fresh install:

- `nodum --version` — prints `nodum <version>`.
- `nodum schema-dump` — prints the whole command tree as JSON, including
  parameters.

Note `schema-dump` (this CLI's own surface) is a different thing from `schema
<type>` (one node or edge type's catalog entry, read from the database).

## Full surface

Run `nodum schema-dump` for the machine-readable version of this list,
including every parameter.

### Graph

- `init` — Create the database (if needed) and apply pending migrations.
- `node create` — Create a node (`active` for a human; over MCP, per the
  agent's grant). `--space` is the **write target** — the space the node lands
  in, `main` when absent.
- `node get` — Fetch one node by id (plus its neighborhood when `--depth > 0`).
- `node list` — List nodes in creation order, optionally filtered. `--space`
  narrows to one space (default: every space in scope) and `--include-meta`
  adds the meta space (off by default).
- `node update` — Update a node (applies for a human or an `edit` grant; stages a proposed version on `suggest`).
- `node children` — List a node's children in position order.
- `edge create` — Create a typed, directed edge between two nodes.
- `edge create-batch` — Propose a batch of edges; bad suggestions are reported, not fatal.
- `edge list` — List edges, optionally filtered by incident node, type, or state.
- `types` — Show the full type catalog (node types and edge types).
- `schema <type>` — Show one type's catalog entry, including its JSON schema.

### Traversal and search

- `search <query>` — Hybrid-search node title + content (BM25 + vector,
  RRF-fused). Takes the same two read-side space controls as `node list`:
  `--space` and `--include-meta`. **Terms are ORed under a quorum, not ANDed**:
  a node matches when the query terms it carries are worth at least half the
  query's inverse-document-frequency weight, counting **content words only** —
  ordinary English function words (`what`, `does`, `how`, `let`, …) are dropped
  first, so the words a question is *asked* with never outvote the word it is
  *about*. A rare word earns a match and a common one costs nothing, so the fix
  for an empty result is usually a *different* word rather than a shorter query
  — but a **rarer** one only helps if the graph holds it. **A query the graph
  knows no content word of matches nothing at all**, deliberately: `zarquon`
  finds nothing, so *"What does zarquon protect against?"* answers with nothing
  too, rather than with whatever notes happen to share its phrasing. In that
  case the word to change is the one the question is *about*. (With an embedding
  provider configured, the vector signal has no such rule and still returns its
  nearest chunks — a result whose every hit shows only a `vector` signal is
  telling you the keyword half found nothing.) A query may carry at most 64
  distinct terms.
  `--nl` asks the model to rewrite the question into search terms first and adds
  a `rewrite` object saying what was asked on your behalf; it is a rewrite of
  the words only — every signal, filter and cap below it is unchanged — and with
  no provider it is a no-op that says so and searches your own words.
- `traverse` — Walk the subgraph reachable from a node over active edges.
- `subgraph <root-id>` — Bounded, filtered neighborhood of a node — node and edge caps stop the walk.
- `find-path` — Find the shortest path between two nodes over active edges.
- `suggest-links <prefix>` — Suggest wikilink targets by title prefix (case-insensitive).

### Asking the graph

These three read and **never write**. Each prints one JSON object and exits 0
whatever the model did: a question nothing answered is an ordinary result
carrying `answered: false` and a `refusal` saying why, not a failure. Exit 1
still means what it always did — your own error (a blank question, a node that
does not resolve). With no provider configured the refusal names
`NODUM_LLM_MODEL`.

- `ask <question> [--k N] [--space S]` — Answer a question from the graph, with
  citations. **`answered` is computed, never taken from the model**, and it is
  exactly four deterministic checks: at least one cited id is a note this
  request actually retrieved; the model did not *also* name a note that does not
  exist while offering only one that does; every number in the answer appears in
  the text that was really sent or in your question (`unsupported_numbers` lists
  what did not); and there is answer text. Anything the model cited that this
  retrieval did not return lands in `unresolved`, and a failed check means the
  answer text is not returned.
  **It does not mean the answer is true.** A model that invents content while
  citing a real node passes all four — citation resolvability is not
  groundedness — so the envelope is built to be read rather than trusted:
  `considered` is what reached the model (empty when no call was made, which
  `used.calls` corroborates), `truncated_notes` is what reached it **in part**
  and every citation carries the same `truncated` flag, `dropped` is what the
  retrieval found and the context window could not carry at all, and `used` is
  what the attempt cost.
  A `[n]` at the start of a line is how a note is introduced to the model, so
  one in your question — or in a node's text — is rewritten to `(n)` before the
  prompt is built. It reads the same and can no longer open a note that was
  never retrieved.
- `summarize <node-id> [--depth N]` — Summarise a node and its neighbourhood,
  under the same citation and grounding rules. It reads the subgraph whether or
  not a provider is configured, so a node that does not exist is the ordinary
  not-found refusal rather than a complaint about the model. **What it sends is
  narrower than what you can read**: archived, proposed and meta-space nodes the
  walk returned are never put in front of the model — `ask` cannot reach them
  either, and two endpoints on one install must not disagree about what leaves
  the machine — and they are named in `withheld`, with each note's `state` in
  the envelope. `truncated` stays the separate fact that the *walk* stopped at
  its cap.
- `llm status [--no-probe]` — Whether a provider is **configured**, and
  separately whether it is **reachable**. The two are different facts:
  configuration is free and permanent, reachability costs one small call and is
  true only of this instant. `reachable` is therefore tri-state, and `null` is
  *not established* rather than "not asked": nothing was configured, `--no-probe`
  declined it, or the probe got no answer inside `call_timeout` — which is
  deliberately not `false`, because a refused connection is a server that is not
  running while no answer yet is very often a live server loading a model. The
  failing probe is free (nothing listening answers in about 3 ms, a model the
  server does not have in about 4); `--no-probe` spends nothing at all. It takes
  `--as` although it reads no graph, because the probe is a real model call and
  every model call in this system is attributed — and `used` reports what it
  spent (34 tokens, measured). The probe waits the run's own
  `NODUM_LLM_CALL_TIMEOUT`, so the sentence in `detail` and the number in
  `call_timeout` agree.
  The `context_tokens` it reports is `NODUM_LLM_CONTEXT_TOKENS` — **the window
  your server serves, not the one the model card advertises**. With `ollama`
  that is `num_ctx` (`OLLAMA_CONTEXT_LENGTH`, 4096 unless you raise it),
  applied to every model it serves; setting this above it means an over-long
  prompt is sent instead of refused, and the server answers from the part it
  read without saying so. A **recognised** model name (for example
  `deepseek-v4-flash`) supplies its endpoint's real window, so the setting you
  are most likely to get wrong is one you need not set. Recognition is an
  **exact** match on a hosted model id, and only when you have not set
  `NODUM_LLM_BASE_URL` yourself: a name that merely starts with `deepseek-`
  (`deepseek-r1:8b` and the rest of the ollama library) is a local model and
  stays on your local endpoint. Once you name a base URL, **that URL decides
  which profile applies, and a model name decides nothing**: point at
  `https://api.deepseek.com/v1` and you still get DeepSeek's own window and
  modes, because the profile is a fact about the endpoint; point anywhere else
  and no model name can give that server a window it does not have.

  `NODUM_LLM_BASE_URL` must be a URL that can be posted to, scheme included. A
  scheme-less `api.deepseek.com/v1` is **not** repaired into one — choosing
  `http` or `https` for you decides whether your `NODUM_LLM_API_KEY` crosses the
  network in clear text — so it is reported the way an unusable setting always
  is here: no provider, a `detail` naming the variable and the fix, and exit 0.

  **Your key goes only to an endpoint you named.** Either you set
  `NODUM_LLM_BASE_URL`, or your model name is exactly a hosted model id nodum
  ships a profile for. A model id it does not recognise falls back to the local
  default — a host nodum chose, not one you configured a key for — so the key is
  left behind rather than posted there, and `api_key_withheld` says so with the
  sentence that fixes it. A local gateway that requires a key keeps it: naming
  that gateway in `NODUM_LLM_BASE_URL` is you saying the key belongs to it.
  Three more fields say what your provider is really doing, as opposed to what
  you asked it for. **`structured_output`** is `json_schema` or `json_object`:
  under the first, the server's constrained decoding makes an answer the schema
  forbids impossible to produce; under the second — which is what a provider
  that refuses JSON schemas gets — the schema is only stated in the prompt, so
  it is a request rather than a guarantee. **`thinking`** is your
  `NODUM_LLM_THINKING` level, and **`thinking_applied`** says whether it reached
  the endpoint at all: `ollama` accepts only `none`, so a graded level
  configured against it is withheld and the model runs at its own default.
  **`effective_max_output_tokens`** is what `NODUM_LLM_MAX_OUTPUT_TOKENS` is
  worth against this window — never more than half of it, because the answer and
  the prompt share the window on a server like `ollama`. It is the ceiling
  `summarize` uses; `ask` reserves its own smaller number (2 048, measured
  against a worst case of 528 output tokens over 24 samples) and the
  reachability probe a smaller one again, because the call sites do not need the
  same room.

### History and state

- `events` — Show the most recent event-log entries (newest first). `--cycle
  <id>` narrows to one consolidation cycle: that is a journal entry's diff, read
  off the append-only log rather than stored a second time.
- `history <node-id>` — Show a node's version history (chronological).
- `diff <a> <b>` — Unified diff between two versions of one node.
- `undo [seq]` — Reverse an event, restoring the prior state from its payload.
  An event carrying a `cycle_id` is **not** undoable here — `rollback` takes
  the whole cycle instead — and the no-argument search *finds* those rather than
  stepping over them, so a bare `undo` after a consolidation is answered by a
  refusal naming the cycle instead of by silently reversing an older write.
  **A version review comes back whole on both halves.** Undoing an `accept`
  restores the node *and* puts the proposal back to `proposed` (reported as
  `restored_version`), so it is a queue item again rather than a row stuck on
  `applied` over content that has gone back; a `reject` is itself reversible, so
  a rejection made by mistake is one command away from being reviewable again.
- `accept <id>` — Accept a proposed node, edge, or update (proposed → active). Human only.
- `reject <id> --reason` — Reject a proposed node, edge, or update (proposed → archived). Human only.
- `archive <id>` — Archive an active node or edge (active → archived).

### Review queue

- `review queue` — List pending proposals with reviewer context.
- `review accept` / `review reject` — Act on proposals by id; bad ids are reported, not fatal.
- `review accept-all` / `review reject-all` — Act on every proposal matching the filters.

### Accounts, grants, and spaces

- `human create/list/passwd/disable/enable` — Manage human accounts (passwordless
  until `passwd`; argon2id, six characters minimum, and setting one ends that
  human's live sessions). The last enabled human cannot be disabled: with none
  enabled, no surface can mint a principal at all, the CLI's trusted-local path
  included.
- `agent create/list/token-rotate/disable/enable` — Manage agent accounts
  (`create`/`token-rotate` print the show-once token to stderr). Every account
  created here is **external** and there is no flag for anything else: the
  service refuses an internal one, because the gardener is selected by being the
  only row of that kind and a second one takes it away rather than adding to it.
- `grant <agent> <space> <level>` / `revoke <agent> <space>` / `grants [--agent]` —
  Event-logged grant administration, levels `read`/`suggest`/`edit`. `revoke`
  reaches an **archived** space, by id or by name: archiving makes a grant
  inert but keeps the row, and a grant with no way to remove it would be an
  authority you cannot take back. `grant` refuses an archived space, since the
  grant would confer nothing until someone undid the archive.
- `space-create` / `space-list` / `space-rename` / `space-archive` — Spaces as
  nodes: a space is a node of builtin type `space` living in the meta space, so
  creating one is a node create, renaming one is a title update, and archiving
  one is a state transition — each event-logged, versioned, and undoable like
  any other write. `space-rename` and `space-archive` take a space id **or**
  name and refuse anything that is not a space. `space-list` reports each
  space's **live node count** (`active` + `proposed`; archived rows are retired,
  not territory) and the **agents granted on it**. These are all human-only.

  Four rules are enforced in the service, so every surface has them:

  - **`main` and `meta` cannot be archived** — by `space-archive` or by the
    generic `archive <id>`. Archiving `main` would hide it from every listing
    while every write that names no space kept landing there (that default
    resolves by id, whatever state the row is in), and archiving `meta` would
    retire the space every other space lives in. Neither failure reports
    anything as it happens, which is why the refusal is the guard rather than
    `undo` after the fact. A *rename* of either is fine: it moves the title and
    leaves the id alone.
  - **No two spaces can share a name.** A space reference resolves as
    `id = ? OR title = ?`, so a duplicate would make `--space research` mean
    whichever row SQLite reached first. Names are compared exactly, as the
    lookup does — `Research` and `research` are two spaces. A space **keeps its
    name when it is archived**: the name stays reserved, so that undoing the
    archive can never land on a name something else has taken. Reusing a
    retired name means renaming that space first (`node update <id> --title …`;
    `space-rename` resolves live spaces only).
  - **A space lives in `meta`.** `node create --type space --space main` is
    refused: a space nested in ordinary territory would still be listed by
    `space-list` and still resolve as real, while the grants governing it were
    the host space's — and renaming one is authorised by a grant on that host,
    which is how the name check above could be turned into a way to probe for
    spaces you cannot list. Renaming any space additionally requires a grant on
    `meta`, so a database written before this rule cannot reopen that.
  - **Archiving a space makes every grant on it inert.** While a space is
    archived, an agent granted on it can read nothing, write nothing, propose
    nothing and review nothing there — including nodes it reaches by id. The
    grant rows survive so `grants` still shows them and `revoke` still removes
    them, and undoing the archive restores the delegation unchanged.

  A space is used in two independent ways, and they are two controls rather
  than one mode: `--space` on a *read* (`node list`, `search`) narrows the view
  and defaults to every space in scope, while `--space` on a *write*
  (`node create`, `ingest`) targets where the node lands and defaults to `main`
  — reading one space while filing into another is the ordinary case. The read
  filter is a convenience, not a boundary: an agent stays confined to its
  grants underneath it, and a space it holds no grant on does not resolve at
  all, answering exactly as a nonexistent one does.

### Consolidation and the curative tier

- `consolidate` — Run a consolidation cycle: the gardener's four deterministic
  jobs, and its report. `--scope` confines it to one space, `--job` selects jobs
  (repeatable; default is all of them, in order), `--dry-run` computes
  everything and emits **no** event at all.
- `cycle-list` — List cycles, newest first: the dream journal. Human-only.
- `cycle-get <id>` — One journal entry: what ran, what it measured, how it
  ended. Human-only. It returns the **row alone**; what the cycle *changed* is
  `events --cycle <id>`, because the row stores no diff of its own.
- `cycle-abandon <id>` — Close a cycle a crash left `running`, as `failed`.
  Human-only, and the door out of an interrupted run: `rollback` refuses a cycle
  that has not closed and `undo` refuses every cycle-stamped event, so until it
  is closed the run's writes are irreversible on every surface. A cycle that
  already said how it ended is refused rather than re-closed. You should not
  have to remember the command: every refusal a stranded cycle causes names it
  with the id already filled in — the `rollback` refusal, and the "a
  consolidation cycle is already running" that now blocks *every* later run
  rather than only the ones in the same process.
- `cycle-stop <id>` — Ask a **running** cycle to stop: the kill switch.
  Human-only. It records who asked and when, and changes nothing else — the
  entry stays `running`, and the run closes its own entry `failed` when it
  next checks. Asking twice keeps the first asker rather than raising, so
  pressing it again is never ambiguous; a cycle that has said how it ended is
  refused, since nothing is left to obey it. See *stop, abandon, rollback*
  below for which of the three you want.
- `rollback <cycle-id>` — Take a whole cycle back, all of it or none of it.
  `--dry-run` reports what would be reversed and what stands in the way.
  Human-only. A review the cycle performed is part of "the whole of it":
  `restored_versions` names the proposals put back to `proposed`, and a cycle
  that did nothing but reject is a cycle with something to take back.
- `merge-nodes <ids…> --into <id>` — Merge nodes into a survivor. Soft and
  reversible: nothing is destroyed.
- `retype <ids…> --type <t>` — Change nodes' type. The one sanctioned exception
  to a node's type being fixed at creation. Per-item failures are reported in
  `failed[]`, named on stderr, **and in the exit code**: 1 if any node was
  skipped, exactly as `ingest file` does, so a run that accomplished nothing
  cannot report success.
- `supersede-edge <edge-id>` — Retire an edge that stopped being true,
  optionally naming its successor with `--src`, `--dst`, `--type`,
  `--confidence` and `--set`.
- `bulk-relink` — Repoint or retype many edges at once. `--src`/`--dst`/
  `--type`/`--state` select, `--to-type`/`--to-dst` say what changes, and
  `--dry-run` prints the diff and writes nothing. Its answer separates two
  things that used to share a list: `unchanged` is bare edge ids the change
  would not alter (a fact about the diff — you asked for something already
  true), while `skipped` is the refusals with their reasons — a self-loop, a
  duplicate the graph already carries, or a space you may not edit. Because
  `skipped` is now the failures and nothing else, the exit code is derived from
  it exactly as `retype`'s is: **1 if anything was refused**, with each one named
  on stderr. `unchanged` never affects it. **A `--dry-run` exits 0 whatever it
  predicts** — every check a real run makes runs on the rehearsal, so its
  `skipped` is accurate, but nothing was attempted there and nothing was lost.

**Stop, abandon, rollback — three verbs, three different situations.** They all
end up near a `failed` journal entry, which is exactly why they are worth
keeping straight.

| You want to | Run | What it does | What it does *not* do |
|---|---|---|---|
| Wind down a run that is going right now | `cycle-stop <id>` | Records who asked and when. The entry stays `running`; the run closes its own entry `failed` when it next checks. | Close the entry. Reverse anything. |
| Close the entry of a run that is never going to finish | `cycle-abandon <id>` | Closes it `failed` from outside, with a report naming who closed it — which is what makes its writes reversible at all. | Stop a process. Reverse anything. |
| Take back what a closed cycle wrote | `rollback <cycle-id>` | Reverses every event the cycle wrote, all of it or none of it. | Work on a cycle that has not closed. |

The order is the order: a run you stopped closes itself, and then you can roll it
back. A run a `SIGKILL` ended never closes itself, so you abandon it first and
roll it back after. **Neither stop nor abandon reverses a single write** —
stopping and undoing are two decisions, and a switch that did both would make
"stop, look at what it did, then decide" impossible.

`cycle-stop` records an instruction; what obeys it is the run. Today the only
check is the one immediately before a model call, so a run of the **four
deterministic jobs** — which make none — finishes even after you stop it, with
the stop kept on the entry. The switch is worth having now because it is the
model-spending half it was built to bound, and the entry says who asked and when
whichever way the run ended.

The writes a cycle makes are the **gardener's** (`agent:builtin-gardener`), an
internal agent seeded with `read` on `meta` and `edit` on `main` as ordinary
grant rows —
they show up in `space-list` and `nodum revoke builtin-gardener main` takes them
away. Every other space needs an explicit
`nodum grant builtin-gardener <space> edit`; `consolidate --scope` on a space
the gardener holds nothing on refuses with that command in the message rather
than running — and that refusal reaches every surface intact, the journal's
scope picker in the browser included. Two consolidation cycles never run at
once **against one database file**, and that is stronger than it sounds: the
guard is a uniqueness rule on the journal, not a lock inside one interpreter, so
a `nodum consolidate` you type here while `nodum serve` runs one is refused too.
It used not to be — both ran, and every duplicate pair was proposed twice into
the review queue. The refusal is *a consolidation cycle is already running*,
naming the cycle in the way and the `nodum cycle-abandon <id>` that clears it,
because a run that was killed never closes itself and would otherwise block
every later run. Curative operations and `rollback` are deliberately outside the
rule: each is one short operation you asked for, and blocking those for the
length of a nightly sweep would take the curative tier offline every night.
Ctrl-C
closes the cycle `failed` on the way out, so an interrupted run is still one a
`nodum rollback <cycle-id>` can take back. `--as` on `consolidate` names who
*asked*, which the journal records as `triggered_by`, and that is deliberately not the same thing as who acted: an
entry carrying only one of the two could answer "I did not ask for this" or
"who ran this at 04:00", never both.

**The gardener proposes; it does not dispose.** Duplicate candidates become
`proposed` `duplicate_of` edges in the review queue — a merge is always
human-approved — and every edge it infers is filed `proposed` even though its
grant would let it write live, because a suggestion nobody reviews is not a
suggestion. What it does apply are the two prunings a machine can be right
about: an exact duplicate edge, and an edge incident to an archived node.

**What a cycle changed is not in its report.** The report says what each job
examined, proposed, applied and skipped, plus the coherence metrics before and
after; the diff is `nodum events --cycle <id>`, read off the same append-only
log as everything else, so the journal can never become a second record that
disagrees with what happened.

**Every curative command runs inside a cycle**, including when you type it
yourself — each writes several rows from one decision, and `undo` reverses one
row from one payload, so undoing half a merge would leave the other half
standing. `rollback <cycle-id>` is therefore the way back from all of them, and
`undo` refuses a cycle-stamped event by name rather than doing the wrong thing
quietly. A rollback is itself a cycle, so rolling *that* back re-applies the
original.

The refusal names **`rollback` and nothing else**, and that is deliberate. It
briefly also named "the last write outside a cycle" as an `undo <seq>` you could
still run, on the reasoning that pointing at rollback alone was a loop. It is
not a loop — `nodum rollback <cycle-id>` reverses the cycle, and no state
follows it in which a bare `undo` is what you need. Meanwhile the event that
sentence named is exactly the one this refusal exists to keep `undo` away from:
running it reaches *past* the cycle, so a merge you wanted back cost you an edge
it had relinked, and that undo then became a conflict standing between the merge
and its rollback — both reversal verbs spent, the merge unrollbackable. A
reversal verb that reaches past a cycle is the harm the refusal prevents, so the
refusal does not print one as a remedy.

**A rollback refuses rather than clobbers.** If anything outside the cycle has
touched a row the cycle touched, nothing is written and the refusal is this
CLI's one structured error — `{"error": {"type", "message", "conflicts"}}` on
stdout, each conflict naming both the cycle event that wrote the row and the
later event that moved it, with the message on stderr and exit 1 as usual.
`--dry-run` asks the same question without the refusal.

### Derived indexes

- `projector run` — Apply pending event-log entries to the derived indexes.
- `projector status` — Show every projector's checkpoint, backlog, and derived-store size.
- `projector rebuild <name>` — Drop one projector's derived state and replay the full event log.

### Assets

- `asset register` — Register a file as a content-addressed asset (idempotent dedup by sha256).
- `asset get` — Show one asset's metadata (never its bytes). Takes `--as`.
- `asset list` — List every registered asset. Takes `--as`.
- `asset rendition` — Fetch a rendition, generating and caching it on first
  request. `--profile` takes `thumb` or `preview` for an image, or `page:<n>`
  for a 1-based page of a PDF. Takes `--as`.
- `asset download-url` — Mint a short-lived, single-use URL for an asset's
  original bytes. Takes `--as`.
- `asset upload-url` — Mint a short-lived, single-use URL to PUT one file to
  (`--name`, `--mime`, `--size` required). Takes `--as`.
- `asset purge` — Evict stored renditions (they rebuild on next request).

The reads carry `--as` because they read through the graph — an
`asset_ref` node id is a valid handle, and it resolves only in a space the
caller can read. `register` and `purge` touch the blob store alone.

A `page:<n>` raster is an ordinary rendition: same lazy generation, same cache,
same eviction by `asset purge`. It needs the `pdf` extra, which it names rather
than failing at import time.

The two capability-URL commands are the escape hatch for a host that shares no
filesystem with the graph. Both print the token **once** — only its sha256 is
stored — and log both the mint and the later redemption. `--ttl` is bounded
(1 second to 1 hour, 5 minutes by default). An `upload-url` whose `--sha256`
this graph already holds answers with the existing `asset` and **no** `grant`:
the bytes are here, so no bytes move. The URLs resolve against `nodum serve`,
which has to be running; set `NODUM_PUBLIC_URL` when that server is not on the
default address.

### Ingestion

- `ingest file <path>…` — Ingest local files: register the bytes, extract text,
  describe, propose. Takes `--as`.
- `ingest url <url>` — Fetch an `http`/`https` URL into the blob store and
  ingest it exactly like a local file. Takes `--as`.
- `ingest handlers` — List every extraction handler, its MIME families, and
  whether it can run. No `--as`, and no database.

Each document becomes an asset, an `asset_ref` node describing those bytes in
one space, a `source` node whose content is the extracted text, a
`derived_from` edge between them, and one `block` child per page of text. Every
write goes through the ordinary service API, so the subgraph lands in the state
the writer's grant earns.

**`ingest file` takes one or more paths, and a directory argument ingests the
files directly inside it** (`--recursive` walks deeper). Dot-names and anything
that is not a regular file are skipped, and the rest are ingested in sorted
order, so the same folder ingests the same way twice. One path naming a *file*
prints that ingestion as a single JSON object; anything else — several paths,
or a directory — is a batch and prints `{"ingestions": [...], "count": n}`.
`--name` and `--title` describe one document and are refused for a batch;
`--space` applies to all of it.

**A batch never loses its successes.** Each file is ingested on its own; one
that fails prints its reason and then `skipped <path>` on stderr, and the batch
carries on. Every file that landed is in the envelope on stdout. **The exit
code is 1 if any file failed**, so a non-zero exit from `ingest file` means
"read stderr for what is missing", not "nothing happened". Re-running is safe:
ingestion is idempotent per `(hash, space)`, so what already landed comes back
with `created: false` instead of being duplicated.

`ingest url` fetches `http`/`https` only, once, with a timeout and a size
ceiling, and refuses a redirect that leaves those two schemes. It does *not*
block loopback or private ranges — nodum is itself a loopback service — so
granting ingestion grants the server's network position.

`ingest handlers` is the answer to "my PDF produced no text": it reports every
handler with its MIME families, whether it is `available`, and — when it is not
— a `detail` naming the extra to install. Plain text, Markdown, JSON, and HTML
are handled by the standard library and always work; PDF text (`pdf`), image
OCR (`ocr`, which also needs the `tesseract` binary on PATH) and audio
transcription (`audio`) are optional extras. A missing handler is never fatal:
the asset is still registered and still described, and the result says plainly
that no text came out.

### Servers

- `serve` — Serve the human web UI and its JSON API.
- `mcp serve` — Launch the MCP server on stdio (read + additive tiers); the
  agent token comes from `NODUM_AGENT_TOKEN`.

## Serving

Every `/api` route but `POST /api/login` needs a valid session — log in with
a human name and password (`nodum human passwd` sets one). A non-loopback
bind is allowed: login, not the bind, is the boundary, and the session cookie
gains `Secure` there — `serve` warns on stderr that it speaks plain HTTP, so
put TLS in front of it or the password crosses the network in the clear. `serve` prints the database path on stderr and
translates a port already in use into the contract's exit 1.

Account and grant administration is on the API as well: `GET /api/me` returns
the session's human, and `/api/humans`, `/api/agents` and `/api/grants`
mirror the CLI's `human`/`agent`/`grant`/`revoke`/`grants` commands — the
show-once agent token comes back in the create/token-rotate response body.
Spaces mirror their commands the same way: `GET /api/nodes` and
`GET /api/search` take `?space=` and `?include_meta=`, `POST /api/nodes` takes
`space` in the body, and the lifecycle is `POST /api/spaces`,
`POST /api/spaces/{id}/rename` and `POST /api/spaces/{id}/archive`, with
`GET /api/spaces` returning exactly what `nodum space-list` prints.
`POST /api/ingest` mirrors `nodum ingest`, taking exactly one of `path` and
`url`. The consolidation journal is there too: `GET /api/cycles` (newest first,
byte-identical to `nodum cycle-list`),
`POST /api/cycles` (run one now, with optional `scope` and `dry_run`),
`GET /api/cycles/{id}`, `POST /api/cycles/{id}/abandon` (close a run that never
finished) and `POST /api/cycles/{id}/rollback`, where
a graph that has moved on is a **409** carrying the conflicting rows rather than
a bare refusal — and asking for a cycle while one is running is a 409 too, since
the request was fine and the graph was busy. `GET /api/cycles/{id}` is the one
place the two surfaces deliberately differ: it composes the row, its metrics and
`events --cycle` (bounded by `?limit=`, with `events_truncated` when it bit)
into one round trip, because a browser paints one screen from one request, while
`nodum cycle-get` returns the row and leaves the diff to `nodum events --cycle`.
`POST /api/cycles` runs the cycle **off the event loop**, so the rest of the
server keeps answering for the minutes a real cycle takes. That frees the loop
and not the database: SQLite has one writer, so a read issued while a cycle runs
queues behind whichever burst holds the write lock — measured at **1168 ms**
against **5 ms** on an idle server. The server stays responsive; individual
requests do not stay fast.
The curative tier has no HTTP routes at all — it is the CLI's.
The two capability-URL redemption routes — `GET /api/download/{token}`
and `PUT /api/uploads/{token}` — are the only `/api` routes outside the session
gate: the single-use token in the path *is* the authorisation, so there is no
ambient cookie for a cross-origin page to ride.

```sh
nodum serve [--host 127.0.0.1] [--port 8600] [--allow-host NAME] [--db PATH]
```

**The nightly consolidation cycle is configured by environment, not by flag.**
Set `NODUM_CONSOLIDATE_AT` to a 24-hour local wall-clock time and `nodum serve`
runs one cycle a night in the process it is already running — no cron, no second
process. Unset means **off**, which is the default: a background process that
writes to the graph without being asked is not something to enable by surprise,
and a flag would put that one keystroke away from an ordinary `serve`. A value
that is set but unreadable is announced on stderr and ignored, rather than
stopping the server from booting — and a value that **works** is announced too,
in the startup banner beside the database path, since a background writer on the
graph is the last thing that should start silently. "One a night" holds across
daylight-saving changes: the wait is computed in aware local time, so the
25-hour night does not run two cycles and the 23-hour one does not run late.

**A night the schedule skipped says so in the log, not in the journal.** Cycles
are serialised, so a cycle you started yourself — from `nodum consolidate` or
the journal's run button — that is still going at the configured time makes the
timer bounce off it. That is a *skip*: it is logged as one, at warning level
with the reason and no traceback, and it writes nothing to the journal, because
the journal records runs that happened and the cycle that ran that night is
already in it, listed under whoever asked for it.

The line matters most in the case that is not benign. A cycle a `SIGKILL` or a
power cut left `running` never closes itself and now blocks **every** later
night, so the warning repeats — and it carries the whole refusal, which names
that cycle and the `nodum cycle-abandon <id>` that clears it. The journal shows
the cause too: the blocking cycle is sitting in `nodum cycle-list` as
`running`, which is where "why has nothing run since Tuesday" is answered.

```sh
NODUM_CONSOLIDATE_AT=03:30 nodum serve
```
