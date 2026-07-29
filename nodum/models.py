"""Pydantic I/O models shared by every surface (CLI today, HTTP/MCP later).

Every surface serialises the same ``model_dump(mode="json")`` envelope, so
identical data yields identical JSON across adapters.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class NodeOut(BaseModel):
    """A graph node as emitted to clients. ``type`` is the type id."""

    id: str
    space_id: str | None
    type: str
    parent_id: str | None
    position: float | None
    title: str | None
    content: str
    props: dict[str, Any]
    state: str
    created_by: str
    created_at: str
    updated_at: str


class EdgeOut(BaseModel):
    """A typed, directed edge. ``type`` is the edge-type id."""

    id: str
    src_id: str
    dst_id: str
    type: str
    props: dict[str, Any]
    confidence: float | None
    created_by: str
    state: str
    valid_from: str | None
    valid_to: str | None
    created_at: str


class VersionOut(BaseModel):
    """One snapshot of a node's title/content/props after a mutation.

    ``state`` is ``applied`` for snapshots of applied node state, ``proposed``
    for an agent's pending update (design §8.1), and ``archived`` for a
    rejected one.

    ``proposed_fields`` names the fields a proposed update actually asked to
    change — and the only ones an accept writes back; the rest of the snapshot
    is the node's state at proposal time, shown as reviewer context. It is
    ``None`` on snapshots that are not proposals.
    """

    id: int
    node_id: str
    title: str | None
    content: str
    props: dict[str, Any]
    actor: str
    event_seq: int
    state: str
    proposed_fields: list[str] | None = None
    created_at: str


class EventOut(BaseModel):
    """One append-only event-log entry."""

    seq: int
    actor: str
    op: str
    payload: dict[str, Any]
    cycle_id: str | None
    created_at: str


class TypeOut(BaseModel):
    """A node type (user-extensible class)."""

    id: str
    name: str
    parent_type_id: str | None
    json_schema: dict[str, Any]
    is_builtin: bool


class EdgeTypeOut(BaseModel):
    """An edge type, optionally naming its inverse."""

    id: str
    name: str
    inverse_name: str | None
    json_schema: dict[str, Any]
    is_builtin: bool


class TypesOut(BaseModel):
    """The live type catalog: node types and edge types."""

    node_types: list[TypeOut]
    edge_types: list[EdgeTypeOut]


class UndoResult(BaseModel):
    """The outcome of reversing one event.

    ``restored`` is the row state written back (``None`` when the reversal
    deleted a created row); ``deleted`` lists rows removed by a create
    reversal (the created row plus, for nodes, its versions and incident
    edges).

    ``restored_version`` is the second row a reversal can move: accepting a
    proposed update rewrites the node *and* flips the ``versions`` row to
    ``applied``, so undoing it puts the proposal back to ``proposed`` and says
    so here. It is ``None`` on every reversal that moved no version — and on
    undoing a *rejection*, where the version row is the one under ``restored``.
    """

    undone_seq: int
    undone_op: str
    restored: dict[str, Any] | None
    deleted: list[dict[str, Any]]
    restored_version: dict[str, Any] | None = None
    undo_event_seq: int


class InitResult(BaseModel):
    """The outcome of ``init``: where the DB lives and what was applied."""

    db_path: str
    applied: list[str]
    already_applied: list[str]


class ProjectorStatus(BaseModel):
    """One projector's checkpoint state and backlog.

    ``last_event_seq`` is the highest event the projector has applied;
    ``pending_events`` counts newer events it has not seen yet; ``rows`` is
    the size of its derived store (index rows for the FTS projector, chunks
    for the vector projector). ``available`` is false when the projector
    cannot make progress (e.g. no embedding provider for ``vec``); ``detail``
    then carries the reason.
    """

    name: str
    last_event_seq: int
    pending_events: int
    rows: int
    available: bool = True
    detail: str | None = None


class ProjectorRun(BaseModel):
    """The outcome of running (or rebuilding) one projector.

    ``applied`` counts the events consumed in this call; ``from_seq`` /
    ``to_seq`` are the checkpoint before and after. When a projector is
    unavailable the run is a no-op (``applied`` 0, checkpoint unmoved) and
    ``detail`` carries the reason.
    """

    name: str
    applied: int
    from_seq: int
    to_seq: int
    detail: str | None = None


class SearchHit(BaseModel):
    """One search result: a node plus its fused score and per-signal breakdown.

    ``score`` is the fused ranking score (higher is better); ``signals``
    carries each retrieval signal's contribution: RRF contributions for
    ``bm25`` and ``vector`` (they sum to ``score``), and the edge weight for
    ``graph`` expansion hits.

    ``space_id`` is the space the node lives in. A result list spans every
    space in scope unless ``--space`` narrowed it, so without this a reader
    scanning results cannot tell a ``main`` hit from a ``research`` one
    (human-UI D1: the filter is a filter, not a mode). It is nullable for the
    same reason :class:`NodeOut` is — the column is.
    """

    node_id: str
    space_id: str | None
    type: str
    title: str | None
    snippet: str
    score: float
    signals: dict[str, float]


class SearchResult(BaseModel):
    """A ranked result list for one query."""

    query: str
    k: int
    hits: list[SearchHit]


class ProposalOut(BaseModel):
    """One pending proposal in the review queue.

    ``kind`` is ``node`` (a proposed node), ``edge`` (a proposed edge), or
    ``update`` (a proposed new version of an existing node). ``context``
    carries what a reviewer needs beyond the row itself, as one entry per
    referenced node — ``src``/``dst`` for an edge, ``parent`` for a node that
    has one, ``node`` for an update. Every entry is ``{id, title, space_id}``,
    and the space is what lets the review queue group by space (human-UI D4):
    a proposed node states its own, and an edge or an update would otherwise
    state none. A referenced node that no longer resolves comes back as ``{id}``
    alone, so read the other two as optional.
    """

    kind: str
    id: str
    type: str
    created_by: str
    created_at: str
    node: NodeOut | None = None
    edge: EdgeOut | None = None
    version: VersionOut | None = None
    context: dict[str, Any] = {}


class TransitionFailure(BaseModel):
    """One id a batch transition could not process, with the reason."""

    id: str
    error: str


class BatchTransitionOut(BaseModel):
    """The outcome of a batch accept/reject.

    ``transitioned`` lists the ids that moved state (each emitted its own
    event); ``failed`` lists ids skipped because they were unknown or not in
    the required state — a batch never aborts on a single bad id.
    """

    action: str
    actor: str
    reason: str | None = None
    transitioned: list[str]
    failed: list[TransitionFailure]


class SubgraphOut(BaseModel):
    """A rooted subgraph: the nodes and edges reached by a traversal.

    ``nodes`` always includes the root (first); ``edges`` are the edges the
    walk followed (empty at depth 0). No edge ever names a node ``nodes`` does
    not carry. From the capped read (``subgraph``) the edge list is also
    *closed* over ``nodes``: an edge between two returned nodes is returned
    even when the walk never traversed it, so nothing is drawn as unconnected
    that the stored graph connects. The uncapped walks (``traverse``,
    ``get_neighborhood``) return only what they traversed, so at their
    outermost ring two returned nodes may be connected by an edge they omit.

    ``truncated`` is true when a cap — on nodes **or** on edges — stopped the
    walk before it ran out of graph, so a caller can say "showing 200 of more"
    instead of presenting a partial subgraph as the whole neighborhood. A
    filter removing nodes is not truncation: the caller asked for that. Only
    the capped read (``subgraph``) can set it; the uncapped walks always
    report false.
    """

    root: str
    depth: int
    nodes: list[NodeOut]
    edges: list[EdgeOut]
    truncated: bool = False


class PathOut(BaseModel):
    """The shortest active-edge path between two nodes.

    When ``found`` is true, ``edges[i]`` connects ``nodes[i]`` to
    ``nodes[i+1]`` (in either stored direction).
    """

    found: bool
    hops: int
    nodes: list[NodeOut]
    edges: list[EdgeOut]


class DiffOut(BaseModel):
    """A unified diff between two versions of a node.

    ``diff`` is a ``difflib`` unified diff over a stable text rendering
    (title line, props JSON, then content); ``changed_fields`` names the
    fields that differ.
    """

    node_id: str
    a: VersionOut
    b: VersionOut
    changed_fields: list[str]
    diff: str


class ItemFailure(BaseModel):
    """One item a batch create could not process, with the reason."""

    index: int
    error: str


class ProposeEdgesOut(BaseModel):
    """The outcome of a batch edge proposal.

    ``created`` lists the edges that were written (each its own event, in
    input order); ``failed`` lists the suggestions that raised — a batch
    never aborts on a single bad suggestion.
    """

    created: list[EdgeOut]
    failed: list[ItemFailure]


class AssetOut(BaseModel):
    """A registered content-addressed binary asset (design §5.2).

    Metadata only — the bytes live in the ``asset_blobs`` table of the same
    database file, keyed by the same sha256. ``extracted_text`` is NULL until
    the Phase-4 ingestion pipeline fills it.
    """

    hash: str
    mime: str
    size_bytes: int
    original_name: str | None
    extracted_text: str | None
    created_at: str


class RenditionOut(BaseModel):
    """A derived image rendition of an asset (design §5.7).

    Renditions are lazily generated, stored in the database, and evictable —
    all regenerable from the original. ``cached`` is false on the call that
    generated (or regenerated) the rendition and true on cache hits.
    ``data_base64`` carries the WebP bytes only when requested
    (``include_data``) — the MCP path.
    """

    id: str
    asset_hash: str
    profile: str
    mime: str
    width: int
    height: int
    size_bytes: int
    cached: bool
    data_base64: str | None = None


class PurgeResult(BaseModel):
    """The outcome of evicting stored renditions (rows deleted, bytes reclaimed)."""

    purged: int
    bytes_freed: int


class HandlerStatus(BaseModel):
    """One extraction handler's reach and whether it can run (design §5.7).

    ``mimes`` are the MIME families the handler claims. ``available`` is
    false when its optional dependency is absent, and ``detail`` then names
    the extra to install — the same degradation contract the embedding
    provider reports through :class:`ProjectorStatus`.
    """

    name: str
    mimes: list[str]
    available: bool
    detail: str | None = None


class ExtractionOut(BaseModel):
    """What extraction got out of one asset, as reported to clients.

    The text itself is not echoed here — it lands on ``assets.extracted_text``
    and in the ``source`` node's content, and a result envelope carrying a
    whole PDF's text would be unusable. ``chars`` and ``pages`` are how a
    caller tells "nothing came out" from "a lot came out", and ``detail``
    says why when the answer is nothing.
    """

    handler: str
    chars: int
    pages: int
    detail: str | None = None


class IngestOut(BaseModel):
    """The outcome of ingesting one file or URL (design §5.5–§5.7).

    ``created`` is false when this asset already had a describing node in the
    target space: ingestion is idempotent, so a re-run after a partial
    failure returns the existing subgraph rather than tripping the
    one-``asset_ref``-per-(hash, space) index. ``pages`` are the per-page
    ``block`` children created under ``source``, and ``pages_truncated`` says
    the document had more than the cap allowed — never a silent truncation.
    """

    asset: AssetOut
    asset_ref: NodeOut
    source: NodeOut
    pages: list[NodeOut] = []
    pages_truncated: bool = False
    edges: list[EdgeOut] = []
    extraction: ExtractionOut
    created: bool
    event_seq: int


class UrlGrantOut(BaseModel):
    """A short-lived, single-use capability URL (design §5.7 rule 4).

    ``token`` is shown once and never stored in the clear — the database
    keeps only its sha256, exactly as an agent token is kept. ``url`` is the
    ready-to-use address the token is embedded in.
    """

    kind: str
    token: str
    url: str
    asset_hash: str | None
    expires_at: str
    max_bytes: int | None = None


class UploadGrantOut(BaseModel):
    """The answer to ``request_upload_url``: a grant, or an instant dedup hit.

    When the caller declares a sha256 the store already holds, ``asset`` is
    the existing row, ``grant`` is ``None``, and **no bytes move** — the
    dedup shortcut the design asks the declared hash to enable. Otherwise
    ``grant`` carries the single-use upload URL and ``asset`` is ``None``.
    """

    grant: UrlGrantOut | None = None
    asset: AssetOut | None = None


class HumanOut(BaseModel):
    """A human account (identity + credentials + attribution, never a scope)."""

    id: str
    name: str
    has_password: bool
    disabled: bool
    created_at: str


class AgentOut(BaseModel):
    """An agent account. ``has_token`` is all anyone ever learns of the token."""

    id: str
    kind: str
    name: str
    owner_human_id: str | None
    has_token: bool
    disabled: bool
    created_at: str


class AgentCreatedOut(BaseModel):
    """A new agent plus its token — the one and only time the token is shown."""

    agent: AgentOut
    token: str


class GrantOut(BaseModel):
    """One (agent, space) grant row."""

    agent_id: str
    space_id: str
    level: str
    created_at: str


class CycleOut(BaseModel):
    """One consolidation cycle — a dream-journal entry (design §8.4).

    A cycle groups a set of graph writes under one id so a human can take the
    whole of it back in one action. The fields mirror the ``cycles`` table, and
    the omission is deliberate: there is **no diff here**. What the cycle
    changed is ``list_events(cycle_id=…)``, read from the append-only log
    itself, so the journal can never become a second record that disagrees with
    it.

    ``triggered_by`` is who *asked* — a human's ``human:<id>``, or the literal
    ``scheduler`` — and is deliberately not the ``actor`` on the events inside,
    which is who *acted* (the gardener). ``report`` is the runner's summary,
    ``None`` while the cycle is still running, as ``finished_at`` is.
    ``rolled_back_by`` names the rollback cycle that reversed this one.
    """

    id: str
    trigger: str
    triggered_by: str
    scope: str | None
    dry_run: bool
    status: str
    report: dict[str, Any] | None
    started_at: str
    finished_at: str | None
    rolled_back_by: str | None


class CycleDetailOut(BaseModel):
    """One journal entry with the diff a reviewer reads it by (design §8.4).

    :class:`CycleOut` is the row; this is the row plus the two things a journal
    view has to render beside it, and **neither is a second record**.

    ``events`` is ``list_events(cycle_id=…)`` — the append-only log itself,
    newest first, exactly as ``GET /api/events`` returns it — so the "what
    changed" a reader sees is the log and cannot disagree with it.
    ``events_truncated`` is true when the read hit its limit and is
    deliberately conservative (the same rule :class:`SubgraphOut` follows): it
    says the list may be short, not that it provably is.

    ``metrics`` is a *projection* of ``cycle.report["metrics"]``, lifted out so
    the before/after coherence numbers are one field rather than a path into an
    untyped blob. It is read from the report on every request and stored
    nowhere, so it cannot drift from it; a cycle whose report carries no metrics
    — a rollback, or a one-op curative cycle — reports ``{}``.
    """

    cycle: CycleOut
    metrics: dict[str, dict[str, float]] = {}
    events: list[EventOut] = []
    events_truncated: bool = False


class MergeRedirectOut(BaseModel):
    """One ``merge_redirects`` row: where a tombstone went, and on which event.

    The table has existed since migration ``0001`` and the curative tier is its
    first writer. ``event_seq`` names the ``node.merge`` event that archived the
    tombstone, so the redirect and the log entry that caused it are one lookup
    apart in either direction.
    """

    tombstone_id: str
    into_id: str
    event_seq: int
    created_at: str


class RetiredEdgeOut(EdgeOut):
    """An edge a merge archived instead of repointing, **with its reason**.

    Every :class:`EdgeOut` field is here unchanged, so a client that only wants
    the edge keeps reading it as one. ``reason`` is the sentence the merge
    recorded in the event payload — the repointing would have produced a
    self-loop, or a duplicate of an edge the survivor already carries — lifted
    into the return value because a caller reading the result should not have to
    go to the event log to learn why an edge left the live graph.
    """

    reason: str


class MergeOut(BaseModel):
    """The outcome of a soft merge (design D9): reversible, nothing destroyed.

    ``tombstones`` are the merged-away nodes as they now stand — ``archived``,
    each carrying ``props.merged_into``. ``relinked`` are the edges repointed at
    the survivor, each carrying its original endpoints in
    ``props.merged_from``; ``retired`` are the incident edges that could not be
    repointed because the repointing would have produced a self-loop or a
    duplicate of an edge the survivor already carries — each carrying the
    ``reason`` it was retired. Both lists are reported rather than summarised: an
    edge that quietly vanished from the live graph is the kind of thing a merge
    must never do without saying so, and "without saying so" includes saying
    *which* of the two rules bit.

    ``cycle_id`` is the consolidation cycle the whole merge was stamped with —
    its own one-op ``curative`` cycle when a human invoked it directly, or the
    ambient one when a runner did. It is what a rollback takes back.
    """

    into: NodeOut
    tombstones: list[NodeOut]
    redirects: list[MergeRedirectOut]
    relinked: list[EdgeOut]
    retired: list[RetiredEdgeOut]
    cycle_id: str


class RetypeOut(BatchTransitionOut):
    """The outcome of a curative retype — a batch transition plus what it wrote.

    A node's type is fixed at creation by design; ``retype`` is the one
    sanctioned exception (design §8.2), which is why it is a curative operation
    and not a field on ``PATCH /api/nodes/{id}``. ``transitioned`` lists the
    nodes whose type actually changed and ``failed`` the ones skipped, exactly
    as a batch accept/reject reports them.
    """

    new_type: str
    cycle_id: str


class SupersedeOut(BaseModel):
    """The outcome of superseding one edge (design §8.2).

    ``superseded`` is the original edge as it now stands: ``valid_to`` closed
    (*when it stopped being true*) **and** ``archived`` (*it is no longer part
    of the live graph*) — two different facts, both recorded. ``replacement``
    is the edge that takes over, when one was given; the two are linked through
    the seeded ``supersedes``/``superseded_by`` vocabulary carried in each
    edge's ``props``, since an edge's endpoints are nodes and one edge cannot
    point at another.
    """

    superseded: EdgeOut
    replacement: EdgeOut | None
    cycle_id: str


class RelinkDiff(BaseModel):
    """One edge a bulk relink would change (or did), stated as old → new."""

    edge_id: str
    src_id: str
    from_dst_id: str
    to_dst_id: str
    from_type: str
    to_type: str


class BulkRelinkOut(BaseModel):
    """The outcome — or, on a dry run, the proposal — of a bulk relink.

    ``matched`` counts the edges the selector reached and ``changes`` the ones
    that would change (or did). The rest are reported in **two** lists, because
    they are two different facts. ``unchanged`` is a bare list of edge ids the
    change would not alter — a diff annotation, and the reason a caller asked
    for something that was already true. ``skipped`` is the refusals, each with
    a reason: a self-loop, a duplicate of an edge the graph already carries, or
    a space the caller may not edit.

    They used to share one list under a field named ``error``, so "nothing would
    change on this edge" and "you may not edit that space" were distinguishable
    only by matching the sentence — which is why ``bulk-relink`` was for one
    round the only batch verb whose exit code was not derived from its failure
    list. It is derived from it now: ``skipped`` is the failures, ``unchanged``
    is not one, and ``nodum bulk-relink`` exits 1 when ``skipped`` is non-empty
    on a run that actually happened.

    **A dry run is the exception, and it is the only one.** Every check a real
    run makes runs on the rehearsal too, so ``skipped`` there is an accurate
    *prediction* — but nothing was attempted and nothing was lost, so it costs
    no exit code. Read ``dry_run`` before reading ``skipped`` as a failure.

    ``truncated`` is true when the server-side ceiling stopped the selection
    short of the whole match — never a silent truncation.

    ``dry_run`` writes nothing at all: no cycle is opened and no event is
    emitted, so ``cycle_id`` is ``None``. That is the reviewable diff §8.5 asks
    for on a large refactor; the reversal, once it is applied for real, is the
    cycle.
    """

    dry_run: bool
    matched: int
    changes: list[RelinkDiff]
    unchanged: list[str]
    skipped: list[TransitionFailure]
    truncated: bool
    cycle_id: str | None = None


class RollbackConflictOut(BaseModel):
    """One row that stands between a cycle and its rollback (decision C4).

    Rollback is atomic and **refuses rather than clobbers**: if anything outside
    the cycle has touched a row the cycle touched, reversing the cycle would
    overwrite that later work, which is the failure shape this project has
    already closed twice. So the refusal is a list of these, and each one names
    both ends of the collision — the cycle's own event, and the event that moved
    the row since — because a human told *which* rows are in the way can act,
    and one told "rollback failed" cannot.

    ``kind`` is ``node`` or ``edge``; ``conflicting_cycle_id`` is set when the
    later work was itself a cycle's (still "outside this cycle", and still a
    conflict).
    """

    kind: str
    row_id: str
    cycle_event_seq: int
    cycle_event_op: str
    conflicting_seq: int
    conflicting_op: str
    conflicting_actor: str
    conflicting_cycle_id: str | None


class RollbackBlockerOut(BaseModel):
    """A row a rollback would have to delete but cannot — the guards, as data.

    A conflict is the graph having *moved* a row the cycle wrote
    (:class:`RollbackConflictOut`). A blocker is the other refusal shape: the
    graph having *grown something onto* a row the cycle created, so the delete
    that reverses that create would have to cascade past what the reversal was
    asked to touch. Both stop a rollback; only one of them was visible to the
    preflight before, which meant a dry run could report ``conflicts: []`` for a
    rollback that then failed.

    ``row_id`` is the row the cycle created, named by ``cycle_event_seq`` /
    ``cycle_event_op``. ``dependants`` are the ids in the way — children of a
    node, occupants of a space, nodes typed by a type node, agents granted on a
    space, merge redirects naming a node — and ``reason`` is the guard's own
    sentence, which is what the refusal says if the rollback is attempted.
    """

    kind: str
    row_id: str
    cycle_event_seq: int
    cycle_event_op: str
    dependants: list[str]
    reason: str


class RollbackOut(BaseModel):
    """The outcome — or, on a dry run, the verdict — of rolling a cycle back.

    ``cycle_id`` is the cycle taken back and ``rollback_cycle_id`` the new
    ``trigger='rollback'`` cycle every reversal event is stamped with (decision
    C5): a rollback is reversed the way everything else with a ``cycle_id`` is,
    by rolling *it* back, which re-applies the original. It is ``None`` on a dry
    run, which opens no cycle and writes nothing.

    ``reversed_events`` lists the cycle's event seqs in the order they were
    reversed (newest first, which is the only order in which a create and the
    updates on top of it come apart). ``skipped_events`` are the cycle's
    non-graph events — audit records like ``asset.download`` that have no graph
    effect to reverse.

    ``restored_versions`` names the ``versions`` rows the reversal put back —
    the review decisions inside the cycle, by version id. A review moves two
    rows from one decision and only the node is a graph record, so an accept's
    version move rides on the ``node.update`` it caused and a reject is a
    ``version.reject`` of its own; both are reversed, and both are counted here
    rather than in ``restored_nodes``.

    ``conflicts`` is empty on a rollback that happened; on a dry run it is the
    reason it would not. ``blockers`` is the second half of that verdict — the
    delete guards, which refuse for a different reason and used to be invisible
    until the rollback was already running. Both lists are empty on a rollback
    that happened, and a dry run reporting either is a rollback that would fail.
    """

    cycle_id: str
    rollback_cycle_id: str | None
    dry_run: bool
    reversed_events: list[int]
    skipped_events: list[int]
    restored_nodes: list[str]
    restored_edges: list[str]
    restored_versions: list[int] = []
    deleted_nodes: list[str]
    deleted_edges: list[str]
    redirects_removed: list[str]
    conflicts: list[RollbackConflictOut]
    blockers: list[RollbackBlockerOut] = []


class SpaceOut(NodeOut):
    """A space node plus what makes it *territory* rather than a name.

    A space is a node of builtin type ``space``, so every :class:`NodeOut`
    field is here unchanged and a client that only wants the node keeps
    reading it as one.

    ``node_count`` counts the space's **live** nodes — ``active`` plus
    ``proposed``, since a space holding nothing but proposals is not empty —
    and excludes ``archived`` ones, which are retired rather than territory.
    ``grants`` lists the agents holding a grant on the space, which is how a
    human sees delegated territory at a glance (an ``edit``-granted space
    governs itself and never reaches the review queue).
    """

    node_count: int
    grants: list[GrantOut]
