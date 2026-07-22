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
    """One snapshot of a node's title/content/props after a mutation."""

    id: int
    node_id: str
    title: str | None
    content: str
    props: dict[str, Any]
    actor: str
    event_seq: int
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
    the size of its derived store (index rows for the FTS projector).
    """

    name: str
    last_event_seq: int
    pending_events: int
    rows: int


class ProjectorRun(BaseModel):
    """The outcome of running (or rebuilding) one projector.

    ``applied`` counts the events consumed in this call; ``from_seq`` /
    ``to_seq`` are the checkpoint before and after.
    """

    name: str
    applied: int
    from_seq: int
    to_seq: int


class SearchHit(BaseModel):
    """One search result: a node plus its fused score and per-signal breakdown.

    ``score`` is the fused ranking score (higher is better); ``signals``
    carries each retrieval signal's contribution (``bm25`` only today, with
    vector and graph-expansion signals slotting in alongside it later).
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
