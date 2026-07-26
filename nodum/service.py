"""The deterministic, LLM-free service layer — the only writer to the graph.

Every mutation goes through this module: it validates input, enforces the
``proposed → active → archived`` state machine, appends to the event log with
full before/after payloads, snapshots node versions, and materialises
``[[wikilinks]]`` as ``mentions`` edges. No LLM calls live anywhere in here —
an LLM outage may degrade smart features, never data integrity.

Each public function opens its own short-lived connection and commits, so the
adapters (CLI today, HTTP/MCP later) stay stateless and hold no logic.
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from nodum import auth, db
from nodum.migrations import MAIN_SPACE_ID, META_SPACE_ID
from nodum.models import (
    AgentCreatedOut,
    AgentOut,
    BatchTransitionOut,
    DiffOut,
    EdgeOut,
    EdgeTypeOut,
    EventOut,
    GrantOut,
    HumanOut,
    InitResult,
    ItemFailure,
    NodeOut,
    PathOut,
    ProposalOut,
    ProposeEdgesOut,
    SubgraphOut,
    TransitionFailure,
    TypeOut,
    TypesOut,
    UndoResult,
    VersionOut,
)
from nodum.principal import EDIT, READ, SUGGEST, Principal
from nodum.store import GrantNotPermitted, Store

#: Allowed state values shared by nodes and edges.
STATES = ("proposed", "active", "archived")

#: State transitions: action → (required current state, resulting state).
TRANSITIONS = {
    "accept": ("proposed", "active"),
    "reject": ("proposed", "archived"),
    "archive": ("active", "archived"),
}

#: The transitions that *review* a proposal. Reviewing turns proposed
#: structure into live structure (and archives what it replaces), so it needs
#: a human — or an ``edit`` grant on the item's space (Q13 note 03 Q1).
REVIEW_ACTIONS = ("accept", "reject")

#: The node fields a version snapshots, and the only fields a proposed update
#: may name.
VERSION_FIELDS = ("title", "content", "props")

#: Node states :func:`suggest_links` draws link targets from. ``proposed``
#: stays in, as it does for every other node read; ``archived`` is out,
#: because a retired node is not something to link to.
SUGGEST_STATES = ("active", "proposed")

#: Edge states :func:`subgraph` follows when the caller names none — the live
#: graph, matching every other traversal (design §8.1).
DEFAULT_EDGE_STATES = ("active",)

#: Ceiling on :func:`subgraph`'s node cap. A caller's ``limit`` is clamped to
#: it rather than refused, so a query string cannot turn the bounded read into
#: an unbounded one; ``truncated`` reports the clamp like any other cap.
MAX_SUBGRAPH_LIMIT = 2000

#: Edges :func:`subgraph` may return per node it is allowed to admit. The node
#: cap bounds nodes only — one pair of nodes can carry any number of edges — so
#: the edge list needs its own bound, derived from ``limit`` so that one
#: argument still describes the size of the whole result.
SUBGRAPH_EDGE_FACTOR = 8

#: Sentinel distinguishing "argument not given" from an explicit ``None``.
_UNSET: Any = object()

#: A wikilink target: ``[[node-id]]`` or ``[[Exact Title]]``.
WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")


class RecordNotFound(LookupError):
    """Raised when a graph record id does not resolve to any kind of row.

    The base of :class:`NodeNotFound`, :class:`EdgeNotFound`, and
    :class:`VersionNotFound`, so a caller can catch the specific kind it asked
    for *and* the id-shaped operations (``transition``) that accept all three
    and therefore cannot say which kind was meant.
    """


class NodeNotFound(RecordNotFound):
    """Raised when a node id does not resolve."""


class EdgeNotFound(RecordNotFound):
    """Raised when an edge id does not resolve."""


class TypeNotFound(LookupError):
    """Raised when a node or edge type id/name does not resolve."""


class EventNotFound(LookupError):
    """Raised when an event seq does not resolve."""


class VersionNotFound(RecordNotFound):
    """Raised when a version id does not resolve."""


class InvalidTransition(ValueError):
    """Raised when a state transition is not allowed from the current state."""


class UndoNotPossible(ValueError):
    """Raised when an event cannot be reversed against the current graph."""


# ── Connection and row helpers ────────────────────────────────────────────────


def _connect(path: str | Path | None) -> sqlite3.Connection:
    """Open a connection and apply any pending migrations (idempotent)."""
    conn = db.connect(path)
    db.init_db(conn)
    return conn


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


def _node_out(row: sqlite3.Row | dict[str, Any]) -> NodeOut:
    """Build the public node model from a nodes row (props decoded)."""
    data = dict(row)
    return NodeOut(
        id=data["id"],
        space_id=data["space_id"],
        type=data["type_id"],
        parent_id=data["parent_id"],
        position=data["position"],
        title=data["title"],
        content=data["content"],
        props=json.loads(data["props"]),
        state=data["state"],
        created_by=data["created_by"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


def _edge_out(row: sqlite3.Row | dict[str, Any]) -> EdgeOut:
    """Build the public edge model from an edges row (props decoded)."""
    data = dict(row)
    return EdgeOut(
        id=data["id"],
        src_id=data["src_id"],
        dst_id=data["dst_id"],
        type=data["type_id"],
        props=json.loads(data["props"]),
        confidence=data["confidence"],
        created_by=data["created_by"],
        state=data["state"],
        valid_from=data["valid_from"],
        valid_to=data["valid_to"],
        created_at=data["created_at"],
    )


def _get_node_row(conn: sqlite3.Connection, node_id: str) -> sqlite3.Row:
    """Fetch a node row or raise :class:`NodeNotFound`."""
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        raise NodeNotFound(f"node not found: {node_id}")
    return row


def _get_edge_row(conn: sqlite3.Connection, edge_id: str) -> sqlite3.Row:
    """Fetch an edge row or raise :class:`EdgeNotFound`."""
    row = conn.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone()
    if row is None:
        raise EdgeNotFound(f"edge not found: {edge_id}")
    return row


def _get_version_row(conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
    """Fetch a version row or raise :class:`VersionNotFound`."""
    row = conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
    if row is None:
        raise VersionNotFound(f"version not found: {version_id}")
    return row


def _version_out(row: sqlite3.Row | dict[str, Any]) -> VersionOut:
    """Build the public version model from a versions row (props decoded)."""
    data = dict(row)
    raw_fields = data.get("proposed_fields")
    return VersionOut(
        id=data["id"],
        node_id=data["node_id"],
        title=data["title"],
        content=data["content"],
        props=json.loads(data["props"]),
        actor=data["actor"],
        event_seq=data["event_seq"],
        state=data["state"],
        proposed_fields=None if raw_fields is None else json.loads(raw_fields),
        created_at=data["created_at"],
    )


def _proposed_fields(version: dict[str, Any]) -> list[str]:
    """Return the node fields a proposed version actually names.

    A proposal stores a whole title/content/props snapshot — fields the agent
    did not name are copied from the node at proposal time as reviewer
    context — so this list is what an accept may write back. ``NULL`` means
    the row predates the column (migration ``0008``); such a proposal is read
    as naming all three fields, which is exactly what it meant when it was
    staged.
    """
    raw = version.get("proposed_fields")
    if raw is None:
        return list(VERSION_FIELDS)
    return [name for name in json.loads(raw) if name in VERSION_FIELDS]


def _resolve_node_type(
    conn: sqlite3.Connection, type_ref: str, principal: Principal | None = None
) -> str:
    """Resolve a node-type id or name to its id, or raise :class:`TypeNotFound`.

    Types are nodes (Q13, migration ``0009``): a node type is an active node
    whose own type is the ``type`` metaclass root, distinguished from edge
    types by ``type_kind`` in props. With ``principal``, a type in a space
    the principal cannot read does not resolve — the catalog is not a leak
    channel either.
    """
    row = conn.execute(
        "SELECT id, space_id FROM nodes WHERE (id = ? OR title = ?) AND type_id = 'type'"
        " AND json_extract(props, '$.type_kind') = 'node' AND state = 'active'",
        (type_ref, type_ref),
    ).fetchone()
    if row is None or (principal is not None and principal.level_on(row["space_id"]) < READ):
        raise TypeNotFound(f"unknown node type: {type_ref}")
    return row["id"]


def _resolve_edge_type(
    conn: sqlite3.Connection, type_ref: str, principal: Principal | None = None
) -> tuple[str, str | None]:
    """Resolve an edge-type id or name to ``(id, space_id)``, or raise.

    The space comes back because a cross-space edge's type node must live in
    meta (the one structural rule, enforced in :func:`_create_edge_in_conn`).
    The same read check as :func:`_resolve_node_type` applies.
    """
    row = conn.execute(
        "SELECT id, space_id FROM nodes WHERE (id = ? OR title = ?) AND type_id = 'type'"
        " AND json_extract(props, '$.type_kind') = 'edge' AND state = 'active'",
        (type_ref, type_ref),
    ).fetchone()
    if row is None or (principal is not None and principal.level_on(row["space_id"]) < READ):
        raise TypeNotFound(f"unknown edge type: {type_ref}")
    return row["id"], row["space_id"]


def _resolve_space(conn: sqlite3.Connection, space_ref: str) -> str:
    """Resolve a space id or name to its id, or raise :class:`TypeNotFound`.

    Spaces are nodes of builtin type ``space`` (Q13 note 03 Q7).
    """
    row = conn.execute(
        "SELECT id FROM nodes WHERE (id = ? OR title = ?) AND type_id = 'space'"
        " AND state = 'active'",
        (space_ref, space_ref),
    ).fetchone()
    if row is None:
        raise TypeNotFound(f"unknown space: {space_ref}")
    return row["id"]


def _create_op(state: str) -> str:
    """Name a create-op after the state it lands in (``create`` vs ``propose``)."""
    return "create" if state == "active" else "propose"


# ── Event log and versions ────────────────────────────────────────────────────


def _emit(
    conn: sqlite3.Connection,
    actor: str,
    op: str,
    payload: dict[str, Any],
    cycle_id: str | None = None,
) -> int:
    """Append one event to the log and return its seq.

    Args:
        conn: The open connection (the caller commits).
        actor: Who performed the mutation (``human``, ``agent:<name>``, …).
        op: Dotted op name, e.g. ``node.create``, ``edge.archive``, ``undo``.
        payload: JSON-serialisable full before/after (or undo detail) payload.
        cycle_id: Consolidation-cycle grouping (always ``None`` in Phase 1).

    Returns:
        The new event's ``seq``.
    """
    cur = conn.execute(
        "INSERT INTO events (actor, op, payload, cycle_id) VALUES (?, ?, ?, ?)",
        (actor, op, json.dumps(payload, ensure_ascii=False), cycle_id),
    )
    return int(cur.lastrowid)


def _write_version(
    conn: sqlite3.Connection, node_row: sqlite3.Row | dict[str, Any], actor: str, event_seq: int
) -> None:
    """Snapshot a node's title/content/props into ``versions`` after a mutation."""
    data = dict(node_row)
    conn.execute(
        """
        INSERT INTO versions (node_id, title, content, props, actor, event_seq)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (data["id"], data["title"], data["content"], data["props"], actor, event_seq),
    )


# ── Wikilink materialisation ──────────────────────────────────────────────────


def _resolve_wikilink(conn: sqlite3.Connection, target: str, store: Store) -> str | None:
    """Resolve a ``[[target]]` to a node id, or ``None`` when unresolvable.

    Resolution order: exact node id first, then exact title match among
    non-archived nodes (oldest first when several share a title). Targets
    outside the principal's read set do not resolve — a wikilink must not
    probe the existence of a node the writer cannot see.
    """
    scope, params = store.node_scope()
    row = conn.execute(f"SELECT id FROM nodes WHERE id = ?{scope}", (target, *params)).fetchone()
    if row is not None:
        return row["id"]
    row = conn.execute(
        f"""
        SELECT id FROM nodes
        WHERE title = ? AND state != 'archived'{scope}
        ORDER BY created_at, rowid
        LIMIT 1
        """,
        (target, *params),
    ).fetchone()
    return row["id"] if row is not None else None


def _materialize_mentions(
    conn: sqlite3.Connection,
    node_row: sqlite3.Row | dict[str, Any],
    actor: str,
    store: Store,
    cycle_id: str | None = None,
) -> None:
    """Sync a node's ``[[wikilinks]]`` with its pending/active ``mentions`` edges.

    Creates a ``mentions`` edge (``created_by`` = writer) for every newly
    linked target, archives edges whose target text disappeared, and silently
    skips unresolvable targets (no dangling edges). Self-links are ignored.

    A materialised edge lands in the state the writer's grants allow on both
    endpoint spaces (``active`` on edit, ``proposed`` on suggest); a target
    the writer may not link to — unreadable, or under-granted — is skipped
    rather than failing the write (:func:`Store.edge_landing_state`). A
    pending edge goes live when a reviewer accepts the proposing node
    (:func:`_activate_pending_mentions`) or the edge itself.

    Idempotent: re-running on unchanged content changes nothing, whichever
    state the existing edges are in — pending edges count as already
    materialised, so a later human write never duplicates them.
    """
    node = dict(node_row)
    targets = set(WIKILINK_RE.findall(node["content"] or ""))
    resolved: set[str] = set()
    landing: dict[str, str] = {}
    for target in targets:
        dst = _resolve_wikilink(conn, target, store)
        if dst is None or dst == node["id"]:
            continue
        dst_space = conn.execute("SELECT space_id FROM nodes WHERE id = ?", (dst,)).fetchone()[
            "space_id"
        ]
        try:
            landing[dst] = store.edge_landing_state(node["space_id"], dst_space, META_SPACE_ID)
        except GrantNotPermitted:
            continue  # no grant to link there — the wikilink is skipped, not fatal
        resolved.add(dst)
    current = conn.execute(
        """
        SELECT * FROM edges
        WHERE src_id = ? AND type_id = 'mentions' AND state IN ('active', 'proposed')
        ORDER BY created_at, rowid
        """,
        (node["id"],),
    ).fetchall()
    current_by_dst = {edge["dst_id"]: edge for edge in current}

    for dst_id in sorted(resolved - set(current_by_dst)):
        _insert_edge(
            conn,
            src_id=node["id"],
            dst_id=dst_id,
            type_id="mentions",
            props={},
            confidence=None,
            actor=actor,
            state=landing[dst_id],
            cycle_id=cycle_id,
        )
    for dst_id, edge in current_by_dst.items():
        if dst_id not in resolved:
            # A pending edge leaves `proposed`, so its op is `reject`, not
            # `archive` — the state machine allows only one of the two.
            action = "archive" if edge["state"] == "active" else "reject"
            _set_edge_state(conn, dict(edge), "archived", action, actor, cycle_id=cycle_id)


def _activate_pending_mentions(conn: sqlite3.Connection, node: dict[str, Any], actor: str) -> None:
    """Bring an accepted node's own pending ``mentions`` edges to ``active``.

    An agent's ``[[wikilinks]]`` materialise as ``proposed`` edges, so
    accepting the node is what actually attaches it to the graph. Only the
    edges the node's own author materialised are swept (``created_by`` match);
    an unrelated agent's proposed ``mentions`` edge out of the same node stays
    in the queue on its own merits. Each transition is its own event,
    attributed to the accepting reviewer.
    """
    rows = conn.execute(
        """
        SELECT * FROM edges
        WHERE src_id = ? AND type_id = 'mentions' AND state = 'proposed' AND created_by = ?
        ORDER BY created_at, rowid
        """,
        (node["id"], node["created_by"]),
    ).fetchall()
    for row in rows:
        _set_edge_state(conn, _row_dict(row), "active", "accept", actor)


# ── Internal edge writers (shared by public ops and wikilink materialisation) ─


def _insert_edge(
    conn: sqlite3.Connection,
    *,
    src_id: str,
    dst_id: str,
    type_id: str,
    props: dict[str, Any],
    confidence: float | None,
    actor: str,
    state: str,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    """Insert one edge row and emit its create/propose event; returns the row."""
    edge_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO edges (id, src_id, dst_id, type_id, props, confidence, created_by, state)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            edge_id,
            src_id,
            dst_id,
            type_id,
            json.dumps(props, ensure_ascii=False),
            confidence,
            actor,
            state,
        ),
    )
    row = _row_dict(_get_edge_row(conn, edge_id))
    payload: dict[str, Any] = {"before": None, "after": row}
    _emit(
        conn,
        actor,
        f"edge.{_create_op(state)}",
        payload,
        cycle_id=cycle_id,
    )
    return row


def _set_edge_state(
    conn: sqlite3.Connection,
    before: dict[str, Any],
    new_state: str,
    action: str,
    actor: str,
    cycle_id: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Transition an edge's state, emitting the event; returns the after row.

    ``reason`` is recorded in the event payload on rejects (design §8.1).
    """
    conn.execute("UPDATE edges SET state = ? WHERE id = ?", (new_state, before["id"]))
    after = _row_dict(_get_edge_row(conn, before["id"]))
    payload: dict[str, Any] = {"before": before, "after": after}
    if reason is not None:
        payload["reason"] = reason
    _emit(conn, actor, f"edge.{action}", payload, cycle_id=cycle_id)
    return after


# ── Public API ────────────────────────────────────────────────────────────────


def init(path: str | Path | None = None) -> InitResult:
    """Create the database file (if needed) and apply pending migrations.

    Args:
        path: Explicit database path; defaults to ``NODUM_DB`` or the standard
            location.

    Returns:
        The DB path plus which migrations were applied now vs. already present.
    """
    conn = db.connect(path)
    try:
        already = db.applied_migrations(conn) if _has_migrations_table(conn) else []
        applied = db.init_db(conn)
        return InitResult(
            db_path=str(db.db_path() if path is None else path),
            applied=applied,
            already_applied=already,
        )
    finally:
        conn.close()


def _has_migrations_table(conn: sqlite3.Connection) -> bool:
    """Return True when the schema_migrations table exists."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    return row is not None


def create_node(
    *,
    type: str,
    title: str | None = None,
    content: str = "",
    parent_id: str | None = None,
    props: dict[str, Any] | None = None,
    space: str | None = None,
    principal: Principal,
    path: str | Path | None = None,
) -> NodeOut:
    """Create a node, emit ``node.create``/``node.propose``, snapshot a version.

    The landing state comes from the principal's grant on the target space:
    ``edit`` writes ``active``, ``suggest`` writes ``proposed`` (design §6 as
    amended, Q13). When ``parent_id`` is given the node is appended after its
    siblings (``max(position) + 1.0``). Wikilinks in ``content`` are
    materialised as ``mentions`` edges.

    Args:
        type: Node-type id or name (must resolve, and be readable).
        title: Optional display title (wikilink targets resolve against it).
        content: Canonical Markdown body.
        parent_id: Optional parent node id (must exist, be readable, and live
            in the same space — the document tree does not cross spaces).
        props: Free-form JSON-object metadata.
        space: Target space id or name (default: the ``main`` space).
        principal: Who is writing (default: the trusted-local owner).
        path: Explicit database path.

    Returns:
        The created node.

    Raises:
        GrantNotPermitted: If the principal has no write grant on the space.
    """
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        actor = principal.actor_string
        type_id = _resolve_node_type(conn, type, principal)
        target_space = _resolve_space(conn, space) if space is not None else MAIN_SPACE_ID
        if parent_id is not None:
            parent = _get_node_row(conn, parent_id)
            if not store.node_visible(parent):
                raise NodeNotFound(f"node not found: {parent_id}")
            if parent["space_id"] != target_space:
                raise ValueError(
                    f"a node's parent must live in the same space: parent is in "
                    f"{parent['space_id']!r}, target is {target_space!r}"
                )
        node_id = uuid.uuid4().hex
        state = store.landing_state(target_space)
        if parent_id is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1.0 AS pos FROM nodes WHERE parent_id IS NULL"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1.0 AS pos FROM nodes WHERE parent_id = ?",
                (parent_id,),
            ).fetchone()
        position = float(row["pos"])
        conn.execute(
            """
            INSERT INTO nodes (id, space_id, type_id, parent_id, position, title, content,
                               props, state, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_id,
                target_space,
                type_id,
                parent_id,
                position,
                title,
                content,
                json.dumps(props or {}, ensure_ascii=False),
                state,
                actor,
            ),
        )
        node = _row_dict(_get_node_row(conn, node_id))
        seq = _emit(conn, actor, f"node.{_create_op(state)}", {"before": None, "after": node})
        _write_version(conn, node, actor, seq)
        _materialize_mentions(conn, node, actor, store)
        conn.commit()
        return _node_out(node)
    finally:
        conn.close()


def get_node(node_id: str, *, principal: Principal, path: str | Path | None = None) -> NodeOut:
    """Fetch one node by id.

    Raises:
        NodeNotFound: If the id does not resolve — or the node sits in a
            space the principal cannot read (an unreadable space does not
            exist, Q13 note 03).
    """
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        row = _get_node_row(conn, node_id)
        if not store.node_visible(row):
            raise NodeNotFound(f"node not found: {node_id}")
        return _node_out(row)
    finally:
        conn.close()


def update_node(
    node_id: str,
    *,
    title: Any = _UNSET,
    content: Any = _UNSET,
    props: Any = _UNSET,
    principal: Principal,
    path: str | Path | None = None,
) -> NodeOut | VersionOut:
    """Update a node's title/content/props — or propose the update.

    **Only the given fields change**, on both paths. For the ``human`` actor
    the update applies in place: emit ``node.update``, snapshot an ``applied``
    version, and re-run wikilink materialisation when the content changed. For
    any other actor (design §8.1) the edit is staged as a ``proposed``
    version — the node itself is untouched — and waits in the review queue;
    accepting applies exactly the fields this call named (emitting
    ``node.update`` then), rejecting archives the version. The version row
    still carries a full snapshot, but the fields the agent did not name are
    reviewer context only: they are recorded in ``proposed_fields`` as *not*
    proposed, so an accept can never replay them over edits made in the
    meantime.

    Returns:
        The updated node (human path) or the proposed version (agent path).

    Raises:
        NodeNotFound: If the id does not resolve.
    """
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        actor = principal.actor_string
        before_row = _get_node_row(conn, node_id)
        if not store.node_visible(before_row):
            raise NodeNotFound(f"node not found: {node_id}")
        before = _row_dict(before_row)
        if not principal.is_human and principal.level_on(before["space_id"]) < SUGGEST:
            raise GrantNotPermitted(f"{actor} has no write grant on space {before['space_id']!r}")
        new_title = before["title"] if title is _UNSET else title
        new_content = before["content"] if content is _UNSET else content
        new_props = before["props"] if props is _UNSET else json.dumps(props, ensure_ascii=False)
        if principal.level_on(before["space_id"]) < EDIT:
            # Suggest path: stage the edit as a proposed version (design §8.1).
            # The event precedes the insert so the version can point at it.
            given = dict(zip(VERSION_FIELDS, (title, content, props), strict=True))
            fields = [name for name, value in given.items() if value is not _UNSET]
            seq = _emit(
                conn,
                actor,
                "version.propose",
                {
                    "node_id": node_id,
                    "before": before,
                    "fields": fields,
                    "proposed": {
                        "title": new_title,
                        "content": new_content,
                        "props": json.loads(new_props),
                    },
                },
            )
            cur = conn.execute(
                """
                INSERT INTO versions (node_id, title, content, props, actor, event_seq,
                                      state, proposed_fields)
                VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)
                """,
                (
                    node_id,
                    new_title,
                    new_content,
                    new_props,
                    actor,
                    seq,
                    json.dumps(fields),
                ),
            )
            version = _row_dict(_get_version_row(conn, int(cur.lastrowid)))
            conn.commit()
            return _version_out(version)
        conn.execute(
            """
            UPDATE nodes
            SET title = ?, content = ?, props = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (new_title, new_content, new_props, node_id),
        )
        after = _row_dict(_get_node_row(conn, node_id))
        seq = _emit(conn, actor, "node.update", {"before": before, "after": after})
        _write_version(conn, after, actor, seq)
        if content is not _UNSET:
            _materialize_mentions(conn, after, actor, store)
        conn.commit()
        return _node_out(after)
    finally:
        conn.close()


def list_nodes(
    *,
    type: str | None = None,
    state: str | None = None,
    parent_id: str | None = None,
    include_meta: bool = False,
    principal: Principal,
    limit: int = 500,
    path: str | Path | None = None,
) -> list[NodeOut]:
    """List nodes, optionally filtered by type name/id, state, or parent.

    Ordered by ``created_at``; ``limit`` caps the result (default 500).
    Meta-space nodes (the type vocabulary, spaces) are excluded unless
    ``include_meta`` — content listings are not the type catalog. An agent
    principal is additionally confined to its read set (which may include
    meta, e.g. for the type vocabulary).
    """
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        clauses: list[str] = []
        params: list[Any] = []
        scope, scope_params = store.node_scope()
        if scope:
            clauses.append(scope.removeprefix(" AND "))
            params.extend(scope_params)
        elif not include_meta:
            clauses.append("space_id != ?")
            params.append(META_SPACE_ID)
        if type is not None:
            clauses.append("type_id = ?")
            params.append(_resolve_node_type(conn, type))
        if state is not None:
            if state not in STATES:
                raise ValueError(f"state must be one of {STATES}, got {state!r}")
            clauses.append("state = ?")
            params.append(state)
        if parent_id is not None:
            clauses.append("parent_id = ?")
            params.append(parent_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM nodes {where} ORDER BY created_at, rowid LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_node_out(row) for row in rows]
    finally:
        conn.close()


def list_children(
    node_id: str, *, principal: Principal, path: str | Path | None = None
) -> list[NodeOut]:
    """List a node's children in ``position`` order.

    Raises:
        NodeNotFound: If the id does not resolve, or is not readable.
    """
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        parent = _get_node_row(conn, node_id)
        if not store.node_visible(parent):
            raise NodeNotFound(f"node not found: {node_id}")
        scope, params = store.node_scope()
        rows = conn.execute(
            f"SELECT * FROM nodes WHERE parent_id = ?{scope} ORDER BY position",
            (node_id, *params),
        ).fetchall()
        return [_node_out(row) for row in rows]
    finally:
        conn.close()


def _normalized(text: str) -> str:
    """Return ``text`` in NFC, so equivalent spellings compare equal.

    The same characters reach the database in more than one encoding — macOS
    paths and some input methods produce NFD ("É" as E + U+0301), most other
    sources NFC — and Python compares them by code point, so an unnormalised
    ``startswith`` misses a title it should match.
    """
    return unicodedata.normalize("NFC", text)


def _match_key(text: str) -> str:
    """Return the case- and normalisation-insensitive form of ``text``.

    Case folding can itself denormalise (and expands ``ß`` to ``ss``, which
    SQL ``lower()`` cannot do), so normalisation is applied on both sides of
    it — matching UAX #15's caseless-match recipe, with NFC as the form.
    """
    return _normalized(_normalized(text).casefold())


def suggest_links(
    prefix: str,
    *,
    limit: int = 20,
    principal: Principal,
    path: str | Path | None = None,
) -> list[NodeOut]:
    """Suggest ``[[wikilink]]`` targets whose title starts with ``prefix``.

    Backs the editor's ``[[`` autocomplete. It reads the ``nodes`` table
    directly rather than an index, so it answers on a database whose
    projectors have never run — an empty suggestion list always means "no such
    title", never "the index is cold".

    Matching folds case in Python (:meth:`str.casefold`) rather than in SQL:
    SQLite's ``LIKE`` and ``lower()`` fold ASCII only, and titles here are
    multilingual. Both sides are Unicode-normalised as well
    (:func:`_match_key`), so a title stored NFD — as macOS paths and some
    input methods write it — is found by an NFC prefix and vice versa.
    Ranking puts titles that match the typed case first (typing ``Gra``
    surfaces "Graph Theory" ahead of "grammar"), then sorts by title and id,
    so the same prefix always returns the same list.

    Archived nodes are excluded — a retired node is not a link target — while
    ``proposed`` ones stay in, matching how the other node reads treat state
    (:data:`SUGGEST_STATES`). An empty prefix matches every titled node.

    Args:
        prefix: The text typed so far, matched against the start of the title.
        limit: Maximum suggestions returned.
        path: Explicit database path.

    Returns:
        Matching nodes, best-ranked first, capped at ``limit``.

    Raises:
        ValueError: If ``limit`` is below 1.
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        placeholders = ",".join("?" * len(SUGGEST_STATES))
        scope, scope_params = store.node_scope()
        if scope:
            candidates = conn.execute(
                f"SELECT id, title FROM nodes WHERE title IS NOT NULL"
                f" AND state IN ({placeholders}){scope}",
                (*SUGGEST_STATES, *scope_params),
            ).fetchall()
        else:
            candidates = conn.execute(
                f"SELECT id, title FROM nodes WHERE title IS NOT NULL AND state IN ({placeholders})"
                " AND space_id != ?",
                (*SUGGEST_STATES, META_SPACE_ID),
            ).fetchall()
        folded = _match_key(prefix)
        typed = _normalized(prefix)
        matches = [row for row in candidates if _match_key(row["title"]).startswith(folded)]
        matches.sort(
            key=lambda row: (
                not _normalized(row["title"]).startswith(typed),
                _match_key(row["title"]),
                _normalized(row["title"]),
                row["id"],
            )
        )
        # Only the survivors are read in full — titles are cheap, content is not.
        return [_node_out(_get_node_row(conn, row["id"])) for row in matches[:limit]]
    finally:
        conn.close()


def _create_edge_in_conn(
    conn: sqlite3.Connection,
    src_id: str,
    dst_id: str,
    type: str,
    *,
    props: dict[str, Any] | None,
    confidence: float | None,
    actor: str,
    store: Store,
) -> dict[str, Any]:
    """Validate and write one edge inside an open connection (no commit).

    Shared by :func:`create_edge` and :func:`propose_edges`. An endpoint the
    principal cannot read is *not found* (an unreadable space does not
    exist); the landing state needs the matching grant on **both** endpoint
    spaces, and a cross-space edge's type node must live in meta
    (:func:`Store.edge_landing_state` — Q13 note 03).
    """
    src = _get_node_row(conn, src_id)
    dst = _get_node_row(conn, dst_id)
    if not store.node_visible(src):
        raise NodeNotFound(f"node not found: {src_id}")
    if not store.node_visible(dst):
        raise NodeNotFound(f"node not found: {dst_id}")
    type_id, type_space = _resolve_edge_type(conn, type, store.principal)
    if confidence is not None and not 0 <= confidence <= 1:
        raise ValueError(f"confidence must be between 0 and 1, got {confidence}")
    state = store.edge_landing_state(src["space_id"], dst["space_id"], type_space)
    return _insert_edge(
        conn,
        src_id=src_id,
        dst_id=dst_id,
        type_id=type_id,
        props=props or {},
        confidence=confidence,
        actor=actor,
        state=state,
    )


def create_edge(
    src_id: str,
    dst_id: str,
    type: str,
    *,
    props: dict[str, Any] | None = None,
    confidence: float | None = None,
    principal: Principal,
    path: str | Path | None = None,
) -> EdgeOut:
    """Create a typed, directed edge and emit ``edge.create``/``edge.propose``.

    Both endpoints must exist and be readable. The landing state needs the
    matching grant on both endpoint spaces (``edit`` → ``active``,
    ``suggest`` → ``proposed``); a cross-space edge's type node must live in
    meta.

    Raises:
        NodeNotFound: If either endpoint does not resolve — or is not
            readable by the principal.
        TypeNotFound: If the edge type does not resolve.
        GrantNotPermitted: If the grants on the endpoint spaces do not cover
            the write, or a cross-space edge uses a non-meta type.
        ValueError: If ``confidence`` is outside ``[0, 1]``.
    """
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        row = _create_edge_in_conn(
            conn,
            src_id,
            dst_id,
            type,
            props=props,
            confidence=confidence,
            actor=principal.actor_string,
            store=store,
        )
        conn.commit()
        return _edge_out(row)
    finally:
        conn.close()


def propose_edges(
    suggestions: list[dict[str, Any]],
    *,
    principal: Principal,
    path: str | Path | None = None,
) -> ProposeEdgesOut:
    """Write a batch of edge suggestions, one event per edge (design §8.1).

    Each suggestion names ``src``, ``dst``, and ``edge_type``, plus optional
    ``props`` and ``confidence`` — the same inputs as :func:`create_edge`,
    A malformed suggestion (missing key,
    unknown endpoint/type, bad confidence) lands in ``failed`` with its
    input index; the rest still write. One commit for the whole batch.

    Raises:
        ValueError: If ``suggestions`` is not a list of objects.
    """
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        created: list[EdgeOut] = []
        failed: list[ItemFailure] = []
        for index, suggestion in enumerate(suggestions):
            if not isinstance(suggestion, dict):
                failed.append(ItemFailure(index=index, error="suggestion must be an object"))
                continue
            try:
                row = _create_edge_in_conn(
                    conn,
                    str(suggestion["src"]),
                    str(suggestion["dst"]),
                    str(suggestion["edge_type"]),
                    props=suggestion.get("props"),
                    confidence=suggestion.get("confidence"),
                    actor=principal.actor_string,
                    store=store,
                )
                created.append(_edge_out(row))
            except KeyError as exc:
                failed.append(ItemFailure(index=index, error=f"missing key: {exc.args[0]}"))
            except (NodeNotFound, TypeNotFound, ValueError, GrantNotPermitted) as exc:
                failed.append(ItemFailure(index=index, error=str(exc)))
        conn.commit()
        return ProposeEdgesOut(created=created, failed=failed)
    finally:
        conn.close()


def list_edges(
    *,
    node_id: str | None = None,
    type: str | None = None,
    state: str | None = None,
    principal: Principal,
    limit: int = 500,
    path: str | Path | None = None,
) -> list[EdgeOut]:
    """List edges, optionally filtered by incident node, type, or state.

    ``node_id`` matches edges in either direction. An agent principal sees
    only edges whose endpoints are both readable.
    """
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        clauses: list[str] = []
        params: list[Any] = []
        scope, scope_params = store.edge_scope()
        if scope:
            clauses.append(scope.removeprefix(" AND "))
            params.extend(scope_params)
        if node_id is not None:
            clauses.append("(src_id = ? OR dst_id = ?)")
            params.extend([node_id, node_id])
        if type is not None:
            clauses.append("type_id = ?")
            params.append(_resolve_edge_type(conn, type)[0])
        if state is not None:
            if state not in STATES:
                raise ValueError(f"state must be one of {STATES}, got {state!r}")
            clauses.append("state = ?")
            params.append(state)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM edges {where} ORDER BY created_at, rowid LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_edge_out(row) for row in rows]
    finally:
        conn.close()


def _transition_version(
    conn: sqlite3.Connection,
    before: dict[str, Any],
    action: str,
    actor: str,
    store: Store,
    reason: str | None = None,
) -> dict[str, Any]:
    """Accept or reject a proposed version inside an open connection.

    Accepting *applies* the fields the proposal named — and only those
    (:func:`_proposed_fields`) — to the node's **current** row, so an edit
    landed between proposal and review survives instead of being reverted by a
    stale snapshot. It emits ``node.update`` (undoable, and picked up by the
    FTS projector) with the applied version id in the payload, flips the
    version to ``applied``, and re-runs wikilink materialisation **as the
    accepting actor** when the content was among the applied fields: the
    reviewer owns every state change the accept causes (new ``mentions`` edges
    go live, dropped ones are archived), not the agent that proposed the text.
    Rejecting flips it to ``archived`` and emits ``version.reject``.
    """
    version_id = before["id"]
    if action == "accept":
        node_before = _row_dict(_get_node_row(conn, before["node_id"]))
        fields = _proposed_fields(before)
        assignments = [f"{name} = ?" for name in fields] + ["updated_at = datetime('now')"]
        conn.execute(
            f"UPDATE nodes SET {', '.join(assignments)} WHERE id = ?",
            (*[before[name] for name in fields], before["node_id"]),
        )
        node_after = _row_dict(_get_node_row(conn, before["node_id"]))
        conn.execute("UPDATE versions SET state = 'applied' WHERE id = ?", (version_id,))
        _emit(
            conn,
            actor,
            "node.update",
            {
                "before": node_before,
                "after": node_after,
                "applied_version_id": version_id,
                "applied_fields": fields,
                "proposed_event_seq": before["event_seq"],
            },
        )
        if "content" in fields:
            _materialize_mentions(conn, node_after, actor, store)
    else:  # reject
        conn.execute("UPDATE versions SET state = 'archived' WHERE id = ?", (version_id,))
        archived = _row_dict(_get_version_row(conn, version_id))
        payload: dict[str, Any] = {"before": before, "after": archived}
        if reason is not None:
            payload["reason"] = reason
        _emit(conn, actor, "version.reject", payload)
    return _row_dict(_get_version_row(conn, version_id))


def _item_spaces(conn: sqlite3.Connection, kind: str, row: dict[str, Any]) -> set[str | None]:
    """The spaces a transition touches: the node's, both endpoints' for an
    edge, the node's for a version (typed through it)."""
    if kind == "node":
        return {row["space_id"]}
    if kind == "version":
        node = _get_node_row(conn, row["node_id"])
        return {node["space_id"]}
    src = _get_node_row(conn, row["src_id"])
    dst = _get_node_row(conn, row["dst_id"])
    return {src["space_id"], dst["space_id"]}


def _transition_row(
    conn: sqlite3.Connection,
    record_id: str,
    action: str,
    actor: str,
    store: Store,
    reason: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Apply one state transition inside an open connection (no commit).

    Returns:
        A ``(kind, after_row)`` pair where kind is ``"node"``, ``"edge"``, or
        ``"version"``.

    Raises:
        GrantNotPermitted: If the principal is not a human and holds no
            ``edit`` grant on the item's space (both endpoint spaces for an
            edge).
        RecordNotFound: If the id resolves to neither a node, an edge, nor a
            version the principal can read — the id alone does not say which
            kind was meant, so the base class is what is raised.
        InvalidTransition: If the transition is not allowed from the current
            state.
    """
    from_state, to_state = TRANSITIONS[action]
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (record_id,)).fetchone()
    kind = "node"
    if row is None:
        row = conn.execute("SELECT * FROM edges WHERE id = ?", (record_id,)).fetchone()
        kind = "edge"
    if row is None and record_id.isdigit():
        row = conn.execute("SELECT * FROM versions WHERE id = ?", (int(record_id),)).fetchone()
        kind = "version"
    if row is None:
        raise RecordNotFound(f"no node, edge, or version with id: {record_id}")
    before = _row_dict(row)
    spaces = _item_spaces(conn, kind, before)
    if not store.principal.is_human and not all(
        space in (store.principal.read_spaces or ()) for space in spaces
    ):
        raise RecordNotFound(f"no node, edge, or version with id: {record_id}")
    store.require_review(spaces, action)
    if before["state"] != from_state:
        raise InvalidTransition(
            f"cannot {action} a {kind} in state {before['state']!r} (requires state {from_state!r})"
        )
    if kind == "version":
        # Versions only ever sit in `proposed`; accept applies, reject archives.
        return kind, _transition_version(conn, before, action, actor, store, reason=reason)
    if kind == "node":
        conn.execute(
            "UPDATE nodes SET state = ?, updated_at = datetime('now') WHERE id = ?",
            (to_state, record_id),
        )
        after = _row_dict(_get_node_row(conn, record_id))
        payload: dict[str, Any] = {"before": before, "after": after}
        if reason is not None:
            payload["reason"] = reason
        seq = _emit(conn, actor, f"node.{action}", payload)
        _write_version(conn, after, actor, seq)
        if action == "accept":
            _activate_pending_mentions(conn, after, actor)
        return kind, after
    return kind, _set_edge_state(conn, before, to_state, action, actor, reason=reason)


def transition(
    record_id: str,
    action: str,
    *,
    reason: str | None = None,
    principal: Principal,
    path: str | Path | None = None,
) -> NodeOut | EdgeOut | VersionOut:
    """Apply a state-machine transition to a node, edge, or proposed version.

    Args:
        record_id: A node or edge id, or a version id (proposed update).
            Nodes are checked first, then edges, then versions.
        action: One of ``accept`` (proposed→active, or applies a proposed
            update), ``reject`` (proposed→archived), ``archive``
            (active→archived; nodes/edges only).
        reason: Recorded in the event payload — the same audit trail a batch
            :func:`reject_proposals` writes, so reviewing one item and
            reviewing a hundred leave the same record.
        actor: Who performs the transition. Every transition is the human
            tier (:data:`HUMAN_ONLY_ACTIONS`) and refuses any other actor.
        path: Explicit database path.

    Returns:
        The updated node, edge, or version.

    Raises:
        GrantNotPermitted: If the principal may not review this item.
        RecordNotFound: If the id resolves to no node, edge, or version.
        InvalidTransition: If the transition is not allowed from the current
            state.
    """
    if action not in TRANSITIONS:
        raise ValueError(f"unknown transition {action!r}; expected one of {sorted(TRANSITIONS)}")
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        kind, after = _transition_row(
            conn, record_id, action, principal.actor_string, store, reason=reason
        )
        conn.commit()
        if kind == "node":
            return _node_out(after)
        if kind == "version":
            return _version_out(after)
        return _edge_out(after)
    finally:
        conn.close()


# ── Review queue: pending proposals and batch accept/reject (design §8.1) ────


def _proposal_filters(
    conn: sqlite3.Connection,
    *,
    created_by: str | None,
    type: str | None,
    created_before: str | None,
    created_after: str | None,
) -> tuple[tuple[str, list[Any]] | None, tuple[str, list[Any]] | None, str | None]:
    """Build WHERE clauses/params for proposed nodes and proposed edges.

    Returns ``(node_filter, edge_filter, node_type_id)``: each filter is a
    ``(where_sql, params)`` pair, a ``None`` filter excludes that kind
    entirely, and ``node_type_id`` is the resolved node-type id (used by the
    proposed-update query, which types through the node). A ``type`` filter
    resolves against both catalogs: a name known only as a node type excludes
    edges (and vice versa) — filtering by a type shows that type, never
    unfiltered rows of the other kind. ``created_before`` / ``created_after``
    compare lexicographically against the SQLite ``datetime('now')`` format
    (``YYYY-MM-DD HH:MM:SS``).

    Raises:
        TypeNotFound: If ``type`` resolves in neither catalog.
    """
    node_type_id = edge_type_id = None
    if type is not None:
        row = conn.execute(
            "SELECT id, json_extract(props, '$.type_kind') AS kind FROM nodes"
            " WHERE (id = ? OR title = ?) AND type_id = 'type' AND state = 'active'",
            (type, type),
        ).fetchall()
        for r in row:
            if r["kind"] == "node":
                node_type_id = r["id"]
            elif r["kind"] == "edge":
                edge_type_id = r["id"]
        if node_type_id is None and edge_type_id is None:
            raise TypeNotFound(f"unknown node or edge type: {type}")

    def build(type_id: str | None) -> tuple[str, list[Any]]:
        clauses, params = ["state = 'proposed'"], []
        if created_by is not None:
            clauses.append("created_by = ?")
            params.append(created_by)
        if created_before is not None:
            clauses.append("created_at < ?")
            params.append(created_before)
        if created_after is not None:
            clauses.append("created_at > ?")
            params.append(created_after)
        if type_id is not None:
            clauses.append("type_id = ?")
            params.append(type_id)
        return " AND ".join(clauses), params

    node_filter = build(node_type_id) if type is None or node_type_id is not None else None
    edge_filter = build(edge_type_id) if type is None or edge_type_id is not None else None
    return node_filter, edge_filter, node_type_id


def _update_proposal_filter(
    node_type_id: str | None,
    *,
    created_by: str | None,
    created_before: str | None,
    created_after: str | None,
) -> tuple[str, list[Any]]:
    """Build the WHERE clause for proposed versions (kind ``update``).

    Column names differ from nodes/edges: the proposing actor is
    ``versions.actor`` and the type filter joins through the node.
    """
    clauses, params = ["v.state = 'proposed'"], []
    if created_by is not None:
        clauses.append("v.actor = ?")
        params.append(created_by)
    if created_before is not None:
        clauses.append("v.created_at < ?")
        params.append(created_before)
    if created_after is not None:
        clauses.append("v.created_at > ?")
        params.append(created_after)
    if node_type_id is not None:
        clauses.append("n.type_id = ?")
        params.append(node_type_id)
    return " AND ".join(clauses), params


def _proposal_rows(
    conn: sqlite3.Connection,
    store: Store | None = None,
    *,
    kind: str | None = None,
    **filters: Any,
) -> list[tuple[str, sqlite3.Row]]:
    """Fetch proposed node/edge/version rows matching the filters, oldest first.

    Within one kind the order is creation order (``created_at``, then
    ``rowid``); timestamps have one-second resolution, so same-second rows of
    different kinds may interleave.
    """
    node_filter, edge_filter, node_type_id = _proposal_filters(conn, **filters)
    node_scope = edge_scope = ""
    scope_params: list[Any] = []
    edge_scope_params: list[Any] = []
    if store is not None:
        node_scope, scope_params = store.node_scope()
        edge_scope, edge_scope_params = store.edge_scope()
    results: list[tuple[str, sqlite3.Row]] = []
    if kind in (None, "node") and node_filter is not None:
        where, params = node_filter
        results += [
            ("node", row)
            for row in conn.execute(
                f"SELECT rowid AS _rowid, * FROM nodes WHERE {where}{node_scope}"
                " ORDER BY created_at, rowid",
                (*params, *scope_params),
            ).fetchall()
        ]
    if kind in (None, "edge") and edge_filter is not None:
        where, params = edge_filter
        results += [
            ("edge", row)
            for row in conn.execute(
                f"SELECT rowid AS _rowid, * FROM edges WHERE {where}{edge_scope}"
                " ORDER BY created_at, rowid",
                (*params, *edge_scope_params),
            ).fetchall()
        ]
    if kind in (None, "update") and (filters.get("type") is None or node_type_id is not None):
        where, params = _update_proposal_filter(
            node_type_id,
            created_by=filters.get("created_by"),
            created_before=filters.get("created_before"),
            created_after=filters.get("created_after"),
        )
        update_scope = node_scope.replace("space_id", "n.space_id") if node_scope else ""
        results += [
            ("update", row)
            for row in conn.execute(
                "SELECT v.rowid AS _rowid, v.*, n.type_id AS node_type_id FROM versions v "
                "JOIN nodes n ON n.id = v.node_id "
                f"WHERE {where}{update_scope} ORDER BY v.created_at, v.rowid",
                (*params, *scope_params),
            ).fetchall()
        ]
    results.sort(key=lambda item: (item[1]["created_at"], item[1]["_rowid"]))
    return results


def _node_context(conn: sqlite3.Connection, node: dict[str, Any]) -> dict[str, Any]:
    """Reviewer context for a proposed node: its parent's id/title, if any."""
    if node["parent_id"] is None:
        return {}
    row = conn.execute("SELECT id, title FROM nodes WHERE id = ?", (node["parent_id"],)).fetchone()
    parent = {"id": row["id"], "title": row["title"]} if row else {"id": node["parent_id"]}
    return {"parent": parent}


def _edge_context(conn: sqlite3.Connection, edge: dict[str, Any]) -> dict[str, Any]:
    """Reviewer context for a proposed edge: endpoint ids and titles."""
    context: dict[str, Any] = {}
    for key, column in (("src", "src_id"), ("dst", "dst_id")):
        row = conn.execute("SELECT id, title FROM nodes WHERE id = ?", (edge[column],)).fetchone()
        context[key] = {"id": row["id"], "title": row["title"]} if row else {"id": edge[column]}
    return context


def _update_context(conn: sqlite3.Connection, version: dict[str, Any]) -> dict[str, Any]:
    """Reviewer context for a proposed update: the current node's id/title."""
    row = conn.execute("SELECT id, title FROM nodes WHERE id = ?", (version["node_id"],)).fetchone()
    node = {"id": row["id"], "title": row["title"]} if row else {"id": version["node_id"]}
    return {"node": node}


def list_proposals(
    *,
    created_by: str | None = None,
    type: str | None = None,
    kind: str | None = None,
    created_before: str | None = None,
    created_after: str | None = None,
    principal: Principal,
    limit: int = 500,
    path: str | Path | None = None,
) -> list[ProposalOut]:
    """List pending proposals (proposed nodes, edges, and updates), oldest first.

    Args:
        created_by: Filter by proposing actor (e.g. ``agent:researcher``).
        type: Filter by node/edge type id or name (applies within each kind).
        kind: ``"node"``, ``"edge"``, or ``"update"`` to list one kind only
            (default: all three).
        created_before: Only proposals created before this timestamp.
        created_after: Only proposals created after this timestamp.
        limit: Maximum proposals returned.
        path: Explicit database path.

    Returns:
        Proposals with reviewer context (edge endpoints, node parent, or the
        node an update targets).
    """
    if kind not in (None, "node", "edge", "update"):
        raise ValueError(f"kind must be 'node', 'edge', or 'update', got {kind!r}")
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        rows = _proposal_rows(
            conn,
            store,
            kind=kind,
            created_by=created_by,
            type=type,
            created_before=created_before,
            created_after=created_after,
        )[:limit]
        proposals = []
        for row_kind, row in rows:
            data = _row_dict(row)
            if row_kind == "node":
                proposals.append(
                    ProposalOut(
                        kind="node",
                        id=data["id"],
                        type=data["type_id"],
                        created_by=data["created_by"],
                        created_at=data["created_at"],
                        node=_node_out(data),
                        context=_node_context(conn, data),
                    )
                )
            elif row_kind == "edge":
                proposals.append(
                    ProposalOut(
                        kind="edge",
                        id=data["id"],
                        type=data["type_id"],
                        created_by=data["created_by"],
                        created_at=data["created_at"],
                        edge=_edge_out(data),
                        context=_edge_context(conn, data),
                    )
                )
            else:
                proposals.append(
                    ProposalOut(
                        kind="update",
                        id=str(data["id"]),
                        type=data["node_type_id"],
                        created_by=data["actor"],
                        created_at=data["created_at"],
                        version=_version_out(data),
                        context=_update_context(conn, data),
                    )
                )
        return proposals
    finally:
        conn.close()


def _transition_many(
    ids: list[str],
    action: str,
    *,
    principal: Principal,
    reason: str | None,
    path: str | Path | None,
) -> BatchTransitionOut:
    """Transition many ids in one connection; bad ids are skipped, not fatal.

    A refused reviewer is *not* a per-id failure: the whole batch raises
    before any id is touched, so an agent never gets a partially applied
    review. Grant-refusals on individual items land in ``failed`` like any
    other per-id error.
    """
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        actor = principal.actor_string
        transitioned: list[str] = []
        failed: list[TransitionFailure] = []
        for record_id in ids:
            try:
                _transition_row(conn, record_id, action, actor, store, reason=reason)
                transitioned.append(record_id)
            except (RecordNotFound, InvalidTransition, GrantNotPermitted) as exc:
                failed.append(TransitionFailure(id=record_id, error=str(exc)))
        conn.commit()
        return BatchTransitionOut(
            action=action, actor=actor, reason=reason, transitioned=transitioned, failed=failed
        )
    finally:
        conn.close()


def accept_proposals(
    ids: list[str], *, principal: Principal, path: str | Path | None = None
) -> BatchTransitionOut:
    """Accept proposed nodes/edges/updates by id, one event each.

    Accepting an update (a proposed version id, given as a string) applies
    its staged fields to the node. Accepting a node also brings the pending
    ``mentions`` edges its wikilinks materialised to ``active``. Ids that are
    unknown or not ``proposed`` are collected in ``failed``; the rest still
    transition.

    Raises:
        GrantNotPermitted: If the principal may not review — review is the
            human tier, whoever filed the proposal.
    """
    return _transition_many(ids, "accept", principal=principal, reason=None, path=path)


def reject_proposals(
    ids: list[str],
    *,
    reason: str,
    principal: Principal,
    path: str | Path | None = None,
) -> BatchTransitionOut:
    """Reject proposed nodes/edges/updates by id, one event each.

    The ``reason`` is recorded in every reject event's payload (design §8.1).
    Ids that are unknown or not ``proposed`` are collected in ``failed``.

    Raises:
        GrantNotPermitted: If the principal may not review an item — an agent
            may not reject another agent's proposal any more than its own,
            outside its edit-granted spaces.
    """
    return _transition_many(ids, "reject", principal=principal, reason=reason, path=path)


def _matching_ids(conn: sqlite3.Connection, *, kind: str | None, **filters: Any) -> list[str]:
    """Resolve a proposal filter to concrete ids (the batch-by-filter input)."""
    return [str(row["id"]) for _, row in _proposal_rows(conn, kind=kind, **filters)]


def accept_matching(
    *,
    created_by: str | None = None,
    type: str | None = None,
    kind: str | None = None,
    created_before: str | None = None,
    created_after: str | None = None,
    principal: Principal,
    path: str | Path | None = None,
) -> BatchTransitionOut:
    """Accept every proposal matching the filters (e.g. one agent's whole run).

    The filter resolves to concrete ids first, then each id transitions with
    its own event — the batch is a convenience, never a silent bulk update.

    Raises:
        GrantNotPermitted: If the principal may not review an item.
    """
    if kind not in (None, "node", "edge", "update"):
        raise ValueError(f"kind must be 'node', 'edge', or 'update', got {kind!r}")
    conn = _connect(path)
    try:
        ids = _matching_ids(
            conn,
            kind=kind,
            created_by=created_by,
            type=type,
            created_before=created_before,
            created_after=created_after,
        )
    finally:
        conn.close()
    return _transition_many(ids, "accept", principal=principal, reason=None, path=path)


def reject_matching(
    *,
    reason: str,
    created_by: str | None = None,
    type: str | None = None,
    kind: str | None = None,
    created_before: str | None = None,
    created_after: str | None = None,
    principal: Principal,
    path: str | Path | None = None,
) -> BatchTransitionOut:
    """Reject every proposal matching the filters, recording ``reason``.

    The filter resolves to concrete ids first, then each id transitions with
    its own event carrying the reason.

    Raises:
        GrantNotPermitted: If the principal may not review an item.
    """
    if kind not in (None, "node", "edge", "update"):
        raise ValueError(f"kind must be 'node', 'edge', or 'update', got {kind!r}")
    conn = _connect(path)
    try:
        ids = _matching_ids(
            conn,
            kind=kind,
            created_by=created_by,
            type=type,
            created_before=created_before,
            created_after=created_after,
        )
    finally:
        conn.close()
    return _transition_many(ids, "reject", principal=principal, reason=reason, path=path)


def undo(
    seq: int | None = None, *, principal: Principal, path: str | Path | None = None
) -> UndoResult:
    """Reverse one event (default: the latest non-undo event), restoring state.

    Uses the event's before/after payload: a create is reversed by deleting
    the created row (for nodes, along with its versions and incident edges —
    all recorded in the undo event's payload); any other mutation is reversed
    by writing the ``before`` state back. Undoing a node restore re-runs
    wikilink materialisation so the graph stays consistent with the restored
    content. The reversal itself is appended as an ``undo`` event; undo
    events cannot themselves be undone. Only graph events (``node.*`` /
    ``edge.*``) are reversible — audited non-graph events
    are skipped by default and refused when named explicitly.

    Undo is the **human tier**: restoring an event's payload writes arbitrary
    prior state back, ``state = 'active'`` included, so an agent allowed to
    undo would be an agent allowed to write live state (design §8.1/§8.2).

    Reversal never cascades beyond what the event itself created: an event
    the graph has since grown past (a created node that now has children, a
    row a later undo already removed) is refused, not forced.

    Raises:
        GrantNotPermitted: If the principal is not a human.
        EventNotFound: If no event matches ``seq`` (or none exist to undo).
        UndoNotPossible: If the target row is gone or the reversal would have
            to delete rows the event never created.
        ValueError: If the target event is an ``undo`` event or a non-graph
            event.
    """
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        store.require_human("undo")
        actor = principal.actor_string
        # Events already reversed by a prior undo are not reversible again.
        reversed_seqs = {
            json.loads(row["payload"])["reversed_seq"]
            for row in conn.execute("SELECT payload FROM events WHERE op = 'undo'").fetchall()
        }
        undoable = "(op LIKE 'node.%' OR op LIKE 'edge.%')"
        if seq is None:
            event = conn.execute(
                f"SELECT * FROM events WHERE op != 'undo' AND {undoable} ORDER BY seq DESC LIMIT 1"
                if not reversed_seqs
                else (
                    f"SELECT * FROM events WHERE op != 'undo' AND {undoable} "
                    f"AND seq NOT IN ({','.join(str(s) for s in sorted(reversed_seqs))}) "
                    "ORDER BY seq DESC LIMIT 1"
                )
            ).fetchone()
        else:
            event = conn.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
        if event is None:
            raise EventNotFound(f"no event to undo (seq={seq})")
        if event["op"] == "undo":
            raise ValueError(f"event {event['seq']} is an undo event and cannot be undone")
        kind = event["op"].split(".", 1)[0]
        if kind not in ("node", "edge"):
            raise ValueError(
                f"event {event['seq']} ({event['op']}) is not a graph event and cannot be undone"
            )
        if event["seq"] in reversed_seqs:
            raise ValueError(f"event {event['seq']} has already been undone")

        payload = json.loads(event["payload"])
        table = "nodes" if kind == "node" else "edges"
        before, after = payload["before"], payload["after"]
        deleted: list[dict[str, Any]] = []

        if before is None:
            # Reverse a create: remove the created row and anything that would
            # block the delete (a node's versions and incident edges).
            if kind == "node":
                # Children are separate creates this event never covered, and
                # they hold an FK to the row being deleted. Cascading would
                # destroy work the reversal was never asked to touch, so the
                # undo refuses and says what is in the way.
                children = conn.execute(
                    "SELECT id FROM nodes WHERE parent_id = ?", (after["id"],)
                ).fetchall()
                if children:
                    raise UndoNotPossible(
                        f"cannot undo event {event['seq']} ({event['op']}): node {after['id']} "
                        f"still has {len(children)} child node(s) — undo or reparent them first"
                    )
                for edge in conn.execute(
                    "SELECT * FROM edges WHERE src_id = ? OR dst_id = ?",
                    (after["id"], after["id"]),
                ).fetchall():
                    deleted.append({"table": "edges", "row": _row_dict(edge)})
                    conn.execute("DELETE FROM edges WHERE id = ?", (edge["id"],))
                for version in conn.execute(
                    "SELECT * FROM versions WHERE node_id = ?", (after["id"],)
                ).fetchall():
                    deleted.append({"table": "versions", "row": _row_dict(version)})
                    conn.execute("DELETE FROM versions WHERE id = ?", (version["id"],))
            deleted.append({"table": table, "row": after})
            conn.execute(f"DELETE FROM {table} WHERE id = ?", (after["id"],))
            restored = None
        else:
            columns = [key for key in before if key != "id"]
            assignments = ", ".join(f"{key} = ?" for key in columns)
            cursor = conn.execute(
                f"UPDATE {table} SET {assignments} WHERE id = ?",
                (*[before[key] for key in columns], before["id"]),
            )
            # An UPDATE that matched nothing restores nothing: the row was
            # deleted after this event (typically by undoing its create), so
            # reporting `restored` would be a lie and marking the event
            # reversed would bury it.
            if cursor.rowcount == 0:
                raise UndoNotPossible(
                    f"cannot undo event {event['seq']} ({event['op']}): "
                    f"{kind} {before['id']} no longer exists"
                )
            restored = before
        undo_seq = _emit(
            conn,
            actor,
            "undo",
            {
                "reversed_seq": event["seq"],
                "reversed_op": event["op"],
                "restored": restored,
                "deleted": deleted,
            },
        )
        if restored is not None and kind == "node":
            _write_version(conn, restored, actor, undo_seq)
            _materialize_mentions(conn, restored, actor, store)
        conn.commit()
        return UndoResult(
            undone_seq=event["seq"],
            undone_op=event["op"],
            restored=restored,
            deleted=deleted,
            undo_event_seq=undo_seq,
        )
    finally:
        conn.close()


def history(
    node_id: str, *, principal: Principal, path: str | Path | None = None
) -> list[VersionOut]:
    """Return a node's version snapshots in chronological order.

    Proposed and rejected updates appear alongside applied snapshots, marked
    by their ``state``.

    Raises:
        NodeNotFound: If the id does not resolve, or is not readable.
    """
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        node = _get_node_row(conn, node_id)
        if not store.node_visible(node):
            raise NodeNotFound(f"node not found: {node_id}")
        rows = conn.execute(
            "SELECT * FROM versions WHERE node_id = ? ORDER BY id", (node_id,)
        ).fetchall()
        return [_version_out(row) for row in rows]
    finally:
        conn.close()


def list_events(
    principal: Principal, *, limit: int = 50, path: str | Path | None = None
) -> list[EventOut]:
    """Return the most recent events (newest first), capped at ``limit``.

    The event log is the audit trail — a human surface (CLI today); agents
    do not read it.
    """
    conn = _connect(path)
    try:
        Store(conn, principal).require_human("read the event log")
        rows = conn.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [
            EventOut(
                seq=row["seq"],
                actor=row["actor"],
                op=row["op"],
                payload=json.loads(row["payload"]),
                cycle_id=row["cycle_id"],
                created_at=row["created_at"],
            )
            for row in rows
        ]
    finally:
        conn.close()


def _type_out(row: sqlite3.Row) -> TypeOut | EdgeTypeOut:
    """Build the public type-catalog model from a type-node row."""
    props = json.loads(row["props"])
    base = {
        "id": row["id"],
        "name": row["title"] or row["id"],
        "json_schema": props.get("schema_json", {}),
        "is_builtin": bool(props.get("is_builtin", 0)),
    }
    if props.get("type_kind") == "edge":
        return EdgeTypeOut(inverse_name=props.get("inverse_name"), **base)
    return TypeOut(parent_type_id=props.get("parent_type_id"), **base)


def list_types(*, principal: Principal, path: str | Path | None = None) -> TypesOut:
    """Return the full type catalog (node types and edge types).

    Types are nodes (Q13, migration ``0009``): the catalog is the active
    type-nodes, which live in the meta space — an agent must be able to read
    meta (the parity/file-birth grants give it that) to use the vocabulary.
    """
    conn = _connect(path)
    try:
        principal_meta = principal.level_on(META_SPACE_ID)
        if principal_meta < READ:
            raise TypeNotFound("the type catalog is not readable by this principal")
        rows = conn.execute(
            "SELECT * FROM nodes WHERE type_id = 'type' AND state = 'active' ORDER BY title"
        ).fetchall()
        types = [_type_out(row) for row in rows]
        return TypesOut(
            node_types=[t for t in types if isinstance(t, TypeOut)],
            edge_types=[t for t in types if isinstance(t, EdgeTypeOut)],
        )
    finally:
        conn.close()


# ── Curated graph reads (design §8.1 read tier — no query DSL, per T2) ───────


def get_schema(
    type: str, *, principal: Principal, path: str | Path | None = None
) -> TypeOut | EdgeTypeOut:
    """Fetch one type's catalog entry (node types checked first, then edges).

    Raises:
        TypeNotFound: If the id/name resolves in neither catalog — or the
            catalog is not readable by the principal.
    """
    conn = _connect(path)
    try:
        if principal.level_on(META_SPACE_ID) < READ:
            raise TypeNotFound("the type catalog is not readable by this principal")
        row = conn.execute(
            "SELECT * FROM nodes WHERE (id = ? OR title = ?) AND type_id = 'type'"
            " AND state = 'active' ORDER BY json_extract(props, '$.type_kind') DESC LIMIT 1",
            (type, type),
        ).fetchone()
        if row is None:
            raise TypeNotFound(f"unknown node or edge type: {type}")
        return _type_out(row)
    finally:
        conn.close()


#: Valid traversal directions: follow edges out of, into, or through a node.
DIRECTIONS = ("out", "in", "both")


def _walk(
    conn: sqlite3.Connection,
    start_id: str,
    *,
    type_ids: list[str] | None,
    depth: int,
    direction: str,
    store: Store,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Breadth-first walk over ``active`` edges; returns (node rows, edge rows).

    The root comes first in the node list; every edge the walk crossed is
    returned once (including edges between two visited nodes). Proposed and
    archived edges are never followed — reads default to the live graph. An
    edge is followed only when **both** endpoints are readable by the
    principal, so the walk never crosses into an unreadable space (Q13).
    """
    start = _get_node_row(conn, start_id)
    if not store.node_visible(start):
        raise NodeNotFound(f"node not found: {start_id}")
    nodes: dict[str, dict[str, Any]] = {start_id: _row_dict(start)}
    order = [start_id]
    edges: list[dict[str, Any]] = []
    seen_edges: set[str] = set()
    frontier = {start_id}
    for _level in range(depth):
        if not frontier:
            break
        next_frontier: set[str] = set()
        placeholders = ",".join("?" * len(frontier))
        params: list[Any] = sorted(frontier)
        if direction == "both":
            where = f"(src_id IN ({placeholders}) OR dst_id IN ({placeholders}))"
            params = params * 2
        else:
            column = "src_id" if direction == "out" else "dst_id"
            where = f"{column} IN ({placeholders})"
        scope, scope_params = store.edge_scope()
        sql = f"SELECT * FROM edges WHERE state = 'active' AND {where}{scope}"
        params += scope_params
        if type_ids:
            sql += f" AND type_id IN ({','.join('?' * len(type_ids))})"
            params += type_ids
        for edge_row in conn.execute(f"{sql} ORDER BY created_at, rowid", params).fetchall():
            edge = _row_dict(edge_row)
            if edge["id"] in seen_edges:
                continue
            seen_edges.add(edge["id"])
            edges.append(edge)
            for endpoint in (edge["src_id"], edge["dst_id"]):
                if endpoint not in nodes:
                    nodes[endpoint] = _row_dict(_get_node_row(conn, endpoint))
                    order.append(endpoint)
                    next_frontier.add(endpoint)
        frontier = next_frontier
    return [nodes[node_id] for node_id in order], edges


def get_neighborhood(
    node_id: str,
    *,
    depth: int = 1,
    principal: Principal,
    path: str | Path | None = None,
) -> SubgraphOut:
    """Return a node plus its active-edge neighborhood out to ``depth`` hops.

    Depth 0 returns the node alone. Design §8.1 ``get_node(id, depth)``.

    Raises:
        NodeNotFound: If the id does not resolve, or is not readable.
        ValueError: If ``depth`` is negative.
    """
    if depth < 0:
        raise ValueError(f"depth must be >= 0, got {depth}")
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        nodes, edges = _walk(
            conn, node_id, type_ids=None, depth=depth, direction="both", store=store
        )
        return SubgraphOut(
            root=node_id,
            depth=depth,
            nodes=[_node_out(row) for row in nodes],
            edges=[_edge_out(row) for row in edges],
        )
    finally:
        conn.close()


def traverse(
    start_id: str,
    *,
    edge_types: list[str] | None = None,
    depth: int = 2,
    direction: str = "both",
    principal: Principal,
    path: str | Path | None = None,
) -> SubgraphOut:
    """Walk the subgraph reachable from ``start_id`` over active edges.

    The pattern parameters (design §8.1 / T2): ``edge_types`` restricts the
    walk to those edge types (ids or names), ``depth`` caps the hops, and
    ``direction`` (``out`` / ``in`` / ``both``) orients it.

    Raises:
        NodeNotFound: If ``start_id`` does not resolve.
        TypeNotFound: If an edge type does not resolve.
        ValueError: If ``direction`` is unknown or ``depth`` is negative.
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, got {direction!r}")
    if depth < 0:
        raise ValueError(f"depth must be >= 0, got {depth}")
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        type_ids = (
            [_resolve_edge_type(conn, edge_type)[0] for edge_type in edge_types]
            if edge_types
            else None
        )
        nodes, edges = _walk(
            conn, start_id, type_ids=type_ids, depth=depth, direction=direction, store=store
        )
        return SubgraphOut(
            root=start_id,
            depth=depth,
            nodes=[_node_out(row) for row in nodes],
            edges=[_edge_out(row) for row in edges],
        )
    finally:
        conn.close()


def subgraph(
    root_id: str,
    *,
    depth: int = 2,
    edge_types: list[str] | None = None,
    edge_states: list[str] | None = None,
    min_confidence: float | None = None,
    created_by: str | None = None,
    node_types: list[str] | None = None,
    principal: Principal,
    limit: int = 200,
    path: str | Path | None = None,
) -> SubgraphOut:
    """Walk a bounded, server-side-filtered neighborhood of one node.

    The filtered sibling of :func:`traverse`, written for clients that render
    a graph: every filter is applied in the database and **both caps are
    enforced during the walk, not after it**.

    *Nodes* are capped by ``limit``, tested before the far side of an edge is
    read, so the walk costs O(``limit``) node reads no matter how many
    neighbours a hub has. ``limit`` is itself clamped to
    :data:`MAX_SUBGRAPH_LIMIT`: a caller passing an enormous number gets the
    ceiling, not the whole graph. *Edges* are capped separately at ``limit *``
    :data:`SUBGRAPH_EDGE_FACTOR`, because a node cap bounds nodes only — one
    pair of nodes can carry any number of edges between them, and without its
    own bound the edge list is unbounded whatever ``limit`` says.
    ``truncated`` is true when **either** cap bit, so a caller can always tell
    a partial graph from a whole one; it is deliberately conservative, true
    whenever a cap stopped the walk early even if the graph happened to have
    nothing more to give.

    The filters compose as one conjunction. An edge is followed only if it
    passes edge state **and** type **and** ``min_confidence`` **and**
    ``created_by``; a node is admitted only if it also passes ``node_types``.
    An edge to a node the type filter excludes is dropped with it, so the
    result never contains an edge pointing at a node it does not carry. The
    root is always present and is exempt from ``node_types`` — it is what was
    asked for, not something the walk found. A ``min_confidence`` floor drops
    edges with no stated confidence: unstated is not "meets the bar". A filter
    that removes nodes does *not* set ``truncated`` — the caller asked for that.

    The walk is breadth-first and undirected (edges count in either
    direction); within a level, edges are taken in ``(created_at, rowid)``
    order and nodes appear in the order first reached, so the same call always
    returns the same subgraph.

    The result is then **closed over its own node set**: one further bounded
    query adds edges whose endpoints were both admitted but which no walked
    level was incident to — the B–C edge of a triangle read at depth 1. A
    renderer therefore never draws two nodes it is showing as unconnected when
    the stored graph connects them.

    Args:
        root_id: Node at the centre of the subgraph.
        depth: Maximum hops from the root (0 returns the root alone).
        edge_types: Edge type ids/names the walk may follow (default: any).
        edge_states: Edge states the walk may follow (default:
            :data:`DEFAULT_EDGE_STATES`, the live graph).
        min_confidence: Floor on an edge's stored confidence.
        created_by: Only follow edges written by this actor.
        node_types: Node type ids/names that may be admitted (default: any).
        limit: Hard cap on the number of nodes returned, root included,
            clamped to :data:`MAX_SUBGRAPH_LIMIT`.
        path: Explicit database path.

    Returns:
        The subgraph, with ``truncated`` true when the node cap or the edge
        cap stopped it short of the whole neighborhood.

    Raises:
        NodeNotFound: If ``root_id`` does not resolve.
        TypeNotFound: If an edge or node type does not resolve.
        ValueError: If ``depth`` is negative, ``limit`` is below 1, an edge
            state is unknown, or ``min_confidence`` is outside [0, 1].
    """
    if depth < 0:
        raise ValueError(f"depth must be >= 0, got {depth}")
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    # Clamped rather than refused: a caller asking for more than the server
    # will ever draw gets the ceiling plus an honest `truncated`.
    limit = min(limit, MAX_SUBGRAPH_LIMIT)
    edge_limit = limit * SUBGRAPH_EDGE_FACTOR
    states = tuple(edge_states) if edge_states else DEFAULT_EDGE_STATES
    for state in states:
        if state not in STATES:
            raise ValueError(f"state must be one of {STATES}, got {state!r}")
    if min_confidence is not None and not 0 <= min_confidence <= 1:
        raise ValueError(f"min_confidence must be between 0 and 1, got {min_confidence}")
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        root = _get_node_row(conn, root_id)
        if not store.node_visible(root):
            raise NodeNotFound(f"node not found: {root_id}")
        edge_clauses = [f"state IN ({','.join('?' * len(states))})"]
        edge_params: list[Any] = list(states)
        scope, scope_params = store.edge_scope()
        if scope:
            edge_clauses.append(scope.removeprefix(" AND "))
            edge_params += scope_params
        if edge_types:
            type_ids = [_resolve_edge_type(conn, edge_type)[0] for edge_type in edge_types]
            edge_clauses.append(f"type_id IN ({','.join('?' * len(type_ids))})")
            edge_params += type_ids
        if min_confidence is not None:
            edge_clauses.append("confidence IS NOT NULL AND confidence >= ?")
            edge_params.append(min_confidence)
        if created_by is not None:
            edge_clauses.append("created_by = ?")
            edge_params.append(created_by)
        admissible_types = (
            {_resolve_node_type(conn, node_type) for node_type in node_types}
            if node_types
            else None
        )

        nodes: dict[str, dict[str, Any]] = {root_id: _row_dict(root)}
        order = [root_id]
        edges: list[dict[str, Any]] = []
        seen_edges: set[str] = set()
        truncated = False
        frontier = {root_id}
        for _level in range(depth):
            if not frontier:
                break
            if len(edges) >= edge_limit:
                # No budget left to connect anything a further level could
                # reach, so stop rather than admit nodes with no edge to them.
                break
            placeholders = ",".join("?" * len(frontier))
            sql = (
                f"SELECT * FROM edges WHERE (src_id IN ({placeholders}) "
                f"OR dst_id IN ({placeholders})) AND {' AND '.join(edge_clauses)} "
                "ORDER BY created_at, rowid"
            )
            next_frontier: set[str] = set()
            # Streamed, not fetched whole: the loop stops pulling rows the
            # moment the edge budget is spent.
            for edge_row in conn.execute(sql, sorted(frontier) * 2 + edge_params):
                edge = _row_dict(edge_row)
                if edge["id"] in seen_edges:
                    continue
                if len(edges) >= edge_limit:
                    break  # the edge cap bites here, mid-walk
                # One endpoint is in the frontier, so at most one is new.
                far = edge["dst_id"] if edge["src_id"] in nodes else edge["src_id"]
                if far not in nodes:
                    if len(nodes) >= limit:
                        # Tested *before* the far row is read: a hub with
                        # 10_000 spokes costs `limit` node reads, not 10_000.
                        truncated = True
                        continue  # the node cap bites here, mid-walk
                    row = _row_dict(_get_node_row(conn, far))
                    if admissible_types is not None and row["type_id"] not in admissible_types:
                        continue  # excluded node — its edge would dangle
                    nodes[far] = row
                    order.append(far)
                    next_frontier.add(far)
                seen_edges.add(edge["id"])
                edges.append(edge)
            frontier = next_frontier
        # Close the ring: edges between two admitted nodes that no walked level
        # was incident to (B–C of a triangle rooted at A, depth 1). One extra
        # query over a node set the cap already bounds — without it the graph
        # view draws nodes it is showing as unconnected, under `truncated`
        # false, which reads as data loss.
        if len(order) > 1 and len(edges) < edge_limit:
            admitted = sorted(nodes)
            placeholders = ",".join("?" * len(admitted))
            # `LIMIT edge_limit` is exactly enough and never too few: every row
            # this returns is either one the walk already took (at most
            # `len(edges)` of them) or a new one (at most the remaining
            # budget), and those two sum to `edge_limit`. It also keeps
            # SQLite's own sorter bounded on a dense node set.
            closing_sql = (
                f"SELECT * FROM edges WHERE src_id IN ({placeholders}) "
                f"AND dst_id IN ({placeholders}) AND {' AND '.join(edge_clauses)} "
                "ORDER BY created_at, rowid LIMIT ?"
            )
            for edge_row in conn.execute(closing_sql, admitted * 2 + edge_params + [edge_limit]):
                edge = _row_dict(edge_row)
                if edge["id"] in seen_edges:
                    continue
                if len(edges) >= edge_limit:
                    break
                seen_edges.add(edge["id"])
                edges.append(edge)
        if len(edges) >= edge_limit:
            # A spent budget is reported as truncation wherever it ran out —
            # mid-level, between levels, or in the closing pass, whose SQL
            # `LIMIT` makes "no more rows" and "no more budget" the same event.
            # Conservative by design: a graph that happens to fill the cap
            # exactly says "partial" rather than claiming a completeness the
            # walk never checked.
            truncated = True
        return SubgraphOut(
            root=root_id,
            depth=depth,
            nodes=[_node_out(nodes[node_id]) for node_id in order],
            edges=[_edge_out(row) for row in edges],
            truncated=truncated,
        )
    finally:
        conn.close()


def find_path(
    a_id: str, b_id: str, *, principal: Principal, path: str | Path | None = None
) -> PathOut:
    """Find the shortest path between two nodes over active edges (any type).

    Breadth-first, direction-agnostic. When no path exists, ``found`` is
    false and both lists are empty. The walk never crosses into a space the
    principal cannot read — a path through one simply does not exist.

    Raises:
        NodeNotFound: If either id does not resolve, or is not readable.
    """
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        a = _get_node_row(conn, a_id)
        b = _get_node_row(conn, b_id)
        if not store.node_visible(a):
            raise NodeNotFound(f"node not found: {a_id}")
        if not store.node_visible(b):
            raise NodeNotFound(f"node not found: {b_id}")
        if a_id == b_id:
            node = _node_out(a)
            return PathOut(found=True, hops=0, nodes=[node], edges=[])
        # parent[child] = (parent node id, edge row connecting them)
        parent: dict[str, tuple[str, dict[str, Any]]] = {}
        visited = {a_id}
        frontier = [a_id]
        found = False
        while frontier and not found:
            frontier_set = set(frontier)
            next_frontier: list[str] = []
            placeholders = ",".join("?" * len(frontier_set))
            scope, scope_params = store.edge_scope()
            rows = conn.execute(
                "SELECT * FROM edges WHERE state = 'active' "
                f"AND (src_id IN ({placeholders}) OR dst_id IN ({placeholders})){scope} "
                "ORDER BY created_at, rowid",
                sorted(frontier_set) * 2 + scope_params,
            ).fetchall()
            for edge_row in rows:
                edge = _row_dict(edge_row)
                for current in (edge["src_id"], edge["dst_id"]):
                    if current not in frontier_set:
                        continue
                    other = edge["dst_id"] if current == edge["src_id"] else edge["src_id"]
                    if other in visited:
                        continue
                    visited.add(other)
                    parent[other] = (current, edge)
                    next_frontier.append(other)
                    if other == b_id:
                        found = True
                        break
                if found:
                    break
            frontier = next_frontier
        if not found:
            return PathOut(found=False, hops=0, nodes=[], edges=[])
        node_ids = [b_id]
        path_edges: list[dict[str, Any]] = []
        while node_ids[-1] != a_id:
            previous, edge = parent[node_ids[-1]]
            path_edges.append(edge)
            node_ids.append(previous)
        node_ids.reverse()
        path_edges.reverse()
        return PathOut(
            found=True,
            hops=len(path_edges),
            nodes=[_node_out(_get_node_row(conn, node_id)) for node_id in node_ids],
            edges=[_edge_out(edge) for edge in path_edges],
        )
    finally:
        conn.close()


def _render_version(version: dict[str, Any]) -> str:
    """Render a version as stable diffable text (title, props, then content)."""
    props = json.dumps(json.loads(version["props"]), sort_keys=True, ensure_ascii=False)
    return f"title: {version['title'] or ''}\nprops: {props}\n\n{version['content']}"


def diff_versions(
    a: int, b: int, *, principal: Principal, path: str | Path | None = None
) -> DiffOut:
    """Diff two versions of one node (design §8.1 ``diff(a, b)``).

    Raises:
        VersionNotFound: If either version id does not resolve.
        NodeNotFound: If the node is not readable by the principal.
        ValueError: If the versions belong to different nodes.
    """
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        version_a = _row_dict(_get_version_row(conn, a))
        version_b = _row_dict(_get_version_row(conn, b))
        node = _get_node_row(conn, version_a["node_id"])
        if not store.node_visible(node):
            raise NodeNotFound(f"node not found: {version_a['node_id']}")
        if version_a["node_id"] != version_b["node_id"]:
            raise ValueError(
                f"versions {a} and {b} belong to different nodes "
                f"({version_a['node_id']} vs {version_b['node_id']})"
            )
        changed = [
            field for field in ("title", "content", "props") if version_a[field] != version_b[field]
        ]
        diff = "\n".join(
            difflib.unified_diff(
                _render_version(version_a).splitlines(),
                _render_version(version_b).splitlines(),
                fromfile=f"version {a}",
                tofile=f"version {b}",
                lineterm="",
            )
        )
        return DiffOut(
            node_id=version_a["node_id"],
            a=_version_out(version_a),
            b=_version_out(version_b),
            changed_fields=changed,
            diff=diff,
        )
    finally:
        conn.close()


# ── Account and grant administration (Q13; human-only, event-logged) ──────────

#: Grant levels accepted by :func:`grant` (hierarchical: read ⊂ suggest ⊂ edit).
GRANT_LEVEL_NAMES = ("read", "suggest", "edit")


def _admin_actor(conn: sqlite3.Connection, principal: Principal) -> str:
    """Gate account/grant administration to humans; return the actor string."""
    Store(conn, principal).require_human("administer accounts and grants")
    return principal.actor_string


def _human_out(row: sqlite3.Row) -> HumanOut:
    return HumanOut(
        id=row["id"],
        name=row["name"],
        has_password=row["credential_hash"] is not None,
        disabled=bool(row["disabled"]),
        created_at=row["created_at"],
    )


def _agent_out(row: sqlite3.Row) -> AgentOut:
    return AgentOut(
        id=row["id"],
        kind=row["kind"],
        name=row["name"],
        owner_human_id=row["owner_human_id"],
        has_token=row["credential_hash"] is not None,
        disabled=bool(row["disabled"]),
        created_at=row["created_at"],
    )


def list_humans(*, principal: Principal, path: str | Path | None = None) -> list[HumanOut]:
    """List human accounts (human-only)."""
    conn = _connect(path)
    try:
        Store(conn, principal).require_human("list humans")
        return [_human_out(row) for row in conn.execute("SELECT * FROM humans ORDER BY id")]
    finally:
        conn.close()


def create_human(name: str, *, principal: Principal, path: str | Path | None = None) -> HumanOut:
    """Create a human account (passwordless until ``human passwd`` sets one)."""
    conn = _connect(path)
    try:
        actor = _admin_actor(conn, principal)
        human_id = uuid.uuid4().hex[:12]
        conn.execute("INSERT INTO humans (id, name) VALUES (?, ?)", (human_id, name))
        row = conn.execute("SELECT * FROM humans WHERE id = ?", (human_id,)).fetchone()
        _emit(conn, actor, "human.create", {"before": None, "after": {"id": human_id, "name": name}})
        conn.commit()
        return _human_out(row)
    finally:
        conn.close()


def set_human_password(
    human_id: str, password: str, *, principal: Principal, path: str | Path | None = None
) -> None:
    """Set or change a human's password (argon2id). The hash never enters a payload."""
    conn = _connect(path)
    try:
        actor = _admin_actor(conn, principal)
        cursor = conn.execute(
            "UPDATE humans SET credential_hash = ? WHERE id = ?",
            (auth.hash_password(password), human_id),
        )
        if cursor.rowcount == 0:
            raise RecordNotFound(f"no humans row with id: {human_id}")
        _emit(conn, actor, "human.password", {"human_id": human_id})
        conn.commit()
    finally:
        conn.close()


def _set_disabled(
    table: str, row_id: str, disabled: bool, *, conn: sqlite3.Connection
) -> None:
    cursor = conn.execute(
        f"UPDATE {table} SET disabled = ? WHERE id = ?", (int(disabled), row_id)
    )
    if cursor.rowcount == 0:
        raise RecordNotFound(f"no {table} row with id: {row_id}")


def disable_human(
    human_id: str, *, principal: Principal, path: str | Path | None = None
) -> None:
    """Disable a human: its sessions die, and its external agents' tokens with
    them (verification-time cascade — R3). In-flight proposals are untouched."""
    conn = _connect(path)
    try:
        actor = _admin_actor(conn, principal)
        _set_disabled("humans", human_id, True, conn=conn)
        conn.execute("DELETE FROM sessions WHERE human_id = ?", (human_id,))
        _emit(conn, actor, "human.disable", {"human_id": human_id})
        conn.commit()
    finally:
        conn.close()


def enable_human(human_id: str, *, principal: Principal, path: str | Path | None = None) -> None:
    """Re-enable a disabled human (its agents' tokens verify again)."""
    conn = _connect(path)
    try:
        actor = _admin_actor(conn, principal)
        _set_disabled("humans", human_id, False, conn=conn)
        _emit(conn, actor, "human.enable", {"human_id": human_id})
        conn.commit()
    finally:
        conn.close()


def list_agents(*, principal: Principal, path: str | Path | None = None) -> list[AgentOut]:
    """List agent accounts (human-only)."""
    conn = _connect(path)
    try:
        Store(conn, principal).require_human("list agents")
        return [_agent_out(row) for row in conn.execute("SELECT * FROM agents ORDER BY id")]
    finally:
        conn.close()


def create_agent(
    name: str,
    *,
    kind: str = "external",
    owner_human_id: str | None = None,
    grants: dict[str, str] | None = None,
    principal: Principal,
    path: str | Path | None = None,
) -> AgentCreatedOut:
    """Create an agent account and mint its token — shown this once, hashed at rest.

    External agents must name their owning human; internal agents (Phase 5)
    have none and get no token (they authenticate by being in-process).
    ``grants`` is the creation-time template (Q13 note 03 Q6): rows copied
    verbatim at birth, leaving no trace — the default is the minimal viable
    set, ``read`` on meta (an agent that cannot read the vocabulary cannot
    resolve a type). Everything beyond that is an explicit ``grant`` call.
    """
    if kind == "external" and not owner_human_id:
        raise ValueError("an external agent needs an owner_human_id")
    if kind not in ("external", "internal"):
        raise ValueError(f"kind must be 'external' or 'internal', got {kind!r}")
    template = {"meta": "read"} if grants is None else grants
    for level in template.values():
        if level not in GRANT_LEVEL_NAMES:
            raise ValueError(f"level must be one of {GRANT_LEVEL_NAMES}, got {level!r}")
    conn = _connect(path)
    try:
        actor = _admin_actor(conn, principal)
        agent_id = name
        token, token_hash = auth.generate_token()
        conn.execute(
            "INSERT INTO agents (id, kind, name, owner_human_id, credential_hash)"
            " VALUES (?, ?, ?, ?, ?)",
            (agent_id, kind, name, owner_human_id, token_hash if kind == "external" else None),
        )
        for space_id, level in template.items():
            conn.execute(
                "INSERT INTO grants (agent_id, space_id, level) VALUES (?, ?, ?)",
                (agent_id, space_id, level),
            )
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        _emit(
            conn,
            actor,
            "agent.create",
            {"before": None, "after": {"id": agent_id, "kind": kind, "name": name}},
        )
        conn.commit()
        out = _agent_out(row)
        return AgentCreatedOut(agent=out, token=token if kind == "external" else "")
    finally:
        conn.close()


def rotate_agent_token(
    agent_id: str, *, principal: Principal, path: str | Path | None = None
) -> str:
    """Replace an agent's token: the old one dies now; the new one shows once."""
    conn = _connect(path)
    try:
        actor = _admin_actor(conn, principal)
        token, token_hash = auth.generate_token()
        cursor = conn.execute(
            "UPDATE agents SET credential_hash = ? WHERE id = ?", (token_hash, agent_id)
        )
        if cursor.rowcount == 0:
            raise RecordNotFound(f"no agents row with id: {agent_id}")
        _emit(conn, actor, "agent.token_rotate", {"agent_id": agent_id})
        conn.commit()
        return token
    finally:
        conn.close()


def disable_agent(agent_id: str, *, principal: Principal, path: str | Path | None = None) -> None:
    """Disable an agent: its token dies immediately; its proposals stay, flagged."""
    conn = _connect(path)
    try:
        actor = _admin_actor(conn, principal)
        _set_disabled("agents", agent_id, True, conn=conn)
        _emit(conn, actor, "agent.disable", {"agent_id": agent_id})
        conn.commit()
    finally:
        conn.close()


def enable_agent(agent_id: str, *, principal: Principal, path: str | Path | None = None) -> None:
    """Re-enable a disabled agent (its current token verifies again)."""
    conn = _connect(path)
    try:
        actor = _admin_actor(conn, principal)
        _set_disabled("agents", agent_id, False, conn=conn)
        _emit(conn, actor, "agent.enable", {"agent_id": agent_id})
        conn.commit()
    finally:
        conn.close()


def grant(
    agent_id: str,
    space: str,
    level: str,
    *,
    principal: Principal,
    path: str | Path | None = None,
) -> GrantOut:
    """Grant (or re-level) an agent's access to a space; event-logged."""
    if level not in GRANT_LEVEL_NAMES:
        raise ValueError(f"level must be one of {GRANT_LEVEL_NAMES}, got {level!r}")
    conn = _connect(path)
    try:
        actor = _admin_actor(conn, principal)
        space_id = _resolve_space(conn, space)
        before = conn.execute(
            "SELECT * FROM grants WHERE agent_id = ? AND space_id = ?", (agent_id, space_id)
        ).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO grants (agent_id, space_id, level) VALUES (?, ?, ?)",
            (agent_id, space_id, level),
        )
        row = conn.execute(
            "SELECT * FROM grants WHERE agent_id = ? AND space_id = ?", (agent_id, space_id)
        ).fetchone()
        _emit(
            conn,
            actor,
            "grant.set",
            {
                "before": dict(before) if before else None,
                "after": {"agent_id": agent_id, "space_id": space_id, "level": level},
            },
        )
        conn.commit()
        return GrantOut(
            agent_id=row["agent_id"],
            space_id=row["space_id"],
            level=row["level"],
            created_at=row["created_at"],
        )
    finally:
        conn.close()


def revoke(agent_id: str, space: str, *, principal: Principal, path: str | Path | None = None) -> None:
    """Revoke an agent's grant on a space; event-logged."""
    conn = _connect(path)
    try:
        actor = _admin_actor(conn, principal)
        space_id = _resolve_space(conn, space)
        before = conn.execute(
            "SELECT * FROM grants WHERE agent_id = ? AND space_id = ?", (agent_id, space_id)
        ).fetchone()
        if before is None:
            raise RecordNotFound(f"no grant for {agent_id!r} on space {space_id!r}")
        conn.execute(
            "DELETE FROM grants WHERE agent_id = ? AND space_id = ?", (agent_id, space_id)
        )
        _emit(conn, actor, "grant.revoke", {"before": dict(before), "after": None})
        conn.commit()
    finally:
        conn.close()


def list_grants(
    agent_id: str | None = None, *, principal: Principal, path: str | Path | None = None
) -> list[GrantOut]:
    """List grant rows, optionally for one agent (human-only)."""
    conn = _connect(path)
    try:
        Store(conn, principal).require_human("list grants")
        if agent_id is not None:
            rows = conn.execute(
                "SELECT * FROM grants WHERE agent_id = ? ORDER BY space_id", (agent_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM grants ORDER BY agent_id, space_id").fetchall()
        return [
            GrantOut(
                agent_id=row["agent_id"],
                space_id=row["space_id"],
                level=row["level"],
                created_at=row["created_at"],
            )
            for row in rows
        ]
    finally:
        conn.close()
