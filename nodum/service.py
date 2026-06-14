"""The unified data-service layer — kind-aware CRUD over an evolvable schema.

The single source of truth for every operation and all validation. Nodes and
edges live in one generic table each; a row's ``kind`` references the kind
catalog stored in the ``node_kinds`` / ``edge_kinds`` tables (loaded by
:mod:`nodum.db`, validated by :mod:`nodum.metamodel`). The CLI, HTTP API, and web
view are thin adapters over these functions. Each function opens a short-lived
connection and commits.

The kind catalog is itself editable here (``add_node_kind`` … ``delete_edge_kind``),
so the schema evolves at runtime. Instance validation is soft (raised as
``metamodel.ValidationError``, a ``ValueError``); the database enforces only the
cheap universals (FKs, ``from≠to``, ``kind ∈`` the lookup tables). Deleting a kind
that is still referenced raises ``KindInUse`` unless an ``into`` reassignment
target is given.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from psycopg import Cursor
from psycopg.types.json import Json

from nodum import db, metamodel
from nodum.db import connect
from nodum.models import (
    Deleted,
    EdgeOut,
    KindDeleted,
    NodeOut,
    NodeWithEdges,
    SearchHit,
    SearchResult,
    Subgraph,
)

_NODE_COLS = "uuid, kind, content, data, created_at, updated_at"
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


class KindNotFound(Exception):
    """Raised when a kind operation references a kind name that does not exist."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"kind {name!r} not found")


class KindInUse(Exception):
    """Raised when deleting a kind still referenced by rows or edge signatures.

    Carries a human message that suggests reassigning with ``into`` before delete.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


def _kind_of(cur: Cursor, uuid: str | UUID) -> str:
    """Return a node's kind, raising NodeNotFound if it does not exist."""
    cur.execute("SELECT kind FROM nodes WHERE uuid = %s", (str(uuid),))
    row = cur.fetchone()
    if row is None:
        raise NodeNotFound(uuid)
    return row["kind"]


def _require_node_kind(cur: Cursor, name: str) -> metamodel.NodeKind:
    """Resolve a node kind from the DB or raise a clear validation error."""
    node_kind = db.load_node_kind(cur, name)
    if node_kind is None:
        known = sorted(db.load_node_kinds(cur))
        raise metamodel.ValidationError(f"unknown node kind {name!r} (known: {known})")
    return node_kind


def _require_edge_kind(cur: Cursor, name: str) -> metamodel.EdgeKind:
    """Resolve an edge kind from the DB or raise a clear validation error."""
    edge_kind = db.load_edge_kind(cur, name)
    if edge_kind is None:
        known = sorted(db.load_edge_kinds(cur))
        raise metamodel.ValidationError(f"unknown edge kind {name!r} (known: {known})")
    return edge_kind


# ── Create ──────────────────────────────────────────────────────────────────


def add_node(kind: str, content: str, data: dict | None = None) -> NodeOut:
    """Create a typed node and return it.

    Args:
        kind: A node kind from the catalog.
        content: The node's universal plain-text body. Required and non-empty.
        data: Optional kind-specific metadata, validated against the kind.

    Raises:
        metamodel.ValidationError: Unknown kind, empty content, or a bad field.
    """
    payload: dict = dict(data or {})
    with connect() as conn, conn.cursor() as cur:
        node_kind = _require_node_kind(cur, kind)
        metamodel.validate_node(node_kind, content, payload)
        cur.execute(
            f"INSERT INTO nodes (kind, content, data) VALUES (%s, %s, %s) RETURNING {_NODE_COLS}",
            (kind, content, Json(payload)),
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
        edge_kind = _require_edge_kind(cur, kind)
        from_kind = _kind_of(cur, from_uuid)
        to_kind = _kind_of(cur, to_uuid)
        metamodel.validate_edge(edge_kind, from_kind, to_kind, payload)
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
    """Full-text search over node content, ranked best-first, optionally by kind.

    Args:
        query: Free-text query (``plainto_tsquery`` — AND of terms).
        kind: Optional node-kind filter.
        limit: Maximum number of hits.
    """
    clause = "AND kind = %(kind)s" if kind else ""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_NODE_COLS}, "
            "ts_rank(to_tsvector('english', content), "
            "        plainto_tsquery('english', %(q)s)) AS score "
            "FROM nodes "
            "WHERE to_tsvector('english', content) "
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
    """Return the live kind catalog (node + edge kinds, signatures, usage counts).

    Each kind entry carries ``usage`` — how many nodes/edges currently use that
    kind — so a client can show what a deletion would affect before attempting it.
    The metamodel serialiser stays DB-agnostic; usage is annotated here.
    """
    with connect() as conn, conn.cursor() as cur:
        node_kinds = db.load_node_kinds(cur)
        edge_kinds = db.load_edge_kinds(cur)
        node_usage = db.node_kind_counts(cur)
        edge_usage = db.edge_kind_counts(cur)
    result = metamodel.schema_from(node_kinds, edge_kinds)
    for entry in result["node_kinds"]:
        entry["usage"] = node_usage.get(entry["name"], 0)
    for entry in result["edge_kinds"]:
        entry["usage"] = edge_usage.get(entry["name"], 0)
    return result


