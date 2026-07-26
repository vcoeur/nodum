---
title: Concepts · nodum
description: The ideas nodum is built on — nodes and typed edges, the state machine, the event log, actors and privilege, and projectors.
---

# Concepts

## Nodes and typed edges

Knowledge is a **typed graph**, not a folder of files. A node carries Markdown
content plus structured props; an edge is typed and directed. Both are governed
by a **type catalog** stored in the database, and each type carries a JSON
schema that the service layer validates against.

```sh
nodum types --as owner       # the catalog
nodum schema note --as owner # one type's entry, including its JSON schema
```

Because the catalog lives in the database rather than in code, the schema can
evolve at runtime without a release.

### Markdown is the truth

Node content is Markdown, and `[[wikilinks]]` inside it are **materialised as
real edges** — the prose and the graph cannot disagree, because one derives
from the other. A link only becomes (or stops being) an edge as far as the
writer's grants reach: a mention into a space they may only suggest in stays
`proposed`, and one into a space they cannot read at all is left exactly as it
is, since an unresolvable link is not a deleted one.

## The state machine

Every node and edge is in exactly one state:

```mermaid
stateDiagram-v2
    [*] --> proposed
    [*] --> active
    proposed --> active: accept
    proposed --> archived: reject
    active --> archived: archive
```

`proposed` is the waiting room for anything an agent wrote. `archived` is how
things retire — nodum does not delete.

## The event log

Every mutation appends an entry with **full before/after payloads**. That one
decision buys three properties at once:

- **Versioned** — a node's history is a sequence of snapshots (`nodum history`).
- **Auditable** — who changed what, when, and with what reason.
- **Reversible** — `nodum undo` restores the prior state from the payload.

The log is also the input to the projectors, below.

## Actors and privilege

nodum assumes humans and agents both write, and separates them **at the service
layer** rather than by convention. There are human and agent **accounts** (tables
`humans` and `agents`), and per-(agent, space) **grants** at three hierarchical
levels: `read` ⊂ `suggest` ⊂ `edit`.

| | Human | Agent |
|---|---|---|
| A write lands as | `active` | `proposed` on a `suggest` grant, `active` on `edit` |
| Can accept / reject / archive | yes | only with `edit` on the item's space |
| Can undo | yes | no |
| Administers accounts and grants | yes | no |

The human-only set is not delegable, whoever filed the proposal. `undo` most of
all, since restoring an event's payload can write `state = 'active'` back.

### Proposed updates

An agent with a `suggest` grant editing a node does not overwrite it. It stages
a `proposed` **version** that records *which fields it named*. Accepting applies
only those fields to the node as it stands at that moment, so a human edit made
while the proposal waited survives.

### Grants

A grant is one row per (agent, space). It is set with
`nodum grant <agent> <space> <level>` (human-only, event-logged). A `suggest`
grant queues everything for review; an `edit` grant writes live and carries
in-space review authority. There is deliberately no auto-accept machinery: an
agent earns `edit`, or it waits.

### Spaces

A space is the second axis beside the type graph: `main` and `meta` are seeded,
and `nodum space-create` adds more. A space is itself a **node** — builtin type
`space`, living in the meta space — so creating, renaming
(`nodum space-rename`) and archiving (`nodum space-archive`) one are ordinary
node writes, each event-logged, versioned and undoable. `nodum space-list`
reports every active space with its live node count and the agents granted on
it.

Two spaces may not share a name, because every space reference resolves as
`id = ? OR title = ?` and a duplicate would make `--space research` mean
whichever row the database reached first. The comparison is exact — `Research`
and `research` are two spaces, and both resolve — and an **archived** space
keeps its name. A retired name stays reserved for good: archiving is not
deletion, and the one route back (undoing the `node.archive` event) has to be
able to put the space back exactly as it was, which it cannot do if something
else took the name meanwhile. The price is that a retired name is not reusable
unless you rename that space.

`main` and `meta` cannot be archived. Every write that names no space lands in
`main`, and that default resolves by id whatever state the row is in, so
archiving it would hide the space while nodes kept arriving there; `meta` is the
space that spaces themselves live in. Renaming either is fine — a rename moves
the title, and it is the id everything structural depends on.

**Archiving a space cuts every agent off it.** That is usually the reason to
reach for it, so it is what it does: while a space is archived, a grant on it
confers nothing — no reads, no writes, no proposals, no review — whether the
call names the space or reaches a node inside it by id. The grant *rows* are
kept rather than deleted, so `nodum grants` still lists them and
`nodum revoke <agent> <space>` still takes one away (by the space's id or its
name, archived or not), and undoing the archive puts the delegation back exactly
as it was. Granting on an archived space is refused: it would confer nothing
until someone undid the archive, which is delegation by accident.

A space is used two ways, and they are deliberately two separate controls:

- **Reading** — an optional filter (`--space` on `node list` and `search`,
  `?space=` on the HTTP reads) that defaults to *every space in scope*.
- **Writing** — a target (`--space` on `node create` and `ingest`, `space` in
  the `POST /api/nodes` body) that defaults to `main`.

