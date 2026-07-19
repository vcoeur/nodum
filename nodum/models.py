"""The single pydantic I/O schema shared by every surface.

The CLI, the HTTP API, and the web view all serialise through these models, so
one canonical JSON envelope is produced regardless of entry point. UUID and
datetime fields render as strings under ``model_dump(mode="json")``, which is
what every adapter emits. A node/edge carries its ``kind`` (a kind name); a
node's universal text lives in the top-level ``content`` field (the embeddable
body), and kind-specific metadata in ``data``. Kinds themselves are editable at
runtime — the kind-definition models below carry the field shape / endpoint
signature in and out of the kind-CRUD surfaces.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

# Maximum length of the derived display title (the first line of content).
TITLE_MAX_CHARS = 80


def derive_title(content: str) -> str:
    """Derive a node's display title from its content's first non-blank line.

    Titles are derived at read time (never stored), so they track edits for
    free and no migration is needed. The title is the first non-blank line,
    stripped, capped at ``TITLE_MAX_CHARS`` characters.
    """
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:TITLE_MAX_CHARS]
    return ""


# ── Output models ───────────────────────────────────────────────────────────


class NodeOut(BaseModel):
    """A node: its kind, its plain-text content, its metadata, and timestamps.

    The serialised payload also carries ``title`` — a computed display title
    derived from the first non-blank line of ``content`` (see
    :func:`derive_title`). It is additive and identical across every surface.
    """

    uuid: UUID
    kind: str
    content: str
    data: dict
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def title(self) -> str:
        """Display title derived from the first non-blank line of content."""
        return derive_title(self.content)


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
    """Input for ``add_node``: the kind, the node content, and extra payload keys."""

    kind: str
    content: str
    data: dict = Field(default_factory=dict)


class AddEdgeIn(BaseModel):
    """Input for ``add_edge``: the kind, the two endpoint UUIDs, extra payload."""

    kind: str
    from_uuid: UUID
    to_uuid: UUID
    data: dict = Field(default_factory=dict)


class UpdateNodeIn(BaseModel):
    """Input for ``update_node``: an optional new content and/or payload keys to merge."""

    content: str | None = None
    data: dict | None = None


class UpdateEdgeIn(BaseModel):
    """Input for ``update_edge``: payload keys to merge (kind and endpoints are fixed)."""

    data: dict | None = None


class LoginIn(BaseModel):
    """Input for ``POST /auth/login``: the candidate main password."""

    password: str


# ── Kind-definition inputs (the evolvable schema) ─────────────────────────────


class NodeKindIn(BaseModel):
    """Input for creating a node kind: name, group, content label, field schema."""

    name: str
    group: str = ""
    content_label: str = "text"
    fields: dict = Field(default_factory=dict)


class NodeKindPatch(BaseModel):
    """Input for editing a node kind; only the provided attributes change."""

    group: str | None = None
    content_label: str | None = None
    fields: dict | None = None


class EdgeKindIn(BaseModel):
    """Input for creating an edge kind: name, endpoint signature, fields.

    The endpoint lists are accepted as ``from`` / ``to`` (the schema wire names)
    or as ``from_kinds`` / ``to_kinds``.
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str
    from_kinds: list[str] = Field(default_factory=list, alias="from")
    to_kinds: list[str] = Field(default_factory=list, alias="to")
    symmetric: bool = False
    fields: dict = Field(default_factory=dict)


class EdgeKindPatch(BaseModel):
    """Input for editing an edge kind; only the provided attributes change."""

    model_config = ConfigDict(populate_by_name=True)

    from_kinds: list[str] | None = Field(default=None, alias="from")
    to_kinds: list[str] | None = Field(default=None, alias="to")
    symmetric: bool | None = None
    fields: dict | None = None


# ── Composite results ───────────────────────────────────────────────────────


class NodeWithEdges(BaseModel):
    """A node together with every edge incident on it (either direction)."""

    node: NodeOut
    edges: list[EdgeOut]


class FailedTarget(BaseModel):
    """A target that could not be resolved in a multi-target read."""

    uuid: str
    error: str


class NodesWithEdges(BaseModel):
    """A multi-target ``get``: the resolved nodes (with edges) and the misses.

    ``targets`` echoes the requested identifiers in order; ``nodes`` holds one
    :class:`NodeWithEdges` per resolved target; ``failed`` holds one entry per
    unresolved target, so a single bad UUID never aborts the read.
    """

    targets: list[str]
    nodes: list[NodeWithEdges]
    failed: list[FailedTarget]


class BatchItemResult(BaseModel):
    """One item's outcome in a batch write: its index, and the uuid or the error."""

    index: int
    ok: bool
    uuid: UUID | None = None
    error: str | None = None


class BatchResult(BaseModel):
    """Summary of a batch write: per-item outcomes plus tallies.

    A batch is one connection pass with a savepoint per item, so one bad item
    never aborts the rest; ``dry_run`` validates every item and rolls back.
    """

    operation: str
    count: int
    succeeded: int
    failed: int
    dry_run: bool = False
    results: list[BatchItemResult]


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


class KindDeleted(BaseModel):
    """Result of deleting a kind: name, rows reassigned, rows removed, and that it went.

    ``reassigned`` counts rows moved to another kind (``into``); ``removed`` counts rows
    deleted outright (edge-kind ``purge``). At most one is non-zero.
    """

    name: str
    reassigned: int
    removed: int = 0
    deleted: bool
