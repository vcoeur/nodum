"""The consolidation runner — the gardener's jobs (design §8.4/§8.5).

Phase 5 was cut at the LLM line, and this module is everything on the near side
of it: the five deterministic jobs a cycle can run with arithmetic over data
the file already holds, and the five coherence metrics they are measured by.
There is no provider, no generation, and no judgement in those five — design
Constraint 4 keeps the LLM out of validation, the state machine and the
projectors, and they run on a machine with no model present.

**The abstraction job (5b-ii's first) is the deliberate exception.** Its
selection is exactly as deterministic as the other four — dense, sized, not
already synthesized, all computed before any model call — and the model writes
the text and nothing but the text: it never decides *whether* to synthesize,
only what the synthesis says. The call goes through :mod:`nodum.agent`'s one
door (:meth:`nodum.agent.AgentRun.chat`, which is what consults the kill
switch), and the writes land through the same public :mod:`nodum.service`
functions as every other job's, proposed for review like any inference.

**The internal agent is a peer client (§8.4 rule 1).** Every read and every
write goes through a public :mod:`nodum.service` function, exactly as the MCP
server's do. This module opens no connection, imports no service private, and
touches no table — which is what makes the gardener an agent with grants rather
than a back door with a name. ``tests/test_consolidate.py`` asserts it over this
file's AST, so the rail survives a refactor that forgets it.

**The gardener acts, the human asks.** Writes are attributed to
``agent:builtin-gardener`` (:func:`nodum.auth.internal_principal`), because the
gardener made them. The ``cycles`` row separately records ``triggered_by`` — the
human who asked, or the literal ``scheduler``. Collapsing the two would either
credit a human with edits they never chose or hide that a human asked for the
run, and those are different questions a journal has to be able to answer.

**Every write goes inside the cycle.** :func:`nodum.service.in_cycle` is backed
by a ``ContextVar`` that :func:`nodum.service._emit` reads, so the ordinary
public calls this module makes are stamped without naming a cycle anywhere. The
runner opens the cycle, does its work inside the block, and closes it. That
stamp is what a rollback takes back.

**A dry run writes a journal entry and nothing else.** The ``cycles`` table
carries a ``dry_run`` column and :func:`nodum.service.open_cycle` takes the flag
precisely so a rehearsal is *in* the journal — "the journal has to say which it
was". So a preview opens a cycle flagged ``dry_run``, computes every job, writes
the report, and emits **zero events**: ``list_events(cycle_id=…)`` on a dry-run
cycle is empty, which is the machine-checkable form of "it changed nothing".
This deliberately differs from :func:`nodum.service.bulk_relink`, whose dry run
opens no cycle at all: that one is a diff a human is looking at right now, while
this one is a rehearsal of the nightly run and its whole point is to be
reviewable afterwards.

**The report is not the diff.** ``cycles.report`` carries what each job examined,
proposed, applied and skipped, plus the coherence metrics before and after. What
the cycle *changed* is ``service.list_events(cycle_id=…)`` — the same append-only
log everything else reads — so the dream journal can never drift from what
actually happened.

**One cycle at a time, in the whole file.** The guard is a row, not a lock:
``0014``'s partial unique index admits at most one ``running`` cycle whose
trigger is a consolidation trigger, so :func:`nodum.service.open_cycle` refuses
the second opener on the INSERT with :class:`CycleInProgress`. Every job's
"leave what is already there alone" is a read followed by a write with no
transaction spanning it, so two concurrent runs proposed every duplicate pair
twice — and rolling one cycle back left the other's copies standing.

A module-level lock was the first cut and it guarded the wrong half. It covered
the surfaces sharing one interpreter — the HTTP route, the nightly task, an
in-process caller — and covered a ``nodum consolidate`` fired at a terminal
while the server ran one not at all: both completed, 1580 ``duplicate_of`` edges
over 790 pairs, two journal rows for one human intention. It is **gone** rather
than kept beside the index: it enforced the same rule one layer higher, with a
second sentence for the identical condition and no ability to name the cycle in
the way, and it was also *too wide* — two runs over two different database files
in one process are not a conflict and it refused them anyway.

The refusal is immediate and says so: a blocking wait would hang a request
thread for the length of a cycle and then run a second cycle over a graph the
first had just changed, which is two journal entries for one human intention.
It names the cycle holding the file and ``nodum cycle-abandon <id>``, because a
run killed by a ``SIGKILL`` never closes itself and would otherwise block every
later run behind advice nobody can carry out.

**A curative cycle and a rollback are deliberately outside it.** Each is one
short, human-driven operation, and blocking them for the length of a nightly
sweep would take the curative tier offline every night; neither is what proposes
a duplicate pair twice.

**Revocation bites at the next cycle, not mid-flight.** The gardener's principal
— and with it its grant set — is minted once, when the run starts, so a grant
revoked or a space archived while a cycle is in progress does not stop the cycle
that is already running: its remaining writes land under the grants it started
with, and the change takes effect from the next cycle. This is the same window
:func:`nodum.service.disable_agent` documents for the MCP server, whose
principal is held for the life of the process, and it is stated here for the
same reason — the archive dialog promises an agent can do nothing the moment a
space is archived, and one cycle is how long that promise takes to become true.
A cycle is minutes at most, and :func:`nodum.service.rollback_cycle` takes back
whatever it wrote in the meantime.

**Determinism.** No randomness, and one clock per run: every age is measured
against :func:`_utcnow`, captured once when the cycle opens, so a test pins the
whole run by patching one function. Every pair, group and list is ordered before
it is written, so the same graph consolidates the same way twice.
"""

from __future__ import annotations

import difflib
import itertools
import json
import math
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nodum import agent, auth, embeddings, projectors, service
from nodum.migrations import GARDENER_AGENT_ID, META_SPACE_ID
from nodum.models import CycleOut, EdgeOut, NodeOut
from nodum.principal import Principal
from nodum.store import GrantNotPermitted

#: Re-exported from :mod:`nodum.service`, where the guard now lives: the refusal
#: is the ``cycles`` row a second opener cannot insert. It stays reachable under
#: this name because that is what every caller catches and what
#: ``http_api.EXCEPTION_STATUS`` maps to 409 — and it is the *same class*, not a
#: second one, so a surface holding either reference is holding the one thing.
CycleInProgress = service.CycleInProgress

# ── Job names (the `jobs=` selector's vocabulary) ─────────────────────────────

#: Entity resolution, candidates only (D9: a merge is always human-approved).
JOB_DUPLICATES = "duplicate_candidates"

#: Link inference (embedding proximity, co-citation) and the two prunings a
#: machine can be right about.
JOB_LINKS = "link_maintenance"

#: Fractional-position rebalance (D3) and embedding catch-up (D6).
JOB_HOUSEKEEPING = "housekeeping"

#: Temporality maintenance, report only — deciding a claim is *stale* is
#: judgement, and judgement is 5b.
JOB_NEGLECT = "neglect_report"

#: Concept synthesis — 5b-ii's first LLM job. The deterministic gates select
#: the clusters (dense, sized, not already synthesized); the model writes the
#: one concept they have in common. The model never decides *whether* to
#: synthesize, only what the text says.
JOB_ABSTRACTION = "abstraction"

#: Queue curation (§L1–§L4) — the learned half of §8.3, computed from **row
#: state**: a proposer's historical acceptance rate on each type they write,
#: filed as convention notes in the ``conventions`` space and one annotation
#: per queue item. Deterministic — no model call, nothing gated on the
#: proposer's own ``confidence``, and (by default) nothing accepted.
JOB_CURATION = "curation"


# ── Edge types the jobs write ─────────────────────────────────────────────────

#: Seeded in migration ``0001`` and never used until now. A ``duplicate_of``
#: edge *is* the duplicate proposal: it has a review queue, a diff and an accept
#: button already, so entity resolution needs no new proposal kind and adds
#: nothing to the review surface.
DUPLICATE_EDGE_TYPE = "duplicate_of"

#: The state every edge a job suggests is filed in, whatever the gardener's
#: grant would otherwise allow (see :func:`_write_edges`). A suggestion nobody
#: reviews is not a suggestion.
SUGGESTION_LANDING = "proposed"

#: The honest type for an inferred link. Embedding proximity and co-citation are
#: evidence that two nodes are *about* related things — they are not evidence
#: that one supports, cites, or is part of the other, and ``relates_to`` is the
#: seeded symmetric type that says exactly that much. No type node is invented.
RELATED_EDGE_TYPE = "relates_to"

#: What a synthesis's concept node claims of its members. Seeded in migration
#: ``0001`` and already used by ingestion (:data:`nodum.ingest.PROVENANCE_EDGE`)
#: for provenance; here it is what makes "part of this synthesis" a graph fact
#: rather than a prop, so the review queue and a later supersede can see it.
DERIVED_FROM_EDGE_TYPE = "derived_from"


# ── Thresholds ────────────────────────────────────────────────────────────────

#: Title-similarity bar for a duplicate candidate, over the normalised title key
#: (:func:`_title_key`) using :class:`difflib.SequenceMatcher`.
#:
#: 0.95 is set by the false positives immediately below it rather than by taste.
#: ``"Meeting 2026-07-01"`` vs ``"Meeting 2026-07-02"`` scores 0.944 and
#: ``"Chapter 1"`` vs ``"Chapter 2"`` scores 0.889 — dated and numbered siblings
#: are the commonest titles in a personal graph and must never be proposed as
#: duplicates of each other. ``"Kafka Stream"`` vs ``"Kafka Streams"`` scores
#: 0.96 and is one. The bar is deliberately at the top of the range because the
#: output costs human attention: a missed duplicate is found next cycle, a wrong
#: one is a queue item somebody has to read and reject.
DUPLICATE_TITLE_RATIO = 0.95

#: Cosine bar for a duplicate candidate when embeddings are available.
#:
#: **Measured on a real corpus, and the measurement says this signal cannot
#: draw the bar.** The calibration corpus is 426 kasten prose notes
#: (``note/`` + ``literature/``, frontmatter and wikilinks stripped), scored on
#: 2026-08-02 by ``scripts/measure_kasten_calibration.py`` for volume and
#: precision rather than for band separation — the fixture-derived 0.72/0.38
#: pair separated the fixture's bands cleanly and still flooded on real
#: content, because a hand-built set that demonstrates a separation cannot
#: measure a false-positive rate. On that corpus real duplicate candidates —
#: the same-normalised-title pairs — scored **0.28-0.55**, overlapping the
#: related band completely, so no cosine bar can separate duplicates from
#: related on this content; only exact copies reach 1.000. That band is a
#: calibration-time observation, date-stamped here: the script now prints a
#: duplicate-candidate table, and on a corpus with no same-title groups it
#: reports the honest zero — the claim then rests on the measurement above and
#: re-running the script on a corpus that has such pairs is how it is
#: re-verified. The title-normalisation signal (:data:`DUPLICATE_TITLE_RATIO`)
#: is the real duplicate detector, and this bar stays where only exact copies
#: clear it (0.93). What it cannot express — a near-duplicate worded
#: differently clearing the *link* bar and arriving as ``relates_to`` — is
#: judgement for the learned-curation cycle (§L1 annotations), not a bar this
#: signal can draw.
#:
#: This bar must stay above :data:`LINK_EMBEDDING_COSINE`: the two are read by
#: different jobs, and a duplicate scoring *below* the link bar would be
#: described as merely related by the weaker signal.
DUPLICATE_EMBEDDING_COSINE = 0.93

