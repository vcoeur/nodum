"""The single pydantic I/O schema shared by every surface.

The CLI, the HTTP API, and the web view all serialise through these models, so
one canonical JSON envelope is produced regardless of entry point. UUID and
datetime fields render as strings under ``model_dump(mode="json")``, which is
what every adapter emits.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

# ── Output models (one canonical shape per entity) ──────────────────────────


class NodeOut(BaseModel):
    """A node: one atomic idea or fact, plus its self-describing JSON payload."""

    uuid: UUID
    data: dict
    created_at: datetime
    updated_at: datetime


class EdgeOut(BaseModel):
    """A directed, UUID-keyed edge between two nodes, with a JSON payload."""

    uuid: UUID
    from_uuid: UUID
    to_uuid: UUID
    data: dict
    created_at: datetime
    updated_at: datetime


class SearchHit(NodeOut):
    """A node returned by full-text search, carrying its relevance score."""

    score: float


# ── Operation inputs ────────────────────────────────────────────────────────


class AddNodeIn(BaseModel):
    """Input for ``add_node``: the node text, an optional type, extra payload keys."""

    text: str
    type: str | None = None
    data: dict = Field(default_factory=dict)


class AddEdgeIn(BaseModel):
    """Input for ``add_edge``: the two endpoint UUIDs, an optional type, extra payload."""

    from_uuid: UUID
    to_uuid: UUID
    type: str | None = None
    data: dict = Field(default_factory=dict)


# ── Composite results ───────────────────────────────────────────────────────


class NodeWithEdges(BaseModel):
    """A node together with every edge incident on it (either direction)."""

    node: NodeOut
    edges: list[EdgeOut]


class SearchResult(BaseModel):
    """A full-text search response: the query, the match count, the ranked hits."""

    query: str
    total: int
    hits: list[SearchHit]


class Subgraph(BaseModel):
    """A subgraph expanded from a seed set: the reachable nodes and the edges."""

    seed: list[UUID]
    depth: int
    nodes: list[NodeOut]
    edges: list[EdgeOut]
