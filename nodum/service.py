"""The unified data-service layer — the single source of truth.

Every operation (`add_node`, `add_edge`, `get`, `search`, `expand`) and all
validation live here. The CLI, the HTTP API, and the web view are thin
adapters that call these functions and serialise the returned pydantic models;
they hold no logic of their own. Each function opens its own short-lived
connection and commits, so adapters stay stateless.

Type-as-node: when ``add_node`` is given a ``type``, it resolves-or-creates a
type node (a node whose payload is ``{"text": <type>, "kind": "type"}``) and
links the new node to it with an ``is`` edge. Edge types are stored as a
``data.type`` string on the edge.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from psycopg import Cursor
from psycopg.types.json import Json

from nodum.db import connect
from nodum.models import (
    EdgeOut,
    NodeOut,
    NodeWithEdges,
    SearchHit,
    SearchResult,
    Subgraph,
)

_NODE_COLS = "uuid, data, created_at, updated_at"
_EDGE_COLS = "uuid, from_uuid, to_uuid, data, created_at, updated_at"


class NodeNotFound(Exception):
    """Raised when an operation references a node UUID that does not exist."""

    def __init__(self, uuid: str | UUID) -> None:
        self.uuid = str(uuid)
        super().__init__(f"node {self.uuid} not found")


# ── Writes ──────────────────────────────────────────────────────────────────


def add_node(text: str, type: str | None = None, data: dict | None = None) -> NodeOut:
    """Insert a node and return it.

    When ``type`` is given, its type node is resolved-or-created and linked
    with an ``is`` edge (type-as-node). The type string is also kept on the
    node payload as ``data.type`` for convenience.

    Args:
        text: The node's primary text. Required and non-empty.
        type: Optional type name; drives the type node + ``is`` edge.
        data: Optional extra payload keys merged into the node.

    Returns:
        The newly created node.
    """
    if not text or not text.strip():
        raise ValueError("node text must be a non-empty string")
    payload: dict = {"text": text, **(data or {})}
    if type is not None:
        payload["type"] = type
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO nodes (data) VALUES (%s) RETURNING {_NODE_COLS}",
            (Json(payload),),
        )
        node_row = cur.fetchone()
        if type is not None:
            type_node = _resolve_or_create_type(cur, type)
            cur.execute(
                "INSERT INTO edges (from_uuid, to_uuid, data) VALUES (%s, %s, %s)",
                (node_row["uuid"], type_node["uuid"], Json({"type": "is"})),
            )
        conn.commit()
    return NodeOut(**node_row)


def add_edge(
    from_uuid: str | UUID,
    to_uuid: str | UUID,
    type: str | None = None,
    data: dict | None = None,
) -> EdgeOut:
    """Insert a directed edge ``from_uuid → to_uuid`` and return it.

    Args:
        from_uuid: Source node UUID. Must exist.
        to_uuid: Target node UUID. Must exist and differ from the source.
        type: Optional edge type, stored as ``data.type``.
        data: Optional extra payload keys merged into the edge.

    Returns:
        The newly created edge.

    Raises:
        NodeNotFound: If either endpoint does not exist.
        ValueError: If the two endpoints are identical.
    """
    if str(from_uuid) == str(to_uuid):
        raise ValueError("an edge must connect two distinct nodes")
    payload: dict = dict(data or {})
    if type is not None:
        payload["type"] = type
    with connect() as conn, conn.cursor() as cur:
        for endpoint in (from_uuid, to_uuid):
            cur.execute("SELECT 1 FROM nodes WHERE uuid = %s", (str(endpoint),))
            if cur.fetchone() is None:
                raise NodeNotFound(endpoint)
        cur.execute(
            f"INSERT INTO edges (from_uuid, to_uuid, data) VALUES (%s, %s, %s) "
            f"RETURNING {_EDGE_COLS}",
            (str(from_uuid), str(to_uuid), Json(payload)),
        )
        row = cur.fetchone()
        conn.commit()
    return EdgeOut(**row)


def _resolve_or_create_type(cur: Cursor, type_name: str) -> dict:
    """Find the type node named ``type_name``, creating it if absent.

    A type node is a plain node with payload ``{"text": name, "kind": "type"}``.
    Returns the row mapping (``uuid``/``data``/timestamps).
    """
    cur.execute(
        f"SELECT {_NODE_COLS} FROM nodes "
        "WHERE data ->> 'text' = %s AND data ->> 'kind' = 'type' LIMIT 1",
        (type_name,),
    )
    row = cur.fetchone()
    if row is not None:
        return row
    cur.execute(
        f"INSERT INTO nodes (data) VALUES (%s) RETURNING {_NODE_COLS}",
        (Json({"text": type_name, "kind": "type"}),),
    )
    return cur.fetchone()


# ── Reads ───────────────────────────────────────────────────────────────────


def get(uuid: str | UUID) -> NodeWithEdges:
    """Return a node together with every edge incident on it (either direction).

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
    return NodeWithEdges(node=NodeOut(**node), edges=[EdgeOut(**e) for e in edges])


def search(query: str, limit: int = 20) -> SearchResult:
    """Full-text search over node text, ranked by ``ts_rank`` (best first).

    Args:
        query: The free-text query (``plainto_tsquery`` semantics — AND of terms).
        limit: Maximum number of hits to return.

    Returns:
        The query, the number of hits returned, and the ranked hits.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_NODE_COLS}, "
            "ts_rank(to_tsvector('english', data ->> 'text'), "
            "        plainto_tsquery('english', %(q)s)) AS score "
            "FROM nodes "
            "WHERE to_tsvector('english', data ->> 'text') "
            "      @@ plainto_tsquery('english', %(q)s) "
            "ORDER BY score DESC, created_at, uuid "
            "LIMIT %(limit)s",
            {"q": query, "limit": limit},
        )
        rows = cur.fetchall()
    hits = [SearchHit(score=float(row.pop("score")), **row) for row in rows]
    return SearchResult(query=query, total=len(hits), hits=hits)


def expand(seed: str | UUID | Sequence[str | UUID], depth: int = 1) -> Subgraph:
    """Expand a seed set into its connected subgraph, following edges outward.

    Walks directed edges (``from_uuid → to_uuid``) up to ``depth`` hops via a
    recursive CTE, then loads every node touched. Serialised to JSON, this is
    the LLM context payload.

    Args:
        seed: One UUID or a sequence of UUIDs to start from.
        depth: Maximum number of hops (>= 1).

    Returns:
        The seed list, the depth, and the reachable nodes + edges.
    """
    if depth < 1:
        raise ValueError("depth must be at least 1")
    seeds = [str(seed)] if isinstance(seed, str | UUID) else [str(s) for s in seed]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH RECURSIVE sub AS (
                SELECT {_EDGE_COLS}, 1 AS hop
                FROM edges
                WHERE from_uuid = ANY(%(seeds)s::uuid[])
              UNION ALL
                SELECT e.uuid, e.from_uuid, e.to_uuid, e.data,
                       e.created_at, e.updated_at, s.hop + 1
                FROM edges e
                JOIN sub s ON e.from_uuid = s.to_uuid
                WHERE s.hop < %(depth)s
            )
            SELECT DISTINCT {_EDGE_COLS} FROM sub
            ORDER BY created_at, uuid
            """,
            {"seeds": seeds, "depth": depth},
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
        seed=[UUID(s) for s in seeds],
        depth=depth,
        nodes=[NodeOut(**n) for n in node_rows],
        edges=[EdgeOut(**e) for e in edge_rows],
    )