Reading one space while still filing into another is the ordinary case, which
is why a single "current space" switch would not do. The read filter is a
convenience and **not** a permission boundary: an agent stays confined to its
grants underneath it, and a space it holds no grant on does not resolve at all
— answering exactly as a space that does not exist would, so the filter is
never an existence oracle. Archiving a space retires it from the vocabulary;
nothing moves, and every node in it keeps its `space_id`.

The web UI (`nodum serve`) says the same thing with controls instead of flags.
Search, the graph and the review queue carry a **space filter** that defaults to
every space in scope; a single **write target**, sticky across sessions and
shared by every open tab, says where a new node lands and is shown on every
surface that creates one — a target the human cannot see is how work gets filed
somewhere nobody chose. The `/spaces` screen is the lifecycle: every active
space with its live node count and the agents granted on it, plus create,
rename and archive. The review queue groups proposals by space and then by
agent, which is the only way a space that governs itself — an agent holds
`edit` there, so its writes land `active` and never queue — can be told apart
from a space where nothing happened. And because the server refuses an unknown
space and an ungranted one with identical words, no screen ever reports a space
as missing; it says what changed instead.

## Projectors and derived indexes

Search indexes are **projections of the event log**, not a second source of
truth. Each projector tracks a checkpoint, can report its backlog, and can be
dropped and replayed from event 0:

```sh
nodum projector status
nodum projector rebuild vec     # e.g. after an embedding-model change
```

Two ship today:

- **`fts`** — a SQLite FTS5 full-text index, giving BM25 keyword ranking. An
  ingested document's full extracted text is joined onto the `asset_ref` node
  that stands for its bytes — and onto that node only, so a word on page 3 does
  not match every other page of the document just as strongly.
- **`vec`** — a sqlite-vec chunk-embedding index, using a local in-process
  model. No daemon, no API key. Optional: without the `embeddings` extra it
  reports unavailable rather than failing.

**Hybrid search** fuses the two by reciprocal rank fusion, then re-ranks by
graph expansion — so a result's neighbours in the graph inform its rank.

## Assets

Binaries are content-addressed by sha256 and stored in the same file as the
graph, so one file is still the whole knowledge base. Registering the same
bytes twice is idempotent.

Derived `thumb` and `preview` renditions are generated lazily and cached; they
can be purged and will rebuild on next request. `page:<n>` is the third
rendition shape — a 1-based page of a PDF, rasterised on first request and
cached like any other. **Agents receive renditions, never originals.**

**An asset is as reachable as the nodes that describe it.** A principal may
read an asset exactly when it can read an active `asset_ref` node carrying that
hash — so asset access is an ordinary scoped graph read rather than a rule of
its own. Bytes nobody has described yet are visible to humans only, which is
the right default for a file whose ingestion has not run.

The one documented exception to "agents never receive originals" is a
**capability URL**: single-use, minutes-long, minted against a principal who
could already read the asset, and event-logged at both the mint and the
redemption. The token is a random secret stored only as its sha-256; the row is
the whole authority, so expiry, single use, and revocation are one update.

## Ingestion

`nodum ingest` is how a document becomes knowledge. A file, a folder, or a URL
turns into a small subgraph:

```mermaid
flowchart LR
    bytes["file · folder · URL"] --> asset["asset (sha256, bytes in-database)"]
    asset --> ref["asset_ref node (describes the bytes in one space)"]
    asset --> src["source node (extracted text)"]
    src -- derived_from --> ref
    src --> pages["block per page"]
    style bytes fill:#e6f0ff,color:#000
    style asset fill:#d9f2d9,color:#000
    style ref fill:#fff3cd,color:#000
    style src fill:#fff3cd,color:#000
    style pages fill:#fff3cd,color:#000
```

Every one of those writes goes through the ordinary service layer, so the
subgraph lands in the state the writer's grant earns — an agent with `suggest`
proposes the whole thing into the review queue. Ingestion is **idempotent per
(hash, space)**: re-running the same folder finds what already landed instead
of duplicating it, which is what makes an interrupted run safe to repeat.

Ingestion proposes *sources and structure* and stops there. Turning prose into
**claims** is a judgement call, and it belongs to the research agent — splitting
sentences and calling each one a claim would fill the review queue with noise
rather than knowledge.

### Extraction handlers degrade, they do not fail

Text, Markdown, JSON, and HTML are read by the standard library and always
work. PDF text, image OCR, and audio transcription are optional extras, and an
absent one is a **reported result, not an error**: the asset is still
registered, the nodes are still written, and the answer says plainly that no
text came out. A corrupt file is treated the same way.

```sh
nodum ingest handlers    # every handler, its MIME families, and what to install
```

Nothing is downloaded implicitly — as with the embedding model, a transcription
model is confined to its local cache unless you say otherwise.

## Surfaces are adapters

The CLI, the HTTP API, and the MCP server are thin adapters over one service
layer, each with its own identity rule and no logic of its own:

- **CLI** — human-only; every command that touches the graph names its human
  with a required `--as human:<id>`, reads included.
- **HTTP API** — every write is attributed to the session's human (password
  login, server-side session); no request field can say otherwise.
- **MCP server** — one agent, authenticated by its token (`NODUM_AGENT_TOKEN`),
  exposing the read and additive tool tiers *and nothing else*.

Because the logic lives in one place, the surfaces cannot drift apart.
