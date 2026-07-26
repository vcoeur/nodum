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
    """

    undone_seq: int
    undone_op: str
    restored: dict[str, Any] | None
    deleted: list[dict[str, Any]]
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
    """

    node_id: str
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
    carries what a reviewer needs beyond the row itself — for an edge, the
    source/target node ids and titles; for a node, its parent's id/title when
    it has one; for an update, the current node's id/title/content.
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