#: Cosine bar for an inferred ``relates_to`` edge: "these are about the same
#: area", not "these are the same thing".
#:
#: **Measured on real content, not chosen from the fixture.** Measured
#: 2026-08-02 on the 426-note calibration corpus (kasten ``note/`` +
#: ``literature/``, frontmatter and wikilinks stripped, sampled 200) by
#: ``scripts/measure_kasten_calibration.py`` — the committed script reproduces
#: the method, and re-running it is how a drift is detected. The vault is a
#: live corpus (423 prose notes today), so re-runs land close to but not
#: exactly on the numbers below; see :data:`DUPLICATE_EMBEDDING_COSINE` for
#: why the fixture cannot set a bar.
#:
#: The shipped 0.80 measured **dead**: 0.04 ``relates_to`` per node on real
#: content, the gate's "5 at 0.80" reproduced. The reverted 0.38 measured as a
#: **flood**: 5.9-6.4 per node, the gate's 1 175/200 reproduced. At 0.60 the
#: bar fires at **~1.1-1.2 ``relates_to`` per node with ~6-10 % precision**
#: against the vault's own wikilinks as ground truth — the precision swings
#: with which linked pairs land in the 200-note sample, and it under-counts,
#: since the 0.907 'Software architecture for developers' ↔ 'A Philosophy of
#: Software Design' pair is clearly same-area and not wikilinked — and the
#: above-bar pairs are genuinely same-area by inspection.
#:
#: It must stay below :data:`DUPLICATE_EMBEDDING_COSINE`: the two are read by
#: different jobs, and a pair between the bars is merely related, never the
#: same thing.
LINK_EMBEDDING_COSINE = 0.60

#: The cohesion bar the abstraction job's clusters must clear — **reused, not
#: invented**: the calibrated same-area bar is the cohesion bar. A cluster is
#: cohesive when its members are at least as mutually related as the link bar
#: requires, and nothing about "about the same area" changes when the pair is
#: part of a cluster rather than the start of one. This is why the job needs
#: the embedding provider — cohesion is its one vector signal, and there is no
#: degraded mode to fall back to.
ABSTRACTION_COHESION_COSINE = LINK_EMBEDDING_COSINE

#: Smallest cluster the abstraction job will consider. Two notes are a pair,
#: which the link job already expresses as a ``relates_to`` edge; a synthesis
#: is what several notes have in common, and "several" starts at three.
MIN_CLUSTER_MEMBERS = 3

#: Largest cluster one synthesis may absorb. A ten-member cluster is already a
#: long prompt; a larger one is several concepts pretending to be one.
MAX_CLUSTER_MEMBERS = 10

#: How many clusters one cycle may synthesize, whatever the file holds. The
#: model spend is what is capped — selection stays complete and the overflow
#: is reported, never silent.
MAX_CLUSTERS_PER_CYCLE = 5

#: How many characters of each member's content the synthesis prompt carries.
#: The model does not need the member whole to say what several members share,
#: and a ten-member cluster of long notes would otherwise blow a small window.
#: Sized so the **minimum 3-member cluster fits the default window** — the
#: assumption the bound is measured against: a 4 096-token window
#: (:data:`nodum.llm.DEFAULT_CONTEXT_TOKENS`) halves for the output
#: reservation, leaving ~2 048 tokens of prompt room; the 570-byte template
#: plus message wrapping leaves ~1 470 bytes for the members, and 3 × 400
#: ASCII characters fits inside with room for short titles (the prompt asks
#: for "short and descriptive" ones). The estimator
#: (:func:`nodum.llm.estimate_tokens`) counts UTF-8 bytes, so non-ASCII
#: content costs more and is *refused* rather than truncated — safe, and
#: itemised. Larger clusters degrade to :class:`nodum.agent.PromptTooLong`
#: and a per-cluster skip with a note, which is the honest path for a
#: ten-member cluster at this bound, not a failure.
ABSTRACTION_MEMBER_CHARS = 400

#: The synthesis prompt template. Deterministic framing only: the gates are
#: named as already decided, and the model is asked for the text — what these
#: notes have in common — and nothing else.
ABSTRACTION_PROMPT = """You are the gardener's abstraction job. A deterministic pass has
already decided that the notes below belong together and are not already part of a
synthesis; your job is the text and nothing but the text.

Write the one concept note that synthesizes what these notes have in common. Do not
invent claims none of them make, and do not carry over anything that is true of only
one member. The title is short and descriptive; the content is a few sentences of
Markdown.

Members:
{members}

Reply as JSON with exactly two keys: "title" (a string) and "content" (a string)."""

#: The structured-output envelope for :data:`ABSTRACTION_PROMPT`. It fixes the
#: shape and proves nothing about the content: the model can still answer
#: {title, content} that none of the members support, so the caller validates
#: the body parses and the *members* stay the deterministic half's record.
ABSTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["title", "content"],
    "additionalProperties": False,
}

#: :func:`nodum.agent.prompt_version` of :data:`ABSTRACTION_PROMPT` (A2),
#: computed once at import time so it changes when and only when the template
#: does.
ABSTRACTION_PROMPT_VERSION = agent.prompt_version(ABSTRACTION_PROMPT)

#: How many neighbours two nodes must share before co-citation is evidence.
#:
#: One shared neighbour is the ordinary shape of a document tree — every sibling
#: shares its parent — so a bar of one would propose an edge between every pair
#: of children of every page, which is a quadratic pile of queue items generated
#: by structure the graph already records. Two is the smallest bar that is
#: evidence rather than shape.
MIN_SHARED_NEIGHBOURS = 2

#: A node with more neighbours than this is a hub, and two nodes sharing a hub
#: have learned nothing about each other (a tag, an index page, a daily note).
#: Skipping hubs is also what keeps co-citation from going quadratic in one
#: node's degree.
MAX_COCITATION_DEGREE = 50

#: How long an active node may go untouched before the neglect report names it.
#: A quarter: long enough that an ordinary reference note is not "neglected" for
#: going unread over a holiday, short enough that a year-old claim is caught. The
#: job writes nothing, so the cost of a wrong bar is a line in a report.
NEGLECT_DAYS = 90

#: How far back a proposer's acceptance record reaches (§L1). A quarter, the
#: same horizon as :data:`NEGLECT_DAYS`: long enough that a rate is a record
#: rather than a mood, short enough that a proposer who changed is re-learned
#: within a year. A rolling window, so a rate never stops updating as the
#: proposer's history ages out.
CURATION_WINDOW_DAYS = 90

#: The ``conventions`` space (migration 0016): the gardener's own workspace,
#: where convention notes are ordinary ``note`` nodes the cycle writes like
#: anything else (L2), with the gardener holding ``edit`` on it alone. The
#: space node's id is literally ``conventions`` — the migration's reserved
#: name.
CONVENTIONS_SPACE_ID = "conventions"

#: The typed props schema of a convention node (§L2) — the keys, and nothing
#: else, so a reader of the ``conventions`` space knows what each note claims.
#: ``kind`` is always ``"acceptance-rate"`` (a future convention kind would
#: not silently share the schema), ``rate`` is :func:`_rate`'s rounded
#: quotient, and ``computed_at`` is the run's own clock
#: (:func:`_utcnow`), so a stale note says when it stopped being fresh.
CURATION_CONVENTION_PROPS = (
    "job",
    "kind",
    "proposer",
    "edge_type",
    "rate",
    "accepted",
    "rejected",
    "window_days",
    "computed_at",
)

#: The well-known props field that would turn auto-accept on (§L3). Read from
#: a ``note`` node in the ``conventions`` space: when the field is a number,
#: the interface exists; the accept direction stays OFF regardless — see
#: :func:`_job_curation` for why, and what would turn it on.
AUTO_ACCEPT_PROPS_KEY = "auto_accept_above"

#: Below this gap two siblings' fractional positions are close enough that
#: further insertion between them would run into float precision — the condition
#: D3's nightly rebalance exists for. See :func:`_job_housekeeping` for why it
#: cannot currently arise.
MIN_POSITION_GAP = 1e-6


# ── Bounds ────────────────────────────────────────────────────────────────────

#: Most nodes a job reads in one pass, in the spirit of
#: :data:`nodum.service.MAX_SUBGRAPH_LIMIT`: one cycle must not turn into an
#: unbounded read of the file. A scan that reaches it says so.
MAX_SCAN_NODES = 5000

#: Most edges a job reads in one pass.
MAX_SCAN_EDGES = 20000

#: Most nodes compared pairwise. Duplicate detection and embedding proximity are
#: both O(n²); 400 nodes is 79 800 comparisons, which is milliseconds, and the
#: overflow is reported through ``truncated`` rather than quietly dropped.
MAX_PAIRWISE_NODES = 400

#: Most ids any one list in the report carries. A cycle report is a report, not
#: a second copy of the graph — the full record is the event log.
MAX_REPORTED_ITEMS = 50


#: Node types consolidation refuses to touch, and the reason it also skips the
#: meta space wholesale: a ``space`` node *is* territory and a ``type`` node is
#: the vocabulary every other node is typed from. Both have their own lifecycles.
#: This tier curates knowledge. Taken from the curative tier so the two cannot
#: drift apart.
STRUCTURAL_TYPE_IDS = service.STRUCTURAL_TYPE_IDS


#: What :func:`_job_housekeeping` reports instead of rebalancing. Stated as a
#: constant so the test that pins the no-op pins the *reason* with it.
POSITION_NOOP_NOTE = (
    "no rebalance: `position` is only ever written by `create_node`, as "
    "`max(position) + 1.0` among siblings, and no reorder or move operation exists on any "
    "surface — so no sibling set can converge on float precision and there is nothing to "
    "spread out. Rebalancing anyway would rewrite every sibling's position, one event per "
    "node, for no gain. The gap check below is live: when a move operation lands and "
    "fractional positions start being written, this job starts reporting real work."
)


class JobOutcome(BaseModel):
    """What one consolidation job examined, proposed, applied and skipped.

    ``examined`` is the count of items the job actually looked at — nodes for
    the duplicate, housekeeping and neglect jobs, edges for the link job — and
    ``detail`` carries whatever else that job is about (the pairs it matched,
    the projector run, the neglected ids). ``proposed`` and ``applied`` are ids
    of writes the job made, ``skipped`` is one ``{id, reason}`` per item it
    deliberately left alone, and ``notes`` carries the sentences a reader needs
    to interpret the numbers — a degraded signal, a dry run, a no-op that is
    correct rather than unimplemented.

    ``error`` is set when the job raised: the job's own report survives its
    failure, and the other jobs' reports survive it too.
    """

    name: str
    examined: int = 0
    proposed: list[str] = []
    applied: list[str] = []
    skipped: list[dict[str, str]] = []
    notes: list[str] = []
    detail: dict[str, Any] = {}
    truncated: bool = False
    error: str | None = None


class JobFailure(BaseModel):
    """One job that raised, named with the exception that came out of it."""

    job: str
    error: str


class ConsolidationReport(BaseModel):
    """The JSON a cycle's ``report`` column carries — the dream journal's source.

    Deliberately *not* a diff: what the cycle changed is
    ``service.list_events(cycle_id=…)``, read from the append-only log, so the
    journal cannot become a second record that disagrees with it. ``metrics``
    is ``{"before": {...}, "after": {...}}``, each a JSON object keyed by metric
    name so 5b's two judgement-dependent metrics can be added without a
    migration.
    """

    scope: str | None
    dry_run: bool
    jobs: list[JobOutcome]
    metrics: dict[str, dict[str, float]]
    failed: list[JobFailure] = []
    notes: list[str] = []
    #: ``report["llm"]`` — :meth:`nodum.agent.AgentRun.report` of the run the
    #: abstraction job drove, as JSON, or ``None`` when no LLM job ran. Filed by
    #: :func:`_run_jobs` from the context (not returned from the job) so
    #: :data:`JOBS` keeps its one signature.
    llm: dict[str, Any] | None = None


