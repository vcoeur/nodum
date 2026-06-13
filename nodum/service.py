"""The unified data-service layer — kind-aware CRUD over the typed metamodel.

The single source of truth for every operation and all validation. Nodes and
edges live in one generic table each; a row's ``kind`` references the metamodel
(:mod:`nodum.metamodel`), which defines field shapes and edge endpoint
signatures. The CLI, HTTP API, and web view are thin adapters over these
functions. Each function opens a short-lived connection and commits.

Validation is soft (raised from here as ``metamodel.ValidationError``, a
``ValueError``); the database enforces only the cheap universals (FKs,
``from≠to``, ``data ? 'text'``, ``kind ∈`` the lookup tables).
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from psycopg import Cursor
from psycopg.types.json import Json

from nodum import metamodel
from nodum.db import connect
from nodum.models import (
    Deleted,
    EdgeOut,
    NodeOut,
    NodeWithEdges,
    SearchHit,
    SearchResult,
    Subgraph,
)

_NODE_COLS = "uuid, kind, data, created_at, updated_at"
_EDGE_COLS = "uuid, kind, from_uuid, to_uuid, data, created_at, updated_at"


class NodeNotFound(Exception):
    """Raised when an operation references a node UUID that does not exist."""

    def __init__(self, uuid: str | UUID) -> None:
        self.uuid = str(uuid)
        super().__init__(f"node {self.uuid} not found")


class EdgeNotFound(Exception):
    """Raised when an operation references an edge UUID that does not exist."""

    def __init__(self, uuid: str | UUID) -> None:
        self.uuid = str(uuid)
        super().__init__(f"edge {self.uuid} not found")


def _kind_of(cur: Cursor, uuid: str | UUID) -> str:
    """Return a node's kind, raising NodeNotFound if it does not exist."""
    cur.execute("SELECT kind FROM nodes WHERE uuid = %s", (str(uuid),))
    row = cur.fetchone()
    if row is None:
        raise NodeNotFound(uuid)
    return row["kind"]


# ── Create ──────────────────────────────────────────────────────────────────


def add_node(kind: str, text: str, data: dict | None = None) -> NodeOut:
    """Create a typed node and return it.

    Args:
        kind: A node kind from the metamodel.
        text: The node's universal text. Required and non-empty.
        data: Optional kind-specific payload keys, validated against the kind.

    Raises:
        metamodel.ValidationError: Unknown kind, empty text, or a bad field.
    """
    payload: dict = {"text": text, **(data or {})}
    metamodel.validate_node(kind, payload)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO nodes (kind, data) VALUES (%s, %s) RETURNING {_NODE_COLS}",
            (kind, Json(payload)),
        )
        row = cur.fetchone()
        conn.commit()
    return NodeOut(**row)


def add_edge(
    kind: str, from_uuid: str | UUID, to_uuid: str | UUID, data: dict | None = None
) -> EdgeOut:
    """Create a typed, directed edge and return it.

    The endpoints' kinds are checked against the edge kind's signature.

    Raises:
        NodeNotFound: If either endpoint does not exist.
        metamodel.ValidationError: Unknown edge kind, endpoint kind outside the
            signature, identical endpoints, or a bad field.
    """
    if str(from_uuid) == str(to_uuid):
        raise metamodel.ValidationError("an edge must connect two distinct nodes")
    payload: dict = dict(data or {})
    with connect() as conn, conn.cursor() as cur:
        from_kind = _kind_of(cur, from_uuid)
        to_kind = _kind_of(cur, to_uuid)
        metamodel.validate_edge(kind, from_kind, to_kind, payload)
        cur.execute(
            f"INSERT INTO edges (kind, from_uuid, to_uuid, data) VALUES (%s, %s, %s, %s) "
            f"RETURNING {_EDGE_COLS}",
            (kind, str(from_uuid), str(to_uuid), Json(payload)),
        )
        row = cur.fetchone()
        conn.commit()
    return EdgeOut(**row)


# ── Read ────────────────────────────────────────────────────────────────────


