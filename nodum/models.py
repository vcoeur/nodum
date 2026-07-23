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
    graph_id: str
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
    graph_id: str
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
    """

    id: int
    node_id: str
    title: str | None
    content: str
    props: dict[str, Any]
    actor: str
    event_seq: int
    state: str
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


class PolicyOut(BaseModel):
    """One agent's policy ruleset (design §8.3).

    ``rules`` is the stored JSON ruleset verbatim: rule objects keyed by
    ``job`` (internal-agent jobs, evaluated by the Phase-5 runtime) or
    ``edge_type`` (evaluated on the write path), each with an ``action``
    (``auto_accept`` / ``auto_apply`` / ``always_propose``) and an optional
    ``min_confidence`` gate. Rules are validated on write; unknown extra keys
    are preserved for forward compatibility.
    """

    agent: str
    rules: list[dict[str, Any]]
    updated_by: str
    updated_at: str


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

    ``nodes`` always includes the root (first); ``edges`` are the active
    edges the walk followed (empty at depth 0).
    """

    root: str
    depth: int
    nodes: list[NodeOut]
    edges: list[EdgeOut]


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