class ConsolidationOut(BaseModel):
    """The runner's return: the journal entry, plus the report typed.

    ``cycle.report`` and ``report`` are the same data — the first as the journal
    stored it, the second parsed. ``cycle.dry_run`` is how a caller tells a
    rehearsal from a run, and ``cycle.status`` is ``failed`` when any job raised.
    """

    cycle: CycleOut
    report: ConsolidationReport


# ── The clock ─────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    """The instant every age in one run is measured against.

    A function rather than an inline call so a test pins the whole run by
    patching one name — the same reason the frontend's Vitest run pins ``TZ``.
    Stored timestamps are SQLite's ``datetime('now')``, which is UTC with no
    zone marker, so this must be UTC too or every age is off by the host's
    offset (and every test run in UTC would still pass).
    """
    return datetime.now(UTC)


def _parse_timestamp(value: str) -> datetime:
    """Read a stored ``datetime('now')`` string as the UTC instant it is."""
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _age_days(timestamp: str, now: datetime) -> float:
    """Age of a stored timestamp in days, never negative."""
    return max(0.0, (now - _parse_timestamp(timestamp)).total_seconds() / 86400.0)


# ── Pure helpers ──────────────────────────────────────────────────────────────


def _title_key(title: str | None) -> str:
    """The comparison form of a title: NFC, case-folded, punctuation flattened.

    Case folding can itself denormalise, so normalisation is applied on both
    sides of it — the same caseless-match recipe
    :func:`nodum.service._match_key` follows. Punctuation becomes a space and
    runs of whitespace collapse, so ``"Graph Theory"``, ``"graph  theory"`` and
    ``"Graph-Theory!"`` are one key.
    """
    folded = unicodedata.normalize("NFC", unicodedata.normalize("NFC", title or "").casefold())
    flattened = "".join(char if char.isalnum() or char.isspace() else " " for char in folded)
    return " ".join(flattened.split())


def _cosine(first: list[float], second: list[float]) -> float:
    """Cosine similarity of two vectors; 0.0 when either has no magnitude.

    Computed rather than assumed: the interface in :mod:`nodum.embeddings`
    promises vectors, not *unit* vectors, so a provider that does not normalise
    must not silently produce similarities above 1.
    """
    first_norm = math.sqrt(sum(value * value for value in first))
    second_norm = math.sqrt(sum(value * value for value in second))
    if not first_norm or not second_norm:
        return 0.0
    dot = sum(left * right for left, right in zip(first, second, strict=True))
    return dot / (first_norm * second_norm)


def _rate(numerator: int, denominator: int) -> float:
    """A ratio that is 0.0 on an empty graph rather than a ZeroDivisionError."""
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


def _median(values: list[float]) -> float:
    """Median of a sorted-able list; 0.0 when empty (an empty queue has no age)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _is_curatable(node: NodeOut) -> bool:
    """Is this a node consolidation may reason about at all?

    The meta space and the structural types are out: they are the vocabulary and
    the territory, not knowledge. Leaving them in would make every seeded type
    node an orphan and every space a duplicate candidate of the next.
    """
    return node.space_id != META_SPACE_ID and node.type not in STRUCTURAL_TYPE_IDS


def _proposal_space_ids(proposal: Any) -> set[str | None]:
    """The spaces one review-queue item touches, from public model fields alone.

    A proposed node states its own space; an edge's spaces are its endpoints'
    and an update's is its node's, both of which
    :class:`~nodum.models.ProposalOut` already reports in ``context`` (that is
    what lets the human UI group the queue by space). A referenced node that no
    longer resolves comes back as ``{id}`` alone, so every lookup is optional.
    """
    if proposal.node is not None:
        return {proposal.node.space_id}
    spaces: set[str | None] = set()
    for entry in proposal.context.values():
        if isinstance(entry, dict) and "space_id" in entry:
            spaces.add(entry["space_id"])
    return spaces


# ── The run context ───────────────────────────────────────────────────────────


@dataclass
class _Context:
    """One run's shared state: who acts, over what, and the clock.

    ``principal`` is always the gardener — the jobs never see the human who
    asked, which is what makes "who acted" answerable from the event log alone.
    ``scope`` is the *resolved* space id the cycle recorded (or ``None`` for the
    whole file), so every read narrows the same way the journal says it did.
    ``cycle_id`` is the open cycle's id — the only thing the abstraction job
    needs from the runner beyond the public reads, because the model runtime
    (:func:`nodum.agent.for_cycle`) is wired to that row's kill switch.
    """

    principal: Principal
    scope: str | None
    dry_run: bool
    path: str | Path | None
    now: datetime
    cycle_id: str
    _vectors: dict[str, list[float]] = field(default_factory=dict)
    #: Set by :meth:`edges` / :meth:`typed_edges` when a read returns *more
    #: than* :data:`MAX_SCAN_EDGES` rows (each fetches one past the cap so
    #: "exactly at the cap" — nothing dropped — and "past it" are distinct).
    #: Sticky: one capped read anywhere in the run flags the whole run, so a
    #: consumer knows an edge read dropped rows but not *which* read did.
    truncated: bool = False
    #: The :class:`~nodum.agent.LLMReport` of the run the abstraction job
    #: drove, filed into the cycle's report under :data:`nodum.agent.REPORT_KEY`
    #: by :func:`_run_jobs`. Read here rather than returned from the job so
    #: :data:`JOBS` keeps its one signature.
    llm_report: agent.LLMReport | None = None

    def nodes(self, *, state: str | None = None) -> list[NodeOut]:
        """Curatable nodes in scope, oldest first, capped at :data:`MAX_SCAN_NODES`."""
        rows = service.list_nodes(
            state=state,
            space=self.scope,
            principal=self.principal,
            limit=MAX_SCAN_NODES,
            path=self.path,
        )
        return [node for node in rows if _is_curatable(node)]

    def edges(self, *, state: str | None = None) -> list[EdgeOut]:
        """Readable edges, oldest first, capped at :data:`MAX_SCAN_EDGES`.

        Not narrowed by ``scope`` here — :func:`nodum.service.list_edges` takes
        no space filter — so callers intersect with a node-id set instead. One
        row past the cap is fetched so a graph with *exactly* the cap's worth
        of edges (nothing dropped) is not flagged, while an over-cap graph —
        ``list_edges`` orders oldest-first, so it drops the *newest* rows, the
        freshly rejected edges a suppression read exists to see — sets the
        context's ``truncated`` flag.
        """
        rows = service.list_edges(
            state=state,
            principal=self.principal,
            limit=MAX_SCAN_EDGES + 1,
            path=self.path,
        )
        self.truncated = self.truncated or len(rows) > MAX_SCAN_EDGES
        return rows[:MAX_SCAN_EDGES]

    def typed_edges(self, edge_type: str) -> list[EdgeOut]:
        """Edges of one type, in every state — up to :data:`MAX_SCAN_EDGES`.

        Oldest first, so above the cap the *newest* rows are the ones dropped,
        the freshly rejected ones a suppression read exists to see. Like
        :meth:`edges`, one row past the cap is fetched so the flag means
        "rows were dropped", not "the cap was exactly reached".
        """
        rows = service.list_edges(
            type=edge_type,
            principal=self.principal,
            limit=MAX_SCAN_EDGES + 1,
            path=self.path,
        )
        self.truncated = self.truncated or len(rows) > MAX_SCAN_EDGES
        return rows[:MAX_SCAN_EDGES]

    def vectors(self, nodes: list[NodeOut]) -> dict[str, list[float]] | None:
        """Embed these nodes, or return ``None`` when no provider is available.

        One vector per node, produced by :func:`nodum.embeddings.node_vectors`
        — the *same* chunking the ``vec`` projector indexes with, reduced to
        the single vector a pairwise cosine needs. This job used to embed each
        node's whole text in one call instead, which chunked nothing and so
        silently truncated anything past the model's window: the same node had
        one vector here and a different set in the projector, and a long node
        was compared on its opening page alone.

        Never raises: an absent model is the *default* posture of an install
        without the ``embeddings`` extra, and a consolidation cycle that fell
        over because nobody downloaded a model would be a nightly job that
        breaks on the commonest configuration there is. Results are cached for
        the run so two jobs over the same nodes embed once.
        """
        provider = embeddings.get_provider()
        if provider is None:
            return None
        missing = [node for node in nodes if node.id not in self._vectors]
        if missing:
            payloads = [{"title": node.title, "content": node.content} for node in missing]
            for node, vector in zip(
                missing, embeddings.node_vectors(provider, payloads), strict=True
            ):
                self._vectors[node.id] = vector
        return {node.id: self._vectors[node.id] for node in nodes}


def _pairwise_candidates(context: _Context) -> tuple[list[NodeOut], bool]:
    """The active nodes compared pairwise, and whether the cap bit."""
    active = context.nodes(state="active")
    return active[:MAX_PAIRWISE_NODES], len(active) > MAX_PAIRWISE_NODES


def _unordered(first: str, second: str) -> tuple[str, str]:
    """A pair as a stable unordered key (id order), for "are these connected?"."""
    return (first, second) if first <= second else (second, first)


# ── Job 1: duplicate candidates (D9 — the cycle proposes, a human merges) ─────


def _job_duplicates(context: _Context) -> JobOutcome:
    """Propose ``duplicate_of`` edges for nodes that look like the same thing.

    D9 says a merge is **always** human-approved in v1, and that is implemented
    by the cycle not performing one: the job writes a ``duplicate_of`` edge —
    the seeded type nothing has used until now — and a proposed edge is already
    a reviewable suggestion with a queue, a diff and an accept button. No new
    proposal kind, nothing new in the review queue, and
    :func:`nodum.service.merge_nodes` stays where it was.

    Detection is normalised-title equality, near-equality
    (:data:`DUPLICATE_TITLE_RATIO`), and embedding cosine
    (:data:`DUPLICATE_EMBEDDING_COSINE`) where a provider exists. With no
    provider the job degrades to titles alone and says so — it never raises.
    A pair that already carries a ``duplicate_of`` edge in *any* state is left
    alone, and only one direction of a pair is ever proposed: the newer node is
    the duplicate *of* the older one.
    """
    outcome = JobOutcome(name=JOB_DUPLICATES)
    candidates, outcome.truncated = _pairwise_candidates(context)
    outcome.examined = len(candidates)
    if outcome.truncated:
        outcome.notes.append(
            f"compared the {MAX_PAIRWISE_NODES} oldest active nodes only: "
            "pairwise comparison is quadratic"
        )

    vectors = context.vectors(candidates)
    if vectors is None:
        outcome.notes.append(
            f"no embedding provider ({embeddings.unavailable_reason()}): "
            "title similarity only, so near-duplicates worded differently are not found"
        )

    known = {
        _unordered(edge.src_id, edge.dst_id) for edge in context.typed_edges(DUPLICATE_EDGE_TYPE)
    }
    if context.truncated:
        outcome.truncated = True
        outcome.notes.append(
            f"an edge scan hit MAX_SCAN_EDGES: reads above the {MAX_SCAN_EDGES}-edge "
            "cap drop the newest rows, so the duplicate_of suppression read may have "
            "missed edges — a rejected pair could be re-proposed"
        )
    matched: dict[tuple[str, str], tuple[list[str], float]] = {}
    for index, older in enumerate(candidates):
        for newer in candidates[index + 1 :]:
            signals, score = _duplicate_signals(older, newer, vectors)
            if not signals:
                continue
            if _unordered(older.id, newer.id) in known:
                outcome.skipped.append(
                    {
                        "id": f"{newer.id}->{older.id}",
                        "reason": "a duplicate_of edge already links this pair",
                    }
                )
                continue
            matched[(newer.id, older.id)] = (signals, score)

    suggestions = [
        {
            "src": source,
            "dst": target,
            "edge_type": DUPLICATE_EDGE_TYPE,
            "confidence": round(min(max(score, 0.0), 1.0), 4),
            "props": {"job": JOB_DUPLICATES, "signals": signals},
        }
        for (source, target), (signals, score) in sorted(matched.items())
    ]
    outcome.detail["pairs"] = [[source, target] for source, target in sorted(matched)][
        :MAX_REPORTED_ITEMS
    ]
    outcome.detail["matched"] = len(matched)
    if context.dry_run:
        outcome.notes.append(f"dry run: would propose {len(suggestions)} duplicate_of edge(s)")
        return outcome
    _write_edges(context, outcome, suggestions)
    return outcome