# ── Update ──────────────────────────────────────────────────────────────────


def update_node(uuid: str | UUID, content: str | None = None, data: dict | None = None) -> NodeOut:
    """Merge new content/payload into a node, re-validate against its kind, return it.

    Raises:
        NodeNotFound: If the node does not exist.
        metamodel.ValidationError: If the result violates the kind's shape.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_NODE_COLS} FROM nodes WHERE uuid = %s", (str(uuid),))
        row = cur.fetchone()
        if row is None:
            raise NodeNotFound(uuid)
        node_kind = _require_node_kind(cur, row["kind"])
        new_content = row["content"] if content is None else content
        payload = dict(row["data"])
        if data:
            payload.update(data)
        metamodel.validate_node(node_kind, new_content, payload)
        cur.execute(
            f"UPDATE nodes SET content = %s, data = %s, updated_at = now() WHERE uuid = %s "
            f"RETURNING {_NODE_COLS}",
            (new_content, Json(payload), str(uuid)),
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
        edge_kind = _require_edge_kind(cur, row["kind"])
        from_kind = _kind_of(cur, row["from_uuid"])
        to_kind = _kind_of(cur, row["to_uuid"])
        metamodel.validate_edge(edge_kind, from_kind, to_kind, payload)
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


# ── Kind CRUD (the evolvable schema) ──────────────────────────────────────────


def _node_kind_entry(node_kind: metamodel.NodeKind) -> dict:
    """A node kind's schema entry (the shape ``schema()`` emits per kind)."""
    return {"name": node_kind.name, **metamodel.node_kind_to_spec(node_kind)}


def _edge_kind_entry(edge_kind: metamodel.EdgeKind) -> dict:
    """An edge kind's schema entry (the shape ``schema()`` emits per kind)."""
    return {"name": edge_kind.name, **metamodel.edge_kind_to_spec(edge_kind)}


def _require_kind_name(name: str) -> None:
    """Reject an empty/blank kind name."""
    if not str(name or "").strip():
        raise metamodel.ValidationError("kind name must be a non-empty string")


def add_node_kind(
    name: str, group: str = "", content_label: str = "text", fields: dict | None = None
) -> dict:
    """Register a new node kind. Returns its schema entry.

    Raises:
        metamodel.ValidationError: Blank name, a malformed spec, or a name clash.
    """
    _require_kind_name(name)
    node_kind = metamodel.node_kind_from_spec(
        name, {"group": group, "content_label": content_label, "fields": fields or {}}
    )
    with connect() as conn, conn.cursor() as cur:
        if db.load_node_kind(cur, name) is not None:
            raise metamodel.ValidationError(f"node kind {name!r} already exists")
        cur.execute(
            "INSERT INTO node_kinds (name, spec) VALUES (%s, %s)",
            (name, Json(metamodel.node_kind_to_spec(node_kind))),
        )
        conn.commit()
    return _node_kind_entry(node_kind)


def update_node_kind(
    name: str,
    group: str | None = None,
    content_label: str | None = None,
    fields: dict | None = None,
) -> dict:
    """Edit a node kind's spec; unspecified attributes are kept. Returns its entry.

    Existing nodes are not re-validated — validation stays a write-time gate, so a
    narrowed spec only affects subsequent writes.

    Raises:
        KindNotFound: If the kind does not exist.
        metamodel.ValidationError: If the resulting spec is malformed.
    """
    with connect() as conn, conn.cursor() as cur:
        current = db.load_node_kind(cur, name)
        if current is None:
            raise KindNotFound(name)
        spec = {
            "group": current.group if group is None else group,
            "content_label": current.content_label if content_label is None else content_label,
            "fields": metamodel._fields_to_json(current.fields) if fields is None else fields,
        }
        node_kind = metamodel.node_kind_from_spec(name, spec)
        cur.execute(
            "UPDATE node_kinds SET spec = %s WHERE name = %s",
            (Json(metamodel.node_kind_to_spec(node_kind)), name),
        )
        conn.commit()
    return _node_kind_entry(node_kind)


