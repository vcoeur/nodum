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

import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from nodum import db
from nodum.models import (
    BatchTransitionOut,
    EdgeOut,
    EdgeTypeOut,
    EventOut,
    InitResult,
    NodeOut,
    PolicyOut,
    ProposalOut,
    TransitionFailure,
    TypeOut,
    TypesOut,
    UndoResult,
    VersionOut,
)

#: Actor value for human-driven writes (the CLI default). Human writes land in
#: ``active``; every other actor's writes land in ``proposed``.
ACTOR_HUMAN = "human"

#: Allowed state values shared by nodes and edges.
STATES = ("proposed", "active", "archived")

#: State transitions: action → (required current state, resulting state).
TRANSITIONS = {
    "accept": ("proposed", "active"),
    "reject": ("proposed", "archived"),
    "archive": ("active", "archived"),
}

#: Policy rule actions (design §8.3). Only ``auto_accept`` is evaluated on the
#: direct write path today; ``auto_apply``/``always_propose`` govern internal
#: agent jobs and are stored for the Phase-5 runtime.
POLICY_ACTIONS = ("auto_accept", "auto_apply", "always_propose")

#: Sentinel distinguishing "argument not given" from an explicit ``None``.
_UNSET: Any = object()

#: A wikilink target: ``[[node-id]]`` or ``[[Exact Title]]``.
WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")


class NodeNotFound(LookupError):
    """Raised when a node id does not resolve."""


class EdgeNotFound(LookupError):
    """Raised when an edge id does not resolve."""


class TypeNotFound(LookupError):
    """Raised when a node or edge type id/name does not resolve."""


class EventNotFound(LookupError):
    """Raised when an event seq does not resolve."""


class PolicyNotFound(LookupError):
    """Raised when no policy is stored for an agent."""


class InvalidTransition(ValueError):
    """Raised when a state transition is not allowed from the current state."""


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
        graph_id=data["graph_id"],
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
        graph_id=data["graph_id"],
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


def _resolve_node_type(conn: sqlite3.Connection, type_ref: str) -> str:
    """Resolve a node-type id or name to its id, or raise :class:`TypeNotFound`."""
    row = conn.execute(
        "SELECT id FROM types WHERE id = ? OR name = ?", (type_ref, type_ref)
    ).fetchone()
    if row is None:
        raise TypeNotFound(f"unknown node type: {type_ref}")
    return row["id"]


def _resolve_edge_type(conn: sqlite3.Connection, type_ref: str) -> str:
    """Resolve an edge-type id or name to its id, or raise :class:`TypeNotFound`."""
    row = conn.execute(
        "SELECT id FROM edge_types WHERE id = ? OR name = ?", (type_ref, type_ref)
    ).fetchone()
    if row is None:
        raise TypeNotFound(f"unknown edge type: {type_ref}")
    return row["id"]


def _default_state(actor: str) -> str:
    """Return the initial write state for an actor: humans active, agents proposed."""
    return "active" if actor == ACTOR_HUMAN else "proposed"


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


def _resolve_wikilink(conn: sqlite3.Connection, target: str) -> str | None:
    """Resolve a ``[[target]]`` to a node id, or ``None`` when unresolvable.

    Resolution order: exact node id first, then exact title match among
    non-archived nodes (oldest first when several share a title).
    """
    row = conn.execute("SELECT id FROM nodes WHERE id = ?", (target,)).fetchone()
    if row is not None:
        return row["id"]
    row = conn.execute(
        """
        SELECT id FROM nodes
        WHERE title = ? AND state != 'archived'
        ORDER BY created_at, rowid
        LIMIT 1
        """,
        (target,),
    ).fetchone()
    return row["id"] if row is not None else None