def _duplicate_signals(
    older: NodeOut, newer: NodeOut, vectors: dict[str, list[float]] | None
) -> tuple[list[str], float]:
    """Which duplicate signals fire for one pair, and the strongest score."""
    signals: list[str] = []
    score = 0.0
    older_key, newer_key = _title_key(older.title), _title_key(newer.title)
    if older_key and older_key == newer_key:
        signals.append("title-equal")
        score = 1.0
    elif older_key and newer_key:
        ratio = difflib.SequenceMatcher(None, older_key, newer_key).ratio()
        if ratio >= DUPLICATE_TITLE_RATIO:
            signals.append("title-similar")
            score = max(score, ratio)
    if vectors is not None:
        cosine = _cosine(vectors[older.id], vectors[newer.id])
        if cosine >= DUPLICATE_EMBEDDING_COSINE:
            signals.append("embedding")
            score = max(score, cosine)
    return signals, score


# ── Job 2: link inference and pruning ─────────────────────────────────────────


def _job_links(context: _Context) -> JobOutcome:
    """Prune the two kinds of edge a machine can be right about, then infer.

    **Pruning is exactly two rules.** An exact duplicate edge (same source,
    destination and type — the oldest is kept, the rest archived) and an edge
    incident to an archived node. Anything that requires judgement about an
    edge's *value* is 5b, and there is deliberately no third rule here.

    Both rules touch ``active`` edges only. Retiring a ``proposed`` edge is a
    *review* decision, and the review queue belongs to the human — a gardener
    that quietly rejected proposals nobody had seen would be deciding on their
    behalf. Proposed edges that meet a rule are reported as skipped instead.

    **Inference** proposes ``relates_to`` from two independent signals:
    embedding proximity (:data:`LINK_EMBEDDING_COSINE`) and co-citation — two
    nodes with at least :data:`MIN_SHARED_NEIGHBOURS` neighbours in common,
    hubs excluded. Pruning runs first so co-citation counts are not inflated by
    duplicate edges.

    **Two suppressions, and the second is what makes the queue finite.** A pair
    the graph already connects, in any type and any non-archived state, is never
    proposed — and neither is a pair carrying an *archived* ``relates_to``,
    because archiving is what rejecting does, and a proposal the human has
    already refused must not come back the next night. The second read is scoped
    to ``relates_to`` alone, so a pair whose ``duplicate_of`` proposal was
    rejected can still be proposed as merely related: refusing "these are the
    same thing" is not refusing "these are about the same area". That conversion
    happens once and then settles, since the new read holds the replacement
    down.
    """
    outcome = JobOutcome(name=JOB_LINKS)
    nodes = context.nodes()
    in_scope = {node.id for node in nodes}
    active_ids = {node.id for node in nodes if node.state == "active"}
    archived_ids = {node.id for node in nodes if node.state == "archived"}
    edges = [
        edge for edge in context.edges() if edge.src_id in in_scope and edge.dst_id in in_scope
    ]
    outcome.examined = len(edges)
    outcome.detail["nodes_in_scope"] = len(nodes)

    _prune_edges(context, outcome, edges, archived_ids)
    _infer_links(context, outcome, active_ids)
    return outcome


def _prune_edges(
    context: _Context, outcome: JobOutcome, edges: list[EdgeOut], archived_ids: set[str]
) -> None:
    """Archive exact-duplicate edges and edges incident to an archived node."""
    active = [edge for edge in edges if edge.state == "active"]
    reasons: dict[str, str] = {}

    groups: dict[tuple[str, str, str], list[EdgeOut]] = {}
    for edge in active:
        groups.setdefault((edge.src_id, edge.dst_id, edge.type), []).append(edge)
    for group in (groups[key] for key in sorted(groups)):
        if len(group) < 2:
            continue
        # A stable sort on `created_at` alone, because the group arrives in
        # `created_at, rowid` order and `datetime('now')` has one-second
        # resolution: two edges written in the same second are the ordinary case
        # here, and breaking that tie on the *id* would keep whichever uuid
        # sorted first rather than the row that was actually written first.
        ordered = sorted(group, key=lambda edge: edge.created_at)
        for edge in ordered[1:]:
            reasons.setdefault(
                edge.id,
                f"exact duplicate of edge {ordered[0].id} "
                "(same src, dst and type; the oldest is kept)",
            )

    for edge in sorted(active, key=lambda edge: edge.id):
        if edge.src_id in archived_ids or edge.dst_id in archived_ids:
            reasons.setdefault(edge.id, "incident to an archived node")

    for edge in edges:
        if edge.state == "proposed" and (
            edge.src_id in archived_ids or edge.dst_id in archived_ids
        ):
            outcome.skipped.append(
                {
                    "id": edge.id,
                    "reason": "proposed and incident to an archived node: retiring a proposal "
                    "is a review decision, and the queue belongs to the human",
                }
            )

    outcome.detail["prunable"] = len(reasons)
    if context.dry_run:
        for edge_id in sorted(reasons):
            outcome.skipped.append(
                {"id": edge_id, "reason": f"dry run: would archive — {reasons[edge_id]}"}
            )
        return
    for edge_id in sorted(reasons):
        try:
            service.transition(
                edge_id,
                "archive",
                reason=reasons[edge_id],
                principal=context.principal,
                path=context.path,
            )
            outcome.applied.append(edge_id)
        except (service.InvalidTransition, service.RecordNotFound, GrantNotPermitted) as refusal:
            outcome.skipped.append({"id": edge_id, "reason": str(refusal)})


def _infer_links(context: _Context, outcome: JobOutcome, active_ids: set[str]) -> None:
    """Propose ``relates_to`` from embedding proximity and co-citation."""
    live = context.edges()
    connected = {_unordered(edge.src_id, edge.dst_id) for edge in live if edge.state != "archived"}
    # A rejected proposal has to stay rejected. Rejecting archives the edge, so
    # reading live edges alone drops the pair back out of `connected` and the
    # next cycle proposes it again — a queue nobody can empty by working it.
    # `_job_duplicates` never had the hole; it reads every state of its own edge
    # type, and this mirrors it. Scoped to `relates_to` on purpose: some other
    # edge type archived for its own reasons is not a judgement about
    # relatedness, and reading every archived edge here would suppress proposals
    # nobody ever refused.
    connected |= {
        _unordered(edge.src_id, edge.dst_id) for edge in context.typed_edges(RELATED_EDGE_TYPE)
    }
    if context.truncated:
        outcome.truncated = True
        outcome.notes.append(
            f"an edge scan hit MAX_SCAN_EDGES: reads above the {MAX_SCAN_EDGES}-edge "
            "cap drop the newest rows, so the relates_to suppression read may have "
            "missed edges — a rejected pair could be re-proposed"
        )
    neighbours: dict[str, set[str]] = {}
    for edge in live:
        if edge.state != "active" or edge.src_id == edge.dst_id:
            continue
        if edge.src_id not in active_ids or edge.dst_id not in active_ids:
            continue
        neighbours.setdefault(edge.src_id, set()).add(edge.dst_id)
        neighbours.setdefault(edge.dst_id, set()).add(edge.src_id)

    shared: dict[tuple[str, str], int] = {}
    hubs = 0
    for hub in sorted(neighbours):
        adjacent = neighbours[hub]
        if len(adjacent) > MAX_COCITATION_DEGREE:
            hubs += 1
            continue
        for pair in itertools.combinations(sorted(adjacent), 2):
            shared[pair] = shared.get(pair, 0) + 1
    outcome.detail["hubs_skipped"] = hubs

    candidates, truncated = _pairwise_candidates(context)
    outcome.truncated = outcome.truncated or truncated
    vectors = context.vectors(candidates)
    if vectors is None:
        outcome.notes.append(
            f"no embedding provider ({embeddings.unavailable_reason()}): "
            "co-citation only, so semantically near nodes with no shared neighbour are not linked"
        )
    near: dict[tuple[str, str], float] = {}
    if vectors is not None:
        for index, first in enumerate(candidates):
            for second in candidates[index + 1 :]:
                cosine = _cosine(vectors[first.id], vectors[second.id])
                if cosine >= LINK_EMBEDDING_COSINE:
                    near[_unordered(first.id, second.id)] = cosine

    cocited = {pair for pair, count in shared.items() if count >= MIN_SHARED_NEIGHBOURS}
    suggestions: list[dict[str, Any]] = []
    for pair in sorted(set(near) | cocited):
        if pair in connected:
            continue
        source, target = pair
        if source not in active_ids or target not in active_ids:
            continue
        signals: list[str] = []
        props: dict[str, Any] = {"job": JOB_LINKS}
        confidence: float | None = None
        if shared.get(pair, 0) >= MIN_SHARED_NEIGHBOURS:
            signals.append("co-citation")
            props["shared_neighbours"] = shared[pair]
        if pair in near:
            signals.append("embedding")
            confidence = round(min(max(near[pair], 0.0), 1.0), 4)
        props["signals"] = signals
        suggestions.append(
            {
                "src": source,
                "dst": target,
                "edge_type": RELATED_EDGE_TYPE,
                "confidence": confidence,
                "props": props,
            }
        )

    outcome.detail["inferred"] = len(suggestions)
    if context.dry_run:
        outcome.notes.append(f"dry run: would propose {len(suggestions)} relates_to edge(s)")
        return
    _write_edges(context, outcome, suggestions)


# ── Job 3: housekeeping (D3 positions, D6 embedding catch-up) ────────────────


def _job_housekeeping(context: _Context) -> JobOutcome:
    """Check the position invariant D3's rebalance exists for, then catch embeddings up.

    **The rebalance is a correct no-op, not an unimplemented one.**
    :func:`nodum.service.create_node` is the only writer of ``position`` and it
    writes ``max(position) + 1.0`` among siblings; there is no move, reorder or
    insert-between operation on any surface, so positions are integral and
    append-only and no sibling pair can converge on float precision. The gap
    check below is therefore live rather than decorative: the day a reorder
    operation lands and starts writing fractional positions, this job begins
    reporting pairs that need spreading out, and only then is there a rebalance
    to write. Inventing the scheme now would rewrite every sibling's position —
    one event per node, every night — to fix a condition that cannot occur.

    **Embedding catch-up drives the existing projector.** D6's re-embed path is
    :mod:`nodum.projectors`' ``vec`` projector, which already chunks, embeds and
    checkpoints off the event log; the job runs it rather than growing a second
    embedding path that could disagree with search. With no provider the run is
    a reported no-op and the backlog waits — the projector's own contract.
    """
    outcome = JobOutcome(name=JOB_HOUSEKEEPING)
    nodes = context.nodes()
    outcome.examined = len(nodes)

    siblings: dict[str | None, list[NodeOut]] = {}
    for node in nodes:
        if node.position is not None:
            siblings.setdefault(node.parent_id, []).append(node)
    tight: list[list[str]] = []
    for parent in sorted(siblings, key=lambda value: (value is None, value or "")):
        ordered = sorted(siblings[parent], key=lambda node: (node.position or 0.0, node.id))
        for previous, following in zip(ordered, ordered[1:], strict=False):
            if (following.position or 0.0) - (previous.position or 0.0) < MIN_POSITION_GAP:
                tight.append([previous.id, following.id])
    outcome.detail["sibling_groups"] = len(siblings)
    outcome.detail["tight_pairs"] = tight[:MAX_REPORTED_ITEMS]
    outcome.detail["tight_pair_count"] = len(tight)
    outcome.notes.append(POSITION_NOOP_NOTE)
    if tight:
        outcome.notes.append(
            f"{len(tight)} sibling pair(s) are less than {MIN_POSITION_GAP} apart: a reorder "
            "operation is writing fractional positions, so a rebalance is now real work"
        )

    if context.dry_run:
        outcome.notes.append("dry run: the vec projector was not run (it writes derived state)")
        return outcome
    (run,) = projectors.run_projectors(names=["vec"], path=context.path)
    outcome.detail["vec_projector"] = run.model_dump(mode="json")
    if run.detail is not None:
        outcome.notes.append(
            f"vec projector unavailable ({run.detail}): its backlog waits, nothing crashed"
        )
    else:
        outcome.notes.append(
            f"vec projector applied {run.applied} event(s), "
            f"checkpoint {run.from_seq} → {run.to_seq}"
        )
    return outcome


