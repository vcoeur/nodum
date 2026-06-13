"""The single pydantic I/O schema shared by every surface.

The CLI, the HTTP API, and the web view all serialise through these models, so
one canonical JSON envelope is produced regardless of entry point. UUID and
datetime fields render as strings under ``model_dump(mode="json")``, which is
what every adapter emits. A node/edge carries its ``kind`` (a metamodel name);
kind-specific fields live in ``data`` and are validated by ``nodum.metamodel``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

# ── Output models ───────────────────────────────────────────────────────────


class NodeOut(BaseModel):
    """A node: its kind, its self-describing JSON payload, and timestamps."""

    uuid: UUID
    kind: str
    data: dict
    created_at: datetime
    updated_at: datetime


class EdgeOut(BaseModel):
    """A directed, typed edge between two nodes, with a JSON payload."""

    uuid: UUID
    kind: str
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
    """Input for ``add_node``: the kind, the node text, and extra payload keys."""

    kind: str
    text: str
    data: dict = Field(default_factory=dict)


class AddEdgeIn(BaseModel):
    """Input for ``add_edge``: the kind, the two endpoint UUIDs, extra payload."""

    kind: str
    from_uuid: UUID
    to_uuid: UUID
    data: dict = Field(default_factory=dict)


class UpdateNodeIn(BaseModel):
    """Input for ``update_node``: an optional new text and/or payload keys to merge."""

    text: str | None = None
    data: dict | None = None


class UpdateEdgeIn(BaseModel):
    """Input for ``update_edge``: payload keys to merge (kind and endpoints are fixed)."""

    data: dict | None = None


class LoginIn(BaseModel):
    """Input for ``POST /auth/login``: the candidate main password."""

    password: str


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


class Deleted(BaseModel):
    """Result of a delete: the uuid removed and how many rows it cascaded to."""

    uuid: UUID
    deleted: int