def get(uuid: str | UUID) -> NodeWithEdges:
    """Return a node plus every edge incident on it (either direction).

    Raises:
        NodeNotFound: If no node has the given UUID.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_NODE_COLS} FROM nodes WHERE uuid = %s", (str(uuid),))
        node = cur.fetchone()
        if node is None:
            raise NodeNotFound(uuid)
        cur.execute(
            f"SELECT {_EDGE_COLS} FROM edges "
            "WHERE from_uuid = %s OR to_uuid = %s ORDER BY created_at, uuid",
            (str(uuid), str(uuid)),
        )
        edges = cur.fetchall()
    return NodeWithEdges(node=NodeOut(**node), edges=[EdgeOut(**edge) for edge in edges])


def search(query: str, kind: str | None = None, limit: int = 20) -> SearchResult:
    """Full-text search over node text, ranked best-first, optionally by kind.

    Args:
        query: Free-text query (``plainto_tsquery`` — AND of terms).
        kind: Optional node-kind filter.
        limit: Maximum number of hits.
    """
    clause = "AND kind = %(kind)s" if kind else ""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_NODE_COLS}, "
            "ts_rank(to_tsvector('english', data ->> 'text'), "
            "        plainto_tsquery('english', %(q)s)) AS score "
            "FROM nodes "
            "WHERE to_tsvector('english', data ->> 'text') "
            f"      @@ plainto_tsquery('english', %(q)s) {clause} "
            "ORDER BY score DESC, created_at, uuid "
            "LIMIT %(limit)s",
            {"q": query, "kind": kind, "limit": limit},
        )
        rows = cur.fetchall()
    hits = [SearchHit(score=float(row.pop("score")), **row) for row in rows]
    return SearchResult(query=query, total=len(hits), hits=hits)


def expand(
    seed: str | UUID | Sequence[str | UUID],
    depth: int = 1,
    edge_kinds: Sequence[str] | None = None,
) -> Subgraph:
    """Expand a seed set into its connected subgraph, following edges outward.

    Args:
        seed: One UUID or a sequence of UUIDs.
        depth: Maximum hops (>= 1).
        edge_kinds: Optional list of edge kinds to traverse (others are skipped).

    Raises:
        metamodel.ValidationError: If ``depth < 1``.
    """
    if depth < 1:
        raise metamodel.ValidationError("depth must be at least 1")
    seeds = [str(seed)] if isinstance(seed, str | UUID) else [str(item) for item in seed]
    kinds = list(edge_kinds) if edge_kinds else None
    edge_filter = "AND e.kind = ANY(%(ek)s)" if kinds else ""
    anchor_filter = "AND kind = ANY(%(ek)s)" if kinds else ""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH RECURSIVE sub AS (
                SELECT {_EDGE_COLS}, 1 AS hop
                FROM edges
                WHERE from_uuid = ANY(%(seeds)s::uuid[]) {anchor_filter}
              UNION ALL
                SELECT e.uuid, e.kind, e.from_uuid, e.to_uuid, e.data,
                       e.created_at, e.updated_at, s.hop + 1
                FROM edges e
                JOIN sub s ON e.from_uuid = s.to_uuid
                WHERE s.hop < %(depth)s {edge_filter}
            )
            SELECT DISTINCT {_EDGE_COLS} FROM sub
            ORDER BY created_at, uuid
            """,
            {"seeds": seeds, "depth": depth, "ek": kinds},
        )
        edge_rows = cur.fetchall()
        node_ids = set(seeds)
        for edge in edge_rows:
            node_ids.add(str(edge["from_uuid"]))
            node_ids.add(str(edge["to_uuid"]))
        cur.execute(
            f"SELECT {_NODE_COLS} FROM nodes "
            "WHERE uuid = ANY(%s::uuid[]) ORDER BY created_at, uuid",
            (list(node_ids),),
        )
        node_rows = cur.fetchall()
    return Subgraph(
        seed=[UUID(item) for item in seeds],
        depth=depth,
        nodes=[NodeOut(**node) for node in node_rows],
        edges=[EdgeOut(**edge) for edge in edge_rows],
    )


def schema() -> dict:
    """Return the metamodel contract (node kinds + edge kinds + signatures)."""
    return metamodel.schema()


# ── Update ──────────────────────────────────────────────────────────────────


def update_node(uuid: str | UUID, text: str | None = None, data: dict | None = None) -> NodeOut:
    """Merge new text/payload into a node, re-validate against its kind, return it.

    Raises:
        NodeNotFound: If the node does not exist.
        metamodel.ValidationError: If the result violates the kind's shape.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_NODE_COLS} FROM nodes WHERE uuid = %s", (str(uuid),))
        row = cur.fetchone()
        if row is None:
            raise NodeNotFound(uuid)
        payload = dict(row["data"])
        if data:
            payload.update(data)
        if text is not None:
            payload["text"] = text
        metamodel.validate_node(row["kind"], payload)
        cur.execute(
            f"UPDATE nodes SET data = %s, updated_at = now() WHERE uuid = %s "
            f"RETURNING {_NODE_COLS}",
            (Json(payload), str(uuid)),
        )
        out = cur.fetchone()
        conn.commit()
    return NodeOut(**out)


def update_edge(uuid: str | UUID, data: dict | None = None) -> EdgeOut:
    """Merge new payload into an edge (kind and endpoints fixed), return it.

    Raises:
        EdgeNotFound: If the edge does not exist.
        metamodel.ValidationError: If the result violates the edge kind's fields.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_EDGE_COLS} FROM edges WHERE uuid = %s", (str(uuid),))
        row = cur.fetchone()
        if row is None:
            raise EdgeNotFound(uuid)
        payload = dict(row["data"])
        if data:
            payload.update(data)
        from_kind = _kind_of(cur, row["from_uuid"])
        to_kind = _kind_of(cur, row["to_uuid"])
        metamodel.validate_edge(row["kind"], from_kind, to_kind, payload)
        cur.execute(
            f"UPDATE edges SET data = %s, updated_at = now() WHERE uuid = %s "
            f"RETURNING {_EDGE_COLS}",
            (Json(payload), str(uuid)),
        )
        out = cur.fetchone()
        conn.commit()
    return EdgeOut(**out)


# ── Delete ──────────────────────────────────────────────────────────────────


def delete_node(uuid: str | UUID) -> Deleted:
    """Delete a node; its incident edges cascade. Returns the cascade count.

    Raises:
        NodeNotFound: If the node does not exist.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM edges WHERE from_uuid = %s OR to_uuid = %s",
            (str(uuid), str(uuid)),
        )
        edge_count = cur.fetchone()["n"]
        cur.execute("DELETE FROM nodes WHERE uuid = %s", (str(uuid),))
        if cur.rowcount == 0:
            raise NodeNotFound(uuid)
        conn.commit()
    # The node itself plus the edges that cascaded with it.
    return Deleted(uuid=UUID(str(uuid)), deleted=1 + edge_count)


def delete_edge(uuid: str | UUID) -> Deleted:
    """Delete a single edge.

    Raises:
        EdgeNotFound: If the edge does not exist.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM edges WHERE uuid = %s", (str(uuid),))
        if cur.rowcount == 0:
            raise EdgeNotFound(uuid)
        conn.commit()
    return Deleted(uuid=UUID(str(uuid)), deleted=1)