# ── Job 4: neglect report (temporality maintenance, report only) ─────────────


def _job_neglect(context: _Context) -> JobOutcome:
    """Name the active nodes untouched beyond :data:`NEGLECT_DAYS`, and write nothing.

    Deciding that an untouched claim has gone *stale* is a judgement about its
    content, which is 5b and needs a model. Age is arithmetic, so the
    deterministic half can say honestly which nodes nobody has looked at — and
    then stop. Nothing here writes, in a dry run or otherwise.
    """
    outcome = JobOutcome(name=JOB_NEGLECT)
    nodes = context.nodes(state="active")
    outcome.examined = len(nodes)
    neglected = sorted(
        (node for node in nodes if _age_days(node.updated_at, context.now) >= NEGLECT_DAYS),
        key=lambda node: (node.updated_at, node.id),
    )
    outcome.detail["threshold_days"] = NEGLECT_DAYS
    outcome.detail["neglected_count"] = len(neglected)
    outcome.detail["neglected"] = [node.id for node in neglected][:MAX_REPORTED_ITEMS]
    outcome.notes.append(
        "report only: this job never writes — deciding an untouched claim is *stale* is "
        "judgement, and judgement is 5b"
    )
    return outcome


# ── Job 5: abstraction (5b-ii's first — gates deterministic, text from the model) ─


def _mean_pairwise_cosine(vectors: dict[str, list[float]], members: list[str]) -> float:
    """The mean cosine over every pair of a cluster; 0.0 for a two-member one.

    The gate is *mean*, not minimum: a cluster is a body of related notes, and
    one weaker pair among several strong ones is ordinary shape, not a hole.
    """
    values = [
        _cosine(vectors[first], vectors[second])
        for index, first in enumerate(members)
        for second in members[index + 1 :]
    ]
    if not values:
        return 0.0
    return sum(values) / len(values)


def _cluster_components(
    context: _Context, in_scope: set[str]
) -> tuple[list[EdgeOut], list[list[str]], set[str]]:
    """The active ``relates_to`` graph among in-scope nodes, as components.

    Returns the related edges, the connected components (each sorted, and the
    list itself in id order — the sort that makes a run deterministic), and the
    set of node ids that are already members of a synthesis (the ``dst`` of a
    non-archived ``derived_from`` edge from a synthesized node — non-archived
    because a *pending* synthesis's ``proposed`` edges protect its members
    too, and only a rejected concept — whose edges the service archives with
    it — frees them again).

    A node with no ``relates_to`` edge is not a cluster — it is not a member of
    any component, and it is not reported as one; reporting every orphan's
    one-member "component" would drown the report in the commonest shape there
    is.
    """
    active = context.edges(state="active")
    related = [
        edge
        for edge in active
        if edge.type == RELATED_EDGE_TYPE
        and edge.src_id != edge.dst_id
        and edge.src_id in in_scope
        and edge.dst_id in in_scope
    ]
    # Non-archived, not just active: an *accepted* synthesis's edges are now
    # active (the service settles them with the concept), but a *pending*
    # synthesis's edges are `proposed` and must protect their members too —
    # proposing the same cluster again while a synthesis of it waits in the
    # queue is the duplicate-proposal shape. Only an *archived* edge (a
    # rejected synthesis, whose edges the service archives with the concept)
    # frees the members. The src is matched against the synthesized nodes in
    # every non-archived state — a pending concept is `proposed`, not
    # `active` — and that check is also what tells a synthesis's edge from
    # ingestion's provenance ``derived_from`` edges.
    synthesized_nodes = {
        node.id
        for node in context.nodes()
        if node.state != "archived" and bool(node.props.get("synthesized"))
    }
    synthesized_ancestors = {
        edge.dst_id
        for edge in context.typed_edges(DERIVED_FROM_EDGE_TYPE)
        if edge.state != "archived" and edge.src_id in synthesized_nodes and edge.dst_id in in_scope
    }
    adjacency: dict[str, set[str]] = {}
    for edge in related:
        adjacency.setdefault(edge.src_id, set()).add(edge.dst_id)
        adjacency.setdefault(edge.dst_id, set()).add(edge.src_id)

    components: list[list[str]] = []
    seen: set[str] = set()
    for start in sorted(in_scope):
        if start in seen or start not in adjacency:
            continue
        stack = [start]
        seen.add(start)
        members: list[str] = []
        while stack:
            current = stack.pop()
            members.append(current)
            for neighbour in sorted(adjacency.get(current, ())):
                if neighbour in seen or neighbour not in in_scope:
                    continue
                seen.add(neighbour)
                stack.append(neighbour)
        components.append(sorted(members))
    return related, components, synthesized_ancestors


def _render_abstraction_prompt(members: list[NodeOut]) -> str:
    """The synthesis prompt for one cluster, from :data:`ABSTRACTION_PROMPT`.

    Each member's content is capped at :data:`ABSTRACTION_MEMBER_CHARS`,
    sized so a minimum 3-member cluster fits the default window with the
    template and the output reservation; a larger cluster degrades to the
    provider's :class:`~nodum.agent.PromptTooLong` refusal and a per-cluster
    skip, which is the honest path for a ten-member cluster of long notes.
    """
    rendered = "\n".join(
        f"- **{node.title or '(untitled)'}**: {(node.content or '')[:ABSTRACTION_MEMBER_CHARS]}"
        for node in members
    )
    return ABSTRACTION_PROMPT.format(members=rendered)


def _decode_abstraction_body(text: str, item_id: str) -> dict[str, str]:
    """Read the model's reply as the ``{title, content}`` the schema asked for.

    A schema makes this reliable and not certain — a provider that ignores
    ``response_format`` answers with prose, which is a job error here, never a
    write. Mirrors :func:`nodum.answers._decode`'s shape for the same reason:
    a body that fails to parse must not become a node nobody asked for.

    Schema-valid-but-false substance is the design's accepted stance, but
    empty strings are a refusal to answer: a body whose title or content is
    blank after stripping would write an empty node, which is a write nobody
    asked for either.
    """
    try:
        body = json.loads(text)
    except (ValueError, TypeError):
        raise ValueError(
            f"the abstraction model body for {item_id} is not JSON: {text[:120]!r}"
        ) from None
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("title"), str)
        or not isinstance(body.get("content"), str)
    ):
        raise ValueError(
            f"the abstraction model body for {item_id} is not a {{title, content}} "
            f"object: {str(body)[:120]!r}"
        )
    title, content = body["title"], body["content"]
    if not title.strip() or not content.strip():
        raise ValueError(
            f"the abstraction model body for {item_id} is an empty {{title, content}} "
            f"object: {str(body)[:120]!r}"
        )
    return {"title": title, "content": content}


