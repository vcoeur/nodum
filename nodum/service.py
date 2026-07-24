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
import uuid
from pathlib import Path
from typing import Any

from nodum import db
from nodum.models import (
    BatchTransitionOut,
    DiffOut,
    EdgeOut,
    EdgeTypeOut,
    EventOut,
    InitResult,
    ItemFailure,
    NodeOut,
    PathOut,
    PolicyOut,
    ProposalOut,
    ProposeEdgesOut,
    SubgraphOut,
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

#: The transitions that *review* a proposal. They are the human's tier: they
#: turn proposed structure into live structure (and archive what it replaces),
#: so no ``agent:*`` actor may perform them (design §8.1/§8.2).
REVIEW_ACTIONS = ("accept", "reject")

#: Operations reserved to the ``human`` actor, each mapped to why it is not
#: delegable. They either write or remove **live** state directly, or (setting
#: a policy) grant the auto-accept privilege that does — precisely what an
#: agent may never reach on its own (design §8.1/§8.2). A review rule would
#: mean nothing if the same agent could reach the same live state through the
#: back door of an archive, an undo, or a self-granted auto-accept policy.
HUMAN_ONLY_ACTIONS = {
    "accept": "review is the human tier and is never delegated to an agent",
    "reject": "review is the human tier and is never delegated to an agent",
    "archive": "archiving retires live structure, which is the human's call",
    "undo": (
        "undo writes an event's prior payload back verbatim — including "
        "state 'active' — so delegating it would hand an agent the live state "
        "it may not write directly"
    ),
    "set a policy": (
        "a policy grants auto-accept — the privilege to land writes live "
        "without review — so an agent setting one would self-grant the direct "
        "write to live state the human tier exists to withhold"
    ),
}

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

#: Policy rule actions (design §8.3). Only ``auto_accept`` is evaluated on the
#: direct write path today; ``auto_apply``/``always_propose`` govern internal
#: agent jobs and are stored for the Phase-5 runtime.
POLICY_ACTIONS = ("auto_accept", "auto_apply", "always_propose")

#: Policy rule key opting a ``min_confidence`` gate in to grading the *agent's
#: own* self-reported confidence. Absent (the default), a gated rule never
#: auto-accepts on the direct write path — see :func:`_auto_accept_rule`.
TRUST_SELF_CONFIDENCE = "trust_self_reported_confidence"

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


class PolicyNotFound(LookupError):
    """Raised when no policy is stored for an agent."""


class VersionNotFound(RecordNotFound):
    """Raised when a version id does not resolve."""


class InvalidTransition(ValueError):
    """Raised when a state transition is not allowed from the current state."""


class ReviewNotPermitted(PermissionError):
    """Raised when a non-human actor tries to review, archive, undo, or set a policy.

    The human tier is :data:`HUMAN_ONLY_ACTIONS`: accepting or rejecting a
    proposal, archiving live state, undoing an event, and setting an agent
    policy (which grants auto-accept).
    """


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


def _require_human_reviewer(actor: str, action: str) -> None:
    """Refuse a :data:`HUMAN_ONLY_ACTIONS` operation by anything but the human.

    Every gated action reaches live state (design §8.1/§8.2). Accepting turns
    proposed structure into live structure — and archives whatever that
    structure replaces — whether the proposal is the actor's own or another
    agent's; archiving retires live structure; undo writes an event's prior
    payload back verbatim, which is how an agent could otherwise restore
    ``state = 'active'`` it was never allowed to write. Enforcing all of them
    here, at the choke point each one passes through, keeps the guarantee
    structural rather than adapter-deep.

    Args:
        actor: Who is performing the operation.
        action: The operation name (only :data:`HUMAN_ONLY_ACTIONS` are gated).

    Raises:
        ReviewNotPermitted: If ``action`` is gated and ``actor`` is not
            :data:`ACTOR_HUMAN`.
    """
    reason = HUMAN_ONLY_ACTIONS.get(action)
    if reason is not None and actor != ACTOR_HUMAN:
        raise ReviewNotPermitted(
            f"only the {ACTOR_HUMAN!r} actor may {action}; got {actor!r} — {reason}"
        )


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
    """Sync a node's ``[[wikilinks]]`` with its pending/active ``mentions`` edges.

    Creates a ``mentions`` edge (``created_by`` = writer) for every newly
    linked target, archives edges whose target text disappeared, and silently
    skips unresolvable targets (no dangling edges). Self-links are ignored.

    A materialised edge lands in the state its **actor** is allowed to write —
    ``active`` for the human, ``proposed`` for an agent (:func:`_default_state`).
    A wikilink is structure like any other: an agent writing ``[[Target]]``
    must not thereby attach live structure to someone else's active node; the
    pending edge goes live when a human accepts the proposing node
    (:func:`_activate_pending_mentions`) or the edge itself.

    Idempotent: re-running on unchanged content changes nothing, whichever
    state the existing edges are in — pending edges count as already
    materialised, so a later human write never duplicates them.
    """
    node = dict(node_row)
    targets = set(WIKILINK_RE.findall(node["content"] or ""))
    resolved = {
        dst
        for target in targets
        if (dst := _resolve_wikilink(conn, target)) is not None and dst != node["id"]
    }
    current = conn.execute(
        """
        SELECT * FROM edges
        WHERE src_id = ? AND type_id = 'mentions' AND state IN ('active', 'proposed')
        ORDER BY created_at, rowid
        """,
        (node["id"],),
    ).fetchall()
    current_by_dst = {edge["dst_id"]: edge for edge in current}
    state = _default_state(actor)

    for dst_id in sorted(resolved - set(current_by_dst)):
        _insert_edge(
            conn,
            src_id=node["id"],
            dst_id=dst_id,
            type_id="mentions",
            props={},
            confidence=None,
            actor=actor,
            state=state,
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
    ``action``, and — when present — a ``min_confidence`` in ``[0, 1]`` and a
    boolean :data:`TRUST_SELF_CONFIDENCE`. An ``edge_type`` must resolve
    against the catalog (it is stored as its id). Unknown extra keys pass
    through untouched.

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
        trusted = rule.get(TRUST_SELF_CONFIDENCE)
        if trusted is not None and not isinstance(trusted, bool):
            raise ValueError(
                f"policy rule {index}: {TRUST_SELF_CONFIDENCE} must be true or false, "
                f"got {trusted!r}"
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
    the internal runtime, not direct writes. A rule with no ``min_confidence``
    is an unconditional grant and matches on edge type alone.

    **A ``min_confidence`` gate grades untrusted input.** The only confidence
    available on the direct write path is the one the writing agent reports
    about its own write, so an agent that wants a write to land ``active`` can
    simply claim ``1.0`` — the gate is self-graded and, on its own, worth
    nothing. A gated rule therefore fires only when the policy explicitly opts
    in with ``"trust_self_reported_confidence": true``
    (:data:`TRUST_SELF_CONFIDENCE`); without that flag the gate can never be
    satisfied here and the edge stays ``proposed`` for human review. When a
    later phase supplies an independently measured confidence, it grades
    against the same gate without the opt-in.
    """
    row = conn.execute("SELECT rules FROM policies WHERE agent = ?", (actor,)).fetchone()
    if row is None:
        return None
    for rule in json.loads(row["rules"]):
        if rule.get("action") != "auto_accept" or rule.get("edge_type") != edge_type_id:
            continue
        gate = rule.get("min_confidence")
        if gate is None:
            return rule
        if rule.get(TRUST_SELF_CONFIDENCE) is not True:
            continue
        if confidence is not None and confidence >= gate:
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
        before = _row_dict(_get_node_row(conn, node_id))
        new_title = before["title"] if title is _UNSET else title
        new_content = before["content"] if content is _UNSET else content
        new_props = before["props"] if props is _UNSET else json.dumps(props, ensure_ascii=False)
        if actor != ACTOR_HUMAN:
            # Agent path: stage the edit as a proposed version (design §8.1).
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


def suggest_links(prefix: str, *, limit: int = 20, path: str | Path | None = None) -> list[NodeOut]:
    """Suggest ``[[wikilink]]`` targets whose title starts with ``prefix``.

    Backs the editor's ``[[`` autocomplete. It reads the ``nodes`` table
    directly rather than an index, so it answers on a database whose
    projectors have never run — an empty suggestion list always means "no such
    title", never "the index is cold".

    Matching folds case in Python (:meth:`str.casefold`) rather than in SQL:
    SQLite's ``LIKE`` and ``lower()`` fold ASCII only, and titles here are
    multilingual. Ranking puts titles that match the typed case first (typing
    ``Gra`` surfaces "Graph Theory" ahead of "grammar"), then sorts by title
    and id, so the same prefix always returns the same list.

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
        placeholders = ",".join("?" * len(SUGGEST_STATES))
        candidates = conn.execute(
            f"SELECT id, title FROM nodes WHERE title IS NOT NULL AND state IN ({placeholders})",
            SUGGEST_STATES,
        ).fetchall()
        folded = prefix.casefold()
        matches = [row for row in candidates if row["title"].casefold().startswith(folded)]
        matches.sort(
            key=lambda row: (
                not row["title"].startswith(prefix),
                row["title"].casefold(),
                row["title"],
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
) -> dict[str, Any]:
    """Validate and write one edge inside an open connection (no commit).

    Shared by :func:`create_edge` and :func:`propose_edges`. The landing
    state follows the actor rule unless the actor's policy auto-accepts the
    write (design §8.3).
    """
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
    return _insert_edge(
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
        row = _create_edge_in_conn(
            conn, src_id, dst_id, type, props=props, confidence=confidence, actor=actor
        )
        conn.commit()
        return _edge_out(row)
    finally:
        conn.close()


def propose_edges(
    suggestions: list[dict[str, Any]],
    *,
    actor: str = ACTOR_HUMAN,
    path: str | Path | None = None,
) -> ProposeEdgesOut:
    """Write a batch of edge suggestions, one event per edge (design §8.1).

    Each suggestion names ``src``, ``dst``, and ``edge_type``, plus optional
    ``props`` and ``confidence`` — the same inputs as :func:`create_edge`,
    including policy auto-accept. A malformed suggestion (missing key,
    unknown endpoint/type, bad confidence) lands in ``failed`` with its
    input index; the rest still write. One commit for the whole batch.

    Raises:
        ValueError: If ``suggestions`` is not a list of objects.
    """
    conn = _connect(path)
    try:
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
                    actor=actor,
                )
                created.append(_edge_out(row))
            except KeyError as exc:
                failed.append(ItemFailure(index=index, error=f"missing key: {exc.args[0]}"))
            except (NodeNotFound, TypeNotFound, ValueError) as exc:
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


def _transition_version(
    conn: sqlite3.Connection,
    before: dict[str, Any],
    action: str,
    actor: str,
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
            _materialize_mentions(conn, node_after, actor)
    else:  # reject
        conn.execute("UPDATE versions SET state = 'archived' WHERE id = ?", (version_id,))
        archived = _row_dict(_get_version_row(conn, version_id))
        payload: dict[str, Any] = {"before": before, "after": archived}
        if reason is not None:
            payload["reason"] = reason
        _emit(conn, actor, "version.reject", payload)
    return _row_dict(_get_version_row(conn, version_id))


def _transition_row(
    conn: sqlite3.Connection,
    record_id: str,
    action: str,
    actor: str,
    reason: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Apply one state transition inside an open connection (no commit).

    Returns:
        A ``(kind, after_row)`` pair where kind is ``"node"``, ``"edge"``, or
        ``"version"``.

    Raises:
        ReviewNotPermitted: If a non-human actor tries to transition anything.
        RecordNotFound: If the id resolves to neither a node, an edge, nor a
            version — the id alone does not say which kind was meant, so the
            base class is what is raised.
        InvalidTransition: If the transition is not allowed from the current
            state.
    """
    _require_human_reviewer(actor, action)
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
    if before["state"] != from_state:
        raise InvalidTransition(
            f"cannot {action} a {kind} in state {before['state']!r} (requires state {from_state!r})"
        )
    if kind == "version":
        # Versions only ever sit in `proposed`; accept applies, reject archives.
        return kind, _transition_version(conn, before, action, actor, reason=reason)
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
    actor: str = ACTOR_HUMAN,
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
        ReviewNotPermitted: If ``actor`` is not ``human``.
        RecordNotFound: If the id resolves to no node, edge, or version.
        InvalidTransition: If the transition is not allowed from the current
            state.
    """
    if action not in TRANSITIONS:
        raise ValueError(f"unknown transition {action!r}; expected one of {sorted(TRANSITIONS)}")
    conn = _connect(path)
    try:
        kind, after = _transition_row(conn, record_id, action, actor, reason=reason)
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
    if kind in (None, "update") and (filters.get("type") is None or node_type_id is not None):
        where, params = _update_proposal_filter(
            node_type_id,
            created_by=filters.get("created_by"),
            created_before=filters.get("created_before"),
            created_after=filters.get("created_after"),
        )
        results += [
            ("update", row)
            for row in conn.execute(
                "SELECT v.rowid AS _rowid, v.*, n.type_id AS node_type_id FROM versions v "
                "JOIN nodes n ON n.id = v.node_id "
                f"WHERE {where} ORDER BY v.created_at, v.rowid",
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
    actor: str,
    reason: str | None,
    path: str | Path | None,
) -> BatchTransitionOut:
    """Transition many ids in one connection; bad ids are skipped, not fatal.

    A refused reviewer is *not* a per-id failure: the whole batch raises
    before any id is touched, so an agent never gets a partially applied
    review.
    """
    _require_human_reviewer(actor, action)
    conn = _connect(path)
    try:
        transitioned: list[str] = []
        failed: list[TransitionFailure] = []
        for record_id in ids:
            try:
                _transition_row(conn, record_id, action, actor, reason=reason)
                transitioned.append(record_id)
            except (RecordNotFound, InvalidTransition) as exc:
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
    """Accept proposed nodes/edges/updates by id, one event each.

    Accepting an update (a proposed version id, given as a string) applies
    its staged fields to the node. Accepting a node also brings the pending
    ``mentions`` edges its wikilinks materialised to ``active``. Ids that are
    unknown or not ``proposed`` are collected in ``failed``; the rest still
    transition.

    Raises:
        ReviewNotPermitted: If ``actor`` is not ``human`` — review is the
            human tier, whoever filed the proposal.
    """
    return _transition_many(ids, "accept", actor=actor, reason=None, path=path)


def reject_proposals(
    ids: list[str],
    *,
    reason: str,
    actor: str = ACTOR_HUMAN,
    path: str | Path | None = None,
) -> BatchTransitionOut:
    """Reject proposed nodes/edges/updates by id, one event each.

    The ``reason`` is recorded in every reject event's payload (design §8.1).
    Ids that are unknown or not ``proposed`` are collected in ``failed``.

    Raises:
        ReviewNotPermitted: If ``actor`` is not ``human`` — an agent may not
            reject another agent's proposal any more than its own.
    """
    return _transition_many(ids, "reject", actor=actor, reason=reason, path=path)


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
    actor: str = ACTOR_HUMAN,
    path: str | Path | None = None,
) -> BatchTransitionOut:
    """Accept every proposal matching the filters (e.g. one agent's whole run).

    The filter resolves to concrete ids first, then each id transitions with
    its own event — the batch is a convenience, never a silent bulk update.

    Raises:
        ReviewNotPermitted: If ``actor`` is not ``human``.
    """
    _require_human_reviewer(actor, "accept")
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

    Raises:
        ReviewNotPermitted: If ``actor`` is not ``human``.
    """
    _require_human_reviewer(actor, "reject")
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

    A rule's optional ``min_confidence`` gate grades the confidence the agent
    reports about **its own** write — untrusted input the agent is free to
    inflate. A gated rule is therefore inert on the direct write path unless
    the same rule also carries ``"trust_self_reported_confidence": true``,
    which is how a human says in writing "I accept this agent's self-grading
    for this edge type". Set a gate without the flag and the write stays
    ``proposed``; see :func:`_auto_accept_rule`.

    Args:
        agent: The actor string the policy governs (e.g. ``agent:researcher``).
        rules: The ruleset (list of rule objects).
        actor: Who is editing the policy. Human-only: a policy grants
            auto-accept, so an agent setting one would self-grant the live
            write the human tier exists to withhold.
        path: Explicit database path.

    Returns:
        The stored policy.

    Raises:
        ReviewNotPermitted: If ``actor`` is not ``human``.
    """
    _require_human_reviewer(actor, "set a policy")
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

    Undo is the **human tier**: restoring an event's payload writes arbitrary
    prior state back, ``state = 'active'`` included, so an agent allowed to
    undo would be an agent allowed to write live state (design §8.1/§8.2).

    Reversal never cascades beyond what the event itself created: an event
    the graph has since grown past (a created node that now has children, a
    row a later undo already removed) is refused, not forced.

    Raises:
        ReviewNotPermitted: If ``actor`` is not ``human``.
        EventNotFound: If no event matches ``seq`` (or none exist to undo).
        UndoNotPossible: If the target row is gone or the reversal would have
            to delete rows the event never created.
        ValueError: If the target event is an ``undo`` event or a non-graph
            event.
    """
    _require_human_reviewer(actor, "undo")
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

    Proposed and rejected updates appear alongside applied snapshots, marked
    by their ``state``.

    Raises:
        NodeNotFound: If the id does not resolve.
    """
    conn = _connect(path)
    try:
        _get_node_row(conn, node_id)
        rows = conn.execute(
            "SELECT * FROM versions WHERE node_id = ? ORDER BY id", (node_id,)
        ).fetchall()
        return [_version_out(row) for row in rows]
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


# ── Curated graph reads (design §8.1 read tier — no query DSL, per T2) ───────


def get_schema(type: str, *, path: str | Path | None = None) -> TypeOut | EdgeTypeOut:
    """Fetch one type's catalog entry (node types checked first, then edges).

    Raises:
        TypeNotFound: If the id/name resolves in neither catalog.
    """
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM types WHERE id = ? OR name = ?", (type, type)).fetchone()
        if row is not None:
            return TypeOut(
                id=row["id"],
                name=row["name"],
                parent_type_id=row["parent_type_id"],
                json_schema=json.loads(row["schema_json"]),
                is_builtin=bool(row["is_builtin"]),
            )
        row = conn.execute(
            "SELECT * FROM edge_types WHERE id = ? OR name = ?", (type, type)
        ).fetchone()
        if row is None:
            raise TypeNotFound(f"unknown node or edge type: {type}")
        return EdgeTypeOut(
            id=row["id"],
            name=row["name"],
            inverse_name=row["inverse_name"],
            json_schema=json.loads(row["schema_json"]),
            is_builtin=bool(row["is_builtin"]),
        )
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Breadth-first walk over ``active`` edges; returns (node rows, edge rows).

    The root comes first in the node list; every edge the walk crossed is
    returned once (including edges between two visited nodes). Proposed and
    archived edges are never followed — reads default to the live graph.
    """
    nodes: dict[str, dict[str, Any]] = {start_id: _row_dict(_get_node_row(conn, start_id))}
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
        sql = f"SELECT * FROM edges WHERE state = 'active' AND {where}"
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
    node_id: str, *, depth: int = 1, path: str | Path | None = None
) -> SubgraphOut:
    """Return a node plus its active-edge neighborhood out to ``depth`` hops.

    Depth 0 returns the node alone. Design §8.1 ``get_node(id, depth)``.

    Raises:
        NodeNotFound: If the id does not resolve.
        ValueError: If ``depth`` is negative.
    """
    if depth < 0:
        raise ValueError(f"depth must be >= 0, got {depth}")
    conn = _connect(path)
    try:
        nodes, edges = _walk(conn, node_id, type_ids=None, depth=depth, direction="both")
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
        type_ids = (
            [_resolve_edge_type(conn, edge_type) for edge_type in edge_types]
            if edge_types
            else None
        )
        nodes, edges = _walk(conn, start_id, type_ids=type_ids, depth=depth, direction=direction)
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
    limit: int = 200,
    path: str | Path | None = None,
) -> SubgraphOut:
    """Walk a bounded, server-side-filtered neighborhood of one node.

    The filtered sibling of :func:`traverse`, written for clients that render
    a graph: every filter is applied in the database and **the node cap is
    enforced during the walk, not after it**. The walk stops admitting nodes
    the moment ``limit`` is reached, so no caller — a browser least of all —
    can make this materialise an unbounded graph and slice it afterwards.
    ``truncated`` on the result says whether the cap actually bit.

    The filters compose as one conjunction. An edge is followed only if it
    passes edge state **and** type **and** ``min_confidence`` **and**
    ``created_by``; a node is admitted only if it also passes ``node_types``.
    An edge to a node the type filter excludes is dropped with it, so the
    result never contains an edge pointing at a node it does not carry. The
    root is always present and is exempt from ``node_types`` — it is what was
    asked for, not something the walk found. A ``min_confidence`` floor drops
    edges with no stated confidence: unstated is not "meets the bar", the same
    reading the policy gate takes (:func:`_auto_accept_rule`).

    The walk is breadth-first and undirected (edges count in either
    direction); within a level, edges are taken in ``(created_at, rowid)``
    order and nodes appear in the order first reached, so the same call always
    returns the same subgraph.

    Args:
        root_id: Node at the centre of the subgraph.
        depth: Maximum hops from the root (0 returns the root alone).
        edge_types: Edge type ids/names the walk may follow (default: any).
        edge_states: Edge states the walk may follow (default:
            :data:`DEFAULT_EDGE_STATES`, the live graph).
        min_confidence: Floor on an edge's stored confidence.
        created_by: Only follow edges written by this actor.
        node_types: Node type ids/names that may be admitted (default: any).
        limit: Hard cap on the number of nodes returned, root included.
        path: Explicit database path.

    Returns:
        The subgraph, with ``truncated`` true when the cap stopped the walk
        before it ran out of graph.

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
    states = tuple(edge_states) if edge_states else DEFAULT_EDGE_STATES
    for state in states:
        if state not in STATES:
            raise ValueError(f"state must be one of {STATES}, got {state!r}")
    if min_confidence is not None and not 0 <= min_confidence <= 1:
        raise ValueError(f"min_confidence must be between 0 and 1, got {min_confidence}")
    conn = _connect(path)
    try:
        edge_clauses = [f"state IN ({','.join('?' * len(states))})"]
        edge_params: list[Any] = list(states)
        if edge_types:
            type_ids = [_resolve_edge_type(conn, edge_type) for edge_type in edge_types]
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

        nodes: dict[str, dict[str, Any]] = {root_id: _row_dict(_get_node_row(conn, root_id))}
        order = [root_id]
        edges: list[dict[str, Any]] = []
        seen_edges: set[str] = set()
        truncated = False
        frontier = {root_id}
        for _level in range(depth):
            if not frontier:
                break
            placeholders = ",".join("?" * len(frontier))
            sql = (
                f"SELECT * FROM edges WHERE (src_id IN ({placeholders}) "
                f"OR dst_id IN ({placeholders})) AND {' AND '.join(edge_clauses)} "
                "ORDER BY created_at, rowid"
            )
            next_frontier: set[str] = set()
            for edge_row in conn.execute(sql, sorted(frontier) * 2 + edge_params).fetchall():
                edge = _row_dict(edge_row)
                if edge["id"] in seen_edges:
                    continue
                # One endpoint is in the frontier, so at most one is new.
                far = edge["dst_id"] if edge["src_id"] in nodes else edge["src_id"]
                if far not in nodes:
                    row = _row_dict(_get_node_row(conn, far))
                    if admissible_types is not None and row["type_id"] not in admissible_types:
                        continue  # excluded node — its edge would dangle
                    if len(nodes) >= limit:
                        truncated = True
                        continue  # the cap bites here, mid-walk
                    nodes[far] = row
                    order.append(far)
                    next_frontier.add(far)
                seen_edges.add(edge["id"])
                edges.append(edge)
            frontier = next_frontier
        return SubgraphOut(
            root=root_id,
            depth=depth,
            nodes=[_node_out(nodes[node_id]) for node_id in order],
            edges=[_edge_out(row) for row in edges],
            truncated=truncated,
        )
    finally:
        conn.close()


def find_path(a_id: str, b_id: str, *, path: str | Path | None = None) -> PathOut:
    """Find the shortest path between two nodes over active edges (any type).

    Breadth-first, direction-agnostic. When no path exists, ``found`` is
    false and both lists are empty.

    Raises:
        NodeNotFound: If either id does not resolve.
    """
    conn = _connect(path)
    try:
        _get_node_row(conn, a_id)
        _get_node_row(conn, b_id)
        if a_id == b_id:
            node = _node_out(_get_node_row(conn, a_id))
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
            rows = conn.execute(
                "SELECT * FROM edges WHERE state = 'active' "
                f"AND (src_id IN ({placeholders}) OR dst_id IN ({placeholders})) "
                "ORDER BY created_at, rowid",
                sorted(frontier_set) * 2,
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


def diff_versions(a: int, b: int, *, path: str | Path | None = None) -> DiffOut:
    """Diff two versions of one node (design §8.1 ``diff(a, b)``).

    Raises:
        VersionNotFound: If either version id does not resolve.
        ValueError: If the versions belong to different nodes.
    """
    conn = _connect(path)
    try:
        version_a = _row_dict(_get_version_row(conn, a))
        version_b = _row_dict(_get_version_row(conn, b))
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