def delete_node_kind(name: str, into: str | None = None) -> KindDeleted:
    """Delete a node kind. Blocks when still referenced unless ``into`` is given.

    Without ``into``, deletion is refused if any node has this kind or any edge
    kind names it in a signature. With ``into``, every such node is reassigned to
    ``into`` and every signature reference is rewritten to ``into`` before deleting.

    Raises:
        KindNotFound: If the kind (or ``into``) does not exist.
        KindInUse: If referenced and no ``into`` was given.
        metamodel.ValidationError: If ``into`` equals ``name``.
    """
    with connect() as conn, conn.cursor() as cur:
        if db.load_node_kind(cur, name) is None:
            raise KindNotFound(name)
        cur.execute("SELECT count(*) AS n FROM nodes WHERE kind = %s", (name,))
        node_count = cur.fetchone()["n"]
        referencing = _edge_kinds_referencing(cur, name)
        reassigned = 0
        if into is not None:
            if into == name:
                raise metamodel.ValidationError("cannot reassign a kind into itself")
            if db.load_node_kind(cur, into) is None:
                raise metamodel.ValidationError(f"unknown target node kind {into!r}")
            cur.execute(
                "UPDATE nodes SET kind = %s, updated_at = now() WHERE kind = %s", (into, name)
            )
            reassigned = cur.rowcount
            _replace_node_kind_in_signatures(cur, removed=name, into=into)
        elif node_count or referencing:
            raise KindInUse(_node_kind_in_use_message(name, node_count, referencing))
        cur.execute("DELETE FROM node_kinds WHERE name = %s", (name,))
        conn.commit()
    return KindDeleted(name=name, reassigned=reassigned, deleted=True)


def add_edge_kind(
    name: str,
    from_kinds: Sequence[str],
    to_kinds: Sequence[str],
    symmetric: bool = False,
    fields: dict | None = None,
) -> dict:
    """Register a new edge kind. Returns its schema entry.

    Raises:
        metamodel.ValidationError: Blank name, a malformed spec, an endpoint that
            names an unknown node kind, or a name clash.
    """
    _require_kind_name(name)
    edge_kind = metamodel.edge_kind_from_spec(
        name,
        {
            "from": list(from_kinds),
            "to": list(to_kinds),
            "symmetric": symmetric,
            "fields": fields or {},
        },
    )
    with connect() as conn, conn.cursor() as cur:
        metamodel.validate_edge_endpoints_known(edge_kind, set(db.load_node_kinds(cur)))
        if db.load_edge_kind(cur, name) is not None:
            raise metamodel.ValidationError(f"edge kind {name!r} already exists")
        cur.execute(
            "INSERT INTO edge_kinds (name, spec) VALUES (%s, %s)",
            (name, Json(metamodel.edge_kind_to_spec(edge_kind))),
        )
        conn.commit()
    return _edge_kind_entry(edge_kind)


def update_edge_kind(
    name: str,
    from_kinds: Sequence[str] | None = None,
    to_kinds: Sequence[str] | None = None,
    symmetric: bool | None = None,
    fields: dict | None = None,
) -> dict:
    """Edit an edge kind's spec; unspecified attributes are kept. Returns its entry.

    Raises:
        KindNotFound: If the kind does not exist.
        metamodel.ValidationError: If the resulting spec is malformed or names an
            unknown node kind.
    """
    with connect() as conn, conn.cursor() as cur:
        current = db.load_edge_kind(cur, name)
        if current is None:
            raise KindNotFound(name)
        spec = {
            "from": sorted(current.from_kinds) if from_kinds is None else list(from_kinds),
            "to": sorted(current.to_kinds) if to_kinds is None else list(to_kinds),
            "symmetric": current.symmetric if symmetric is None else symmetric,
            "fields": metamodel._fields_to_json(current.fields) if fields is None else fields,
        }
        edge_kind = metamodel.edge_kind_from_spec(name, spec)
        metamodel.validate_edge_endpoints_known(edge_kind, set(db.load_node_kinds(cur)))
        cur.execute(
            "UPDATE edge_kinds SET spec = %s WHERE name = %s",
            (Json(metamodel.edge_kind_to_spec(edge_kind)), name),
        )
        conn.commit()
    return _edge_kind_entry(edge_kind)