def _job_abstraction(context: _Context) -> JobOutcome:
    """Find the clusters worth synthesizing, and write the synthesis (5b-ii).

    **The model never decides *whether* to synthesize — only what the text
    says.** Every gate below is arithmetic over data the file already holds,
    and all of it runs before any model call. A component is eligible when
    four gates hold:

    1. **Sized** — at least :data:`MIN_CLUSTER_MEMBERS` members and at most
       :data:`MAX_CLUSTER_MEMBERS` (a two-member pair is the link job's
       ``relates_to`` edge, not a synthesis).
    2. **Dense, graph half** — at least as many internal active
       ``relates_to`` edges as members: average degree ≥ 2, which is one
       cycle rather than a chain.
    3. **Not already synthesized** — no member carries truthy
       ``props["synthesized"]``, and no member is the target of a non-archived
       ``derived_from`` edge from a node that does. A synthesis is decided
       together with its members: accepting the concept activates its edges,
       rejecting it archives them — so a *pending* synthesis (``proposed``
       edges) protects its members too, and only a rejected one frees them.
    4. **Dense, vector half** — the mean pairwise cosine among the members is
       at least :data:`ABSTRACTION_COHESION_COSINE` (the calibrated link bar,
       reused rather than invented). This is why the job needs the embedding
       provider, and why it **no-ops** rather than degrading when none is
       present: cohesion is its one vector signal, so there is no degraded
       mode to fall back to.

    Eligible clusters are capped at :data:`MAX_CLUSTERS_PER_CYCLE`, sorted by
    their member-id tuple; overflow is reported in a note, never silent. For
    each considered cluster the job then calls the model through
    :func:`nodum.agent.for_cycle` — gated first on the cycle budget
    (:data:`nodum.agent.ENV_CYCLE_BUDGET`, off by default) and on a configured
    provider — and the model's ``{title, content}`` is the *only* thing the
    gates did not decide. The write files the concept node ``proposed`` with
    ``props.synthesized`` (the freshness gate's own record) plus one
    ``derived_from`` edge per member — in the cycle's scope when there is one
    (the concept belongs with its members, and the whole unit waits in review
    together), and in ``main`` — the default write target — on an unscoped
    cycle, as before. A dry run still pays for the model
    calls (B4) while writing nothing. A body that fails to parse is a job
    error, not a write; a ceiling or a stop behave as the runtime documents
    them. The run's :meth:`nodum.agent.AgentRun.report` is filed into the
    cycle's report under :data:`nodum.agent.REPORT_KEY` by :func:`_run_jobs`.
    """
    outcome = JobOutcome(name=JOB_ABSTRACTION)
    nodes = context.nodes(state="active")
    nodes_by_id = {node.id: node for node in nodes}
    in_scope = set(nodes_by_id)
    outcome.examined = len(nodes)

    related, components, synthesized_ancestors = _cluster_components(context, in_scope)

    eligible: list[list[str]] = []
    for members in components:
        count = len(members)
        if count < MIN_CLUSTER_MEMBERS or count > MAX_CLUSTER_MEMBERS:
            reason = (
                f"{count} members is below the {MIN_CLUSTER_MEMBERS}-member minimum"
                if count < MIN_CLUSTER_MEMBERS
                else f"{count} members is above the {MAX_CLUSTER_MEMBERS}-member maximum"
            )
            outcome.skipped.append({"id": ",".join(members), "reason": reason})
            continue
        internal = sum(1 for edge in related if edge.src_id in members and edge.dst_id in members)
        if internal < count:
            outcome.skipped.append(
                {
                    "id": ",".join(members),
                    "reason": (
                        f"not dense: {internal} relates_to edge(s) among {count} members — "
                        "a cluster needs at least one cycle, not a chain"
                    ),
                }
            )
            continue
        if any(bool(nodes_by_id[member].props.get("synthesized")) for member in members) or any(
            member in synthesized_ancestors for member in members
        ):
            outcome.skipped.append(
                {"id": ",".join(members), "reason": "member is already part of a synthesis"}
            )
            continue
        selected = [nodes_by_id[member] for member in members]
        vectors = context.vectors(selected)
        if vectors is None:
            # The size and density skips already recorded stay: the "did not
            # run" note explains why no cluster was considered, and wiping the
            # list would throw away the skips a reader has to see to know what
            # would otherwise have been eligible.
            outcome.notes.append(
                f"no embedding provider ({embeddings.unavailable_reason()}): "
                "abstraction cannot compute cohesion and did not run"
            )
            return outcome
        mean_cosine = _mean_pairwise_cosine(vectors, members)
        if mean_cosine < ABSTRACTION_COHESION_COSINE:
            outcome.skipped.append(
                {
                    "id": ",".join(members),
                    "reason": (
                        f"not cohesive: mean pairwise cosine {mean_cosine:.4f} is below the "
                        f"{ABSTRACTION_COHESION_COSINE:g} bar"
                    ),
                }
            )
            continue
        eligible.append(members)

    eligible.sort()
    outcome.detail["clusters_eligible"] = len(eligible)
    if len(eligible) > MAX_CLUSTERS_PER_CYCLE:
        outcome.notes.append(
            f"{len(eligible) - MAX_CLUSTERS_PER_CYCLE} more eligible cluster(s) beyond the "
            f"{MAX_CLUSTERS_PER_CYCLE}-cluster cap were not considered"
        )
    considered = eligible[:MAX_CLUSTERS_PER_CYCLE]
    outcome.detail["clusters_considered"] = len(considered)
    outcome.detail["eligible_clusters"] = considered
    outcome.skipped = outcome.skipped[:MAX_REPORTED_ITEMS]

    # The write half. The deterministic selection above already ran whatever
    # this half decides — the gates cost nothing, the model spend is what is
    # gated.
    run = agent.for_cycle(cycle_id=context.cycle_id, principal=context.principal, path=context.path)
    try:
        if run.budget.tokens <= 0:
            outcome.notes.append("NODUM_LLM_CYCLE_BUDGET is 0: the abstraction job did not run")
            return outcome
        if not run.available:
            outcome.notes.append(
                f"no LLM provider ({run.unavailable_reason}): the abstraction job did not run"
            )
            return outcome
        job_budget = run.job(JOB_ABSTRACTION, share=1.0)
        outcome.detail["synthesized"] = []
        synthesized_count = 0
        member_count = 0
        for members in considered:
            item_id = "cluster:" + "-".join(members)
            prompt = _render_abstraction_prompt([nodes_by_id[member] for member in members])
            try:
                generation = run.chat(
                    [agent.Message(role="user", content=prompt)],
                    prompt_version=ABSTRACTION_PROMPT_VERSION,
                    schema=ABSTRACTION_SCHEMA,
                    job=job_budget,
                    item_id=item_id,
                )
            except agent.CycleStopped:
                raise
            except agent.BudgetExhausted:
                # The run's report carries the exhausted flag and the itemised
                # skip; the job is not a failure, the ceiling stopped the work.
                break
            except agent.PromptTooLong:
                outcome.skipped.append(
                    {"id": item_id, "reason": "the prompt does not fit the model's window"}
                )
                continue
            body = _decode_abstraction_body(generation.text, item_id)
            synthesized_count += 1
            member_count += len(members)
            if context.dry_run:
                outcome.detail["synthesized"].append(
                    {
                        "node": None,
                        "members": members,
                        "title": body["title"],
                        "dry_run": True,
                    }
                )
                continue
            node = service.create_node(
                type="concept",
                title=body["title"],
                content=body["content"],
                landing=SUGGESTION_LANDING,
                space=context.scope,
                props={
                    "synthesized": True,
                    "members": members,
                    "job": JOB_ABSTRACTION,
                    **generation.generated_by.as_props(),
                },
                principal=context.principal,
                path=context.path,
            )
            # The whole unit is proposed: the concept *and* its membership
            # edges. `outcome.applied` stays empty — this job only proposes.
            outcome.proposed.append(node.id)
            for member in members:
                try:
                    edge = service.create_edge(
                        node.id,
                        member,
                        DERIVED_FROM_EDGE_TYPE,
                        landing=SUGGESTION_LANDING,
                        props={"job": JOB_ABSTRACTION},
                        principal=context.principal,
                        path=context.path,
                    )
                    outcome.proposed.append(edge.id)
                except (
                    GrantNotPermitted,
                    service.NodeNotFound,
                    service.TypeNotFound,
                    ValueError,
                ) as refusal:
                    # A cross-space member the gardener may not write costs one
                    # skipped line and not the synthesis.
                    outcome.skipped.append({"id": f"{node.id}->{member}", "reason": str(refusal)})
            outcome.detail["synthesized"].append(
                {"node": node.id, "members": members, "title": body["title"]}
            )
        if context.dry_run and synthesized_count:
            outcome.notes.append(
                f"dry run: would synthesize {synthesized_count} concept(s) from "
                f"{member_count} member(s)"
            )
        outcome.detail["cost"] = {
            "calls": run.budget.calls,
            "failed_calls": run.budget.failed_calls,
            "prompt_tokens": run.budget.spent_prompt_tokens,
            "output_tokens": run.budget.spent_output_tokens,
            "total_tokens": run.budget.spent_tokens,
        }
        return outcome
    finally:
        context.llm_report = run.report()


# ── Job 6: learned queue curation (§L1–§L4 — statistics and the record, never
#    the judgement) ────────────────────────────────────────────────────────────


def _acceptance_counts(
    rows: list[Any],
    *,
    proposer_key: str,
    type_key: str,
    accepted_states: tuple[str, ...],
    rejected_states: tuple[str, ...],
    now: datetime,
) -> dict[tuple[str, str], tuple[int, int]]:
    """Per ``(proposer, type)`` accepted/rejected counts over row state.

    The window is measured from ``created_at`` — the **row's** creation
    timestamp, which is all row state records. The decision time of an accept
    or reject lives only in the event log, which the gardener
    (:func:`nodum.service.list_events`) refuses to read, so "the last quarter
    of a proposer's record" is the quarter of rows created then; a row the
    proposer filed this week and a human decided years later both count as
    fresh by their creation date. Accepted and rejected are the two terminal
    states, so a ``proposed`` row is history still in flight and counts for
    neither side.
    """
    counts: dict[tuple[str, str], tuple[int, int]] = {}
    for row in rows:
        if _age_days(row.created_at, now) > CURATION_WINDOW_DAYS:
            continue
        accepted = row.state in accepted_states
        rejected = row.state in rejected_states
        if not accepted and not rejected:
            continue
        key = (getattr(row, proposer_key), getattr(row, type_key))
        current = counts.get(key, (0, 0))
        counts[key] = (current[0] + 1, current[1]) if accepted else (current[0], current[1] + 1)
    return counts


def _version_counts(
    context: _Context, update_types: set[str]
) -> dict[tuple[str, str], tuple[int, int]]:
    """Per ``(actor, node type)`` applied/archived counts, from the versions of
    the in-scope nodes of the targeted types.

    There is no bulk version read on the public surface — :func:`nodum.service.history`
    is per node, the only version listing there is — so the read is one
    :func:`nodum.service.history` call per in-scope node of a type the queue's
    update proposals target, bounded by the node scan
    (:data:`MAX_SCAN_NODES`) the context already caps. ``applied`` is the
    accepted state of a proposed version and ``archived`` the rejected one
    (:func:`nodum.service._transition_version`).
    """
    counts: dict[tuple[str, str], tuple[int, int]] = {}
    if not update_types:
        return counts
    for node in context.nodes():
        if node.type not in update_types:
            continue
        for version in service.history(node.id, principal=context.principal, path=context.path):
            if _age_days(version.created_at, context.now) > CURATION_WINDOW_DAYS:
                continue
            if version.state not in ("applied", "archived"):
                continue
            key = (version.actor, node.type)
            current = counts.get(key, (0, 0))
            counts[key] = (
                (current[0] + 1, current[1])
                if version.state == "applied"
                else (current[0], current[1] + 1)
            )
    return counts


def _rate_entries(
    counts: dict[tuple[str, str], tuple[int, int]], kind: str
) -> list[dict[str, Any]]:
    """One ``detail["acceptance"]`` entry per counted ``(proposer, type)`` pair.

    Only pairs with history — a pair with none is a cold start, which has no
    rate to report. Sorted by ``(proposer, kind, type)`` so a run is
    deterministic.
    """
    entries = []
    for (proposer, item_type), (accepted, rejected) in counts.items():
        entries.append(
            {
                "proposer": proposer,
                "kind": kind,
                "type": item_type,
                "accepted": accepted,
                "rejected": rejected,
                "rate": _rate(accepted, accepted + rejected),
            }
        )
    return sorted(entries, key=lambda entry: (entry["proposer"], entry["kind"], entry["type"]))


def _proposal_signals(proposal: Any) -> list[str]:
    """The signals a proposal's own props named — the ones its annotation echoes.

    The proposer's judgement is their own props (``props.signals`` for the
    link and duplicate jobs, for instance); the annotation repeats those
    signals so the reviewer sees what fired, never a number the job invented.
    """
    row = proposal.node or proposal.edge or proposal.version
    signals = row.props.get("signals") if row is not None else None
    return signals if isinstance(signals, list) else []


def _auto_accept_control(
    context: _Context,
) -> tuple[float | None, str | None, tuple[str, object] | None]:
    """The ``conventions``-space note setting :data:`AUTO_ACCEPT_PROPS_KEY`.

    Read through the public surface (``list_nodes(space=…)``) so it answers
    the same on a scoped cycle, where ``context.nodes()`` would not reach the
    conventions space. Returns ``(threshold, note id, malformed)``: the
    threshold and the note carrying it when a non-archived conventions note
    sets a numeric ``auto_accept_above`` — a numeric *string* counts, since a
    human editing graph props writes ``"0.9"`` — else ``(None, None,
    malformed)``, where ``malformed`` names the ``(note id, value)`` of the
    first note whose value is not a number. The malformed value is reported
    rather than silently ignored, because "no conventions-space note sets it"
    would then be false.
    """
    notes = service.list_nodes(
        space=CONVENTIONS_SPACE_ID,
        principal=context.principal,
        limit=MAX_SCAN_NODES,
        path=context.path,
    )
    malformed: tuple[str, object] | None = None
    for note in notes:
        if note.state == "archived":
            continue
        value = note.props.get(AUTO_ACCEPT_PROPS_KEY)
        if isinstance(value, bool):
            malformed = malformed or (note.id, value)
            continue
        if isinstance(value, (int, float)):
            return float(value), note.id, None
        if isinstance(value, str):
            try:
                return float(value), note.id, None
            except ValueError:
                malformed = malformed or (note.id, value)
                continue
        malformed = malformed or (note.id, value)
    return None, None, malformed