def _materialize_mentions(
    conn: sqlite3.Connection,
    node_row: sqlite3.Row | dict[str, Any],
    actor: str,
    cycle_id: str | None = None,
) -> None:
    """Sync a node's ``[[wikilinks]]`` with its active ``mentions`` edges.

    Creates a ``mentions`` edge (active, ``created_by`` = writer) for every
    newly linked target, archives edges whose target text disappeared, and
    silently skips unresolvable targets (no dangling edges). Self-links are
    ignored. Idempotent: re-running on unchanged content changes nothing.
    """
    node = dict(node_row)
    targets = set(WIKILINK_RE.findall(node["content"] or ""))
    resolved = {
        dst
        for target in targets
        if (dst := _resolve_wikilink(conn, target)) is not None and dst != node["id"]
    }
    current = conn.execute(
        "SELECT * FROM edges WHERE src_id = ? AND type_id = 'mentions' AND state = 'active'",
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
            state="active",
            cycle_id=cycle_id,
        )
    for dst_id, edge in current_by_dst.items():
        if dst_id not in resolved:
            _set_edge_state(conn, dict(edge), "archived", "archive", actor, cycle_id=cycle_id)


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
    policy_rule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert one edge row and emit its create/propose event; returns the row.

    ``policy_rule`` records the policy rule that auto-accepted the write, so
    the event log alone shows *why* an agent write landed active (design §6).
    """
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
    if policy_rule is not None:
        payload["policy_rule"] = policy_rule
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


# ── Agent policies (design §8.3) ──────────────────────────────────────────────


def _validate_rules(conn: sqlite3.Connection, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate a policy ruleset and return it with edge types resolved to ids.

    A rule must name at least one key (``job`` or ``edge_type``), carry a known
    ``action``, and — when present — a ``min_confidence`` in ``[0, 1]``. An
    ``edge_type`` must resolve against the catalog (it is stored as its id).
    Unknown extra keys pass through untouched.

    Raises:
        ValueError: If a rule is malformed.
        TypeNotFound: If a rule's ``edge_type`` does not resolve.
    """
    validated: list[dict[str, Any]] = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"policy rule {index} must be an object, got {rule!r}")
        action = rule.get("action")
        if action not in POLICY_ACTIONS:
            raise ValueError(
                f"policy rule {index}: action must be one of {POLICY_ACTIONS}, got {action!r}"
            )
        if "job" not in rule and "edge_type" not in rule:
            raise ValueError(f"policy rule {index}: needs a 'job' or 'edge_type' key")
        min_confidence = rule.get("min_confidence")
        if min_confidence is not None and not 0 <= min_confidence <= 1:
            raise ValueError(
                f"policy rule {index}: min_confidence must be between 0 and 1, got {min_confidence}"
            )
        normalized = dict(rule)
        if "edge_type" in rule:
            normalized["edge_type"] = _resolve_edge_type(conn, str(rule["edge_type"]))
        validated.append(normalized)
    return validated


def _policy_out(row: sqlite3.Row) -> PolicyOut:
    """Build the public policy model from a policies row (rules decoded)."""
    return PolicyOut(
        agent=row["agent"],
        rules=json.loads(row["rules"]),
        updated_by=row["updated_by"],
        updated_at=row["updated_at"],
    )