def delete_edge_kind(name: str, into: str | None = None, purge: bool = False) -> KindDeleted:
    """Delete an edge kind. Blocks when edges still use it unless told what to do.

    Two ways resolve an in-use kind (mutually exclusive):

    - ``into`` — **replace**: reassign every edge of this kind to ``into``, then delete.
    - ``purge`` — **remove**: delete every edge of this kind, then delete the kind.

    Raises:
        KindNotFound: If the kind (or ``into``) does not exist.
        KindInUse: If edges use it and neither ``into`` nor ``purge`` was given.
        metamodel.ValidationError: If ``into`` equals ``name``, or both ``into`` and
            ``purge`` are given.
    """
    if into is not None and purge:
        raise metamodel.ValidationError("pass either into or purge, not both")
    with connect() as conn, conn.cursor() as cur:
        if db.load_edge_kind(cur, name) is None:
            raise KindNotFound(name)
        cur.execute("SELECT count(*) AS n FROM edges WHERE kind = %s", (name,))
        edge_count = cur.fetchone()["n"]
        reassigned = 0
        removed = 0
        if into is not None:
            if into == name:
                raise metamodel.ValidationError("cannot reassign a kind into itself")
            if db.load_edge_kind(cur, into) is None:
                raise metamodel.ValidationError(f"unknown target edge kind {into!r}")
            cur.execute(
                "UPDATE edges SET kind = %s, updated_at = now() WHERE kind = %s", (into, name)
            )
            reassigned = cur.rowcount
        elif purge:
            cur.execute("DELETE FROM edges WHERE kind = %s", (name,))
            removed = cur.rowcount
        elif edge_count:
            raise KindInUse(
                f"{name!r} is used by {edge_count} edge(s); reassign (CLI '--into <kind>', "
                f"API '?into=<kind>') or remove its edges (CLI '--purge', API '?purge=true') "
                f"to delete"
            )
        cur.execute("DELETE FROM edge_kinds WHERE name = %s", (name,))
        conn.commit()
    return KindDeleted(name=name, reassigned=reassigned, removed=removed, deleted=True)


def _edge_kinds_referencing(cur: Cursor, node_kind_name: str) -> list[str]:
    """Names of edge kinds whose from/to signature names ``node_kind_name``."""
    referencing = []
    for name, edge_kind in db.load_edge_kinds(cur).items():
        if node_kind_name in edge_kind.from_kinds or node_kind_name in edge_kind.to_kinds:
            referencing.append(name)
    return sorted(referencing)


def _node_kind_in_use_message(name: str, node_count: int, referencing: list[str]) -> str:
    """Build the KindInUse message naming what blocks a node-kind deletion."""
    reasons = []
    if node_count:
        reasons.append(f"{node_count} node(s)")
    if referencing:
        reasons.append(f"edge kind(s) {referencing}")
    return (
        f"{name!r} is referenced by {' and '.join(reasons)}; reassign first "
        f"(CLI '--into <kind>', API '?into=<kind>') to delete"
    )


def _replace_node_kind_in_signatures(cur: Cursor, *, removed: str, into: str) -> None:
    """Rewrite every edge signature that names ``removed`` to name ``into`` instead.

    Keeps the stored specs referentially clean after a node kind is reassigned;
    ``into`` is always inserted in place, so a signature is never left empty.
    """
    cur.execute("SELECT name, spec FROM edge_kinds")
    for row in cur.fetchall():
        spec = dict(row["spec"])
        changed = False
        for endpoint in ("from", "to"):
            members = list(spec.get(endpoint, []))
            if removed in members:
                rewritten = [member for member in members if member != removed]
                if into not in rewritten:
                    rewritten.append(into)
                spec[endpoint] = sorted(rewritten)
                changed = True
        if changed:
            cur.execute(
                "UPDATE edge_kinds SET spec = %s WHERE name = %s", (Json(spec), row["name"])
            )