def _job_curation(context: _Context) -> JobOutcome:
    """Compute proposers' acceptance rates from row state, and record them (§L1–§L4).

    **Statistics and the record, not the judgement.** This job never accepts
    and never rejects: it reads the review queue and the graph's row state,
    works out, per ``(proposer, type)``, how often that proposer's proposals
    were accepted against how often they were rejected — ``active`` vs
    ``archived`` rows, ``applied`` vs ``archived`` versions — over the last
    :data:`CURATION_WINDOW_DAYS`, and writes two records of the result:

    - **A convention node (L2)** — one ``note`` node in the
      :data:`CONVENTIONS_SPACE_ID` space per ``(proposer, edge_type)`` with
      history, filed ``proposed`` through the landing seam, carrying exactly
      :data:`CURATION_CONVENTION_PROPS` (proposer, edge_type, rate, accepted,
      rejected, window_days, computed_at, plus the job and kind tags).
    - **A per-item annotation (§L1)** — one ``annotations`` row per queue item
      whose proposer has history on the item's type, whose body says the rate,
      the counts, the window, and the signals the proposal's own props named.
      A proposer with no history on that type gets **no** annotation: the §L1
      shape needs a rate, and a cold start has none.

    **Row state, never the event log.** The gardener is ``kind="internal"``
    and :func:`nodum.service.list_events` refuses it, which is the design's
    whole point: acceptance is read from where the graph is now, not from what
    happened. The window is measured from row ``created_at`` — the decision
    time of an accept lives only in the log, so the quarter is a quarter of
    rows created then (see :func:`_acceptance_counts`). A row in the
    ``proposed`` state is history still in flight and counts for neither side.

    **Nothing gates a write on ``confidence``.** The proposer's own
    self-reported ``confidence`` (edge props, the D6 seam) is indicative data
    that triggers nothing hardcoded (§8.3): the rate here is the graph's
    measure of the proposer, and the signals echoed into an annotation are
    the proposer's own props repeated, not a score.

    **Auto-accept exists as an interface and stays OFF.** The job reads the
    well-known :data:`AUTO_ACCEPT_PROPS_KEY` field off ``conventions``-space
    notes; a numeric value is acknowledged in the report, and **nothing is
    accepted either way** — the accept direction is not implemented, because
    the design's measured 2/4 score put both misses in the accept direction
    and the safe default is OFF. What would turn it on is a deliberate
    implementation of the accept path behind that threshold — and even then,
    nothing may gate on the proposer's own ``confidence``.

    A dry run computes everything and writes nothing, saying in a note what it
    would have written. ``outcome.truncated`` mirrors the context's edge-scan
    flag and the queue's own cap (one past the cap is fetched so "exactly at"
    and "past" stay distinct). ``outcome.detail["acceptance"]`` is the
    per-(proposer, type) rate list the journal's acceptance section renders
    (§L4) — the delta basis, with no second copy stored: deltas compose from
    the convention nodes' own versions.
    """
    outcome = JobOutcome(name=JOB_CURATION)
    proposals = service.list_proposals(
        principal=context.principal, limit=MAX_SCAN_NODES + 1, path=context.path
    )
    if len(proposals) > MAX_SCAN_NODES:
        outcome.truncated = True
        outcome.notes.append(
            f"the review queue exceeds {MAX_SCAN_NODES} proposals: the oldest "
            f"{MAX_SCAN_NODES} were curated, the rest wait for a later cycle"
        )
        proposals = proposals[:MAX_SCAN_NODES]
    if context.scope is not None:
        proposals = [
            proposal for proposal in proposals if context.scope in _proposal_space_ids(proposal)
        ]
    outcome.examined = len(proposals)

    # Row state, read once and shared across proposers. Nodes and edges carry
    # their creator and type on the row; versions are read per node of the
    # types the queue's update proposals target (see `_version_counts`).
    edge_counts = _acceptance_counts(
        context.edges(),
        proposer_key="created_by",
        type_key="type",
        accepted_states=("active",),
        rejected_states=("archived",),
        now=context.now,
    )
    node_counts = _acceptance_counts(
        context.nodes(),
        proposer_key="created_by",
        type_key="type",
        accepted_states=("active",),
        rejected_states=("archived",),
        now=context.now,
    )
    update_types = {proposal.type for proposal in proposals if proposal.kind == "update"}
    version_counts = _version_counts(context, update_types)
    outcome.detail["acceptance"] = (
        _rate_entries(edge_counts, "edge")
        + _rate_entries(node_counts, "node")
        + _rate_entries(version_counts, "version")
    )
    if context.truncated:
        outcome.truncated = True
        outcome.notes.append(
            f"an edge scan hit MAX_SCAN_EDGES: reads above the {MAX_SCAN_EDGES}-edge cap "
            "drop the newest rows, so an acceptance rate may have missed the freshest "
            "history"
        )

    # L2: one convention node per (proposer, edge_type) with history. Written
    # every cycle — each node is that cycle's snapshot of the rolling rate,
    # and the acceptance section's deltas compose from their versions.
    convention_entries: list[dict[str, Any]] = []
    for proposer, edge_type in sorted(edge_counts):
        accepted, rejected = edge_counts[(proposer, edge_type)]
        rate = _rate(accepted, accepted + rejected)
        props = {
            "job": JOB_CURATION,
            "kind": "acceptance-rate",
            "proposer": proposer,
            "edge_type": edge_type,
            "rate": rate,
            "accepted": accepted,
            "rejected": rejected,
            "window_days": CURATION_WINDOW_DAYS,
            "computed_at": context.now.isoformat(),
        }
        title = (
            f"{proposer} on {edge_type}: {rate:.0%} accepted "
            f"({accepted}/{accepted + rejected} in {CURATION_WINDOW_DAYS} days)"
        )
        content = (
            f"{proposer} has {accepted} accepted and {rejected} rejected {edge_type} "
            f"edge(s) in the last {CURATION_WINDOW_DAYS} days — an acceptance rate of "
            f"{rate:.1%}. Computed by the curation job from row state; nothing here "
            "accepts or rejects."
        )
        if context.dry_run:
            convention_entries.append(
                {"node": None, "proposer": proposer, "edge_type": edge_type, "rate": rate}
            )
            continue
        try:
            node = service.create_node(
                type="note",
                title=title,
                content=content,
                space=CONVENTIONS_SPACE_ID,
                landing=SUGGESTION_LANDING,
                props=props,
                principal=context.principal,
                path=context.path,
            )
            outcome.proposed.append(node.id)
            convention_entries.append(
                {"node": node.id, "proposer": proposer, "edge_type": edge_type, "rate": rate}
            )
        except (GrantNotPermitted, service.TypeNotFound, ValueError) as refusal:
            # A revoked conventions grant costs one skipped line and not the
            # job — the same per-item tolerance every other job's writes have.
            outcome.skipped.append(
                {"id": f"convention:{proposer}@{edge_type}", "reason": str(refusal)}
            )
    outcome.detail["conventions"] = convention_entries

    # §L1: one annotation per queue item whose proposer has history on its
    # type. Cold start (no history) means no annotation.
    annotation_ids: list[Any] = []
    would_annotate = 0
    for proposal in proposals:
        if proposal.kind == "node":
            counts = node_counts.get((proposal.created_by, proposal.type))
            target_kind = "node"
        elif proposal.kind == "edge":
            counts = edge_counts.get((proposal.created_by, proposal.type))
            target_kind = "edge"
        else:
            counts = version_counts.get((proposal.created_by, proposal.type))
            target_kind = "version"
        if counts is None:
            continue
        accepted, rejected = counts
        if accepted + rejected == 0:
            continue
        body = {
            "rate": _rate(accepted, accepted + rejected),
            "signals": _proposal_signals(proposal),
            "window_days": CURATION_WINDOW_DAYS,
            "counts": {"accepted": accepted, "rejected": rejected},
        }
        if context.dry_run:
            would_annotate += 1
            annotation_ids.append(
                {"kind": target_kind, "id": proposal.id, "rate": body["rate"], "dry_run": True}
            )
            continue
        try:
            annotation = service.annotate(
                target_kind,
                int(proposal.id) if target_kind == "version" else proposal.id,
                body,
                principal=context.principal,
                path=context.path,
            )
            annotation_ids.append(annotation.id)
        except (GrantNotPermitted, service.RecordNotFound, ValueError) as refusal:
            outcome.skipped.append({"id": proposal.id, "reason": f"annotation refused: {refusal}"})
    outcome.detail["annotations"] = annotation_ids

    # §L3: auto-accept is a real interface, read and reported, and OFF.
    threshold, control, malformed = _auto_accept_control(context)
    outcome.detail["auto_accept"] = {"enabled": False, "threshold": threshold}
    if threshold is not None:
        outcome.notes.append(
            f"auto-accept is off: conventions note {control} sets "
            f"'{AUTO_ACCEPT_PROPS_KEY}' to {threshold}, and the job read it — but the "
            "accept direction is not implemented (the measured evidence put its misses "
            "there), so nothing was accepted. Turning it on means implementing the accept "
            "path behind that threshold; it never gates on the proposer's own confidence"
        )
    elif malformed is not None:
        malformed_id, malformed_value = malformed
        outcome.notes.append(
            f"auto-accept is off: conventions note {malformed_id} sets "
            f"'{AUTO_ACCEPT_PROPS_KEY}' to {malformed_value!r}, which is not a number, "
            "so it was ignored — this cycle only wrote conventions and annotations, and "
            "nothing was accepted on any proposer's rate"
        )
    else:
        outcome.notes.append(
            f"auto-accept is off: no conventions-space note sets "
            f"'{AUTO_ACCEPT_PROPS_KEY}', so this cycle only wrote conventions and "
            "annotations — nothing was accepted on any proposer's rate"
        )

    if context.dry_run:
        outcome.notes.append(
            f"dry run: would write {len(convention_entries)} convention node(s) and "
            f"{would_annotate} annotation(s)"
        )
    return outcome


#: The jobs, in run order. ``jobs=`` selects a subset by these names.
JOBS = {
    JOB_DUPLICATES: _job_duplicates,
    JOB_LINKS: _job_links,
    JOB_ABSTRACTION: _job_abstraction,
    JOB_CURATION: _job_curation,
    JOB_HOUSEKEEPING: _job_housekeeping,
    JOB_NEGLECT: _job_neglect,
}


# ── Writing (one door, so the landing state is chosen in one place) ──────────


def _write_edges(context: _Context, outcome: JobOutcome, suggestions: list[dict[str, Any]]) -> None:
    """Write a batch of edge suggestions and fold the outcome into the report.

    :func:`nodum.service.propose_edges` reports a malformed or refused
    suggestion rather than aborting the batch, so a cross-space pair the
    gardener may not write costs one ``skipped`` line and not the job.

    **The landing state is chosen, not inherited.** Migration ``0014`` grants
    the gardener ``edit`` on ``main`` (and ``read`` on ``meta``, which is all
    resolving a type needs), and a write lands at the
    writer's grant level by default — so without this these suggestions would
    land ``active`` and become asserted fact instead of reaching the review
    queue, which is exactly what job 1's D9 argument rests on. Design §8.3 has
    the seam: *"``edit`` = the agent writes ``active`` directly and self-governs
    with its own confidence — confident writes go active, uncertain ones are
    filed ``proposed``."* Every edge this door writes is inferred from
    arithmetic over titles, vectors and shared neighbours, which is the
    uncertain half by construction, so the whole door files
    :data:`SUGGESTION_LANDING`. The grant is untouched — it is what lets the
    *pruning* half of job 2 archive an edge outright — and the report still
    says which state each batch landed in.
    """
    if not suggestions:
        return
    result = service.propose_edges(
        suggestions,
        landing=SUGGESTION_LANDING,
        principal=context.principal,
        path=context.path,
    )
    outcome.proposed = [edge.id for edge in result.created]
    outcome.detail["landed"] = sorted({edge.state for edge in result.created})
    for failure in result.failed:
        suggestion = suggestions[failure.index]
        outcome.skipped.append(
            {"id": f"{suggestion['src']}->{suggestion['dst']}", "reason": failure.error}
        )


# ── The five coherence metrics (Q12, the deterministic half) ─────────────────