def _auto_accept_rule(
    conn: sqlite3.Connection,
    actor: str,
    edge_type_id: str,
    confidence: float | None,
) -> dict[str, Any] | None:
    """Return the policy rule auto-accepting this edge write, or ``None``.

    Only the actor's own policy (exact actor-string match) applies, and only
    its ``edge_type`` rules with action ``auto_accept`` — ``job`` rules govern
    the internal runtime, not direct writes. A rule matches when it names the
    edge's type and its confidence gate passes: no ``min_confidence`` means no
    gate; a gate requires the edge to carry a confidence at least as high.
    """
    row = conn.execute("SELECT rules FROM policies WHERE agent = ?", (actor,)).fetchone()
    if row is None:
        return None
    for rule in json.loads(row["rules"]):
        if rule.get("action") != "auto_accept" or rule.get("edge_type") != edge_type_id:
            continue
        gate = rule.get("min_confidence")
        if gate is None or (confidence is not None and confidence >= gate):
            return rule
    return None


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
    actor: str = ACTOR_HUMAN,
    path: str | Path | None = None,
) -> NodeOut:
    """Create a node, emit ``node.create``/``node.propose``, snapshot a version.

    The initial state is ``active`` for the ``human`` actor and ``proposed``
    otherwise. When ``parent_id`` is given the node is appended after its
    siblings (``max(position) + 1.0``). Wikilinks in ``content`` are
    materialised as ``mentions`` edges.

    Args:
        type: Node-type id or name (must exist in the catalog).
        title: Optional display title (wikilink targets resolve against it).
        content: Canonical Markdown body.
        parent_id: Optional parent node id (must exist).
        props: Free-form JSON-object metadata.
        actor: Who is writing (``human`` or ``agent:<name>``).
        path: Explicit database path.

    Returns:
        The created node.
    """
    conn = _connect(path)
    try:
        type_id = _resolve_node_type(conn, type)
        if parent_id is not None:
            _get_node_row(conn, parent_id)
        node_id = uuid.uuid4().hex
        state = _default_state(actor)
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
            INSERT INTO nodes (id, type_id, parent_id, position, title, content, props,
                               state, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_id,
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
        _materialize_mentions(conn, node, actor)
        conn.commit()
        return _node_out(node)
    finally:
        conn.close()


def get_node(node_id: str, *, path: str | Path | None = None) -> NodeOut:
    """Fetch one node by id.

    Raises:
        NodeNotFound: If the id does not resolve.
    """
    conn = _connect(path)
    try:
        return _node_out(_get_node_row(conn, node_id))
    finally:
        conn.close()


def update_node(
    node_id: str,
    *,
    title: Any = _UNSET,
    content: Any = _UNSET,
    props: Any = _UNSET,
    actor: str = ACTOR_HUMAN,
    path: str | Path | None = None,
) -> NodeOut:
    """Update a node's title/content/props, emit ``node.update``, version it.

    Only the given fields change. A content change re-runs wikilink
    materialisation (new links create edges, removed text archives them).

    Raises:
        NodeNotFound: If the id does not resolve.
    """
    conn = _connect(path)
    try:
        before = _row_dict(_get_node_row(conn, node_id))
        new_title = before["title"] if title is _UNSET else title
        new_content = before["content"] if content is _UNSET else content
        new_props = before["props"] if props is _UNSET else json.dumps(props, ensure_ascii=False)
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
            _materialize_mentions(conn, after, actor)
        conn.commit()
        return _node_out(after)
    finally:
        conn.close()


def list_nodes(
    *,
    type: str | None = None,
    state: str | None = None,
    parent_id: str | None = None,
    limit: int = 500,
    path: str | Path | None = None,
) -> list[NodeOut]:
    """List nodes, optionally filtered by type name/id, state, or parent.

    Ordered by ``created_at``; ``limit`` caps the result (default 500).
    """
    conn = _connect(path)
    try:
        clauses: list[str] = []
        params: list[Any] = []
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


def list_children(node_id: str, *, path: str | Path | None = None) -> list[NodeOut]:
    """List a node's children in ``position`` order.

    Raises:
        NodeNotFound: If the id does not resolve.
    """
    conn = _connect(path)
    try:
        _get_node_row(conn, node_id)
        rows = conn.execute(
            "SELECT * FROM nodes WHERE parent_id = ? ORDER BY position", (node_id,)
        ).fetchall()
        return [_node_out(row) for row in rows]
    finally:
        conn.close()


def create_edge(
    src_id: str,
    dst_id: str,
    type: str,
    *,
    props: dict[str, Any] | None = None,
    confidence: float | None = None,
    actor: str = ACTOR_HUMAN,
    path: str | Path | None = None,
) -> EdgeOut:
    """Create a typed, directed edge and emit ``edge.create``/``edge.propose``.

    Both endpoints must exist. The initial state follows the actor rule
    (``active`` for humans, ``proposed`` otherwise) unless the actor's policy
    auto-accepts the write (design §8.3: a matching ``edge_type`` rule with
    action ``auto_accept`` whose confidence gate passes). An auto-accepted
    write is still the agent's own event — the op records the landing state
    (``edge.create``) and the payload records the matched rule.

    Raises:
        NodeNotFound: If either endpoint does not resolve.
        TypeNotFound: If the edge type does not resolve.
        ValueError: If ``confidence`` is outside ``[0, 1]``.
    """
    conn = _connect(path)
    try:
        _get_node_row(conn, src_id)
        _get_node_row(conn, dst_id)
        type_id = _resolve_edge_type(conn, type)
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError(f"confidence must be between 0 and 1, got {confidence}")
        state = _default_state(actor)
        policy_rule = None
        if state == "proposed":
            policy_rule = _auto_accept_rule(conn, actor, type_id, confidence)
            if policy_rule is not None:
                state = "active"
        row = _insert_edge(
            conn,
            src_id=src_id,
            dst_id=dst_id,
            type_id=type_id,
            props=props or {},
            confidence=confidence,
            actor=actor,
            state=state,
            policy_rule=policy_rule,
        )
        conn.commit()
        return _edge_out(row)
    finally:
        conn.close()


def list_edges(
    *,
    node_id: str | None = None,
    type: str | None = None,
    state: str | None = None,
    limit: int = 500,
    path: str | Path | None = None,
) -> list[EdgeOut]:
    """List edges, optionally filtered by incident node, type, or state.

    ``node_id`` matches edges in either direction.
    """
    conn = _connect(path)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if node_id is not None:
            clauses.append("(src_id = ? OR dst_id = ?)")
            params.extend([node_id, node_id])
        if type is not None:
            clauses.append("type_id = ?")
            params.append(_resolve_edge_type(conn, type))
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


def _transition_row(
    conn: sqlite3.Connection,
    record_id: str,
    action: str,
    actor: str,
    reason: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Apply one state transition inside an open connection (no commit).

    Returns:
        A ``(kind, after_row)`` pair where kind is ``"node"`` or ``"edge"``.

    Raises:
        NodeNotFound: If the id resolves to neither a node nor an edge.
        InvalidTransition: If the transition is not allowed from the current
            state.
    """
    from_state, to_state = TRANSITIONS[action]
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (record_id,)).fetchone()
    kind = "node"
    if row is None:
        row = conn.execute("SELECT * FROM edges WHERE id = ?", (record_id,)).fetchone()
        kind = "edge"
    if row is None:
        raise NodeNotFound(f"no node or edge with id: {record_id}")
    before = _row_dict(row)
    if before["state"] != from_state:
        raise InvalidTransition(
            f"cannot {action} a {kind} in state {before['state']!r} (requires state {from_state!r})"
        )
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
        return kind, after
    return kind, _set_edge_state(conn, before, to_state, action, actor, reason=reason)


def transition(
    record_id: str, action: str, *, actor: str = ACTOR_HUMAN, path: str | Path | None = None
) -> NodeOut | EdgeOut:
    """Apply a state-machine transition to a node or edge.

    Args:
        record_id: A node or edge id (nodes are checked first).
        action: One of ``accept`` (proposed→active), ``reject``
            (proposed→archived), ``archive`` (active→archived).
        actor: Who performs the transition.
        path: Explicit database path.

    Returns:
        The updated node or edge.

    Raises:
        NodeNotFound: If the id resolves to neither a node nor an edge.
        InvalidTransition: If the transition is not allowed from the current
            state.
    """
    if action not in TRANSITIONS:
        raise ValueError(f"unknown transition {action!r}; expected one of {sorted(TRANSITIONS)}")
    conn = _connect(path)
    try:
        kind, after = _transition_row(conn, record_id, action, actor)
        conn.commit()
        return _node_out(after) if kind == "node" else _edge_out(after)
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
) -> tuple[tuple[str, list[Any]] | None, tuple[str, list[Any]] | None]:
    """Build WHERE clauses/params for proposed nodes and proposed edges.

    Returns ``(node_filter, edge_filter)`` pairs of ``(where_sql, params)``;
    a ``None`` filter excludes that kind entirely. A ``type`` filter resolves
    against both catalogs: a name known only as a node type excludes edges
    (and vice versa) — filtering by a type shows that type, never unfiltered
    rows of the other kind. ``created_before`` / ``created_after`` compare
    lexicographically against the SQLite ``datetime('now')`` format
    (``YYYY-MM-DD HH:MM:SS``).

    Raises:
        TypeNotFound: If ``type`` resolves in neither catalog.
    """
    node_type_id = edge_type_id = None
    if type is not None:
        row = conn.execute("SELECT id FROM types WHERE id = ? OR name = ?", (type, type)).fetchone()
        node_type_id = row["id"] if row else None
        row = conn.execute(
            "SELECT id FROM edge_types WHERE id = ? OR name = ?", (type, type)
        ).fetchone()
        edge_type_id = row["id"] if row else None
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
    return node_filter, edge_filter


def _proposal_rows(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    **filters: Any,
) -> list[tuple[str, sqlite3.Row]]:
    """Fetch proposed node/edge rows matching the filters, oldest first.

    Within one kind the order is creation order (``created_at``, then
    ``rowid``); timestamps have one-second resolution, so same-second rows of
    different kinds may interleave.
    """
    node_filter, edge_filter = _proposal_filters(conn, **filters)
    results: list[tuple[str, sqlite3.Row]] = []
    if kind in (None, "node") and node_filter is not None:
        where, params = node_filter
        results += [
            ("node", row)
            for row in conn.execute(
                f"SELECT rowid AS _rowid, * FROM nodes WHERE {where} ORDER BY created_at, rowid",
                params,
            ).fetchall()
        ]
    if kind in (None, "edge") and edge_filter is not None:
        where, params = edge_filter
        results += [
            ("edge", row)
            for row in conn.execute(
                f"SELECT rowid AS _rowid, * FROM edges WHERE {where} ORDER BY created_at, rowid",
                params,
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


def list_proposals(
    *,
    created_by: str | None = None,
    type: str | None = None,
    kind: str | None = None,
    created_before: str | None = None,
    created_after: str | None = None,
    limit: int = 500,
    path: str | Path | None = None,
) -> list[ProposalOut]:
    """List pending proposals (proposed nodes and edges), oldest first.

    Args:
        created_by: Filter by proposing actor (e.g. ``agent:researcher``).
        type: Filter by node/edge type id or name (applies within each kind).
        kind: ``"node"`` or ``"edge"`` to list one kind only (default: both).
        created_before: Only proposals created before this timestamp.
        created_after: Only proposals created after this timestamp.
        limit: Maximum proposals returned.
        path: Explicit database path.

    Returns:
        Proposals with reviewer context (edge endpoints, node parent).
    """
    if kind not in (None, "node", "edge"):
        raise ValueError(f"kind must be 'node' or 'edge', got {kind!r}")
    conn = _connect(path)
    try:
        rows = _proposal_rows(
            conn,
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
            else:
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
        return proposals
    finally:
        conn.close()


def _transition_many(
    ids: list[str],
    action: str,
    *,
    actor: str,
    reason: str | None,
    path: str | Path | None,
) -> BatchTransitionOut:
    """Transition many ids in one connection; bad ids are skipped, not fatal."""
    conn = _connect(path)
    try:
        transitioned: list[str] = []
        failed: list[TransitionFailure] = []
        for record_id in ids:
            try:
                _transition_row(conn, record_id, action, actor, reason=reason)
                transitioned.append(record_id)
            except (NodeNotFound, InvalidTransition) as exc:
                failed.append(TransitionFailure(id=record_id, error=str(exc)))
        conn.commit()
        return BatchTransitionOut(
            action=action, actor=actor, reason=reason, transitioned=transitioned, failed=failed
        )
    finally:
        conn.close()


def accept_proposals(
    ids: list[str], *, actor: str = ACTOR_HUMAN, path: str | Path | None = None
) -> BatchTransitionOut:
    """Accept proposed nodes/edges by id (proposed → active), one event each.

    Ids that are unknown or not ``proposed`` are collected in ``failed``; the
    rest still transition.
    """
    return _transition_many(ids, "accept", actor=actor, reason=None, path=path)


def reject_proposals(
    ids: list[str],
    *,
    reason: str,
    actor: str = ACTOR_HUMAN,
    path: str | Path | None = None,
) -> BatchTransitionOut:
    """Reject proposed nodes/edges by id (proposed → archived), one event each.

    The ``reason`` is recorded in every reject event's payload (design §8.1).
    Ids that are unknown or not ``proposed`` are collected in ``failed``.
    """
    return _transition_many(ids, "reject", actor=actor, reason=reason, path=path)


def _matching_ids(conn: sqlite3.Connection, *, kind: str | None, **filters: Any) -> list[str]:
    """Resolve a proposal filter to concrete ids (the batch-by-filter input)."""
    return [row["id"] for _, row in _proposal_rows(conn, kind=kind, **filters)]


def accept_matching(
    *,
    created_by: str | None = None,
    type: str | None = None,
    kind: str | None = None,
    created_before: str | None = None,
    created_after: str | None = None,
    actor: str = ACTOR_HUMAN,
    path: str | Path | None = None,
) -> BatchTransitionOut:
    """Accept every proposal matching the filters (e.g. one agent's whole run).

    The filter resolves to concrete ids first, then each id transitions with
    its own event — the batch is a convenience, never a silent bulk update.
    """
    if kind not in (None, "node", "edge"):
        raise ValueError(f"kind must be 'node' or 'edge', got {kind!r}")
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
    return _transition_many(ids, "accept", actor=actor, reason=None, path=path)


def reject_matching(
    *,
    reason: str,
    created_by: str | None = None,
    type: str | None = None,
    kind: str | None = None,
    created_before: str | None = None,
    created_after: str | None = None,
    actor: str = ACTOR_HUMAN,
    path: str | Path | None = None,
) -> BatchTransitionOut:
    """Reject every proposal matching the filters, recording ``reason``.

    The filter resolves to concrete ids first, then each id transitions with
    its own event carrying the reason.
    """
    if kind not in (None, "node", "edge"):
        raise ValueError(f"kind must be 'node' or 'edge', got {kind!r}")
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
    return _transition_many(ids, "reject", actor=actor, reason=reason, path=path)


# ── Policy CRUD (design §8.3) ─────────────────────────────────────────────────


def set_policy(
    agent: str,
    rules: list[dict[str, Any]],
    *,
    actor: str = ACTOR_HUMAN,
    path: str | Path | None = None,
) -> PolicyOut:
    """Create or replace one agent's policy ruleset, emitting ``policy.set``.

    Rules are validated (see :func:`_validate_rules`) and stored with edge
    types resolved to ids. Setting an empty ruleset disables the policy. The
    event payload carries the full before/after rulesets — policy grants
    write privileges, so every edit is audited with its actor.

    Args:
        agent: The actor string the policy governs (e.g. ``agent:researcher``).
        rules: The ruleset (list of rule objects).
        actor: Who is editing the policy.
        path: Explicit database path.

    Returns:
        The stored policy.
    """
    conn = _connect(path)
    try:
        validated = _validate_rules(conn, rules)
        existing = conn.execute("SELECT rules FROM policies WHERE agent = ?", (agent,)).fetchone()
        before = json.loads(existing["rules"]) if existing else None
        conn.execute(
            """
            INSERT INTO policies (agent, rules, updated_by, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(agent) DO UPDATE SET
                rules = excluded.rules,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """,
            (agent, json.dumps(validated, ensure_ascii=False), actor),
        )
        _emit(
            conn,
            actor,
            "policy.set",
            {"agent": agent, "before": before, "after": validated},
        )
        row = conn.execute("SELECT * FROM policies WHERE agent = ?", (agent,)).fetchone()
        conn.commit()
        return _policy_out(row)
    finally:
        conn.close()


def get_policy(agent: str, *, path: str | Path | None = None) -> PolicyOut:
    """Fetch one agent's policy.

    Raises:
        PolicyNotFound: If no policy is stored for ``agent``.
    """
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM policies WHERE agent = ?", (agent,)).fetchone()
        if row is None:
            raise PolicyNotFound(f"no policy for agent: {agent}")
        return _policy_out(row)
    finally:
        conn.close()


def list_policies(*, path: str | Path | None = None) -> list[PolicyOut]:
    """List every stored policy, ordered by agent."""
    conn = _connect(path)
    try:
        rows = conn.execute("SELECT * FROM policies ORDER BY agent").fetchall()
        return [_policy_out(row) for row in rows]
    finally:
        conn.close()


def undo(
    seq: int | None = None, *, actor: str = ACTOR_HUMAN, path: str | Path | None = None
) -> UndoResult:
    """Reverse one event (default: the latest non-undo event), restoring state.

    Uses the event's before/after payload: a create is reversed by deleting
    the created row (for nodes, along with its versions and incident edges —
    all recorded in the undo event's payload); any other mutation is reversed
    by writing the ``before`` state back. Undoing a node restore re-runs
    wikilink materialisation so the graph stays consistent with the restored
    content. The reversal itself is appended as an ``undo`` event; undo
    events cannot themselves be undone. Only graph events (``node.*`` /
    ``edge.*``) are reversible — audited non-graph events (``policy.set``)
    are skipped by default and refused when named explicitly.

    Raises:
        EventNotFound: If no event matches ``seq`` (or none exist to undo).
        ValueError: If the target event is an ``undo`` event or a non-graph
            event.
    """
    conn = _connect(path)
    try:
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
            conn.execute(
                f"UPDATE {table} SET {assignments} WHERE id = ?",
                (*[before[key] for key in columns], before["id"]),
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
            _materialize_mentions(conn, restored, actor)
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


def history(node_id: str, *, path: str | Path | None = None) -> list[VersionOut]:
    """Return a node's version snapshots in chronological order.

    Raises:
        NodeNotFound: If the id does not resolve.
    """
    conn = _connect(path)
    try:
        _get_node_row(conn, node_id)
        rows = conn.execute(
            "SELECT * FROM versions WHERE node_id = ? ORDER BY id", (node_id,)
        ).fetchall()
        return [
            VersionOut(
                id=row["id"],
                node_id=row["node_id"],
                title=row["title"],
                content=row["content"],
                props=json.loads(row["props"]),
                actor=row["actor"],
                event_seq=row["event_seq"],
                created_at=row["created_at"],
            )
            for row in rows
        ]
    finally:
        conn.close()


def list_events(*, limit: int = 50, path: str | Path | None = None) -> list[EventOut]:
    """Return the most recent events (newest first), capped at ``limit``."""
    conn = _connect(path)
    try:
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


def list_types(*, path: str | Path | None = None) -> TypesOut:
    """Return the full type catalog (node types and edge types)."""
    conn = _connect(path)
    try:
        node_rows = conn.execute("SELECT * FROM types ORDER BY name").fetchall()
        edge_rows = conn.execute("SELECT * FROM edge_types ORDER BY name").fetchall()
        return TypesOut(
            node_types=[
                TypeOut(
                    id=row["id"],
                    name=row["name"],
                    parent_type_id=row["parent_type_id"],
                    json_schema=json.loads(row["schema_json"]),
                    is_builtin=bool(row["is_builtin"]),
                )
                for row in node_rows
            ],
            edge_types=[
                EdgeTypeOut(
                    id=row["id"],
                    name=row["name"],
                    inverse_name=row["inverse_name"],
                    json_schema=json.loads(row["schema_json"]),
                    is_builtin=bool(row["is_builtin"]),
                )
                for row in edge_rows
            ],
        )
    finally:
        conn.close()
