---
title: The gardener · nodum
description: Running the internal agent that consolidates the graph — what it may touch, how to run it on demand or nightly, how to give it a model and a budget, and how to read and reverse what it did.
---

# The gardener

The gardener is the internal agent (`builtin-gardener`) that runs
**consolidation cycles**: duplicate candidates, link maintenance, queue
curation, housekeeping, a neglect report, and — when funded — one LLM job,
abstraction. What those jobs are and why they are cut the way they are is in
[Concepts](concepts.md#consolidation-cycles-and-the-gardener). This page is
the operator's side: what the gardener holds, how to run it, how to give it a
model, and how to see and take back what it did.

Three things are true of it everywhere below. It holds no credential — it
authenticates by being in-process, so there is no token to mint, rotate or
leak. It writes through the same grant-scoped store as an external agent, so a
grant row is the whole of its authority. And everything it *infers* it files
`proposed`, even where its grant would let it land live: its suggestions wait
in the review queue like anyone else's.

## What it holds

A fresh database seeds three grant rows for the gardener. They are ordinary
rows — `nodum grants` lists them beside every other agent's
(`--agent builtin-gardener` for just its three), and the Admin page shows them
the same way.

| Space | Level | Why |
|---|---|---|
| `main` | `edit` | Where every write that names no space lands, so where consolidation curates. |
| `conventions` | `edit` | Its own workspace: the curation job's learned conventions are ordinary `note` nodes here. |
| `meta` | `read` | Types and spaces are nodes in `meta`; resolving a type is a *read* of the vocabulary. |

The `meta` row is `read` on purpose. No consolidation job writes the type
vocabulary, and `edit` on `meta` was tried and rolled back: it bought only
authority no job reaches — creating spaces, renaming `main`, retitling the
`concept` type, archiving the `note` type, after which a human can no longer
write a note. A
grant is a ceiling, and this one is set at what the jobs need.

Widening and narrowing use the commands that already exist:

```sh
nodum grant builtin-gardener research edit --as human:owner   # let it curate another space
nodum revoke builtin-gardener conventions --as human:owner    # stop the convention notes (leave `curation` out of --job to skip the job)
nodum revoke builtin-gardener main --as human:owner           # a gardener with no grant does nothing
```

Every other space is an explicit grant — a cycle scoped to a space the
gardener holds nothing on refuses and prints that `nodum grant` line rather
than reporting that the space does not exist. An unscoped cycle sees only
what its grants let it see. A revoke takes effect from the **next** cycle: the
principal is minted when a run starts, so a run already in flight finishes
under the grants it began with, and `rollback` takes back whatever it wrote
in the meantime.

### Changing a level

`nodum grant` re-levels an existing row in place — the same command with a
new level, event-logged with the before and after. The Admin page in the
browser deliberately does not: its picker offers only the spaces the agent
holds **no** grant on, because re-levelling through an "add" control would be a
real action dressed as a no-op. To change a level there, **Revoke** the row,
then grant the space again at the new level from the picker that reappears.
The Admin page's **Grant** control posts to `POST /api/grants`, the same upsert
as `nodum grant`.

## Running it

Cycles run on demand or on a nightly schedule, and only one runs at a time
against a database file — the guard is the journal itself, so a cycle typed
at a terminal while `nodum serve` is running one is refused, and the refusal
names the cycle in the way.

On demand, from the CLI — rehearse first:

```sh
nodum consolidate --dry-run --as human:owner        # every job computes, nothing is written but the journal entry (a rehearsal still pays for the abstraction job's model calls when a budget is set)
nodum consolidate --as human:owner                  # the real thing, every space it holds a grant on
nodum consolidate --scope research --as human:owner # one space
nodum consolidate --job duplicate_candidates --job link_maintenance --as human:owner
```

`--job` names are `duplicate_candidates`, `link_maintenance`, `abstraction`,
`curation`, `housekeeping`, `neglect_report` — that is also their run order.
The writes are the gardener's (`agent:builtin-gardener`); `--as` names who
*asked*, which the journal records as `triggered_by`. In the browser, the
**Journal** page has the same run panel, with a scope picker and a
**Rehearse only** checkbox; over HTTP it is `POST /api/cycles`.

Nightly: set `NODUM_CONSOLIDATE_AT` to an `HH:MM` (24-hour, the server
process's local clock — UTC in a container that sets no `TZ`). `nodum serve`
runs the cycle in the process it already is, no cron and no second process,
and says so in its startup banner. Unset means off, which is the default; a
value it cannot parse is announced on stderr and ignored rather than
refusing to boot. `nodum consolidate` on demand works either way.

## Giving it a model

The deterministic jobs need no language model at all — a cycle runs fine on
a machine that has none. (The embedding-cosine signals inside duplicate and
link inference come from the local embedding model, which is a separate,
key-less thing — see [Configuration](configuration.md#embeddings). It is
manageable from `settings.env` and the Settings page like the LLM block, and
a model change there blinds every stored chunk to search until the vector
index is rebuilt — the Settings page confirms the change, then offers the
rebuild.) A language
model buys the **abstraction** job: a `proposed` `concept` node synthesised
from a dense cluster, with `derived_from` edges to its members. That job needs
both models: its cohesion gate is an embedding cosine, so without the embedding
model it reports that it did not run, whatever the LLM block says. Three
switches, and each is off until you set it:

| Variable | Off when | What it turns on |
|---|---|---|
| `NODUM_LLM_MODEL` | unset — no provider | Any model call at all, for the gardener and for the human-facing `ask` / `summarize` / `search --nl`. |
| `NODUM_LLM_CYCLE_BUDGET` | unset or `0` — the default | The gardener's LLM jobs. **A cycle with a provider and no budget runs its selection and reports that the abstraction job did not run.** The budget is tokens per cycle; `NODUM_LLM_CYCLE_SECONDS` (default 1800) bounds the same work in wall-clock time. |
| `NODUM_LLM_API_KEY` | unset — none sent | The bearer key, sent **only** to an endpoint somebody named: a `NODUM_LLM_BASE_URL` you set, or a model id nodum ships a profile for. When `NODUM_LLM_ENDPOINT` selects an endpoint, that endpoint's own `NODUM_LLM_KEY_*` is sent instead. |

Three shapes cover most installs. The simplest is to pick an endpoint by label
and give it its key — both storable, so this whole shape can be done from the
Settings page without touching the host:

```sh
nodum config set NODUM_LLM_ENDPOINT kimi --as human:owner
nodum config set NODUM_LLM_KEY_KIMI sk-… --as human:owner
nodum config set NODUM_LLM_MODEL kimi-k3 --as human:owner
nodum config set NODUM_LLM_CONTEXT_TOKENS 1000000 --as human:owner   # Kimi's window is per-model
nodum config set NODUM_LLM_CYCLE_BUDGET 200000 --as human:owner
```

The labels are `local`, `deepseek`, `kimi` and `openrouter`, and
`NODUM_LLM_ENDPOINTS` narrows that menu for a deployment. Each endpoint has its
own key variable, so changing the selection changes which credential travels
with it — a key can never reach an endpoint it was not entered for. A hosted provider nodum knows is two lines
plus the budget, because the exact model id brings its endpoint, its context
window and its structured-output mode with it:

```sh
export NODUM_LLM_MODEL=deepseek-v4-flash
export NODUM_LLM_API_KEY=sk-…
export NODUM_LLM_CYCLE_BUDGET=200000
```

Any other OpenAI-compatible endpoint names itself, and then nothing about the
model id can move a call off it:

```sh
export NODUM_LLM_BASE_URL=https://llm.example.com/v1   # scheme included, or it is refused
export NODUM_LLM_MODEL=some-model-id
export NODUM_LLM_API_KEY=…
export NODUM_LLM_CONTEXT_TOKENS=128000    # the window the endpoint actually serves
export NODUM_LLM_CYCLE_BUDGET=200000
```

The default base URL is a local `ollama` (`http://localhost:11434/v1`), which
needs no key. A model id nodum does not recognise, with no base URL of your
own, goes *there* — and a key set for it is deliberately withheld rather
than posted to a host you did not name. Every variable in the block, with its
default, is in [Configuration](configuration.md#the-llm-block).

**`export` is one of two ways.** Everything above except `NODUM_LLM_BASE_URL`
and `NODUM_LLM_ENDPOINTS` can be stored instead, in `settings.env` beside the
graph:

```sh
nodum config set NODUM_LLM_MODEL deepseek-v4-flash --as human:owner
nodum config set NODUM_LLM_API_KEY sk-… --as human:owner
nodum config set NODUM_LLM_CYCLE_BUDGET 200000 --as human:owner
```

The environment still wins over the file, so a variable already exported is
refused rather than stored where it would never be read — unset it first. What
you gain is that a change applies **without a restart**: the budget funds the
next cycle rather than the next boot, and a model applies at the next call.
`NODUM_LLM_BASE_URL` and `NODUM_LLM_ENDPOINTS` stay environment-only on purpose
— which endpoints a key may travel to is a deployment decision, the first naming
one outright and the second bounding the menu the Settings page offers. The
choice *within* that menu (`NODUM_LLM_ENDPOINT`) is storable, because every
endpoint on it is compiled into the build rather than typed into a form.
`nodum config list` reports which layer each value is in force from.

Then ask what actually resolved, before the first cycle:

```sh
nodum llm status --as human:owner
```

It reports `configured` (from the environment, free) and `reachable`
(one small call — `null` means not established, which is very often a live
server still loading a model), the resolved `model`, `context_tokens` and
`structured_output` mode, and `api_key_withheld`: `null` when no key is set
*or* the key is being sent, a sentence naming the endpoint it would have gone
to when it is not. A smart feature that answers `false` starts here.

In a container, the variables reach the server through the compose
`environment:` block — the [compose example](deploy.md#compose-example)
shows the shape. The key is the one secret nodum reads from the environment:
keep it in an env file (`env_file:`, or `${NODUM_LLM_API_KEY}` interpolation
from one), never in the image and never in a committed compose file. Note the
`${VAR:-}` form renders an **empty** value when the host env file says nothing,
and an empty value is *unset* at every layer — so a key stored in
`settings.env` is still used, and `nodum config list` will say so. `nodum consolidate` and `nodum llm status`
run inside the container (`docker compose exec <service> nodum …`) inherit
the same environment.

## Reading what it did, and taking it back

Every cycle leaves a journal entry — the **dream journal**, `/journal` in the
browser, `nodum cycle-list` and `nodum cycle-get <id>` on the CLI: what ran,
who asked, what it measured (five coherence metrics, before and after), what
the LLM jobs spent, and how it ended. What it *changed* is a separate answer,
read off the same append-only log as everything else:

```sh
nodum events --cycle <id> --as human:owner
```

Its `duplicate_of` and `relates_to` edges, its concept nodes and its
convention notes are ordinary proposals with an ordinary actor: they sit in
the review queue (`nodum review queue --as human:owner`, or **Review** in the
browser) until a human accepts or rejects them, and the curation job's
acceptance rates and per-item annotations are the record it keeps of how that
went — it never accepts and never rejects on its own.

Three verbs sit around a run, and they do different things (the
[Commands](commands.md#consolidation-and-the-curative-tier) table has them side
by side):

- `nodum cycle-stop <id>` — the **kill switch** for a run in flight. It
  records that a human asked, and who, and when; the run closes itself
  `failed` at its next check, which sits before every model call — and only
  there: a cycle that makes no model call (no budget, or the deterministic
  jobs alone) runs to completion and closes `completed` with the stop
  recorded on it, which is not a failure. It reverses nothing.
- `nodum rollback <id>` — reverses a **closed** cycle whole, in one
  transaction, and refuses rather than clobbers when anything outside the
  cycle has since touched a row it wrote. A rollback is itself a cycle.
- `nodum cycle-abandon <id>` — closes the entry of a run that will never
  finish (a killed process, a power cut), which is what makes its writes
  reversible again.

`undo` refuses every cycle-stamped event and names `rollback` instead: a
curative write is several rows from one decision, and reversing one of them
would leave the other half standing.