def _metrics(context: _Context) -> dict[str, float]:
    """One coherence snapshot, keyed by metric name.

    Five of Q12's six candidates. The two left out cannot be computed correctly
    today rather than being unimportant: *unresolved contradictions* needs
    judgement about what contradicts what, and *stale syntheses* needs
    syntheses, which the abstraction job (5b) creates. They join this object
    when they can be computed, with no migration — which is why the metrics are
    an object and not columns.

    *Link density* is deliberately substituted for Q12's mean path length: an
    all-pairs walk costs O(n·m) on a graph this shape and tells you less than
    edges-per-node about whether the file is knitted together.

    Every metric is well defined on an empty graph: :func:`_rate` returns 0.0
    rather than dividing by zero, and an empty review queue has a median age of
    0.0.
    """
    nodes = context.nodes()
    in_scope = {node.id for node in nodes}
    active = [node for node in nodes if node.state == "active"]
    active_ids = {node.id for node in active}
    active_edges = [
        edge
        for edge in context.edges(state="active")
        if edge.src_id in active_ids and edge.dst_id in active_ids
    ]
    incident = {edge.src_id for edge in active_edges} | {edge.dst_id for edge in active_edges}
    duplicates = [
        edge
        for edge in context.typed_edges(DUPLICATE_EDGE_TYPE)
        if edge.state != "archived" and edge.src_id in in_scope and edge.dst_id in in_scope
    ]
    neglected = [node for node in active if _age_days(node.updated_at, context.now) >= NEGLECT_DAYS]
    proposals = service.list_proposals(
        principal=context.principal, limit=MAX_SCAN_NODES, path=context.path
    )
    if context.scope is not None:
        proposals = [
            proposal for proposal in proposals if context.scope in _proposal_space_ids(proposal)
        ]
    ages = [_age_days(proposal.created_at, context.now) for proposal in proposals]
    return {
        "orphan_rate": _rate(len(active_ids - incident), len(active)),
        "duplicate_candidates": float(len(duplicates)),
        "queue_age_days": round(_median(ages), 6),
        "link_density": _rate(len(active_edges), len(active)),
        "neglect_rate": _rate(len(neglected), len(active)),
    }


# ── The entry point ───────────────────────────────────────────────────────────


def _resolve_jobs(names: list[str] | None) -> list[str]:
    """Resolve job names against :data:`JOBS` (``None`` = all, in run order)."""
    if names is None:
        return list(JOBS)
    unknown = sorted(set(names) - set(JOBS))
    if unknown:
        raise ValueError(
            f"unknown consolidation job(s): {', '.join(unknown)} (registered: {', '.join(JOBS)})"
        )
    return list(names)


def _opener(
    triggered_by: str, gardener: Principal, path: str | Path | None
) -> tuple[str, Principal]:
    """Resolve ``triggered_by`` to ``(trigger, the principal that opens the cycle)``.

    The literal ``scheduler`` means nobody asked — the clock did — so the cycle
    is ``scheduled`` and the gardener opens its own; ``open_cycle`` records
    :data:`nodum.service.SCHEDULER_ACTOR` there whatever principal it is handed.
    Anything else is an actor string that has already authenticated on some
    surface, re-minted from stored state by
    :func:`nodum.auth.principal_from_actor` — so a disabled or unknown account
    cannot ask for a cycle, and the journal names somebody who exists.
    """
    if triggered_by == service.SCHEDULER_ACTOR:
        return "scheduled", gardener
    return "manual", auth.principal_from_actor(triggered_by, path=path)


def _require_gardener_scope(
    scope: str | None, cycle: CycleOut, gardener: Principal, path: str | Path | None
) -> None:
    """Refuse a scope the gardener holds no grant on, and name the fix.

    Migration ``0014`` grants the gardener ``main`` and ``meta`` and nothing
    else, so **every space created after it is invisible to the gardener** until
    somebody grants it — including the spaces the human UI's own scope picker
    offers on a default install. Without this check the run reached
    ``list_nodes(space=…, principal=gardener)`` inside the first metrics
    snapshot and failed there with :class:`nodum.service.TypeNotFound`
    ``unknown space: <id>``: the Q13 non-oracle refusal, which is the honest
    answer to a *caller* who lacks the grant and the wrong answer here, where the
    caller is a human looking at the space in a picker and it is the gardener
    that lacks it. It also became a permanent journal row, and the dream journal
    splices a cycle's failure message into the entry's headline — so the one
    sentence no user-facing surface may say ended up on screen, with a bare
    32-hex id in it.

    The check runs **after** :func:`nodum.service.open_cycle`, which is what
    keeps the non-oracle rule intact: a scope the *caller* cannot see is still
    refused there, identically for a space that does not exist and one they hold
    no grant on. Only once the caller has been shown to see it does this ask
    whether the gardener does.

    Args:
        scope: The reference the caller supplied — echoed in the message, so a
            caller who named a space is never shown an id they did not type.
        cycle: The open cycle, carrying the resolved ``scope`` id.
        gardener: The internal principal the jobs will run as.
        path: Explicit database path.

    Raises:
        GrantNotPermitted: If the gardener cannot resolve the cycle's scope.
    """
    if cycle.scope is None:
        return
    reference = scope if scope is not None else cycle.scope
    try:
        service.resolve_space_id(cycle.scope, principal=gardener, path=path)
    except service.TypeNotFound:
        raise GrantNotPermitted(
            f"the gardener holds no grant on space {reference!r}, so it cannot consolidate it: "
            f"migration 0014 seeds {GARDENER_AGENT_ID} with 'main' and 'meta' only, and every "
            f"other space is an explicit grant. Run: "
            f"nodum grant {GARDENER_AGENT_ID} {reference} edit"
        ) from None


def consolidate(
    *,
    scope: str | None = None,
    dry_run: bool = False,
    jobs: list[str] | None = None,
    triggered_by: str,
    path: str | Path | None = None,
) -> ConsolidationOut:
    """Run a consolidation cycle and return its journal entry plus its report.

    Opens a cycle, runs the selected deterministic jobs inside it — so every
    write they make is stamped and a rollback can take the whole run back — and
    closes it with the report. Writes are the gardener's;
    ``cycles.triggered_by`` is whoever asked.

    **One cycle at a time, in the whole file.** The serialisation is the
    ``cycles`` row itself, so a second caller is refused with
    :class:`CycleInProgress` whether it is in this process or another one — see
    the module docstring for why that replaced a lock rather than joining it.

    A job that raises does not lose the run: its own outcome carries the error,
    the other jobs still run and still report, the after-metrics are still
    computed, and the cycle closes ``failed`` with all of it. The events the
    cycle wrote before the failure stay — they are real, and a rollback is what
    takes them back. A failure *outside* a job (a scope the gardener holds no
    grant on, for instance) closes the cycle ``failed`` and re-raises: that is
    not a job result, it is a caller error.

    **``BaseException``, not ``Exception``.** Ctrl-C during ``nodum consolidate``
    raises :class:`KeyboardInterrupt`, which is not an ``Exception`` and used to
    escape this guard with the cycle row still ``running`` — and a ``running``
    cycle cannot be rolled back while ``undo`` refuses every event it stamped,
    so the writes it had already made were irreversible on every surface. The
    cycle is closed ``failed`` and the interrupt re-raised, so the operator's
    Ctrl-C still means what they pressed it for.

    Args:
        scope: A space id or name to confine the cycle to, or ``None`` for the
            whole file. Resolved by :func:`nodum.service.open_cycle` through the
            ordinary space rule, then checked against the gardener's own grants
            by :func:`_require_gardener_scope`.
        dry_run: Compute everything and write nothing to the graph. The cycle
            row is still written, flagged ``dry_run``, and carries the report —
            the journal has to say which it was — but the cycle's event list is
            empty, which is the checkable form of "it changed nothing".
        jobs: Job names to run (see :data:`JOBS`); ``None`` runs all of them in
            registry order.
        triggered_by: Who asked — an actor string that has already
            authenticated, or the literal :data:`nodum.service.SCHEDULER_ACTOR`.
        path: Explicit database path.

    Returns:
        The closed cycle and its report.

    Raises:
        ValueError: If a job name is not registered.
        CycleInProgress: If a consolidation cycle is already running against
            this database — in this process or any other.
        UnknownPrincipal: If ``triggered_by`` names no account.
        PrincipalDisabled: If it names a disabled one, or if the gardener is
            disabled — the supported way to stop it.
        GrantNotPermitted: If the trigger may not open a cycle over ``scope``,
            or if the gardener holds no grant on it.
    """
    # Job names are resolved before anything else so a typo is still reported as
    # a typo while another cycle is running, rather than as "already running".
    selected = _resolve_jobs(jobs)
    return _run_cycle(
        scope=scope, dry_run=dry_run, selected=selected, triggered_by=triggered_by, path=path
    )


def _run_cycle(
    *,
    scope: str | None,
    dry_run: bool,
    selected: list[str],
    triggered_by: str,
    path: str | Path | None,
) -> ConsolidationOut:
    """The body of :func:`consolidate`, once the job names have been resolved."""
    gardener = auth.internal_principal(path=path)
    trigger, opener = _opener(triggered_by, gardener, path)
    cycle = service.open_cycle(
        trigger=trigger, scope=scope, dry_run=dry_run, principal=opener, path=path
    )
    context = _Context(
        principal=gardener,
        scope=cycle.scope,
        dry_run=dry_run,
        path=path,
        now=_utcnow(),
        cycle_id=cycle.id,
    )
    try:
        _require_gardener_scope(scope, cycle, gardener, path)
        # The block wraps the dry run too: a job that wrote when it should not
        # have would at least land inside the cycle a rollback can reach,
        # instead of as an unattributable loose write.
        with service.in_cycle(cycle.id):
            report = _run_jobs(context, cycle, selected)
    except BaseException as failure:
        service.close_cycle(
            cycle.id,
            status="failed",
            report=ConsolidationReport(
                scope=cycle.scope,
                dry_run=dry_run,
                jobs=[],
                metrics={},
                failed=[JobFailure(job="", error=f"{type(failure).__name__}: {failure}")],
            ).model_dump(mode="json"),
            principal=opener,
            path=path,
        )
        raise
    closed = service.close_cycle(
        cycle.id,
        status="failed" if report.failed else "completed",
        report=report.model_dump(mode="json"),
        principal=opener,
        path=path,
    )
    return ConsolidationOut(cycle=closed, report=report)


def _run_jobs(context: _Context, cycle: CycleOut, selected: list[str]) -> ConsolidationReport:
    """Snapshot the metrics, run each job in isolation, snapshot them again.

    The report picks up the context's truncation flag: when an edge scan
    returned more than :data:`MAX_SCAN_EDGES` rows during the run, ``notes``
    says the metric reads may have missed edges — the flag is sticky, so it
    cannot say *which* read dropped rows, only that some edge read did. A
    job's own outcome already says it for its own read.
    """
    before = _metrics(context)
    outcomes: list[JobOutcome] = []
    failed: list[JobFailure] = []
    for name in selected:
        try:
            outcomes.append(JOBS[name](context))
        # `Exception` and not `BaseException`, deliberately unlike the guard in
        # `_run_cycle`: one job falling over must not lose the others,
        # but an interrupt is a request to stop the *run*, not a job result.
        except Exception as failure:
            message = f"{type(failure).__name__}: {failure}"
            outcomes.append(JobOutcome(name=name, error=message))
            failed.append(JobFailure(job=name, error=message))
    after = _metrics(context)
    report_notes: list[str] = []
    if context.truncated:
        report_notes.append(
            f"an edge scan hit MAX_SCAN_EDGES: reads above the {MAX_SCAN_EDGES}-edge "
            "cap drop the newest rows, so a suppression or metric read may have "
            "missed edges — a rejected pair could be re-proposed and "
            "duplicate_candidates may under-count"
        )
    return ConsolidationReport(
        scope=cycle.scope,
        dry_run=cycle.dry_run,
        jobs=outcomes,
        metrics={"before": before, "after": after},
        failed=failed,
        notes=report_notes,
        llm=context.llm_report.model_dump(mode="json") if context.llm_report is not None else None,
    )
