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

import contextvars
import difflib
import json
import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import ValidationError

from nodum import auth, db
from nodum.migrations import BUILTIN_AGENT_PREFIX, GARDENER_AGENT_ID, MAIN_SPACE_ID, META_SPACE_ID
from nodum.models import (
    AgentCreatedOut,
    AgentOut,
    AnnotationOut,
    BatchTransitionOut,
    BulkRelinkOut,
    CycleOut,
    DiffOut,
    EdgeOut,
    EdgeSuggestionIn,
    EdgeTypeOut,
    EventOut,
    GrantOut,
    HumanOut,
    InitResult,
    ItemFailure,
    MergeOut,
    MergeRedirectOut,
    NodeOut,
    PathOut,
    ProposalOut,
    ProposeEdgesOut,
    RelinkDiff,
    RetiredEdgeOut,
    RetypeOut,
    RollbackBlockerOut,
    RollbackConflictOut,
    RollbackOut,
    SpaceOut,
    SubgraphOut,
    SupersedeOut,
    TransitionFailure,
    TypeOut,
    TypesOut,
    UndoResult,
    VersionOut,
)
from nodum.principal import EDIT, READ, SUGGEST, Principal
from nodum.store import GrantNotPermitted, Store, require_landing_state
from nodum.vocab import (
    CONSOLIDATION_TRIGGERS,
    CYCLE_CLOSED_STATUSES,
    CYCLE_STATUSES,
    CYCLE_TRIGGERS,
    DEFAULT_EDGE_STATES,
    DIRECTIONS,
    GRANT_LEVEL_NAMES,
    REVIEW_ACTIONS,
    STATES,
    SUGGEST_STATES,
    TRANSITIONS,
    AgentKind,
    CycleTrigger,
    Direction,
    GrantLevel,
    LandingState,
    NodeState,
    ProposalKind,
    RollbackKind,
    TransitionAction,
    TransitionKind,
)

#: Allowed state values shared by nodes and edges (vocab: :data:`STATES`).
STATES = STATES

#: State transitions: action → (required current state, resulting state).
TRANSITIONS = TRANSITIONS

#: The transitions that *review* a proposal. Reviewing turns proposed
#: structure into live structure (and archives what it replaces), so it needs
#: a human — or an ``edit`` grant on the item's space (Q13 note 03 Q1).
REVIEW_ACTIONS = REVIEW_ACTIONS

#: The node fields a version snapshots, and the only fields a proposed update
#: may name.
VERSION_FIELDS = ("title", "content", "props")

#: Node states :func:`suggest_links` draws link targets from. ``proposed``
#: stays in, as it does for every other node read; ``archived`` is out,
#: because a retired node is not something to link to.
SUGGEST_STATES = SUGGEST_STATES

#: Edge states :func:`subgraph` follows when the caller names none — the live
#: graph, matching every other traversal (design §8.1).
DEFAULT_EDGE_STATES = DEFAULT_EDGE_STATES

#: Ceiling on :func:`subgraph`'s node cap. A caller's ``limit`` is clamped to
#: it rather than refused, so a query string cannot turn the bounded read into
#: an unbounded one; ``truncated`` reports the clamp like any other cap.
MAX_SUBGRAPH_LIMIT = 2000

#: Edges :func:`subgraph` may return per node it is allowed to admit. The node
#: cap bounds nodes only — one pair of nodes can carry any number of edges — so
#: the edge list needs its own bound, derived from ``limit`` so that one
#: argument still describes the size of the whole result.
SUBGRAPH_EDGE_FACTOR = 8

#: The spaces the schema creates and the system depends on, mapped to why
#: archiving one is refused. Both are refused *here*, in the service, so every
#: surface inherits it: a disabled button on one screen leaves the CLI and the
#: API wide open, and archiving `main` is destructive in the quietest possible
#: way — `list_spaces` returns active spaces only, so it disappears from every
#: picker, while `resolve_space_id(None)` keeps returning `main` without ever
#: reading the row's state, so writes go on landing in a space the human can
#: no longer see or name. Undo is not the answer to that, though it *can*
#: reverse an archive (it restores the `before` row past `TRANSITIONS`): it
#: reverses one named event, and this is a failure that reports nothing at the
#: moment it happens — by the time anyone notices where their writes went, the
#: event is buried under everything that kept arriving. Refusing the archive is
#: the only guard that fires while somebody is still watching.
STRUCTURAL_SPACE_IDS: dict[str, str] = {
    MAIN_SPACE_ID: (
        "it is where every write that names no space lands, and that default resolves by id "
        "whatever state the row is in — archiving it would hide the space while nodes kept "
        "arriving in it"
    ),
    META_SPACE_ID: (
        "it is the space that spaces themselves live in, along with the whole type vocabulary — "
        "archiving it would retire the space holding every other space"
    ),
}

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


class AccountExists(ValueError):
    """Raised when an account id is already taken (a duplicate ``agent create``)."""


class SpaceNameTaken(ValueError):
    """Raised when another space already answers to a proposed space name.

    A conflict rather than a bad request — the sibling of
    :class:`AccountExists`, and mapped to the same 409: the name is well-formed
    and the caller may retry with another one. Derives from ``ValueError`` so
    the CLI keeps reporting it as the refusal it always was.
    """


class InvalidTransition(ValueError):
    """Raised when a state transition is not allowed from the current state."""


class UndoNotPossible(ValueError):
    """Raised when an event cannot be reversed against the current graph."""


class CycleInProgress(ValueError):
    """Raised when a consolidation cycle is asked for while one is running.

    It lives here rather than in :mod:`nodum.consolidate` because the guard does:
    the refusal comes from the ``cycles`` row a second opener cannot insert, and
    that row is what makes the rule hold **across processes** rather than within
    one. :mod:`nodum.consolidate` re-exports the class, so
    ``consolidate.CycleInProgress`` — which ``http_api.EXCEPTION_STATUS`` maps to
    409 and which every caller catches — is this exact class and not a second
    one that would silently stop matching.

    A :class:`ValueError` so every adapter already reports it as one line and a
    status rather than a traceback: the CLI's ``_run`` catches it, and a bare
    ``ValueError`` would render as 400. The refusal is about current state rather
    than about the request, which is why that table carries its own **409** row
    for this class — the shape :class:`RollbackConflict` already had — and why
    the row lives there, not here: a domain module does not know about statuses.
    """


class RollbackConflict(UndoNotPossible):
    """Raised when a cycle cannot be rolled back because the graph moved on.

    Decision C4: rollback is atomic and **refuses rather than clobbers**. If
    anything outside the cycle has touched a row the cycle touched, reversing
    the cycle would overwrite that later work — the failure shape this project
    has already closed twice — so nothing is written and this names what is in
    the way. ``conflicts`` carries the whole list as
    :class:`~nodum.models.RollbackConflictOut` rows; the message names the first
    few, because a human told which four nodes are in the way can act and one
    told "rollback failed" cannot.

    A subclass of :class:`UndoNotPossible` on purpose rather than a sibling: it
    is the same 409 ("the graph has grown past the thing you are reversing"),
    so every surface that already handles one handles this.
    """

    def __init__(self, message: str, conflicts: list[RollbackConflictOut]) -> None:
        super().__init__(message)
        self.conflicts = conflicts


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


def _resolve_node_type(conn: sqlite3.Connection, type_ref: str, principal: Principal) -> str:
    """Resolve a node-type id or name to its id, or raise :class:`TypeNotFound`.

    Types are nodes (Q13, migration ``0009``): a node type is an active node
    whose own type is the ``type`` metaclass root, distinguished from edge
    types by ``type_kind`` in props. A type in a space the principal cannot
    read does not resolve — the catalog is not a leak channel either, which
    is why the principal is required rather than optional (Q13 review N1).
    """
    row = conn.execute(
        "SELECT id, space_id FROM nodes WHERE (id = ? OR title = ?) AND type_id = 'type'"
        " AND json_extract(props, '$.type_kind') = 'node' AND state = 'active'",
        (type_ref, type_ref),
    ).fetchone()
    if row is None or principal.level_on(row["space_id"]) < READ:
        raise TypeNotFound(f"unknown node type: {type_ref}")
    return row["id"]


def _resolve_edge_type(
    conn: sqlite3.Connection, type_ref: str, principal: Principal
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
    if row is None or principal.level_on(row["space_id"]) < READ:
        raise TypeNotFound(f"unknown edge type: {type_ref}")
    return row["id"], row["space_id"]


def _resolve_space(conn: sqlite3.Connection, space_ref: str, principal: Principal) -> str:
    """Resolve a space id or name to its id, or raise :class:`TypeNotFound`.

    Spaces are nodes of builtin type ``space`` (Q13 note 03 Q7). A space the
    principal holds no grant on does not resolve, so an existing-but-ungranted
    space and a nonexistent one answer identically (Q13 review S3) — the
    default-deny rule in :mod:`nodum.store`. ``GrantNotPermitted`` is then
    reserved for spaces the principal can genuinely see.
    """
    row = conn.execute(
        "SELECT id, space_id FROM nodes WHERE (id = ? OR title = ?) AND type_id = 'space'"
        " AND state = 'active'",
        (space_ref, space_ref),
    ).fetchone()
    if row is None or principal.level_on(row["id"]) < READ:
        raise TypeNotFound(f"unknown space: {space_ref}")
    return row["id"]


def _resolve_space_for_admin(conn: sqlite3.Connection, space_ref: str) -> tuple[str, str]:
    """Resolve a space id or name to ``(id, state)`` **whatever its state**.

    :func:`_resolve_space` matches ``state = 'active'``, which is right for
    every read and write that names a space: an archived space is out of the
    vocabulary. It is wrong for grant administration, and that left a real hole
    — archiving a space made its grants unrevokable, because :func:`revoke`
    could no longer resolve the space they were on. An authority with no
    supported route to remove it is a bug whichever way the inertness question
    goes, and raw SQL was the only way out.

    Deliberately separate from :func:`_resolve_space` rather than a flag on it:
    the active-only rule is what keeps a space reference from telling an
    ungranted space apart from a nonexistent one, and a flag is how that gets
    switched off by accident. **Human-only callers only** — both callers gate on
    :func:`_admin_actor` first, so naming an archived space back is not a leak;
    humans are unfiltered and can already list every space node.

    Args:
        conn: Open connection.
        space_ref: Space id or title.

    Returns:
        ``(space_id, state)``.

    Raises:
        TypeNotFound: If the reference resolves to no space at all.
    """
    row = conn.execute(
        "SELECT id, state FROM nodes WHERE (id = ? OR title = ?) AND type_id = 'space'",
        (space_ref, space_ref),
    ).fetchone()
    if row is None:
        raise TypeNotFound(f"unknown space: {space_ref}")
    return row["id"], row["state"]


def _require_space_lives_in_meta(space_id: str) -> None:
    """Refuse a ``space``-typed node anywhere but the meta space.

    "A space is a node of builtin type ``space`` living in meta" is the model
    every adapter is written against — :func:`create_space` hardcodes it, and
    :func:`list_spaces` and ``GET /api/spaces`` list whatever carries the type
    regardless of where it sits. The generic ``create_node`` was the one path
    that could put a space somewhere else, and a space living inside an
    ordinary space is incoherent twice over: it is listed and resolved as real
    territory while the grants that govern it are the *host* space's.

    It is also what made :func:`_require_space_name_free` an existence oracle.
    That check is deliberately unscoped, on the premise that only a principal
    that could already list every space can reach it; the premise held for
    creates (:func:`_resolve_node_type` needs READ on meta to resolve the
    ``space`` type, and a meta reader used to list every space) but not for
    renames, which are gated on SUGGEST on the space the node itself lives in.
    A ``space``-typed node in ``main`` therefore let a principal holding
    nothing but ``main`` rename it onto a name and learn from the refusal that
    some space it cannot list holds it — plus that space's id. Keeping spaces
    in meta restores the premise instead of weakening the refusal, which has
    to keep naming an archived holder to be usable at all. The premise is no
    longer inherited from the placement: a space node now resolves through its
    own id, not through ``meta`` (M3), so a meta reader sees only the space
    nodes it holds grants on — and :func:`_require_space_name_free` therefore
    checks the premise itself rather than trusting it.

    Migration ``0013``'s index stays deliberately unscoped to meta, and this
    does not contradict it: the index is the backstop for writers that never
    pass through here (raw SQL, a future adapter), so it covers every space
    node wherever it sits.

    Args:
        space_id: The target space the ``space``-typed node would land in.

    Raises:
        ValueError: If ``space_id`` is not the meta space.
    """
    if space_id != META_SPACE_ID:
        raise ValueError(
            f"a space must live in the {META_SPACE_ID!r} space, not {space_id!r}: spaces are the "
            "vocabulary every other space is named from, and one nested inside ordinary territory "
            "would be listed and resolved as real while being governed by its host's grants"
        )


def _require_space_name_free(
    conn: sqlite3.Connection,
    name: str | None,
    principal: Principal,
    *,
    exclude_id: str | None = None,
) -> None:
    """Refuse a space name another space already answers to, whatever its state.

    The predicate is :func:`_resolve_space`'s own — ``id = ? OR title = ?`` over
    space nodes — because that is what makes a duplicate harmful: two rows
    answering to one reference means ``--space research`` resolves to whichever
    one SQLite reached first. It is deliberately **not** narrowed to live rows.
    That was the first shape of this rule, on the argument that an archived
    space stops resolving so its name is free again; the argument assumed
    nothing ever un-archives, and :func:`undo` does — it restores the ``before``
    row with a raw UPDATE, past ``TRANSITIONS``. A freed name that had been
    re-taken meanwhile turned that undo into an ``IntegrityError``. **A space
    title is reserved forever**, so a restore can never fail; the cost, taken
    knowingly, is that a retired space's name cannot be reused.

    An archived holder gets its own sentence, and both name the space. Nothing
    lists archived spaces — :func:`list_spaces` and ``GET /api/spaces`` return
    active ones — so a human would otherwise be refused a name held by
    something they cannot see anywhere. Naming the holder is not an existence
    oracle only for a principal that can already list every space, and that
    premise is checked here rather than trusted: the search spans every space
    in the file regardless of scope, so the principal it answers has to be one
    that could run the search itself. Reading meta used to buy that — a meta
    reader could list every space node, archived ones included — but a space
    node now resolves through its own id (M3), so a meta reader with a partial
    grant set sees only the spaces it holds grants on. An agent that cannot
    list every space is refused outright, identically for every name, so a
    probe of a taken name and a free one answer the same — and the refusal
    itself names no space at all.

    That premise is checked here rather than at each call site, because this
    is the function that discloses. `create_node` is safe by construction
    (resolving the ``space`` type needs READ on meta, and the grant gate below
    needs every space), but a rename is gated on SUGGEST on the space the node
    *already lives in* — and a ``space``-typed node sitting in ``main``
    therefore let a principal holding nothing but ``main`` read a confirm/deny,
    plus the holder's id, for a space it cannot list.
    :func:`_require_space_lives_in_meta` stops new ones being made; this check
    covers the rows a database already holds, and covers the accept path
    (:func:`_transition_version`) where the reviewer need not be the proposer.
    The refusal is on the **grant**, so it reads identically whether or not the
    name is taken — weakening the message was not the alternative, since it has
    to keep naming an archived holder, the one space no listing shows. Freeing
    that name means renaming it, an ordinary :func:`update_node` by id —
    :func:`rename_space` will not reach it, since :func:`_resolve_space`
    matches ``active`` only.

    Migration ``0013_unique_space_titles`` is the structural half of this and
    holds every path, including a raw ``update_node`` on a space node; this
    check is what turns the collision into one sentence instead of an
    ``IntegrityError``, and it additionally catches the half an index cannot
    express — a title equal to some *other* space's id.

    Args:
        conn: Open connection.
        name: The proposed name; ``None`` (an untitled space) is always free.
        principal: Who is asking — must be able to list every space in the
            file, since the answer describes all of them.
        exclude_id: The space being renamed, so it never clashes with itself.

    Raises:
        GrantNotPermitted: If ``principal`` cannot read the meta space, or —
            for an agent — cannot list every space in the file.
        SpaceNameTaken: If any other space already answers to ``name``.
    """
    if principal.level_on(META_SPACE_ID) < READ:
        raise GrantNotPermitted(
            f"{principal.actor_string} has no read grant on space {META_SPACE_ID!r}: naming a "
            "space is naming part of the vocabulary every space is named from, so it takes a "
            "grant on the space that holds it"
        )
    if not principal.is_human:
        # The search below spans every space in the file, and the refusal names
        # the holder — so the caller must be able to list every space itself.
        # Reading meta no longer buys that (M3): a space node is visible only
        # to a principal granted on that space, so a meta reader with a partial
        # grant set could probe create/rename refusals for spaces it cannot
        # see. The gate is on the grant, so it reads identically whether or not
        # any space holds the probed name.
        readable = principal.read_spaces or frozenset()
        if readable:
            hidden = conn.execute(
                "SELECT 1 FROM nodes WHERE type_id = 'space' AND id NOT IN ("
                + ",".join("?" * len(readable))
                + ") LIMIT 1",
                sorted(readable),
            ).fetchone()
        else:
            hidden = conn.execute("SELECT 1 FROM nodes WHERE type_id = 'space' LIMIT 1").fetchone()
        if hidden is not None:
            raise GrantNotPermitted(
                f"{principal.actor_string} may not name a space: the name check spans every "
                "space in the file, so only a principal that can already list every space may "
                "run it — anything less could learn from the refusal that a space it cannot "
                "see exists"
            )
    if name is None:
        return
    # `IS NOT` rather than `!=` so a None exclusion compares against NULL.
    row = conn.execute(
        "SELECT id, state FROM nodes WHERE type_id = 'space'"
        " AND (id = ? OR title = ?) AND id IS NOT ?",
        (name, name, exclude_id),
    ).fetchone()
    if row is None:
        return
    if row["state"] == "archived":
        raise SpaceNameTaken(
            f"an archived space already answers to {name!r} (id {row['id']}): archiving a space "
            "keeps its name reserved, so that restoring it can never collide with a newer space "
            "— the name is not free again. Give the new space another name, or rename the "
            "archived one to release it."
        )
    raise SpaceNameTaken(
        f"a space already answers to {name!r} (id {row['id']}): a space reference resolves by id "
        "or by title, so the two could not be told apart"
    )


def _create_op(state: NodeState) -> str:
    """Name a create-op after the state it lands in (``create`` vs ``propose``)."""
    return "create" if state == "active" else "propose"


def resolve_space_id(
    space: str | None, *, principal: Principal, path: str | Path | None = None
) -> str:
    """Resolve a space id or name to its id — the public form of the write path's rule.

    ``None`` is the ``main`` space, exactly as every write defaults it. The
    asset pipeline needs this *before* it writes anything, to ask whether a
    space already describes some bytes; without it a caller would have to
    reimplement the resolution and would drift off the rule that an ungranted
    space and a nonexistent one answer identically (Q13 review S3).

    Args:
        space: Space id or name; ``None`` for the default space.
        principal: Who is asking — a space they hold no grant on does not resolve.
        path: Explicit database path.

    Returns:
        The space's node id.

    Raises:
        TypeNotFound: If the reference resolves to no space the principal can see.
    """
    if space is None:
        return MAIN_SPACE_ID
    conn = _connect(path)
    try:
        return _resolve_space(conn, space, principal)
    finally:
        conn.close()


def require_write_grant(
    space_id: str, *, principal: Principal, path: str | Path | None = None
) -> None:
    """Refuse an ingestion whose describing nodes the principal could never write.

    The ingestion pipeline resolves the target space and then stores the bytes;
    this probe is what makes the write grant part of that pre-storage
    resolution. Registration is the irreversible half of ingestion — there is no
    delete route — so a principal whose grant would refuse the node write must
    be refused here, before any byte is stored.

    It asks the **same question the write itself asks**: a
    :class:`~nodum.store.Store` probe of :meth:`Store.landing_state`, the exact
    call :func:`create_node` makes with the same ``principal``. A probe that
    disagreed with the write would be worse than none — this one cannot.

    Args:
        space_id: The resolved target space id.
        principal: Who would be writing.
        path: Explicit database path.

    Raises:
        GrantNotPermitted: If the principal may not write the space.
    """
    conn = _connect(path)
    try:
        Store(conn, principal).landing_state(space_id)
    finally:
        conn.close()


def require_type_read(
    type_ref: str, *, principal: Principal, path: str | Path | None = None
) -> None:
    """Refuse an ingestion whose describing nodes' types the principal cannot read.

    The describing nodes an ingestion writes are typed ``asset_ref`` and
    ``source``, which live in the ``meta`` space — so a principal that can
    write a space but cannot read ``meta`` would resolve no type at all. That
    refusal used to surface inside :func:`find_by_asset_hash` and
    :func:`create_node`, after the bytes were already stored; this probe moves
    it before :func:`~nodum.assets.register_asset`, next to the write-grant
    probe, so the two no-bytes refusals of ingestion — a missing write grant
    and an unresolvable type — both land before anything is irreversible.

    It asks the same question the write asks: :func:`_resolve_node_type` with
    the same ``principal``, the exact resolution :func:`create_node` performs.

    Args:
        type_ref: The node-type id or name the pipeline needs to resolve.
        principal: Who would be writing.
        path: Explicit database path.

    Raises:
        TypeNotFound: If the principal cannot resolve the type.
    """
    conn = _connect(path)
    try:
        _resolve_node_type(conn, type_ref, principal)
    finally:
        conn.close()


def find_by_asset_hash(
    asset_hash: str,
    *,
    type: str,
    space_id: str,
    principal: Principal,
    path: str | Path | None = None,
) -> NodeOut | None:
    """Find the live node of ``type`` in ``space_id`` carrying ``asset_hash`` in props.

    The idempotency lookup the ingestion pipeline runs before it writes: it is
    what makes re-ingesting the same file converge on the existing subgraph
    instead of tripping 0009's one-``asset_ref``-per-``(hash, space)`` index.

    Archived rows are skipped deliberately, and for the same reason that index
    skips them — retiring a describing node frees its hash for a fresh
    ingestion of the same bytes.

    Args:
        asset_hash: The sha256 carried in the node's ``asset_hash`` prop.
        type: Node-type id or name (``asset_ref`` or ``source`` in practice).
        space_id: The space to look in — already resolved.
        principal: Who is asking; a node outside the read set is not found.
        path: Explicit database path.

    Returns:
        The matching node, or ``None``.
    """
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        scope, params = store.node_scope()
        row = conn.execute(
            "SELECT * FROM nodes WHERE type_id = ? AND space_id = ?"
            " AND json_extract(props, '$.asset_hash') = ?"
            f" AND state != 'archived'{scope} ORDER BY created_at, rowid LIMIT 1",
            (_resolve_node_type(conn, type, principal), space_id, asset_hash, *params),
        ).fetchone()
        return _node_out(row) if row is not None else None
    finally:
        conn.close()


# ── Event log and versions ────────────────────────────────────────────────────

#: The consolidation cycle the events emitted on this thread belong to.
#:
#: A cycle is the unit a human takes back **whole**, so every event a cycle
#: produces has to carry its id — and a single missed one is a silently
#: unstamped event that rollback would leave behind and that :func:`undo` would
#: then happily reverse on its own. A ``cycle_id=`` parameter threaded through
#: every public signature is the shape that gets missed: consolidation calls
#: ordinary operations (:func:`create_node`, :func:`create_edge`,
#: :func:`transition`), and each of those calls more. A context variable has no
#: call site to forget, because :func:`_emit` is the only writer to the log and
#: it reads the variable itself.
#:
#: A ``ContextVar`` rather than a module global: it is per-thread and
#: per-task, so a second writer in the same process — the HTTP server handling
#: an ordinary request while a cycle runs — cannot be stamped by it.
_CURRENT_CYCLE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nodum_current_cycle", default=None
)


@contextmanager
def in_cycle(cycle_id: str) -> Iterator[None]:
    """Stamp every event emitted inside the block with ``cycle_id``.

    The consolidation runner wraps its work in this, and then calls the
    ordinary public service functions: nothing inside them mentions a cycle,
    and every event they append carries the id anyway.

    The variable is reset in a ``finally``, which is the whole safety argument.
    A leaked cycle id would stamp the writes that came *after* the cycle — an
    ordinary human edit made through the same process — and
    :func:`undo` refuses a cycle-stamped event by design, so those writes would
    become un-undoable, on a graph where the only route back is a rollback of a
    cycle they were never part of.

    Args:
        cycle_id: The open cycle's id (from :func:`open_cycle`).

    Yields:
        Nothing; the block's events are what changes.
    """
    token = _CURRENT_CYCLE.set(cycle_id)
    try:
        yield
    finally:
        _CURRENT_CYCLE.reset(token)


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
        cycle_id: Consolidation-cycle grouping. Resolved **explicit argument
            first, then the ambient cycle** (:func:`in_cycle`), so a caller
            that names one wins and everything else running inside a cycle is
            stamped without naming anything.

    Returns:
        The new event's ``seq``.
    """
    cur = conn.execute(
        "INSERT INTO events (actor, op, payload, cycle_id) VALUES (?, ?, ?, ?)",
        (
            actor,
            op,
            json.dumps(payload, ensure_ascii=False),
            cycle_id if cycle_id is not None else _CURRENT_CYCLE.get(),
        ),
    )
    if cur.lastrowid is None:
        # The sqlite3 contract: an INSERT that completes sets rowid. A None
        # here would mean the driver did not run the statement — impossible
        # without an exception having already propagated.
        raise RuntimeError("INSERT into events did not set a rowid")
    return int(cur.lastrowid)


def _write_version(
    conn: sqlite3.Connection, node_row: sqlite3.Row | dict[str, Any], actor: str, event_seq: int
) -> int:
    """Snapshot a node's title/content/props into ``versions`` after a mutation.

    The snapshot row lands ``applied`` — the ``state`` column's DDL default
    (``0008_proposed_versions``) — which is what a snapshot *is*: a record of
    state that was true, as opposed to a ``proposed`` row waiting on a review.

    Returns:
        The new version row's id — what an accept records so that reversing
        the accept can remove the snapshot again (:func:`_accept_snapshot_row`).
    """
    data = dict(node_row)
    cur = conn.execute(
        """
        INSERT INTO versions (node_id, title, content, props, actor, event_seq)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (data["id"], data["title"], data["content"], data["props"], actor, event_seq),
    )
    if cur.lastrowid is None:
        # The sqlite3 contract: an INSERT that completes sets rowid. A None
        # here would mean the driver did not run the statement — impossible
        # without an exception having already propagated.
        raise RuntimeError("INSERT into versions did not set a rowid")
    return int(cur.lastrowid)


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
    landing: LandingState | None = None,
) -> None:
    """Sync a node's ``[[wikilinks]]`` with its pending/active ``mentions`` edges.

    Creates a ``mentions`` edge (``created_by`` = writer) for every newly
    linked target, archives edges whose target text disappeared, and silently
    skips unresolvable targets (no dangling edges). Self-links are ignored.

    A materialised edge lands in the state the writer's grants allow on both
    endpoint spaces (``active`` on edit, ``proposed`` on suggest); a target
    the writer may not link to — unreadable, or under-granted — is skipped
    rather than failing the write (:func:`Store.edge_landing_state`). The
    caller's own ``landing`` ceiling is applied on top
    (:func:`Store.cap_landing`), so a writer filing a node ``proposed`` files
    its mentions ``proposed`` too — the node and its links wait in review
    together; accepting the node sweeps the links live, rejecting it leaves
    none. A pending edge goes live when a reviewer accepts the proposing
    node (:func:`_activate_pending_mentions`) or the edge itself.

    **Retirement is gated in two layers.** Q13 review B2: an existing edge is
    only retired when the writer holds ``edit`` on *both* endpoint spaces.
    Without it the edge is left untouched — a writer who cannot see
    the far endpoint cannot tell the link "disappeared" (its target does not
    resolve for them), and must not be able to strip another principal's
    cross-space mentions out of a node it may otherwise edit. On top of that,
    retiring a **live** edge (the ``archive`` half) is the human tier: it
    retires live state, which an ``edit`` grant is in-space authority over,
    not a right to — so a non-human's content change leaves a stale active
    mention in place for a human (:meth:`Store.require_human` at the call
    site, with the edit-on-both bar underneath).

    Idempotent: re-running on unchanged content changes nothing, whichever
    state the existing edges are in — pending edges count as already
    materialised, so a later human write never duplicates them.
    """
    node = dict(node_row)
    targets = set(WIKILINK_RE.findall(node["content"] or ""))
    resolved: set[str] = set()
    edge_landing: dict[str, LandingState] = {}
    for target in targets:
        dst = _resolve_wikilink(conn, target, store)
        if dst is None or dst == node["id"]:
            continue
        dst_space = conn.execute("SELECT space_id FROM nodes WHERE id = ?", (dst,)).fetchone()[
            "space_id"
        ]
        try:
            edge_landing[dst] = store.edge_landing_state(
                node["space_id"], dst_space, META_SPACE_ID, landing
            )
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
            state=edge_landing[dst_id],
            cycle_id=cycle_id,
        )
    for dst_id, edge in current_by_dst.items():
        if dst_id in resolved:
            continue
        # A pending edge leaves `proposed`, so its op is `reject`, not
        # `archive` — the state machine allows only one of the two.
        action = "archive" if edge["state"] == "active" else "reject"
        if action == "archive":
            # Archiving an edge is retiring live state, the human tier: an
            # `edit` grant is in-space authority, not the right to retire it,
            # so a non-human's content change leaves the stale active mention
            # in place for a human rather than pruning it.
            try:
                store.require_human("archive a mention")
            except GrantNotPermitted:
                continue
        if not _may_retire_mention(conn, node["space_id"], dst_id, store):
            continue
        _set_edge_state(conn, dict(edge), "archived", action, actor, cycle_id=cycle_id)


def _may_retire_mention(
    conn: sqlite3.Connection, src_space: str | None, dst_id: str, store: Store
) -> bool:
    """May this writer reject a ``mentions`` edge into ``dst_id``?

    Retiring an edge is a state-machine action on both endpoint spaces, so it
    needs ``edit`` on both — the same bar :meth:`Store.require_review` sets
    for rejecting the edge directly. Unreadable far endpoints fail it too
    (no grant, no level), which is what keeps an under-granted writer from
    silently pruning links it cannot see.

    This is the gate for the ``reject`` half only. The ``archive`` half —
    retiring a **live** edge — is the human tier (it retires live state, which
    an ``edit`` grant is in-space authority over, not a right to), so the
    caller gates it on :meth:`Store.require_human` first and consults this for
    humans alone.
    """
    row = conn.execute("SELECT space_id FROM nodes WHERE id = ?", (dst_id,)).fetchone()
    if row is None:
        return False
    return (
        store.principal.level_on(src_space) >= EDIT
        and store.principal.level_on(row["space_id"]) >= EDIT
    )


def _activate_pending_mentions(
    conn: sqlite3.Connection, node: dict[str, Any], actor: str, store: Store
) -> None:
    """Bring an accepted node's own pending ``mentions`` edges to ``active``.

    An agent's ``[[wikilinks]]`` materialise as ``proposed`` edges, so
    accepting the node is what actually attaches it to the graph. Only the
    edges the node's own author materialised are swept (``created_by`` match);
    an unrelated agent's proposed ``mentions`` edge out of the same node stays
    in the queue on its own merits. Each transition is its own event,
    attributed to the accepting reviewer.

    Each edge is gated on the acceptor's own review authority over both
    endpoint spaces (Q13 review B1): accepting the node must not be a way to
    land an edge into a space the acceptor could not review the edge in
    directly. An edge the acceptor lacks authority over stays ``proposed``
    for someone who has it.
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
        edge = _row_dict(row)
        try:
            store.require_review(_item_spaces(conn, "edge", edge), "accept")
        except GrantNotPermitted:
            continue
        _set_edge_state(conn, edge, "active", "accept", actor)


def _settle_synthesis_edges(
    conn: sqlite3.Connection, node: dict[str, Any], action: str, actor: str, store: Store
) -> None:
    """Settle a reviewed synthesis's own ``derived_from`` edges with the node.

    The abstraction job files a synthesized concept and its ``derived_from``
    edges as **one unit**: the edges are proposals of the same decision, so
    the reviewer's single action settles both halves. Accepting the concept
    brings its pending edges to ``active`` — the synthesis is decided, and
    its members are now protected by a live membership fact; rejecting it
    archives them, because a rejected concept's membership edges are
    meaningless and must not linger as orphaned proposals or protect their
    members. This is the ``derived_from`` parallel of
    :func:`_activate_pending_mentions`, and it shares the same two scoping
    rules: only the edges the node's own author wrote are settled
    (``created_by`` match — an unrelated agent's pending edge out of the same
    node stays in the queue on its own merits), and each edge is gated on the
    reviewer's own authority over both endpoint spaces. Each transition is
    its own event, attributed to the reviewer.

    A node that is not a synthesis is a no-op — ordinary nodes carry no
    ``derived_from`` edges of their own, and the helper must not settle anyone
    else's. "Is a synthesis" is verified against the event log, not read off
    the props (M21): ``props.synthesized`` is forgeable by any writer, but the
    create event is append-only, so the distinction lives in the event that
    wrote the node — see :func:`is_synthesis`. The check is made on the same
    connection: a second one could not see this transaction's uncommitted
    writes, and inside a bulk review's immediate lock it could not even read.
    """
    if not is_synthesis(node["id"], conn=conn):
        return
    rows = conn.execute(
        """
        SELECT * FROM edges
        WHERE src_id = ? AND type_id = 'derived_from' AND state != 'archived' AND created_by = ?
        ORDER BY created_at, rowid
        """,
        (node["id"], node["created_by"]),
    ).fetchall()
    for row in rows:
        edge = _row_dict(row)
        # A proposed edge leaves `proposed`, so its op is `reject`, not
        # `archive` — the state machine allows only one of the two (the same
        # state-picked action `_materialize_mentions`'s retirement uses).
        retiring = action != "accept"
        edge_action = "reject" if retiring and edge["state"] == "proposed" else "archive"
        try:
            store.require_review(
                _item_spaces(conn, "edge", edge), edge_action if retiring else "accept"
            )
        except GrantNotPermitted:
            continue
        _set_edge_state(conn, edge, "archived" if retiring else "active", edge_action, actor)


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
    state: NodeState,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    """Insert one edge row and emit its create/propose event; returns the row."""
    edge_id = uuid.uuid4().hex
    # An edge that lands `active` is true from the moment it exists, so its
    # validity window opens at creation time (D2: the value is a fact — the
    # edge IS true — not a guess). An edge that lands `proposed` is not yet
    # true; its `valid_from` stays NULL until the accept transition opens the
    # window (`_set_edge_state`).
    conn.execute(
        """
        INSERT INTO edges (id, src_id, dst_id, type_id, props, confidence, created_by, state,
                           valid_from)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'active' THEN datetime('now') END)
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
    new_state: NodeState,
    action: TransitionAction,
    actor: str,
    cycle_id: str | None = None,
    reason: str | None = None,
    props: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Transition an edge's state, emitting the event; returns the after row.

    This is the **single writer for the validity window** (D2): the two
    transition directions that change what is true write the corresponding
    column here, so every retirement path — transition, wikilink
    materialisation, synthesis settlement, merge, supersede — records the same
    facts and the paths cannot disagree:

    * ``proposed`` → ``active`` (accept): ``valid_from`` opens at the accept
      time, but only when the edge has none yet — a re-accept after a rollback
      must not rewrite the fact.
    * ``active`` → ``archived`` (archive): ``valid_to`` closes at the archive
      time — the edge stopped being true the moment it was retired.
    * ``proposed`` → ``archived`` (reject): neither column moves. A rejected
      proposal was never true, so it has no window to open or close.

    ``props``, when given, is written in the same UPDATE — ``supersede_edge``
    rides its ``superseded_by`` props write on the shared retirement so the
    row is written once, not twice. ``reason`` is recorded in the event
    payload on rejects (design §8.1).
    """
    sets = ["state = ?"]
    params: list[Any] = [new_state]
    if before["state"] == "active" and new_state == "archived":
        sets.append("valid_to = datetime('now')")
    if before["state"] == "proposed" and new_state == "active" and before["valid_from"] is None:
        sets.append("valid_from = datetime('now')")
    if props is not None:
        sets.append("props = ?")
        params.append(json.dumps(props, ensure_ascii=False))
    params.append(before["id"])
    conn.execute(f"UPDATE edges SET {', '.join(sets)} WHERE id = ?", params)
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
    landing: LandingState | None = None,
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
        landing: The writer's own ceiling on the landing state (design §8.3 —
            a grant is a ceiling, not a mandate): ``"proposed"`` files the
            node for review even when the grant would have written it live.
            It can only lower; asking for more than the grant allows is
            refused (:func:`Store.cap_landing`).
        principal: Who is writing (default: the trusted-local owner).
        path: Explicit database path.

    Returns:
        The created node.

    Raises:
        GrantNotPermitted: If the principal has no write grant on the space,
            or ``landing`` asks for more than the grant allows.
        ValueError: If ``landing`` is not a state a write can land in.
    """
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        actor = principal.actor_string
        type_id = _resolve_node_type(conn, type, principal)
        target_space = (
            _resolve_space(conn, space, principal) if space is not None else MAIN_SPACE_ID
        )
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
        state = store.landing_state(target_space, landing)
        # A space is an ordinary node, so this is the path a raw
        # `node create --type space` takes past `create_space` — both space
        # rules have to sit here or that path is the way around them. After the
        # grant check, so a caller with no authority here is refused for that
        # first.
        if type_id == "space":
            _require_space_lives_in_meta(target_space)
            _require_space_name_free(conn, title, principal)
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
        _materialize_mentions(conn, node, actor, store, landing=landing)
        # The return value is built before the commit so a validation failure
        # lands pre-commit: `finally: conn.close()` then rolls back the write.
        out = _node_out(node)
        conn.commit()
        return out
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
        # The space-name check below is a read the UPDATE would otherwise race:
        # two concurrent renames both probing the name as free (finding M8).
        db.begin_immediate(conn)
        store = Store(conn, principal)
        actor = principal.actor_string
        before_row = _get_node_row(conn, node_id)
        if not store.node_visible(before_row):
            raise NodeNotFound(f"node not found: {node_id}")
        before = _row_dict(before_row)
        if not principal.is_human and principal.level_on(before["space_id"]) < SUGGEST:
            raise GrantNotPermitted(f"{actor} has no write grant on space {before['space_id']!r}")
        # Renaming a space is renaming a node, so the name rule belongs here too:
        # `rename_space` delegates to this function, and `node update` reaches it
        # directly. Refused at propose time as well as at apply time, so an agent
        # learns now rather than the reviewer learning at accept.
        # The grant checked above is on the space the node *lives in*, which for
        # a space node is meta — but only because `_require_space_lives_in_meta`
        # keeps it there, and a database written before that guard can still
        # hold one elsewhere. `_require_space_name_free` owns that gate, since
        # it is the call that discloses.
        if before["type_id"] == "space" and title is not _UNSET and title != before["title"]:
            _require_space_name_free(conn, title, principal, exclude_id=node_id)
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
            if cur.lastrowid is None:
                # The sqlite3 contract: an INSERT that completes sets rowid. A
                # None here would mean the driver did not run the statement —
                # impossible without an exception having already propagated.
                raise RuntimeError("INSERT into versions did not set a rowid")
            version = _row_dict(_get_version_row(conn, int(cur.lastrowid)))
            # The return value is built before the commit so a validation
            # failure lands pre-commit: `finally: conn.close()` then rolls back.
            out = _version_out(version)
            conn.commit()
            return out
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
        # The return value is built before the commit so a validation failure
        # lands pre-commit: `finally: conn.close()` then rolls back the write.
        out = _node_out(after)
        conn.commit()
        return out
    finally:
        conn.close()


def _node_list_filters(
    store: Store,
    *,
    state: NodeState | None,
    type_id: str | None,
    parent_id: str | None,
    space_id: str | None,
    include_meta: bool,
) -> tuple[list[str], list[Any]]:
    """Build :func:`list_nodes`' ``WHERE`` clauses and params (unaliased ``nodes``).

    Extracted so the invariant below is assertable directly, the way
    :func:`nodum.search._node_filters` already is: a runtime call cannot reach
    the state that breaks it, which is exactly why the builder is worth pinning
    rather than only its behaviour.

    **The space filter is ANDed onto the principal's scope; it never replaces
    it.** The scope clause is the boundary (an agent's read set); ``space_id``
    is a convenience that narrows it further. Resolution already refuses to
    hand an agent the id of a space outside its read set, so making the filter
    an *alternative* to the scope would not show up in any behavioural test —
    the suite stayed wholly green under exactly that mutation. Hence the pin.

    Naming the meta space is itself the ``include_meta`` opt-in, so the default
    meta exclusion applies only to an unnarrowed listing by an unfiltered
    principal.

    Args:
        store: The scope-bound store, for the principal's node scope.
        state: One of :data:`STATES`, already validated.
        type_id: Resolved node-type id.
        parent_id: Only this node's children.
        space_id: Resolved space id, or ``None`` for every space in scope.
        include_meta: Include meta-space nodes in an unnarrowed listing.

    Returns:
        ``(clauses, params)``, to be joined with ``AND``.
    """
    clauses: list[str] = []
    params: list[Any] = []
    scope, scope_params = store.node_scope()
    if scope:
        clauses.append(scope.removeprefix(" AND "))
        params.extend(scope_params)
    elif not include_meta and space_id is None:
        clauses.append("space_id != ?")
        params.append(META_SPACE_ID)
    if space_id is not None:
        clauses.append("space_id = ?")
        params.append(space_id)
    if type_id is not None:
        clauses.append("type_id = ?")
        params.append(type_id)
    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    if parent_id is not None:
        clauses.append("parent_id = ?")
        params.append(parent_id)
    return clauses, params


def require_positive_limit(limit: int, name: str = "limit") -> None:
    """Refuse a row cap below 1, which SQLite would read as *unbounded*.

    Every capped read goes through here rather than restating the check —
    :mod:`nodum.search`'s included, which is why this is the one public helper
    in a file of private ones. The failure it prevents is silent and it is not
    even the same failure twice. A caller asking for fewer rows than exist got
    **all** of them wherever the number reached SQL, since SQLite treats a
    negative ``LIMIT`` as "no limit"; where the cap is a Python slice
    (:func:`list_proposals`, :func:`suggest_links`) a negative one instead
    dropped that many rows off the **end** of the list and answered normally,
    which on the review queue means losing a proposal with no sign that
    anything was lost. A cap of 0 returned nothing at all under a spelling that
    reads like "as few as possible". Three different wrong answers, one typo.

    :func:`subgraph` stated the rule first and :func:`list_cycles`,
    :func:`list_events` and :func:`list_nodes` followed; :func:`list_edges`,
    :func:`list_proposals`, :func:`suggest_links` and
    :func:`nodum.search.search` say it identically now.

    Args:
        limit: The cap the caller asked for.
        name: What the caller called it, so the refusal names the parameter
            that was actually typed — ``search`` spells this one ``k``, and a
            message about ``limit`` would name a flag that does not exist.

    Raises:
        ValueError: If ``limit`` is below 1.
    """
    if limit < 1:
        raise ValueError(f"{name} must be >= 1, got {limit}")


def list_nodes(
    *,
    type: str | None = None,
    state: NodeState | None = None,
    parent_id: str | None = None,
    space: str | None = None,
    include_meta: bool = False,
    principal: Principal,
    limit: int = 500,
    path: str | Path | None = None,
) -> list[NodeOut]:
    """List nodes, optionally filtered by type name/id, state, parent, or space.

    Ordered by ``created_at``; ``limit`` caps the result (default 500).
    Meta-space nodes (the type vocabulary, spaces) are excluded unless
    ``include_meta`` — content listings are not the type catalog. An agent
    principal is additionally confined to its read set (which may include
    meta, e.g. for the type vocabulary), and the space nodes inside that read
    set are the granted ones only: a ``space``-typed node resolves through its
    own id (M3), so an agent holding ``meta: read`` lists the space nodes of
    the spaces it holds grants on, and none of the others.

    ``space`` **narrows** that read set and can never widen it: it resolves
    through :func:`_resolve_space`, so a space the principal holds no grant on
    does not resolve at all, and the scope clause is still ANDed underneath it.
    It is a convenience for the human (who is unfiltered and sees the whole
    file), not a boundary — the boundary is the grant set.

    Args:
        type: Node-type id or name.
        state: One of :data:`STATES`.
        parent_id: Only this node's children.
        space: Space id or name; ``None`` spans every space in scope. Naming
            the meta space explicitly is itself the ``include_meta`` opt-in —
            the default exclusion only applies to an unnarrowed listing.
        include_meta: Include meta-space nodes in an unnarrowed listing.
        principal: Who is asking.
        limit: Maximum rows.
        path: Explicit database path.

    Raises:
        TypeNotFound: If ``type`` or ``space`` resolves to nothing the
            principal can read — an ungranted space and a nonexistent one
            answer identically (Q13 review S3).
        ValueError: If ``state`` is not a known state, or ``limit`` is below 1
            — :func:`subgraph`'s rule, said here for the same reason: SQLite
            reads a negative ``LIMIT`` as *unbounded*, so ``--limit -3`` handed
            back every node in scope.
    """
    require_positive_limit(limit)
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        space_id = _resolve_space(conn, space, principal) if space is not None else None
        type_id = _resolve_node_type(conn, type, principal) if type is not None else None
        if state is not None and state not in STATES:
            raise ValueError(f"state must be one of {STATES}, got {state!r}")
        clauses, params = _node_list_filters(
            store,
            state=state,
            type_id=type_id,
            parent_id=parent_id,
            space_id=space_id,
            include_meta=include_meta,
        )
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
        ValueError: If ``limit`` is below 1 — through
            :func:`require_positive_limit` like every other capped read, rather
            than restating it here, which is how the same check drifts into
            four spellings of one rule.
    """
    require_positive_limit(limit)
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
    landing: LandingState | None,
    actor: str,
    store: Store,
) -> dict[str, Any]:
    """Validate and write one edge inside an open connection (no commit).

    Shared by :func:`create_edge` and :func:`propose_edges`. An endpoint the
    principal cannot read is *not found* (an unreadable space does not
    exist); the landing state needs the matching grant on **both** endpoint
    spaces, and a cross-space edge's type node must live in meta
    (:func:`Store.edge_landing_state` — Q13 note 03). ``landing`` is the
    writer's own ceiling on that state (:func:`Store.cap_landing`).
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
    state = store.edge_landing_state(src["space_id"], dst["space_id"], type_space, landing)
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
    landing: LandingState | None = None,
    principal: Principal,
    path: str | Path | None = None,
) -> EdgeOut:
    """Create a typed, directed edge and emit ``edge.create``/``edge.propose``.

    Both endpoints must exist and be readable. The landing state needs the
    matching grant on both endpoint spaces (``edit`` → ``active``,
    ``suggest`` → ``proposed``); a cross-space edge's type node must live in
    meta.

    Args:
        src_id: Source node id.
        dst_id: Destination node id.
        type: Edge-type id or name.
        props: Free-form JSON-object metadata.
        confidence: Optional confidence in ``[0, 1]``.
        landing: The writer's own ceiling on the landing state (design §8.3 —
            a grant is a ceiling, not a mandate): ``"proposed"`` files the
            edge for review even when the grant would have written it live.
            It can only lower; asking for more than the grant allows is
            refused (:func:`Store.cap_landing`).
        principal: Who is writing.
        path: Explicit database path.

    Raises:
        NodeNotFound: If either endpoint does not resolve — or is not
            readable by the principal.
        TypeNotFound: If the edge type does not resolve.
        GrantNotPermitted: If the grants on the endpoint spaces do not cover
            the write, a cross-space edge uses a non-meta type, or ``landing``
            asks for more than the grant allows.
        ValueError: If ``confidence`` is outside ``[0, 1]``, or ``landing`` is
            not a state a write can land in.
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
            landing=landing,
            actor=principal.actor_string,
            store=store,
        )
        # The return value is built before the commit so a validation failure
        # lands pre-commit: `finally: conn.close()` then rolls back the write.
        out = _edge_out(row)
        conn.commit()
        return out
    finally:
        conn.close()


def _suggestion_error(exc: ValidationError) -> str:
    """Map one suggestion's validation failure to the per-suggestion message.

    Unknown keys are named, mirroring the ``unknown search filter(s): …``
    sentence the MCP server uses for the same kind of caller bug; a missing
    field keeps the old ``missing key: …`` wording so batch callers matching
    it keep working. Any other shape failure reports pydantic's own sentence.
    """
    errors = exc.errors()
    unknown = sorted(
        {str(error["loc"][0]) for error in errors if error["type"] == "extra_forbidden"}
    )
    if unknown:
        return f"unknown suggestion key(s): {', '.join(unknown)}"
    missing = sorted({str(error["loc"][0]) for error in errors if error["type"] == "missing"})
    if missing:
        return f"missing key: {missing[0]}"
    return errors[0]["msg"]


def propose_edges(
    suggestions: list[dict[str, Any]],
    *,
    landing: LandingState | None = None,
    principal: Principal,
    path: str | Path | None = None,
) -> ProposeEdgesOut:
    """Write a batch of edge suggestions, one event per edge (design §8.1).

    Each suggestion names ``src``, ``dst``, and ``edge_type``, plus optional
    ``props`` and ``confidence`` — the same inputs as :func:`create_edge`.
    A malformed suggestion (missing key, unknown key, bad value shape,
    unknown endpoint/type, bad confidence) lands in ``failed`` with its
    input index; the rest still write. One commit for the whole batch.

    Every suggestion is validated against :class:`EdgeSuggestionIn` **before
    any write**, so a malformed one can never leave a partial row behind in
    the single batch commit (finding M32).

    Args:
        suggestions: The edges to write, one object each.
        landing: The writer's own ceiling on the landing state, applied to
            every suggestion in the batch (see :func:`create_edge`). It is a
            batch-level argument, so an unusable value is raised once rather
            than reported once per suggestion.
        principal: Who is writing.
        path: Explicit database path.

    Raises:
        ValueError: If ``suggestions`` is not a list of objects, or ``landing``
            is not a state a write can land in.
    """
    require_landing_state(landing)
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
                valid = EdgeSuggestionIn.model_validate(suggestion)
            except ValidationError as exc:
                failed.append(ItemFailure(index=index, error=_suggestion_error(exc)))
                continue
            try:
                row = _create_edge_in_conn(
                    conn,
                    valid.src,
                    valid.dst,
                    valid.edge_type,
                    props=valid.props,
                    confidence=valid.confidence,
                    landing=landing,
                    actor=principal.actor_string,
                    store=store,
                )
                created.append(_edge_out(row))
            except (NodeNotFound, TypeNotFound, ValueError, GrantNotPermitted) as exc:
                failed.append(ItemFailure(index=index, error=str(exc)))
        conn.commit()
        return ProposeEdgesOut(created=created, failed=failed)
    finally:
        conn.close()


def _as_of_edge_clause(t: str, admitted: tuple[str, ...]) -> tuple[str, list[Any]]:
    """The SQL fragment (and params) that admits exactly the edges true at instant ``t``.

    D2's read predicate, spelled once and reused by every as-of read so the
    lens cannot drift between surfaces. Callers pass the states their
    *non*-as-of filter would admit; this clause **replaces** that filter (it
    subsumes it), so the state filter under as-of narrows only the live part.
    An edge is present at instant ``t`` iff:

    * its state is one of ``admitted`` **or** it is ``archived`` — a retired
      edge is admitted when its window covered ``t``, whatever the caller's
      state filter says (the as-of lens shows the graph *as it was true*, and
      an archived edge whose window covered ``t`` was true then), and
    * its window covers ``t``: ``valid_from`` is unset (a pre-D2 edge, valid
      since the beginning of recorded history — as-of at "now" must agree with
      the default read) or ``valid_from <= t``, **and**
    * its window has not closed before ``t``: ``valid_to`` is unset on a row
      still ``active`` (a live edge, window still open) or ``valid_to > t``.

    Two legacy shapes fall out of that composition. A pre-D2 **active** edge
    (neither column set) is present at every instant — it is part of the live
    graph today, so as-of at now agrees with the default read. A pre-D2
    **archived** edge (``valid_to`` NULL — pre-D2 retirement recorded no
    window) is present at *no* instant: its closure is unknown, so no ``t``
    can be placed inside its window, and the default read already hides it by
    state.
    """
    placeholders = ",".join("?" * len(admitted))
    clause = (
        f"((state IN ({placeholders}) OR state = 'archived')"
        " AND (valid_from IS NULL OR valid_from <= ?)"
        " AND ((valid_to IS NULL AND state = 'active') OR valid_to > ?))"
    )
    return clause, [*admitted, t, t]


def list_edges(
    *,
    node_id: str | None = None,
    type: str | None = None,
    state: NodeState | None = None,
    as_of: str | None = None,
    principal: Principal,
    limit: int = 500,
    path: str | Path | None = None,
) -> list[EdgeOut]:
    """List edges, optionally filtered by incident node, type, or state.

    ``node_id`` matches edges in either direction. An agent principal sees
    only edges whose endpoints are both readable.

    ``as_of`` reads the graph as it was true at an instant (D2): pass a
    timestamp and an edge is returned iff its validity window covered it. The
    window clause subsumes the state filter — under as-of the filter narrows
    only which live states are admitted, and a window-covered archived edge is
    returned regardless (``state IN (filter) OR state = 'archived'`` composed
    with ``valid_from <= t AND (valid_to IS NULL OR valid_to > t)``, with the
    pre-D2 NULL rules :func:`_as_of_edge_clause` documents). Without ``as_of``
    the read is unchanged: the state filter alone, defaulting to every state
    (the live graph plus anything retired).

    Raises:
        ValueError: If ``state`` is not a known state, or ``limit`` is below 1
            (:func:`require_positive_limit` — ``limit=-3`` took the number
            straight to SQL and handed back every edge in scope).
    """
    require_positive_limit(limit)
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
            params.append(_resolve_edge_type(conn, type, principal)[0])
        if state is not None and state not in STATES:
            raise ValueError(f"state must be one of {STATES}, got {state!r}")
        if as_of is not None:
            # The window clause subsumes the state filter: under as-of the
            # filter only narrows which *live* states are admitted, and a
            # window-covered archived edge is admitted regardless — the lens
            # shows what was true at the instant.
            as_of_clause, as_of_params = _as_of_edge_clause(
                as_of, (state,) if state is not None else DEFAULT_EDGE_STATES
            )
            clauses.append(as_of_clause)
            params.extend(as_of_params)
        elif state is not None:
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

    **Both spellings record the version row's own move**, because a review
    changes two rows and only one of them is a graph record. The accept carries
    it as :data:`VERSION_STATE_KEY` inside the ``node.update`` payload (the
    ``merge_redirects`` shape: state no event of its own covers rides on the
    event that caused it), and the reject's own before/after *is* that move. A
    reversal reads both — see :func:`_restore_version_state`.
    """
    version_id = before["id"]
    if action == "accept":
        node_before = _row_dict(_get_node_row(conn, before["node_id"]))
        fields = _proposed_fields(before)
        # A proposed space rename was checked when it was filed, but this is
        # where it lands and a space may have taken the name in between. The
        # write path's refusal, so the reviewer reads a sentence rather than the
        # unique index's IntegrityError.
        if node_before["type_id"] == "space" and "title" in fields:
            _require_space_name_free(
                conn, before["title"], store.principal, exclude_id=before["node_id"]
            )
        assignments = [f"{name} = ?" for name in fields] + ["updated_at = datetime('now')"]
        conn.execute(
            f"UPDATE nodes SET {', '.join(assignments)} WHERE id = ?",
            (*[before[name] for name in fields], before["node_id"]),
        )
        node_after = _row_dict(_get_node_row(conn, before["node_id"]))
        conn.execute("UPDATE versions SET state = 'applied' WHERE id = ?", (version_id,))
        seq = _emit(
            conn,
            actor,
            "node.update",
            {
                "before": node_before,
                "after": node_after,
                "applied_version_id": version_id,
                "applied_fields": fields,
                "proposed_event_seq": before["event_seq"],
                VERSION_STATE_KEY: {
                    "before": before,
                    "after": _row_dict(_get_version_row(conn, version_id)),
                },
            },
        )
        # A **true snapshot of the node as it now stands** — the proposal row
        # flipped to `applied` above is the record of the proposal, and its
        # un-named fields are copies from proposal time (:func:`_proposed_fields`),
        # so history must not read it as the state the accept landed (finding
        # M9). Written after the event so it can carry the event's seq, and
        # removed again by the reversal of this accept
        # (:func:`_accept_snapshot_row`).
        _write_version(conn, node_after, actor, seq)
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


def _item_spaces(
    conn: sqlite3.Connection, kind: TransitionKind, row: dict[str, Any]
) -> set[str | None]:
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
    action: TransitionAction,
    actor: str,
    store: Store,
    reason: str | None = None,
) -> tuple[TransitionKind, dict[str, Any]]:
    """Apply one state transition inside an open connection (no commit).

    Returns:
        A ``(kind, after_row)`` pair where kind is ``"node"``, ``"edge"``, or
        ``"version"``.

    Raises:
        GrantNotPermitted: If the principal may not make the transition:
            accept/reject need a human or an ``edit`` grant on the item's
            space (both endpoint spaces for an edge); archive needs a human
            outright, unless it is made inside a consolidation cycle, where
            the cycle's own review bar applies (:data:`_CURRENT_CYCLE`).
        RecordNotFound: If the id resolves to neither a node, an edge, nor a
            version the principal can read — the id alone does not say which
            kind was meant, so the base class is what is raised.
        InvalidTransition: If the transition is not allowed from the current
            state.
    """
    from_state, to_state = TRANSITIONS[action]
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (record_id,)).fetchone()
    kind: TransitionKind = "node"
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
    # accept/reject are the review tier (a human, or `edit` on the item's
    # space); archive is the human tier — it retires live state, which an
    # `edit` grant is in-space authority over, not a right to. The exception
    # is an archive made *inside* a consolidation cycle: the pruning half and
    # the curative tier retire edges as part of the cycle's work — gated at
    # `open_cycle` and reversed by `rollback`, not by a reviewer's archive —
    # so those stay at the cycle's own review bar.
    if action == "archive" and not _CURRENT_CYCLE.get():
        store.require_human("archive")
    else:
        store.require_review(spaces, action)
    # The structural spaces are not archivable by any spelling. This sits here
    # rather than in `archive_space` because `archive <id>` and
    # `POST /api/nodes/{id}/archive` reach the same row without going near it.
    if action == "archive" and kind == "node" and record_id in STRUCTURAL_SPACE_IDS:
        raise InvalidTransition(
            f"cannot archive the {record_id!r} space: {STRUCTURAL_SPACE_IDS[record_id]}"
        )
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
            _activate_pending_mentions(conn, after, actor, store)
        if action in ("accept", "reject"):
            # A synthesis is decided together with its members: the concept's
            # own `derived_from` edges settle with it (active on accept,
            # archived on reject), exactly as its wikilink mentions sweep on
            # accept. No-op for any node without `props.synthesized`.
            _settle_synthesis_edges(conn, after, action, actor, store)
        return kind, after
    return kind, _set_edge_state(conn, before, to_state, action, actor, reason=reason)


def transition(
    record_id: str,
    action: TransitionAction,
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
        actor: Who performs the transition. ``accept`` and ``reject`` are the
            review tier: a human, or an agent holding ``edit`` on every space
            the item touches. ``archive`` is the human tier
            (:meth:`Store.require_human`): it retires live state, and an
            ``edit`` grant is in-space authority, not the right to retire it.
        path: Explicit database path.

    Returns:
        The updated node, edge, or version.

    Raises:
        GrantNotPermitted: If the principal may not make this transition —
            no human and no ``edit`` grant on the item's spaces for
            accept/reject; not a human for archive.
        RecordNotFound: If the id resolves to no node, edge, or version.
        InvalidTransition: If the transition is not allowed from the current
            state.
    """
    if action not in TRANSITIONS:
        raise ValueError(f"unknown transition {action!r}; expected one of {sorted(TRANSITIONS)}")
    conn = _connect(path)
    try:
        # The whole read-check-write is one atomic fact: two concurrent
        # accepts of one proposal must not both pass the state check. The
        # immediate lock is taken before any read, so the second caller sees
        # the first caller's committed state rather than a stale pre-check
        # row (finding M8).
        db.begin_immediate(conn)
        store = Store(conn, principal)
        kind, after = _transition_row(
            conn, record_id, action, principal.actor_string, store, reason=reason
        )
        # The return value is built before the commit so a validation failure
        # lands pre-commit: `finally: conn.close()` then rolls back the write.
        if kind == "node":
            out = _node_out(after)
        elif kind == "version":
            out = _version_out(after)
        else:
            out = _edge_out(after)
        conn.commit()
        return out
    finally:
        conn.close()


# ── Review queue: pending proposals and batch accept/reject (design §8.1) ────


def _proposal_filters(
    conn: sqlite3.Connection,
    principal: Principal,
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
            "SELECT id, space_id, json_extract(props, '$.type_kind') AS kind FROM nodes"
            " WHERE (id = ? OR title = ?) AND type_id = 'type' AND state = 'active'",
            (type, type),
        ).fetchall()
        for r in row:
            if principal.level_on(r["space_id"]) < READ:
                continue  # an unreadable type does not resolve (review N1)
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
    store: Store,
    *,
    kind: ProposalKind | None = None,
    **filters: Any,
) -> list[tuple[str, sqlite3.Row]]:
    """Fetch proposed node/edge/version rows matching the filters, oldest first.

    Within one kind the order is creation order (``created_at``, then
    ``rowid``); timestamps have one-second resolution, so same-second rows of
    different kinds may interleave.
    """
    node_filter, edge_filter, node_type_id = _proposal_filters(conn, store.principal, **filters)
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
        # The version query joins `nodes n`, so the scope needs the `n.` alias
        # on every column it names — the scope builder's own `alias` argument,
        # not a string replace that would leave `type_id`/`id` unprefixed.
        update_scope, _ = store.node_scope(alias="n.")
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


def _node_ref(conn: sqlite3.Connection, node_id: str) -> dict[str, Any]:
    """One node as reviewer context: its id, title and space.

    The **space** is what makes the review queue groupable: the human UI's D4
    puts space at the outer level, and a proposed node states its own while an
    edge and an update state nothing but the row itself. Without this field
    every edge proposal — which is every ``mentions`` edge a ``[[wikilink]]``
    materialised, the commonest thing an agent files — reaches the queue with
    nothing to group on.

    A node that no longer resolves (an ``undo`` took it back) comes out as its
    id alone, with neither a title nor a space, which is the one case a queue
    genuinely cannot report a space for.
    """
    row = conn.execute("SELECT id, title, space_id FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        return {"id": node_id}
    return {"id": row["id"], "title": row["title"], "space_id": row["space_id"]}


def _node_context(conn: sqlite3.Connection, node: dict[str, Any]) -> dict[str, Any]:
    """Reviewer context for a proposed node: its parent, if any."""
    if node["parent_id"] is None:
        return {}
    return {"parent": _node_ref(conn, node["parent_id"])}


def _edge_context(conn: sqlite3.Connection, edge: dict[str, Any]) -> dict[str, Any]:
    """Reviewer context for a proposed edge: both endpoints.

    An edge is only listed when **both** endpoints are readable
    (:meth:`Store.edge_scope`), so this reports nothing the reviewer could not
    already read directly.
    """
    endpoints = (("src", "src_id"), ("dst", "dst_id"))
    return {key: _node_ref(conn, edge[column]) for key, column in endpoints}


def _update_context(conn: sqlite3.Connection, version: dict[str, Any]) -> dict[str, Any]:
    """Reviewer context for a proposed update: the node it targets."""
    return {"node": _node_ref(conn, version["node_id"])}


def _annotations_by_target(
    conn: sqlite3.Connection,
    *,
    node_ids: list[str],
    edge_ids: list[str],
    version_ids: list[int],
) -> tuple[dict[str, Any], dict[str, Any], dict[int, Any]]:
    """The annotations on one proposal listing, as one ``{target: body}`` map per kind.

    The three target columns are separate columns, so a listing reads one
    ``IN`` clause per kind — node, edge, update — and migration 0016's partial
    unique indexes guarantee at most one row per target, which is what makes
    the result a map rather than a list. Bodies are the table's JSON, parsed
    here because :class:`ProposalOut.annotation` carries them as dicts; the
    version map is keyed by int because ``target_version_id`` is INTEGER.
    Empty id lists are skipped — SQLite has no ``IN ()``.
    """
    node_map, edge_map, version_map = {}, {}, {}
    if node_ids:
        placeholders = ",".join("?" * len(node_ids))
        node_map = {
            row["target_id"]: json.loads(row["body"])
            for row in conn.execute(
                f"SELECT target_node_id AS target_id, body FROM annotations"
                f" WHERE target_node_id IN ({placeholders})",
                node_ids,
            ).fetchall()
        }
    if edge_ids:
        placeholders = ",".join("?" * len(edge_ids))
        edge_map = {
            row["target_id"]: json.loads(row["body"])
            for row in conn.execute(
                f"SELECT target_edge_id AS target_id, body FROM annotations"
                f" WHERE target_edge_id IN ({placeholders})",
                edge_ids,
            ).fetchall()
        }
    if version_ids:
        placeholders = ",".join("?" * len(version_ids))
        version_map = {
            row["target_id"]: json.loads(row["body"])
            for row in conn.execute(
                f"SELECT target_version_id AS target_id, body FROM annotations"
                f" WHERE target_version_id IN ({placeholders})",
                version_ids,
            ).fetchall()
        }
    return node_map, edge_map, version_map


def list_proposals(
    *,
    created_by: str | None = None,
    type: str | None = None,
    kind: ProposalKind | None = None,
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
        node an update targets). Each also carries its ``annotation`` — the
        parsed body of its ``annotations`` row (migration 0016), what a
        proposer's acceptance signal judged and at what rate — when the
        learned-curation cycle has written one.

    Raises:
        ValueError: If ``kind`` is not one of the three, or ``limit`` is below
            1. The cap here is a Python slice rather than a SQL ``LIMIT``, so a
            negative one was not "unbounded" but ``rows[:-3]`` — it dropped
            proposals off the **end of the review queue** and answered normally,
            which is the one listing that must not lose an item quietly.
    """
    if kind not in (None, "node", "edge", "update"):
        raise ValueError(f"kind must be 'node', 'edge', or 'update', got {kind!r}")
    require_positive_limit(limit)
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
        node_annotations, edge_annotations, version_annotations = _annotations_by_target(
            conn,
            node_ids=[row["id"] for row_kind, row in rows if row_kind == "node"],
            edge_ids=[row["id"] for row_kind, row in rows if row_kind == "edge"],
            version_ids=[int(row["id"]) for row_kind, row in rows if row_kind == "update"],
        )
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
                        annotation=node_annotations.get(data["id"]),
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
                        annotation=edge_annotations.get(data["id"]),
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
                        annotation=version_annotations.get(int(data["id"])),
                    )
                )
        return proposals
    finally:
        conn.close()


def _transition_many(
    ids: list[str],
    action: TransitionAction,
    *,
    principal: Principal,
    reason: str | None,
    path: str | Path | None,
) -> BatchTransitionOut:
    """Transition many ids in one connection; bad ids are skipped, not fatal.

    Grants are per-item, so a refusal is per-item too: an id the principal
    may not review lands in ``failed`` beside unknown ids and invalid
    transitions, and the rest of the batch still applies. A batch may
    therefore be partially applied — the documented semantics since the
    grant model replaced the all-or-nothing human tier.
    """
    conn = _connect(path)
    try:
        # As in :func:`transition`: one atomic read-check-write per row, so a
        # proposal two callers both picked cannot be accepted twice (finding M8).
        db.begin_immediate(conn)
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
    unknown, not ``proposed``, or outside the principal's review authority
    are collected in ``failed``; the rest still transition.
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
    Ids that are unknown, not ``proposed``, or outside the principal's review
    authority are collected in ``failed`` — an agent may not reject another
    agent's proposal any more than its own, outside its edit-granted spaces.
    """
    return _transition_many(ids, "reject", principal=principal, reason=reason, path=path)


def annotate(
    target_kind: TransitionKind,
    target_id: str | int,
    body: dict[str, Any],
    *,
    principal: Principal,
    path: str | Path | None = None,
) -> AnnotationOut:
    """Write one annotation on a node, edge, or version — replacing a prior one.

    Migration 0016's table is the write seam's schema half (design §L1: one
    annotation per queue item, saying what a proposer's acceptance signal
    judged and at what rate) and this is the writer it shipped without: the
    learned-curation cycle (5b-ii) files the annotations, and a human has no
    reason to — the table is read only attached to a
    :class:`~nodum.models.ProposalOut` the store has already grant-filtered.
    An annotation is **derived judgement, not graph state**: it writes no
    event-log row and no version, which is exactly why it cascades with its
    target and can never be the reason a delete is refused
    (:func:`_delete_blocker`).

    The review gate is the review queue's own (:meth:`Store.require_review`):
    a human, or ``edit`` on the item's space — the same authority accept and
    reject ask for, because attaching the gardener's learned judgement to an
    item is the kind of write that must stay in the hands of whoever reviews
    that item. The read side answers *not found* for anything the principal
    cannot see, identically to something that does not exist — an annotation
    must not be an existence oracle, the Q13 shape every id-carrying write
    follows.

    The target resolves through the principal's read scope like a transition
    row does: a node must be readable, an edge must have both endpoints
    readable, and a version resolves through the node it belongs to.

    Args:
        target_kind: ``"node"``, ``"edge"``, or ``"version"`` — the
            exclusive-arc column the row lands in.
        target_id: The target's id; a version id is the ``versions`` row's
            integer.
        body: The annotation's JSON object — what was judged and at what rate
            (e.g. ``{"rate": 0.92, "signals": [...], "counts": ...}``).
        principal: Who is writing.
        path: Explicit database path.

    Returns:
        The row as written, so the caller can report the annotation id.

    Raises:
        ValueError: If ``target_kind`` is not one of the three, or ``body`` is
            not a JSON-serialisable object.
        RecordNotFound: If the id resolves to no row the principal can read —
            unreadable and nonexistent answer identically.
        GrantNotPermitted: If the principal is not a human and holds no
            ``edit`` grant on the item's space.

    Note:
        **Re-annotating replaces rather than accumulates.** The three partial
        unique indexes hold one annotation per target, and ``INSERT OR REPLACE``
        would not express that (it replaces on the primary key only) — so the
        write is an explicit ``DELETE`` of any prior row on the same target
        column followed by the ``INSERT``, in one connection and one commit. A
        later cycle's annotation on the same queue item supersedes the earlier
        one instead of piling up beside it.
    """
    if target_kind not in ("node", "edge", "version"):
        raise ValueError(f"target_kind must be 'node', 'edge', or 'version', got {target_kind!r}")
    if not isinstance(body, dict):
        raise ValueError("annotation body must be a JSON object")
    try:
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"annotation body must be JSON-serialisable: {exc}") from None
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        row = _resolve_annotatable(conn, store, target_kind, target_id)
        store.require_review(_item_spaces(conn, target_kind, row), "annotate")
        target_column = {
            "node": "target_node_id",
            "edge": "target_edge_id",
            "version": "target_version_id",
        }[target_kind]
        # The replacement path: the unique index is per target column, so a
        # second annotate deletes what the first wrote before inserting its own
        # row. One connection and one commit keep the pair atomic.
        conn.execute(f"DELETE FROM annotations WHERE {target_column} = ?", (target_id,))
        annotation_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO annotations (id, target_node_id, target_edge_id,"
            " target_version_id, body, actor, cycle_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                annotation_id,
                target_id if target_kind == "node" else None,
                target_id if target_kind == "edge" else None,
                target_id if target_kind == "version" else None,
                encoded,
                principal.actor_string,
                _CURRENT_CYCLE.get(),
            ),
        )
        written = _row_dict(
            conn.execute("SELECT * FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
        )
        # The return value is built before the commit so a validation failure
        # lands pre-commit: `finally: conn.close()` then rolls back the whole
        # DELETE+INSERT pair, keeping "the row as written, or nothing" honest.
        out = AnnotationOut(
            id=written["id"],
            target_kind=target_kind,
            target_id=str(written[target_column]),
            body=body,
            actor=written["actor"],
            cycle_id=written["cycle_id"],
            created_at=written["created_at"],
        )
        conn.commit()
        return out
    finally:
        conn.close()


def _resolve_annotatable(
    conn: sqlite3.Connection,
    store: Store,
    target_kind: TransitionKind,
    target_id: str | int,
) -> dict[str, Any]:
    """Resolve an ``annotate`` target to a readable row, or answer *not found*.

    The three kinds resolve through the same read rule a transition row uses —
    a node must be in the principal's read set, an edge needs both endpoints
    readable, a version resolves through its node — and an unreadable target
    raises :class:`RecordNotFound` with the identical sentence a nonexistent
    one does: an annotation must not be an existence oracle (Q13).
    """
    if target_kind == "node":
        try:
            row = _get_node_row(conn, str(target_id))
        except NodeNotFound:
            raise RecordNotFound(f"no readable node with id: {target_id}") from None
        if not store.node_visible(row):
            raise RecordNotFound(f"no readable node with id: {target_id}")
        return _row_dict(row)
    if target_kind == "edge":
        try:
            row = _get_edge_row(conn, str(target_id))
        except EdgeNotFound:
            raise RecordNotFound(f"no readable edge with id: {target_id}") from None
        try:
            src = _get_node_row(conn, row["src_id"])
            dst = _get_node_row(conn, row["dst_id"])
        except NodeNotFound:
            # A dangling endpoint (an undo took the node back) is an edge with
            # no readable scope: the edge_scope rule is *both* endpoints.
            raise RecordNotFound(f"no readable edge with id: {target_id}") from None
        if not store.node_visible(src) or not store.node_visible(dst):
            raise RecordNotFound(f"no readable edge with id: {target_id}")
        return _row_dict(row)
    try:
        row = _get_version_row(conn, int(target_id))
    except (VersionNotFound, ValueError):
        raise RecordNotFound(f"no readable version with id: {target_id}") from None
    try:
        node = _get_node_row(conn, row["node_id"])
    except NodeNotFound:
        raise RecordNotFound(f"no readable version with id: {target_id}") from None
    if not store.node_visible(node):
        raise RecordNotFound(f"no readable version with id: {target_id}")
    return _row_dict(row)


def _matching_ids(
    conn: sqlite3.Connection, store: Store, *, kind: ProposalKind | None, **filters: Any
) -> list[str]:
    """Resolve a proposal filter to concrete ids (the batch-by-filter input).

    Scoped by the caller's store (Q13 review S4): an unscoped scan refuses
    out-of-scope items per-item, but their ids still come back in ``failed``
    — the id itself is the leak.
    """
    return [str(row["id"]) for _, row in _proposal_rows(conn, store, kind=kind, **filters)]


def accept_matching(
    *,
    created_by: str | None = None,
    type: str | None = None,
    kind: ProposalKind | None = None,
    created_before: str | None = None,
    created_after: str | None = None,
    principal: Principal,
    path: str | Path | None = None,
) -> BatchTransitionOut:
    """Accept every proposal matching the filters (e.g. one agent's whole run).

    The filter resolves to concrete ids first — inside the principal's read
    scope, so an unreviewable item is never even named — then each id
    transitions with its own event: the batch is a convenience, never a
    silent bulk update.
    """
    if kind not in (None, "node", "edge", "update"):
        raise ValueError(f"kind must be 'node', 'edge', or 'update', got {kind!r}")
    conn = _connect(path)
    try:
        ids = _matching_ids(
            conn,
            Store(conn, principal),
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
    kind: ProposalKind | None = None,
    created_before: str | None = None,
    created_after: str | None = None,
    principal: Principal,
    path: str | Path | None = None,
) -> BatchTransitionOut:
    """Reject every proposal matching the filters, recording ``reason``.

    The filter resolves to concrete ids first — inside the principal's read
    scope, so an unreviewable item is never even named — then each id
    transitions with its own event carrying the reason.
    """
    if kind not in (None, "node", "edge", "update"):
        raise ValueError(f"kind must be 'node', 'edge', or 'update', got {kind!r}")
    conn = _connect(path)
    try:
        ids = _matching_ids(
            conn,
            Store(conn, principal),
            kind=kind,
            created_by=created_by,
            type=type,
            created_before=created_before,
            created_after=created_after,
        )
    finally:
        conn.close()
    return _transition_many(ids, "reject", principal=principal, reason=reason, path=path)


# ── Reversal primitives (shared by `undo` and `rollback_cycle`) ───────────────
#
# Reversing one event is one of three things, and both reversal mechanisms mean
# the same three: a create comes back out by deleting the row it made, a
# removal by putting the rows back, and everything else by writing the recorded
# `before` row back verbatim. They live here rather than inside :func:`undo`
# because :func:`rollback_cycle` reverses a whole cycle's worth of events and a
# second implementation of "what reversing one event means" would be a second
# set of guards to keep correct — the guards are the part that took two rounds
# of review to get right.
#
# Each refusal takes its leading phrase as ``context`` (``cannot undo event 7
# (node.create)`` / ``cannot roll back event 7 (node.create)``) so the message
# names the operation the caller actually asked for without either mechanism
# restating the reason it failed.

#: Tables a reversal may take rows out of and put back, in the order an insert
#: has to follow: ``versions``, ``edges`` and ``merge_redirects`` all hold a
#: foreign key into ``nodes``, so the node row goes back first. Also the
#: allow-list for the table name in :func:`_reinsert_rows`, which comes out of a
#: stored payload and is interpolated into SQL.
REINSERT_ORDER = ("nodes", "versions", "edges", "merge_redirects")

#: How many blocking row ids a refusal spells out before summarising the rest.
#: A refusal naming none of them cannot be acted on; one naming four hundred is
#: not a sentence.
MAX_NAMED_DEPENDANTS = 5


def _named_rows(ids: list[str]) -> str:
    """The first few ids, then a count of what is left."""
    shown = ", ".join(ids[:MAX_NAMED_DEPENDANTS])
    rest = len(ids) - MAX_NAMED_DEPENDANTS
    return f"{shown} and {rest} more" if rest > 0 else shown


def _delete_blocker(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    doomed_nodes: frozenset[str] = frozenset(),
    doomed_redirects: frozenset[str] = frozenset(),
    doomed_edges: frozenset[str] = frozenset(),
) -> tuple[list[str], str] | None:
    """What stands in the way of deleting a node row, if anything.

    **Every foreign key into ``nodes(id)`` that is neither cascading nor removed
    by the deletion itself is here**, and that completeness is the point rather
    than a list that grew: an unguarded one is not a refusal the caller can read
    but a bare ``sqlite3.IntegrityError`` — a 500 over HTTP and ``database
    error: FOREIGN KEY constraint failed`` on a CLI whose contract promises to
    name "an undo the graph has grown past". The graph is never corrupted by
    one (the transaction rolls back whole); what is lost is the ability to act
    on the answer.

    Completeness is pinned to the schema rather than to this paragraph:
    ``test_rollback`` walks ``PRAGMA foreign_key_list`` through
    :func:`nodum.db.foreign_keys_into` and asserts every non-cascading foreign
    key into ``nodes(id)`` is owned by a guard here or a delete in
    :func:`_delete_created_row` — so a migration that adds a reference fails
    that test on the commit that adds it, not on an install a human runs.

    The six, in the order a caller most likely meets them: ``nodes.parent_id``
    (children), ``nodes.space_id`` (a space's occupants), ``merge_redirects``
    (a node merged away, or merged into), ``grants.space_id`` (agents granted
    on a space), ``nodes.type_id`` (nodes typed by a type node) and
    ``edges.type_id`` (edges typed by a type node). The foreign keys into
    ``nodes(id)`` this guard deliberately does not answer for are the cascade
    and the deletion's own housekeeping: ``annotations.target_node_id``
    (migration 0016) cascades, because an annotation is derived judgement and
    can never be the reason a node's undo is refused (which is what its
    ``cycle_id`` already implies), and ``edges.src_id``/``dst_id`` with
    ``versions.node_id`` are deleted by :func:`_delete_created_row` itself
    before the node goes — a guard over them would refuse a delete that in
    fact succeeds.

    Args:
        conn: The open connection.
        node_id: The node whose deletion is in question.
        doomed_nodes: Nodes the same reversal is going to delete anyway. A
            rollback reverses newest first, so a child a cycle created *after*
            its parent is gone by the time the parent's create is reversed —
            counting it would refuse a rollback that in fact succeeds. Empty for
            :func:`undo`, which reverses exactly one event.
        doomed_redirects: Tombstone ids whose ``merge_redirects`` rows the same
            reversal removes, for the same reason.
        doomed_edges: Edge ids the same reversal removes — every edge incident
            to a node it deletes, and every edge its own create-reversal takes
            out. The ``edges.type_id`` check needs them most: a cycle that
            creates a type node and an edge wearing it reverses the edge first,
            so the preflight has to know the edge is already gone.

    Returns:
        ``(dependant ids, the refusal's sentence)``, or ``None`` if the row can
        go.
    """

    def surviving(rows: list[sqlite3.Row], column: str, doomed: frozenset[str]) -> list[str]:
        return sorted({str(row[column]) for row in rows} - doomed)

    children = surviving(
        conn.execute("SELECT id FROM nodes WHERE parent_id = ?", (node_id,)).fetchall(),
        "id",
        doomed_nodes,
    )
    if children:
        # A child is a separate create this reversal never covered. Cascading
        # would destroy work nobody named, so it refuses — and names the
        # children, because "reparent them" was advice nobody could follow (no
        # surface reparents a node) and "undo them" is wrong when the child was
        # itself written inside a cycle, which `undo` refuses by design.
        return children, (
            f"node {node_id} still has {len(children)} child node(s) ({_named_rows(children)}) — "
            "take those back first: undo their creation, or roll back the cycle that made them"
        )
    occupants = surviving(
        conn.execute(
            "SELECT id FROM nodes WHERE space_id = ? AND id != ?", (node_id, node_id)
        ).fetchall(),
        "id",
        doomed_nodes,
    )
    if occupants:
        return occupants, (
            f"space {node_id} still holds {len(occupants)} node(s) ({_named_rows(occupants)}) — "
            "take those back first: undo their creation, or roll back the cycle that made them"
        )
    redirects = surviving(
        conn.execute(
            "SELECT tombstone_id FROM merge_redirects WHERE tombstone_id = ? OR into_id = ?",
            (node_id, node_id),
        ).fetchall(),
        "tombstone_id",
        doomed_redirects,
    )
    if redirects:
        return redirects, (
            f"node {node_id} is named by {len(redirects)} merge redirect(s) "
            f"({_named_rows(redirects)}) — roll back the consolidation cycle that merged it first"
        )
    granted = surviving(
        conn.execute("SELECT agent_id FROM grants WHERE space_id = ?", (node_id,)).fetchall(),
        "agent_id",
        frozenset(),
    )
    if granted:
        # `grants.space_id` references `nodes(id)`, so a space a cycle created
        # and a human has since delegated cannot be deleted. Revoking is the
        # follow-through, and it reaches a space in any state.
        return granted, (
            f"space {node_id} still carries {len(granted)} grant(s) ({_named_rows(granted)}) — "
            "revoke them first"
        )
    typed = surviving(
        conn.execute(
            "SELECT id FROM nodes WHERE type_id = ? AND id != ?", (node_id, node_id)
        ).fetchall(),
        "id",
        doomed_nodes,
    )
    if typed:
        # `nodes.type_id` references `nodes(id)` since migration 0009 — a type
        # is a node — so a type node that has since been used to type anything
        # is held down by every node wearing it.
        return typed, (
            f"type {node_id} still types {len(typed)} node(s) ({_named_rows(typed)}) — "
            "take those back first: undo their creation, or roll back the cycle that made them"
        )
    typed_edges = surviving(
        conn.execute("SELECT id FROM edges WHERE type_id = ?", (node_id,)).fetchall(),
        "id",
        doomed_edges,
    )
    if typed_edges:
        # `edges.type_id` references `nodes(id)` since migration 0009 — an
        # edge's type is a node — so a type node that has since been used to
        # type any edge is held down by every edge wearing it, exactly as a
        # node type is by the nodes wearing it.
        return typed_edges, (
            f"type {node_id} still types {len(typed_edges)} edge(s) "
            f"({_named_rows(typed_edges)}) — take those back first: undo their "
            "creation, or roll back the cycle that made them"
        )
    return None


def _delete_created_row(
    conn: sqlite3.Connection,
    kind: str,
    table: str,
    row: dict[str, Any],
    context: str,
) -> list[dict[str, Any]]:
    """Reverse a create: delete the row, refusing to cascade past what it made.

    Args:
        conn: The open connection (the caller commits).
        kind: ``node`` or ``edge``.
        table: ``nodes`` or ``edges``.
        row: The row to remove, as it currently stands.
        context: The refusal's leading phrase.

    Returns:
        Every row removed — dependencies first, the row itself last, which is
        the shape :attr:`UndoResult.deleted` has always had and the order
        :func:`_reinsert_rows` reads back.

    Raises:
        UndoNotPossible: If the graph has grown something onto the row that the
            reversal was never asked to touch — every foreign key into
            ``nodes(id)`` that the deletion does not remove itself, checked by
            :func:`_delete_blocker` (the incident edges and versions below are
            the ones it does remove, so the guard is told about them and skips
            them), which :func:`_rollback_plan` also reads so that the preflight
            and the run agree about what will happen.
    """
    deleted: list[dict[str, Any]] = []
    if kind == "node":
        # No `doomed_nodes` or `doomed_redirects`: this is the apply path, and a
        # rollback deletes newest-first, so anything it will remove is already
        # gone by now. The incident edges are the one set that still stands —
        # they go below, and the `edges.type_id` guard must not count what the
        # delete itself removes, or it would refuse a deletion that in fact
        # succeeds.
        incident = conn.execute(
            "SELECT * FROM edges WHERE src_id = ? OR dst_id = ?",
            (row["id"], row["id"]),
        ).fetchall()
        blocker = _delete_blocker(
            conn, row["id"], doomed_edges=frozenset(str(edge["id"]) for edge in incident)
        )
        if blocker is not None:
            raise UndoNotPossible(f"{context}: {blocker[1]}")
        for edge in incident:
            deleted.append({"table": "edges", "row": _row_dict(edge)})
            conn.execute("DELETE FROM edges WHERE id = ?", (edge["id"],))
        for version in conn.execute(
            "SELECT * FROM versions WHERE node_id = ?", (row["id"],)
        ).fetchall():
            deleted.append({"table": "versions", "row": _row_dict(version)})
            conn.execute("DELETE FROM versions WHERE id = ?", (version["id"],))
    deleted.append({"table": table, "row": row})
    conn.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
    return deleted


def _restore_row(
    conn: sqlite3.Connection,
    kind: str,
    table: str,
    before: dict[str, Any],
    principal: Principal,
    context: str,
) -> dict[str, Any]:
    """Write a recorded row back verbatim — every column, past every guard.

    This UPDATE writes the recorded row back past ``TRANSITIONS`` and past every
    guard an ordinary write passes, which is precisely why both callers are
    human-only. A space's title is the one column here that can land on a name
    something else now holds: restoring an *archived* space cannot (the name was
    never freed — migration ``0013``), but reversing a **rename** still can —
    create ``x``, rename it to ``y``, create a new ``x``, reverse the rename —
    so it is checked rather than left to the unique index, which would surface
    as a bare IntegrityError and a 500 on ``/api/undo``.

    Raises:
        UndoNotPossible: If the row no longer exists, or its title would land on
            a space name something else now holds.
    """
    if kind == "node" and before.get("type_id") == "space":
        try:
            _require_space_name_free(conn, before["title"], principal, exclude_id=before["id"])
        except SpaceNameTaken as clash:
            raise UndoNotPossible(f"{context}: {clash}") from clash
    columns = [key for key in before if key != "id"]
    assignments = ", ".join(f"{key} = ?" for key in columns)
    cursor = conn.execute(
        f"UPDATE {table} SET {assignments} WHERE id = ?",
        (*[before[key] for key in columns], before["id"]),
    )
    # An UPDATE that matched nothing restores nothing: the row was deleted after
    # this event (typically by reversing its create), so reporting it restored
    # would be a lie and marking the event reversed would bury it.
    if cursor.rowcount == 0:
        raise UndoNotPossible(f"{context}: {kind} {before['id']} no longer exists")
    return before


#: Payload key carrying the **version row's own** before/after on the event
#: that moved it. A review changes two rows from one decision: the node, which
#: is a graph record with an event of its own, and the ``versions`` row's
#: ``state``, which is not. Accepting therefore hangs the second off the first,
#: exactly as a merge hangs its ``merge_redirects`` row off the tombstone's
#: ``node.merge`` — *state changed outside the event log is state a reversal
#: cannot see*, and this is the fourth row in this file to learn it.
VERSION_STATE_KEY = "version_state"

#: Kinds a reversal can put a row back for, and the table each lives in.
#: ``version`` is here and deliberately **not** in :data:`_TABLE_KIND`: the two
#: maps answer different questions. A version row is *reversible* (a review
#: decision moved it, and :func:`_restore_row` / :func:`_restore_version_state`
#: put it back), but it carries no conflict of **its own** in the
#: :data:`_TABLE_KIND` sense — the conflict map is keyed by payload table name,
#: and a version row's moves ride on the ``node.update`` an accept caused or a
#: ``version.`` event, which :func:`_touched_rows` resolves by op rather than by
#: table. ``versions.state`` has two writers, not one: :func:`_transition_row`
#: moves a row out of ``proposed``, and :func:`_restore_version_state` (via
#: :func:`_restore_row`) moves it back on a rollback or an undo — which is
#: exactly the later write a conflict check has to see.
_REVERSIBLE_TABLES = {"node": "nodes", "edge": "edges", "version": "versions"}

#: The ``version.`` ops a reversal can read, named rather than matched by
#: prefix. **The namespace is not the predicate; the payload shape is.**
#: ``version.propose`` is in the same namespace and is not here: it records the
#: *creation* of a version row rather than a move of one, its ``before`` is the
#: **node** row, it carries no ``after`` at all, and it does not name the
#: version it made — the event is emitted before the insert so the row can point
#: back at it. A prefix match over the namespace swept it into both reversal
#: verbs, where a cycle that staged a proposal and reviewed it in the same run
#: died on ``KeyError: 'after'``. Reversing a *proposal* would be a different
#: operation with a different payload; rejecting it is the one that exists.
_REVERSIBLE_VERSION_OPS = ("version.reject", "version.rollback")


def _is_reversible(op: str) -> bool:
    """Does this event record a row move a reversal can write back?

    The one question :func:`undo` and :func:`_rollback_plan` both have to answer
    the same way, so they ask it here rather than each spelling out a filter.
    """
    kind = op.split(".", 1)[0]
    if kind == "version":
        return op in _REVERSIBLE_VERSION_OPS
    return kind in _REVERSIBLE_TABLES


def _restore_version_state(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    principal: Principal,
    context: str,
) -> dict[str, Any] | None:
    """Put a version row back to where a payload's :data:`VERSION_STATE_KEY` says it was.

    The accept half of the version-review reversal. Reversing the
    ``node.update`` an accept emits restores the node and would leave the
    proposal marked ``applied`` — permanently, since a version only ever leaves
    ``proposed`` once, so nothing could accept or reject it again.

    Args:
        conn: The open connection (the caller commits).
        payload: The event payload being reversed. An event that moved no
            version simply lacks the key.
        principal: Passed through to :func:`_restore_row`.
        context: The refusal's leading phrase.

    Returns:
        The **mirrored** record for the reversal's own payload (``before`` and
        ``after`` swapped), or ``None`` when the event moved no version. The
        mirror is what makes reversing a reversal re-apply the accept, at every
        depth, without a second inverse code path — the rule the rest of this
        module already holds for node and edge rows.
    """
    recorded = payload.get(VERSION_STATE_KEY)
    if recorded is None:
        return None
    _restore_row(conn, "version", "versions", recorded["before"], principal, context)
    return {"before": recorded["after"], "after": recorded["before"]}


def _accept_snapshot_row(
    conn: sqlite3.Connection, event_seq: int, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """The snapshot row an accept's ``node.update`` wrote, if this event is one.

    An accept writes a true snapshot of the accepted node (finding M9),
    stamped with the accept event's own seq — the one ``versions`` row no
    other event shares that stamp with. Reversing the accept takes that row
    with it: it is a record of the exact state the reversal exists to take
    back, and leaving it would show history a state the accept was already
    undone from. The caller records the row in the reversal's ``deleted``, so
    reversing the reversal re-inserts it (the involution the rest of these
    payloads already hold).

    Only events that moved a version row (a :data:`VERSION_STATE_KEY` payload)
    get this. An ordinary edit's snapshot is history like any other
    (:func:`_write_version` on every other node mutation) and stays; and a
    rollback's own snapshot is removed when *that* rollback is reversed, by
    the mirror it carries, which is the same rule applied to its own event.
    """
    if payload.get(VERSION_STATE_KEY) is None:
        return None
    row = conn.execute(
        "SELECT * FROM versions WHERE node_id = ? AND event_seq = ?",
        (payload["before"]["id"], event_seq),
    ).fetchone()
    return None if row is None else _row_dict(row)


def _reinsert_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """Put back rows a reversal removed, in foreign-key order.

    The inverse of :func:`_delete_created_row`, and the reason a rollback can
    itself be rolled back: a reversal that deleted a node recorded the node, its
    versions and its incident edges, and this puts all three back exactly as
    they were — ids included, since an id is what every other row references.

    Args:
        conn: The open connection (the caller commits).
        rows: ``{"table", "row"}`` entries as recorded in a reversal payload.

    Raises:
        ValueError: If an entry names a table outside :data:`REINSERT_ORDER`.
    """
    unknown = sorted({entry["table"] for entry in rows} - set(REINSERT_ORDER))
    if unknown:
        raise ValueError(f"cannot restore rows from unknown table(s): {', '.join(unknown)}")
    for entry in sorted(rows, key=lambda entry: REINSERT_ORDER.index(entry["table"])):
        table, row = entry["table"], entry["row"]
        columns = list(row)
        conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            tuple(row[column] for column in columns),
        )


#: What :func:`undo` can reverse, as SQL — :func:`_is_reversible`'s predicate in
#: the form the newest-first search needs, and not its own reversal. The version
#: ops are **listed**, never `LIKE 'version.%'`: `version.propose` is in that
#: namespace and reverses nothing (see :data:`_REVERSIBLE_VERSION_OPS`), so a
#: prefix here would make a bare ``undo`` after an agent's proposal stop on an
#: event it then cannot reverse. The names are module constants, interpolated
#: the way the table names in :func:`_reinsert_rows` are.
_UNDOABLE_OPS = (
    "op != 'undo' AND (op LIKE 'node.%' OR op LIKE 'edge.%' OR op IN ("
    + ", ".join(f"'{op}'" for op in _REVERSIBLE_VERSION_OPS)
    + "))"
)


def _latest_undoable(conn: sqlite3.Connection, reversed_seqs: set[int]) -> sqlite3.Row | None:
    """The most recent event :func:`undo` could reverse, or ``None``.

    Args:
        conn: The open connection.
        reversed_seqs: Seqs a previous ``undo`` already took back.
    """
    clauses = [_UNDOABLE_OPS]
    if reversed_seqs:
        clauses.append(f"seq NOT IN ({','.join(str(seq) for seq in sorted(reversed_seqs))})")
    return conn.execute(
        f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY seq DESC LIMIT 1"
    ).fetchone()


def _cycle_stamped_refusal(conn: sqlite3.Connection, event: sqlite3.Row) -> str:
    """Refuse an undo of a cycle-stamped event, naming the verb that takes it back.

    The message ends at ``nodum rollback <cycle-id>``, and that is the whole of
    the advice because it is the whole of what works. It briefly also named "the
    last write outside a cycle" as an ``undo <seq>`` a caller could still run,
    on the premise that pointing at rollback alone was a loop — a rollback is
    itself a cycle, so its own events are stamped too. **The premise was wrong
    and the sentence was harmful.** ``nodum rollback <cycle>`` is not a loop: it
    reverses the cycle, and there is no state in which a human needs a bare
    ``undo`` afterwards. Meanwhile the event it named is exactly the one
    :func:`undo` refuses to step over — a human who merged two nodes and ran it
    got an unrelated edit undone and the edge the merge had just relinked
    deleted with it, and that undo then became a conflict standing between the
    merge and its rollback, so both reversal verbs were spent and the merge was
    permanently unrollbackable. A reversal verb that reaches past a cycle is the
    harm this refusal exists to prevent; printing one as a remedy was the
    refusal instructing the human to cause it.

    The merge sentence is **scoped to a cycle that wrote more than one row**. It
    explains why a multi-row decision cannot come apart one event at a time, and
    a cycle carrying a lone ``edge.propose`` has no other half to leave standing
    — so on that one it was explaining the refusal with something that had not
    happened. The refusal itself is unchanged: a cycle is taken back whole.
    """
    cycle_id = event["cycle_id"]
    rows_written = conn.execute(
        f"SELECT COUNT(*) AS written FROM events WHERE cycle_id = ? AND {_UNDOABLE_OPS}",
        (cycle_id,),
    ).fetchone()["written"]
    whole = (
        " — one event of a merge reversed on its own would leave the other half standing"
        if rows_written > 1
        else ""
    )
    return (
        f"cannot undo event {event['seq']} ({event['op']}): it belongs to consolidation "
        f"cycle {cycle_id}, and a cycle is taken back whole{whole}. Roll the cycle back "
        f"instead. Run: nodum rollback {cycle_id}."
    )


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
    events cannot themselves be undone. Only events naming a row a reversal can
    put back (``node.*``, ``edge.*``, ``version.*`` — :data:`_REVERSIBLE_TABLES`)
    are reversible; audited non-graph events are skipped by default and refused
    when named explicitly.

    **A version review is reversible on both halves.** Undoing the
    ``node.update`` an accept emits also puts the proposal back to ``proposed``
    (:func:`_restore_version_state`), and a ``version.reject`` is reversed the
    way any other recorded row move is. Without the first, an undo restored the
    node and left the proposal marked ``applied`` with no way to accept or
    reject it again; without the second, the rejection was the one review
    outcome no verb in this file could take back.

    Undo is the **human tier**: restoring an event's payload writes arbitrary
    prior state back, ``state = 'active'`` included, so an agent allowed to
    undo would be an agent allowed to write live state (design §8.1/§8.2).

    Reversal never cascades beyond what the event itself created: an event
    the graph has since grown past (a created node that now has children, a
    row a later undo already removed) is refused, not forced.

    **An event carrying a ``cycle_id`` is not undoable here at all.** A
    consolidation cycle is the unit a human takes back, and its writes are
    ordinary ``node.``/``edge.`` events — they have to be, since
    :mod:`nodum.projectors` dispatches on that prefix to reproject FTS and
    embeddings, so a curative op outside the namespace would desynchronise the
    search index silently. That means the "undoable" filter above would
    otherwise reverse *one row* of a multi-row merge and leave the other half
    standing. So a cycle-stamped event is refused and points at rollback — and
    the no-``seq`` search **finds** them rather than stepping over them, so that
    the refusal is what a bare ``undo`` after a curative operation gets. Only
    events a previous ``undo`` already reversed are skipped there: those have a
    reversal, while a cycle is simply the most recent thing that happened, and
    reaching past it to an older event is never what the caller meant.

    That refusal names ``nodum rollback <cycle>`` and stops there. It briefly
    also named a ``seq`` this function could still reverse — "the last write
    outside a cycle" — on the premise that rollback alone was a loop. It is not
    one: a rollback reverses its cycle, and no state follows it in which a human
    needs a bare ``undo``. The event that sentence named is precisely the one
    this function refuses to step over, so the refusal was printing the harm it
    exists to prevent as its own remedy (see :func:`_cycle_stamped_refusal`).

    Raises:
        GrantNotPermitted: If the principal is not a human.
        EventNotFound: If no event matches ``seq`` (or none exist to undo).
        UndoNotPossible: If the target row is gone, the reversal would have
            to delete rows the event never created, or the event belongs to a
            consolidation cycle.
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
        # Cycle-stamped events are **in** this search, and are refused below
        # rather than stepped over. Skipping them looked like the safe reading —
        # `undo` cannot reverse one row of a merge, so do not offer to — but the
        # no-`seq` path answers "take back the last thing that happened", and
        # reaching *past* a curative op to an older event is never what that
        # meant: a human who merged two nodes and typed the reversal verb they
        # know got an unrelated edit undone, silently, and the edge the merge
        # had just relinked deleted with it. Worse, that undo then became a
        # conflict standing between the merge and its rollback, so both reversal
        # verbs were spent and the merge was permanently unrollbackable. A
        # refusal naming the cycle costs one command; this cost the graph.
        if seq is None:
            event = _latest_undoable(conn, reversed_seqs)
        else:
            event = conn.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
        if event is None:
            raise EventNotFound(f"no event to undo (seq={seq})")
        if event["op"] == "undo":
            raise ValueError(f"event {event['seq']} is an undo event and cannot be undone")
        kind = event["op"].split(".", 1)[0]
        if not _is_reversible(event["op"]):
            raise ValueError(
                f"event {event['seq']} ({event['op']}) is not a graph event and cannot be undone"
            )
        if event["cycle_id"] is not None:
            raise UndoNotPossible(_cycle_stamped_refusal(conn, event))
        if event["seq"] in reversed_seqs:
            raise ValueError(f"event {event['seq']} has already been undone")

        payload = json.loads(event["payload"])
        table = _REVERSIBLE_TABLES[kind]
        before, after = payload["before"], payload["after"]
        context = f"cannot undo event {event['seq']} ({event['op']})"
        deleted: list[dict[str, Any]] = []

        if before is None:
            # Reverse a create: remove the created row and anything that would
            # block the delete (a node's versions and incident edges).
            deleted = _delete_created_row(conn, kind, table, after, context)
            restored = None
        else:
            restored = _restore_row(conn, kind, table, before, principal, context)
        # An accept moved a version row too, recorded on this same event.
        version_state = _restore_version_state(conn, payload, principal, context)
        # And it wrote a snapshot of the accepted node; reversing it takes the
        # snapshot with it, recorded in `deleted` so a reversal of *this* puts
        # it back (finding M9).
        accept_snapshot = _accept_snapshot_row(conn, int(event["seq"]), payload)
        if accept_snapshot is not None:
            deleted.append({"table": "versions", "row": accept_snapshot})
            conn.execute("DELETE FROM versions WHERE id = ?", (accept_snapshot["id"],))
        undo_payload: dict[str, Any] = {
            "reversed_seq": event["seq"],
            "reversed_op": event["op"],
            "restored": restored,
            "deleted": deleted,
        }
        if version_state is not None:
            undo_payload[VERSION_STATE_KEY] = version_state
        undo_seq = _emit(conn, actor, "undo", undo_payload)
        if restored is not None and kind == "node":
            _write_version(conn, restored, actor, undo_seq)
            _materialize_mentions(conn, restored, actor, store)
        conn.commit()
        return UndoResult(
            undone_seq=event["seq"],
            undone_op=event["op"],
            restored=restored,
            deleted=deleted,
            restored_version=None if version_state is None else version_state["after"],
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
    principal: Principal,
    *,
    limit: int = 50,
    cycle_id: str | None = None,
    path: str | Path | None = None,
) -> list[EventOut]:
    """Return the most recent events (newest first), capped at ``limit``.

    The event log is the audit trail — a human surface (CLI today); agents
    do not read it.

    Args:
        principal: Who is asking; must be a human.
        limit: How many events to return, newest first.
        cycle_id: Narrow to one consolidation cycle. This is what a dream-journal
            entry's diff is: the events the cycle produced, read from the log
            itself. The ``cycles`` row therefore stores no diff of its own —
            a journal that kept a second record of what happened is a journal
            that can disagree with the log.
        path: Explicit database path.

    Raises:
        ValueError: If ``limit`` is below 1 — :func:`subgraph`'s rule, and
            :func:`list_cycles`'. SQLite reads a negative ``LIMIT`` as
            *unbounded*, so ``--limit -3`` answered with the whole log: a caller
            asking for less got everything.
        GrantNotPermitted: If the principal is not a human.
        RecordNotFound: If ``cycle_id`` names no cycle. An empty list is what a
            **dry run** looks like — ``AGENTS.md`` leans on exactly that as the
            machine-checkable proof a rehearsal changed nothing — so a typo in
            the id answering with the same empty list, and exit 0, would make
            that proof unreadable.
    """
    require_positive_limit(limit)
    conn = _connect(path)
    try:
        Store(conn, principal).require_human("read the event log")
        if cycle_id is not None:
            _get_cycle_row(conn, cycle_id)
        where = "" if cycle_id is None else " WHERE cycle_id = ?"
        params: tuple[Any, ...] = (limit,) if cycle_id is None else (cycle_id, limit)
        rows = conn.execute(
            f"SELECT * FROM events{where} ORDER BY seq DESC LIMIT ?", params
        ).fetchall()
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


def list_proposal_creations(
    ids: list[str],
    *,
    path: str | Path | None = None,
) -> set[str]:
    """Which of ``ids`` were created by a ``propose`` op — a proposal, not a direct write.

    The classification the curation job's acceptance rates need (M19): a
    ``node.propose``/``edge.propose`` creation op means the row went through
    the review queue, while a ``node.create``/``edge.create`` — an
    ``edit``-grant write, a materialised wikilink, an ingest subgraph — landed
    ``active`` directly and never was a proposal. Row state cannot tell the
    two apart: both end ``active``, on accept or by grant. The creation
    event's op is the distinction, so it is read here.

    This is the **one deliberate exception to "the event log is a human
    surface"** (:func:`list_events` refuses an agent), and it is shaped to
    disclose nothing the caller does not already hold: it answers only about
    the ids the caller supplies — the rows it read through its own grants —
    one bit per id, no node, space or count beyond that. The parallel is
    :func:`stop_requested`, the other deliberately not-human-only journal
    read: a runner that cannot ask whether it was told to stop cannot obey,
    and a curation job that cannot tell proposals from direct writes cannot
    report an honest acceptance rate.

    A row is classified by the op of the event that created it; a payload
    with no ``after.id`` (an old or foreign event) classifies nothing.

    Args:
        ids: The row ids to classify. Ids with no ``propose`` creation event
            are simply absent from the answer.
        path: Explicit database path.

    Returns:
        The subset of ``ids`` whose creation event op was ``node.propose`` or
        ``edge.propose``.
    """
    if not ids:
        return set()
    conn = _connect(path)
    try:
        created: set[str] = set()
        for (payload,) in conn.execute(
            "SELECT payload FROM events WHERE op IN ('node.propose', 'edge.propose')"
        ).fetchall():
            after = json.loads(payload).get("after")
            if isinstance(after, dict) and isinstance(after.get("id"), str):
                created.add(after["id"])
        return created & set(ids)
    finally:
        conn.close()


#: The only actor the synthesis verification accepts (M21) — the gardener, the
#: one principal that writes through :mod:`nodum.consolidate`. ``actor_string``
#: renders ``agent:<id>``; spelled here so :func:`_synthesized_creation_ids`
#: holds the log's actor strings up against it.
GARDENER_ACTOR = f"agent:{GARDENER_AGENT_ID}"


def _synthesized_creation_ids(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    """Which of ``ids`` were created by the gardener's abstraction write.

    The verification behind :func:`is_synthesis` and
    :func:`synthesized_node_ids`, over an open connection. A node is a
    synthesis iff the event that created it was the gardener's abstraction
    write — the op names the create (a node has exactly one create event), the
    event's actor is the gardener, and its ``after.props.synthesized`` is
    truthy. The event is append-only, so none of that is forgeable by a later
    write: forging the props makes a *new* event (``node.update``), it does
    not rewrite the create.

    The scan-and-intersect shape mirrors :func:`list_proposal_creations`
    (M19): one pass over the create events, cheap in SQLite's C filter and
    paid per call rather than per candidate, so a batch of any size costs the
    same single scan.

    Args:
        conn: The open connection (the caller commits).
        ids: The row ids to classify. Ids with no qualifying create event are
            simply absent from the answer.
    """
    wanted = set(ids)
    if not wanted:
        return set()
    verified: set[str] = set()
    for row in conn.execute(
        "SELECT actor, payload FROM events WHERE op IN ('node.create', 'node.propose')"
    ).fetchall():
        if row["actor"] != GARDENER_ACTOR:
            continue
        after = json.loads(row["payload"]).get("after")
        if not isinstance(after, dict) or not isinstance(after.get("id"), str):
            continue
        if after["id"] not in wanted:
            continue
        props = after.get("props")
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except json.JSONDecodeError:
                continue
        if isinstance(props, dict) and props.get("synthesized"):
            verified.add(after["id"])
    return verified


def is_synthesis(
    node_id: str,
    *,
    conn: sqlite3.Connection | None = None,
    path: str | Path | None = None,
) -> bool:
    """Whether ``node_id`` is a synthesis — a fact about the event that wrote it.

    ``props.synthesized`` is forgeable by any writer (M21): a node whose props
    carry the flag but whose create event was not the gardener's abstraction
    write is not a synthesis, whatever its current props say. The distinction
    lives in the append-only create event — its op names the create, its actor
    is the gardener, and its ``after.props.synthesized`` is truthy. A forged
    flag — at create or by a later ``update_node`` — makes an event with a
    different actor or a different op; it does not rewrite the create.

    This is the **second deliberate exception to "the event log is a human
    surface"** (:func:`list_events` refuses an agent), beside
    :func:`list_proposal_creations`, and it is shaped the same way: it answers
    only about the id the caller already holds, one bit, no node, space or
    count beyond that. It is what lets the review path settle a genuine
    synthesis's membership edges with the concept (:func:`_settle_synthesis_edges`)
    and what lets the abstraction job's freshness gate trust the flag it
    itself wrote.

    Args:
        node_id: The node id to classify.
        conn: An open connection to read through — for a caller already inside
            a transaction (:func:`_settle_synthesis_edges`), where a second
            connection could not see uncommitted writes and a bulk review's
            immediate lock would refuse it outright.
        path: Explicit database path, when ``conn`` is not given.

    Returns:
        ``True`` iff the node's create event was the gardener's synthesis
        write.
    """
    if conn is None:
        conn = _connect(path)
        try:
            return bool(_synthesized_creation_ids(conn, [node_id]))
        finally:
            conn.close()
    return bool(_synthesized_creation_ids(conn, [node_id]))


def synthesized_node_ids(
    ids: list[str],
    *,
    conn: sqlite3.Connection | None = None,
    path: str | Path | None = None,
) -> set[str]:
    """Which of ``ids`` are syntheses, verified against their create events.

    The batched form of :func:`is_synthesis`, shaped like
    :func:`list_proposal_creations`: the ids the caller supplies, answered one
    bit each, nothing beyond. The abstraction job's freshness gate uses it over
    the nodes whose props carry the marker, so a forged flag an ordinary agent
    wrote cannot make the job skip a cluster (M21); the verification scans the
    create events once whatever the batch size, so an attacker inflating the
    candidate list pays the same single scan.

    Args:
        ids: The row ids to classify. Ids with no qualifying create event are
            simply absent from the answer.
        conn: An open connection to read through, when the caller has one.
        path: Explicit database path, when ``conn`` is not given.

    Returns:
        The subset of ``ids`` whose create event was the gardener's synthesis
        write.
    """
    if conn is None:
        conn = _connect(path)
        try:
            return _synthesized_creation_ids(conn, ids)
        finally:
            conn.close()
    return _synthesized_creation_ids(conn, ids)


# ── Asset-pipeline events (design §5.5–§5.7): one named door into the log ─────

#: The only ops :func:`record_asset_event` will write — an **allowlist**, not a
#: ``asset.*`` prefix test. Ingestion (:mod:`nodum.ingest`), the extraction
#: write (:mod:`nodum.assets`) and the capability URLs (:mod:`nodum.urls`) live
#: outside this module and need to append to the append-only log; a helper that
#: took any dotted string would hand them the ability to forge a
#: ``node.create`` or an ``undo``, which is a far larger door than either of
#: them asked for.
ASSET_EVENT_OPS = (
    "asset.ingest",
    "asset.extract",
    "asset.download_url",
    "asset.upload_url",
    "asset.upload",
    "asset.download",
)


def record_asset_event(
    op: str,
    payload: dict[str, Any],
    *,
    principal: Principal | None = None,
    actor: str | None = None,
    conn: sqlite3.Connection | None = None,
    path: str | Path | None = None,
) -> int:
    """Append one asset-pipeline event to the log.

    ``op`` must be named in :data:`ASSET_EVENT_OPS`; anything else is refused.
    That allowlist is the whole point of routing these writes through a
    function instead of exporting :func:`_emit`: the ingestion pipeline and
    the capability URLs need to record what they did, not the ability to write
    any event they like.

    These events are **audit-only by construction**, not by convention:
    :func:`undo` reverses ``node.*`` / ``edge.*`` events only — it skips
    everything else when picking the latest reversible event, and refuses a
    non-graph op by name when one is asked for explicitly — so an ``asset.*``
    entry can be read and listed forever and never replayed into state.

    Payloads are metadata only: hashes, node ids, token ids, counts, reasons.
    Never blob bytes (an original does not belong in a log every projector
    rebuild reads end to end) and never a live credential.

    **``actor`` is not a second identity channel.** Every caller that *has* a
    principal must pass it; the string form exists for exactly two cases,
    neither with a live principal **by design**. One is the redemption of a
    capability URL, where a capability carries no ambient credential and the
    only truthful actor is the ``created_by`` already stored on the token row —
    read from the database, never from a request, and no adapter may reach this
    argument (the HTTP surface's ``_write`` refuses a caller-supplied identity
    before anything gets here). The other is the extraction step
    (:func:`nodum.assets.set_extracted_text` writing ``asset.extract``), which
    takes no principal and is attributed to the system itself
    (:data:`nodum.assets.EXTRACT_ACTOR`).

    **``conn`` keeps a spend and its audit entry in one transaction.** A
    single-use token whose redemption committed while its log entry did not
    would be precisely the gap the design's "log both ends" rule exists to
    close, and a second connection cannot share the first's atomicity. The
    caller owns the commit when it passes one.

    Args:
        op: The event op; must be one of :data:`ASSET_EVENT_OPS`.
        payload: JSON-serialisable metadata describing what happened.
        principal: Who performed it. Required unless ``actor`` is given.
        actor: Actor string for a caller with no principal: the capability
            redemption path (read from stored state, never from a request) or
            the principal-less extraction write (see :data:`nodum.assets.EXTRACT_ACTOR`).
            Mutually exclusive with ``principal``.
        conn: An open connection to write within; the caller then commits.
            Defaults to a short-lived connection this function commits itself.
        path: Explicit database path; defaults to ``NODUM_DB`` resolution.
            Ignored when ``conn`` is given.

    Returns:
        The new event's ``seq``.

    Raises:
        ValueError: If ``op`` is not allowlisted, or if the caller gave both
            an identity and none.
    """
    if op not in ASSET_EVENT_OPS:
        raise ValueError(f"op must be one of {ASSET_EVENT_OPS}, got {op!r}")
    if (principal is None) == (actor is None):
        raise ValueError("pass exactly one of principal= or actor=")
    # The guard proves exactly one identity was passed. Prefer the explicit
    # string; when it is absent, the principal — which the guard guarantees
    # is present in that case — is the identity.
    if principal is not None:
        actor_string = principal.actor_string
    elif actor is not None:
        actor_string = actor
    else:
        raise RuntimeError("unreachable: the guard guarantees exactly one identity")
    if conn is not None:
        return _emit(conn, actor_string, op, payload)
    own_conn = _connect(path)
    try:
        seq = _emit(own_conn, actor_string, op, payload)
        own_conn.commit()
        return seq
    finally:
        own_conn.close()


# ── Auth events and the login lockout (finding M5): the other named door ──────

#: The only ops :func:`record_auth_event` will write — the auth half of the
#: audit trail. An allowlist, not a ``human.*`` prefix test, for the same
#: reason :data:`ASSET_EVENT_OPS` is one: the HTTP adapter lives outside this
#: module and needs to record that a login happened, not the ability to write
#: any event it likes — a helper that took any dotted string could forge a
#: ``node.create`` or an ``undo``.
AUTH_EVENT_OPS = (
    "human.login",
    "human.login_failed",
    "human.logout",
)

#: The ``events.actor`` value for something nobody authenticated did — today
#: only a failed login. Deliberately not parseable as a principal: it carries
#: neither the ``human:`` nor the ``agent:`` prefix
#: :attr:`~nodum.principal.Principal.actor_string` mints, so it can never be
#: mistaken for an account and :func:`nodum.auth.principal_from_actor` refuses
#: it as malformed rather than resolving it to somebody.
UNAUTHENTICATED_ACTOR = "unauthenticated"


def record_auth_event(
    op: str,
    payload: dict[str, Any],
    *,
    path: str | Path | None = None,
) -> int:
    """Append one password-login event to the log — the auth half of the audit trail.

    ``op`` must be named in :data:`AUTH_EVENT_OPS`; anything else is refused.
    Like :func:`record_asset_event`, this is the one exported writer to the
    log for its domain, and the allowlist is the point: :func:`_emit` stays
    private, and the HTTP adapter — the only surface a password is ever
    presented on — records login outcomes through this door and no other.

    The log's ``actor`` is derived from the op, never taken from the caller:
    a verified principal does not exist on every path here, and a request
    must never be able to name an identity. ``human.login`` and
    ``human.logout`` record the verified human (the payload must carry its
    ``human_id``, and the actor is ``human:<id>``, exactly as the service's
    own ``human.*`` events are attributed).

    ``human.login_failed`` records :data:`UNAUTHENTICATED_ACTOR`, and the
    attempted name stays in the *payload* where it belongs. It used to be the
    actor, on the reasoning that a failure has no verified principal so the
    column should say what the attempt claimed — but this route is the one
    ``/api`` path outside the session gate, so that made ``events.actor`` a
    field an unauthenticated caller writes. ``{"name": "human:owner"}`` put
    sixty rows attributed to the seeded owner in the log with no credential
    presented, and :func:`list_events` returned them interleaved with the real
    owner's and indistinguishable from them. It is the log's only reader — what
    ``nodum events`` prints — it orders by ``seq``, and it filters on nothing
    but ``cycle_id``: there is no actor filter to separate the forgeries out
    with, and one would not help, because it would be filtering on a column the
    attempt chose. The actor column is this system's answer to *who did this*;
    the only truthful answer on a failed login is *nobody*, and a claimed name
    is data about the attempt rather than an identity.

    Payloads are metadata only: ids, the attempted name, a reason. Never a
    password, and never a credential hash.

    These events are **audit-only by construction**: :func:`undo` reverses
    ``node.*`` / ``edge.*`` events only, so a ``human.*`` entry can be read
    and listed forever and never replayed into state.

    Args:
        op: The event op; must be one of :data:`AUTH_EVENT_OPS`.
        payload: JSON-serialisable metadata describing the login outcome.
        path: Explicit database path; defaults to ``NODUM_DB`` resolution.

    Returns:
        The new event's ``seq``.

    Raises:
        ValueError: If ``op`` is not allowlisted, or the payload does not
            name the identity the op's actor needs.
    """
    if op not in AUTH_EVENT_OPS:
        raise ValueError(f"op must be one of {AUTH_EVENT_OPS}, got {op!r}")
    if op == "human.login_failed":
        name = payload.get("name")
        if not isinstance(name, str):
            raise ValueError("a 'human.login_failed' payload must carry the attempted 'name'")
        actor = UNAUTHENTICATED_ACTOR
    else:
        human_id = payload.get("human_id")
        if not isinstance(human_id, str):
            raise ValueError(f"a {op!r} payload must carry the verified 'human_id'")
        actor = f"human:{human_id}"
    own_conn = _connect(path)
    try:
        seq = _emit(own_conn, actor, op, payload)
        own_conn.commit()
        return seq
    finally:
        own_conn.close()


#: Failed login attempts within :data:`LOGIN_LOCKOUT_WINDOW_MINUTES` that lock
#: a login name out of password login (finding M5). Five wrong attempts is a
#: pattern rather than a typo streak, and the window means the count is about
#: *recent* failures.
LOGIN_MAX_FAILED_ATTEMPTS = 5

#: How far back (minutes) a failed login still counts toward the lockout. The
#: lockout clears once the window slides past enough failures — that is, after
#: the attempts stop for the window's duration — so this one number is both
#: the counting window and the cooldown.
LOGIN_LOCKOUT_WINDOW_MINUTES = 15


def login_failure_count(name: str, *, path: str | Path | None = None) -> int:
    """How many recent failed login attempts ``name`` has absorbed.

    The audit trail **is** the state: the count is read off the
    ``human.login_failed`` events themselves, so there is no second record of
    who failed to log in that could disagree with the log, and no migration.
    The cost is one query per login attempt — a short scan of the events
    table, fine at this scale.

    Two rules keep the count honest rather than a stale total:

    - **The window.** Only failures inside
      :data:`LOGIN_LOCKOUT_WINDOW_MINUTES` count, so a burst long past does
      not keep a name locked forever.
    - **Reset on success.** Failures before the name's last successful login
      are ignored — a success proves the human behind the name got through,
      which is what "consecutive" means. The success event itself is the
      reset: there is nothing to delete.

    The count is per **attempted name**, existing account or not — the
    lockout must not be an existence oracle, so it cannot look the name up
    before deciding.

    Args:
        name: The login name the attempts claimed.
        path: Explicit database path.

    Returns:
        The number of failed attempts within the window since the name last
        logged in successfully.
    """
    conn = _connect(path)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM events
            WHERE op = 'human.login_failed'
              AND json_extract(payload, '$.name') = ?
              AND created_at >= datetime('now', ?)
              AND seq > COALESCE(
                  (SELECT MAX(e2.seq) FROM events e2
                   JOIN humans h ON h.id = json_extract(e2.payload, '$.human_id')
                   WHERE e2.op = 'human.login' AND h.name = ?),
                  0)
            """,
            (name, f"-{LOGIN_LOCKOUT_WINDOW_MINUTES} minutes", name),
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()


def login_is_locked(name: str, *, path: str | Path | None = None) -> bool:
    """Whether password login for ``name`` is currently refused by the lockout.

    The lockout applies to the **attempt**, not the account: the same failed
    attempts against a name that does not exist lock it exactly as they would
    a real one, so an attacker cannot learn which names exist by watching who
    gets a 429.

    Args:
        name: The attempted login name.
        path: Explicit database path.

    Returns:
        True when :func:`login_failure_count` has reached
        :data:`LOGIN_MAX_FAILED_ATTEMPTS` within the window.
    """
    return login_failure_count(name, path=path) >= LOGIN_MAX_FAILED_ATTEMPTS


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
DIRECTIONS = DIRECTIONS


def _walk(
    conn: sqlite3.Connection,
    start_id: str,
    *,
    type_ids: list[str] | None,
    depth: int,
    direction: Direction,
    store: Store,
    as_of: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Breadth-first walk over ``active`` edges; returns (node rows, edge rows).

    The root comes first in the node list; every edge the walk crossed is
    returned once (including edges between two visited nodes). Proposed and
    archived edges are never followed — reads default to the live graph. An
    edge is followed only when **both** endpoints are readable by the
    principal, so the walk never crosses into an unreadable space (Q13).

    **The endpoint rows are re-checked here, not assumed from that clause.**
    Delegating the whole guarantee to :meth:`Store.edge_scope` is what let a
    space node reach an agent holding no grant on it: that clause tested
    ``space_id`` where the node rule tests a space node's *own id*, and this
    loop then loaded both endpoints with an unscoped row read. The clause is
    fixed, and the row check below is the second layer — a node read in this
    module that does not pass :meth:`Store.node_visible` is the shape of that
    defect, whatever the SQL upstream says. That is a claim about the module,
    so the two other walks that load an endpoint the same way apply it too:
    :func:`subgraph` drops the edge, :func:`find_path` drops the path. An
    invariant applied at one of three sites is a comment, not an invariant.

    ``as_of`` swaps the lens from the live graph to the graph true at an
    instant (D2): the walk then follows ``active`` edges plus ``archived``
    ones whose validity window covered that instant (:func:`_as_of_edge_clause`).
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
        if as_of is not None:
            # `as_of_clause` leads the WHERE, so its params lead the list.
            as_of_clause, as_of_params = _as_of_edge_clause(as_of, DEFAULT_EDGE_STATES)
            sql = f"SELECT * FROM edges WHERE {as_of_clause} AND {where}{scope}"
            params = [*as_of_params, *params, *scope_params]
        else:
            sql = f"SELECT * FROM edges WHERE state = 'active' AND {where}{scope}"
            params += scope_params
        if type_ids:
            sql += f" AND type_id IN ({','.join('?' * len(type_ids))})"
            params += type_ids
        for edge_row in conn.execute(f"{sql} ORDER BY created_at, rowid", params).fetchall():
            edge = _row_dict(edge_row)
            if edge["id"] in seen_edges:
                continue
            unseen = [end for end in (edge["src_id"], edge["dst_id"]) if end not in nodes]
            fetched = {end: _get_node_row(conn, end) for end in unseen}
            # The edge clause should already have excluded an edge with an
            # unreadable endpoint; reaching here means the two rules
            # disagreed, and the answer is to drop the edge whole rather than
            # return it with an endpoint the principal may not see.
            if not all(store.node_visible(row) for row in fetched.values()):
                continue
            seen_edges.add(edge["id"])
            edges.append(edge)
            for endpoint, row in fetched.items():
                nodes[endpoint] = _row_dict(row)
                order.append(endpoint)
                next_frontier.add(endpoint)
        frontier = next_frontier
    return [nodes[node_id] for node_id in order], edges


def get_neighborhood(
    node_id: str,
    *,
    depth: int = 1,
    as_of: str | None = None,
    principal: Principal,
    path: str | Path | None = None,
) -> SubgraphOut:
    """Return a node plus its active-edge neighborhood out to ``depth`` hops.

    Depth 0 returns the node alone. Design §8.1 ``get_node(id, depth)``.
    ``as_of`` reads the neighborhood as it was true at an instant (D2), the
    same lens :func:`_walk` applies.

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
            conn,
            node_id,
            type_ids=None,
            depth=depth,
            direction="both",
            store=store,
            as_of=as_of,
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
    direction: Direction = "both",
    as_of: str | None = None,
    principal: Principal,
    path: str | Path | None = None,
) -> SubgraphOut:
    """Walk the subgraph reachable from ``start_id`` over active edges.

    The pattern parameters (design §8.1 / T2): ``edge_types`` restricts the
    walk to those edge types (ids or names), ``depth`` caps the hops, and
    ``direction`` (``out`` / ``in`` / ``both``) orients it. ``as_of`` reads
    the graph as it was true at an instant (D2) — ``archived`` edges whose
    window covered it are followed too (:func:`_walk`).

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
            [_resolve_edge_type(conn, edge_type, principal)[0] for edge_type in edge_types]
            if edge_types
            else None
        )
        nodes, edges = _walk(
            conn,
            start_id,
            type_ids=type_ids,
            depth=depth,
            direction=direction,
            store=store,
            as_of=as_of,
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
    edge_states: list[NodeState] | None = None,
    min_confidence: float | None = None,
    created_by: str | None = None,
    node_types: list[str] | None = None,
    as_of: str | None = None,
    limit: int = 200,
    principal: Principal,
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
            as_of: Read the subgraph as it was true at an instant (D2): an
                edge is followed iff its validity window covered it —
                ``archived`` edges whose window covered the instant are
                admitted even when ``edge_states`` does not name ``archived``
                (:func:`_as_of_edge_clause`).
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
    require_positive_limit(limit)
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
        edge_clauses: list[str] = []
        edge_params: list[Any] = []
        if as_of is not None:
            # The window clause replaces the state filter: under as-of the
            # filter only narrows which *live* states are followed, and a
            # window-covered archived edge is admitted regardless — the walk
            # shows the graph as it was true at the instant.
            as_of_clause, as_of_params = _as_of_edge_clause(as_of, states)
            edge_clauses.append(as_of_clause)
            edge_params += as_of_params
        else:
            edge_clauses.append(f"state IN ({','.join('?' * len(states))})")
            edge_params = list(states)
        scope, scope_params = store.edge_scope()
        if scope:
            edge_clauses.append(scope.removeprefix(" AND "))
            edge_params += scope_params
        if edge_types:
            type_ids = [
                _resolve_edge_type(conn, edge_type, principal)[0] for edge_type in edge_types
            ]
            edge_clauses.append(f"type_id IN ({','.join('?' * len(type_ids))})")
            edge_params += type_ids
        if min_confidence is not None:
            edge_clauses.append("confidence IS NOT NULL AND confidence >= ?")
            edge_params.append(min_confidence)
        if created_by is not None:
            edge_clauses.append("created_by = ?")
            edge_params.append(created_by)
        admissible_types = (
            {_resolve_node_type(conn, node_type, principal) for node_type in node_types}
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
                    far_row = _get_node_row(conn, far)
                    if not store.node_visible(far_row):
                        # `_walk`'s second layer, applied here for the
                        # same reason: `edge_scope` should already have
                        # excluded an edge with an unreadable endpoint, so
                        # reaching this is the two rules disagreeing — and the
                        # answer is to drop the edge whole rather than return
                        # it with an endpoint the principal may not see.
                        continue
                    row = _row_dict(far_row)
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
        path_rows = [_get_node_row(conn, node_id) for node_id in node_ids]
        if not all(store.node_visible(row) for row in path_rows):
            # `_walk`'s second layer, applied here for the same reason:
            # `edge_scope` should already have kept the walk out of every space
            # this principal cannot read, so an unreadable row on the assembled
            # path is the two rules disagreeing. The answer is the one this
            # function already documents — a path through a space the principal
            # cannot read does not exist — rather than a `NodeNotFound` naming
            # an id the caller never asked about.
            return PathOut(found=False, hops=0, nodes=[], edges=[])
        return PathOut(
            found=True,
            hops=len(path_edges),
            nodes=[_node_out(row) for row in path_rows],
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

    Both versions are visibility-checked, and every refusal is the same
    :class:`VersionNotFound` naming only the id the caller passed (Q13 review
    S1): version ids are sequential integers, so a distinguishable "wrong
    node" or "unreadable" answer would enumerate the store — and the old
    cross-node message named the other node's id outright.

    Raises:
        VersionNotFound: If either version id does not resolve, sits on a node
            the principal cannot read, or the two belong to different nodes.
    """
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        version_a = _row_dict(_get_version_row(conn, a))
        version_b = _row_dict(_get_version_row(conn, b))
        for version_id, version in ((a, version_a), (b, version_b)):
            if not store.node_visible(_get_node_row(conn, version["node_id"])):
                raise VersionNotFound(f"version not found: {version_id}")
        if version_a["node_id"] != version_b["node_id"]:
            raise VersionNotFound(f"versions {a} and {b} do not belong to the same node")
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
GRANT_LEVEL_NAMES = GRANT_LEVEL_NAMES

#: Shortest password :func:`set_human_password` accepts. A floor, not a policy:
#: the empty string used to be storable over both surfaces and logged in fine.
MIN_PASSWORD_LENGTH = 6


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
        _emit(
            conn, actor, "human.create", {"before": None, "after": {"id": human_id, "name": name}}
        )
        conn.commit()
        return _human_out(row)
    finally:
        conn.close()


def set_human_password(
    human_id: str, password: str, *, principal: Principal, path: str | Path | None = None
) -> None:
    """Set or change a human's password (argon2id). The hash never enters a payload.

    Changing a password ends that human's live sessions (Q13 review S10): a
    password change is how a human reacts to a stolen cookie, and a cookie
    that outlives it makes the reaction useless.

    Raises:
        ValueError: If the password is shorter than
            :data:`MIN_PASSWORD_LENGTH`.
        RecordNotFound: If the account does not exist.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    conn = _connect(path)
    try:
        actor = _admin_actor(conn, principal)
        cursor = conn.execute(
            "UPDATE humans SET credential_hash = ? WHERE id = ?",
            (auth.hash_password(password), human_id),
        )
        if cursor.rowcount == 0:
            # `no humans row with id` is this module's schema vocabulary reaching
            # somebody who typed `nodum human passwd <id>` and has no reason to
            # know the table. Every other lookup here says `<thing> not found:
            # <id>`, and so does this one.
            raise RecordNotFound(f"human not found: {human_id}")
        conn.execute("DELETE FROM sessions WHERE human_id = ?", (human_id,))
        _emit(conn, actor, "human.password", {"human_id": human_id})
        conn.commit()
    finally:
        conn.close()


def _set_disabled(table: str, row_id: str, disabled: bool, *, conn: sqlite3.Connection) -> None:
    cursor = conn.execute(f"UPDATE {table} SET disabled = ? WHERE id = ?", (int(disabled), row_id))
    if cursor.rowcount == 0:
        # The table name is this module's schema vocabulary; the caller asked
        # about an account, so it is an account the refusal names.
        raise RecordNotFound(f"{table.rstrip('s')} not found: {row_id}")


def disable_human(human_id: str, *, principal: Principal, path: str | Path | None = None) -> None:
    """Disable a human: its sessions die, and its external agents' tokens with
    them (verification-time cascade — R3). In-flight proposals are untouched.

    The last enabled human is refused (Q13 review S13): disabling it locks
    every surface out of the file for good — ``auth.owner_principal`` refuses
    disabled humans too, so even the trusted-local CLI cannot re-enable it,
    and recovery means hand SQL.

    Raises:
        GrantNotPermitted: If ``human_id`` is the only enabled human.
        RecordNotFound: If the account does not exist.
    """
    conn = _connect(path)
    try:
        actor = _admin_actor(conn, principal)
        enabled = [
            row["id"] for row in conn.execute("SELECT id FROM humans WHERE disabled = 0").fetchall()
        ]
        if enabled == [human_id]:
            raise GrantNotPermitted(
                f"cannot disable {human_id!r}: it is the last enabled human, and a file with "
                "none can only be recovered by hand"
            )
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
    kind: AgentKind = "external",
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

    Names beginning with :data:`~nodum.migrations.BUILTIN_AGENT_PREFIX` are
    **reserved**, and that is a security property rather than naming hygiene.
    ``agents.id`` is this name verbatim, and
    :attr:`~nodum.principal.Principal.actor_string` renders both agent kinds
    identically as ``agent:<id>`` — so without the reservation a human could
    create an *external* agent called ``builtin-gardener``, hand its token to
    anything, and every write it made would be indistinguishable in the event
    log from the internal gardener's. The event log is this system's answer to
    "who is answerable for this write", and an answer two different principals
    can produce is not one.

    **An ``internal`` agent cannot be created here at all.** The design has
    exactly one, seeded by migration ``0014``, and
    :func:`nodum.auth.internal_principal` selects it by
    ``WHERE kind = 'internal'`` and refuses to choose between two — so a second
    one does not add a gardener, it **removes** the one that exists: every
    consolidation path, every curative op the runner makes and the whole nightly
    schedule die with "more than one internal agent account exists".
    ``disable_agent`` is no cure (the count precedes the ``disabled`` check) and
    no surface deletes an agent, so the install is only recoverable by editing
    the database by hand. A second internal agent is a schema change, and it
    should have to be.

    Raises:
        ValueError: If ``kind`` is unknown or ``internal``, an external agent
            names no owner, a grant level is unknown, or ``name`` takes a
            reserved prefix.
        AccountExists: If an agent already answers to ``name``.
        RecordNotFound: If ``owner_human_id`` names no human.
    """
    if kind not in ("external", "internal"):
        raise ValueError(f"kind must be 'external' or 'internal', got {kind!r}")
    if kind == "external" and not owner_human_id:
        raise ValueError("an external agent needs an owner_human_id")
    if name.startswith(BUILTIN_AGENT_PREFIX):
        raise ValueError(
            f"agent names beginning with {BUILTIN_AGENT_PREFIX!r} are reserved for the agents "
            "this system seeds itself: an agent's id is its name, and every agent writes to the "
            f"event log as 'agent:<id>', so an account called {name!r} would be indistinguishable "
            "there from the built-in one. Choose another name."
        )
    # The prefix check runs first on purpose: `builtin-anything` is refused for
    # what it is *called*, whatever kind it asked to be.
    if kind == "internal":
        raise ValueError(
            "an internal agent cannot be created: this system has exactly one, seeded by "
            "migration 0014, and it is chosen by being the only row with kind 'internal'. A "
            f"second one — {name!r} — would not add a gardener, it would take the existing one "
            "away: every consolidation path would refuse to choose between them, and no surface "
            "deletes an agent. A second internal agent is a schema change"
        )
    template = {"meta": "read"} if grants is None else grants
    for level in template.values():
        if level not in GRANT_LEVEL_NAMES:
            raise ValueError(f"level must be one of {GRANT_LEVEL_NAMES}, got {level!r}")
    conn = _connect(path)
    try:
        actor = _admin_actor(conn, principal)
        agent_id = name
        # Everything the schema would otherwise refuse mid-INSERT, checked
        # first so the answer is a 409/404 and not a bare IntegrityError
        # surfacing as a 500 (Q13 review S14).
        if conn.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,)).fetchone():
            raise AccountExists(f"an agent named {agent_id!r} already exists")
        if (
            owner_human_id is not None
            and not conn.execute("SELECT 1 FROM humans WHERE id = ?", (owner_human_id,)).fetchone()
        ):
            # Named as the human the caller typed, not as the row it is stored
            # in — the convention `_get_cycle_row` states.
            raise RecordNotFound(f"human not found: {owner_human_id}")
        template = {
            _resolve_space(conn, space, principal): level for space, level in template.items()
        }
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
    """Replace an agent's token: the old one dies now; the new one shows once.

    Internal agents are refused (Q13 review N3): they authenticate by being
    in-process and are minted without a token, so rotating one would hand out
    a working external credential for an identity that is not supposed to
    have one.

    Raises:
        ValueError: If the agent is internal.
        RecordNotFound: If the account does not exist.
    """
    conn = _connect(path)
    try:
        actor = _admin_actor(conn, principal)
        row = conn.execute("SELECT kind FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            raise RecordNotFound(f"agent not found: {agent_id}")
        if row["kind"] == "internal":
            raise ValueError(
                f"agent {agent_id!r} is internal: it authenticates in-process and holds no token"
            )
        token, token_hash = auth.generate_token()
        cursor = conn.execute(
            "UPDATE agents SET credential_hash = ? WHERE id = ?", (token_hash, agent_id)
        )
        if cursor.rowcount == 0:
            raise RecordNotFound(f"agent not found: {agent_id}")
        _emit(conn, actor, "agent.token_rotate", {"agent_id": agent_id})
        conn.commit()
        return token
    finally:
        conn.close()


def disable_agent(agent_id: str, *, principal: Principal, path: str | Path | None = None) -> None:
    """Disable an agent: its token stops verifying; its proposals stay, flagged.

    Revocation is verification-time, so *when* it bites depends on the
    surface (Q13 review S8): HTTP re-checks every request, and the MCP server
    re-verifies its token on every tool call — a running ``nodum mcp serve``
    refuses its next call after the disable, on every surface. There is no
    longer anything that outlives the disable to kill.
    """
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
    level: GrantLevel,
    *,
    principal: Principal,
    path: str | Path | None = None,
) -> GrantOut:
    """Grant (or re-level) an agent's access to a space; event-logged.

    An archived space is refused, and says so: a grant on it would confer
    nothing (:func:`nodum.auth._grant_set` drops grants on archived spaces) and
    would come to life only if the archive were undone, which is delegation by
    accident. Restore the space first if that is what you meant.

    Raises:
        ValueError: If ``level`` is not one of :data:`GRANT_LEVEL_NAMES`, or if
            the space is archived.
        RecordNotFound: If the agent does not exist.
        TypeNotFound: If the space does not resolve.
    """
    if level not in GRANT_LEVEL_NAMES:
        raise ValueError(f"level must be one of {GRANT_LEVEL_NAMES}, got {level!r}")
    conn = _connect(path)
    try:
        actor = _admin_actor(conn, principal)
        if not conn.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,)).fetchone():
            raise RecordNotFound(f"agent not found: {agent_id}")
        # Human-only (`_admin_actor` above), so resolving an archived space by
        # name is not a leak — and refusing it by name beats the bare "unknown
        # space" the active-only resolver used to answer with.
        space_id, space_state = _resolve_space_for_admin(conn, space)
        if space_state == "archived":
            raise ValueError(
                f"cannot grant on the archived space {space_id!r}: archiving a space makes every "
                "grant on it inert, so this one would confer nothing until the archive was undone"
            )
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


def revoke(
    agent_id: str, space: str, *, principal: Principal, path: str | Path | None = None
) -> None:
    """Revoke an agent's grant on a space; event-logged.

    Reaches an **archived** space too, by id or by its name. Archiving already
    makes the grant inert, but the row survives so the human can see it and
    take it away for good — and resolving active spaces only left no supported
    way to do that at all, short of undoing the archive or raw SQL.

    Raises:
        RecordNotFound: If the agent holds no grant on the space.
        TypeNotFound: If the space does not resolve.
    """
    conn = _connect(path)
    try:
        actor = _admin_actor(conn, principal)
        space_id, _ = _resolve_space_for_admin(conn, space)
        before = conn.execute(
            "SELECT * FROM grants WHERE agent_id = ? AND space_id = ?", (agent_id, space_id)
        ).fetchone()
        if before is None:
            raise RecordNotFound(f"no grant for {agent_id!r} on space {space_id!r}")
        conn.execute("DELETE FROM grants WHERE agent_id = ? AND space_id = ?", (agent_id, space_id))
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


# ── Space lifecycle (a space is a node of builtin type 'space' in meta) ───────
#
# These three are deliberately thin: each delegates to the ordinary node
# operation so a space create, rename and archive are event-logged, versioned
# and undoable exactly like any other node write. What they add is the one
# piece of knowledge that would otherwise be copied into every adapter — that a
# space *is* a node, of type `space`, living in meta — plus the resolution rule
# that makes a name usable everywhere an id is, and that refuses to touch a
# node that is not a space at all.


def create_space(name: str, *, principal: Principal, path: str | Path | None = None) -> NodeOut:
    """Create a space: a node of builtin type ``space``, living in the meta space.

    Args:
        name: The space's title — what ``--space``, a grant, and the space
            filter may name it by, alongside its generated id.
        principal: Who is writing. Writing meta is the human tier in practice,
            so this is a human operation unless an agent was granted meta.
        path: Explicit database path.

    Returns:
        The created space node.

    Raises:
        GrantNotPermitted: If the principal may not write the meta space.
        SpaceNameTaken: If any other space — archived ones included, since a
            space keeps its name for good — already answers to ``name``.
    """
    return create_node(
        type="space", title=name, space=META_SPACE_ID, principal=principal, path=path
    )


def rename_space(
    space: str, name: str, *, principal: Principal, path: str | Path | None = None
) -> NodeOut | VersionOut:
    """Rename a space — a space is a node, so its name is a node title.

    Args:
        space: Space id or name; one the principal cannot read does not
            resolve, and neither does a node that is not a space.
        name: The new title.
        principal: Who is writing.
        path: Explicit database path.

    Returns:
        The renamed space node — or, on the ``suggest`` path, the proposed
        version staging the new title, exactly as :func:`update_node` does for
        any other node.

    Raises:
        TypeNotFound: If ``space`` resolves to no space the principal can see.
            An archived space is one of those: :func:`_resolve_space` matches
            ``active`` only, so freeing an archived space's reserved name is an
            :func:`update_node` by id rather than a rename here.
        SpaceNameTaken: If another space already answers to ``name``.
    """
    space_id = resolve_space_id(space, principal=principal, path=path)
    return update_node(space_id, title=name, principal=principal, path=path)


def archive_space(space: str, *, principal: Principal, path: str | Path | None = None) -> NodeOut:
    """Archive a space; its nodes keep their ``space_id`` and grants on it go inert.

    Nothing is moved or deleted: archiving retires the space from the
    vocabulary (it stops resolving, so nothing new can be written or granted
    there) while every node already in it keeps its ``space_id`` and stays
    exactly as readable **to a human** as it was.

    **Every agent is cut off, which is the point.** "Grants go inert" is what
    archiving promises a human who reaches for it to stop an agent, and it is
    enforced where grants become a principal (:func:`nodum.auth._grant_set`), so
    it holds for reads, writes, proposals and review alike rather than only for
    the calls that spell the space's name. It was previously true only of those:
    an agent kept full live authority over every node already in the space,
    reachable by node id, while :func:`list_spaces` stopped showing the space
    or its grants — hidden authority, and unrevokable to boot.

    The grant **rows** survive on purpose. A human can still see them
    (:func:`list_grants`) and :func:`revoke` them, and undoing the archive
    restores exactly the delegation that was in place. Inert, not destroyed.

    Its **name goes with it and stays reserved** (migration ``0013``): no new
    space may take the name of one that was retired. That is what makes the one
    route back — :func:`undo` of the ``node.archive`` event, which restores the
    row past the state machine — something that cannot fail on a collision.

    The two structural spaces are refused (:data:`STRUCTURAL_SPACE_IDS`), by
    the transition itself so that no spelling of archive misses it. A *rename*
    of either stays allowed, and the asymmetry is the point: a rename touches
    the title, and it is the **id** the schema and the default write target
    depend on.

    Args:
        space: Space id or name.
        principal: Who is writing.
        path: Explicit database path.

    Returns:
        The archived space node.

    Raises:
        TypeNotFound: If ``space`` resolves to no space the principal can see.
        InvalidTransition: If ``space`` is ``main`` or ``meta``.
    """
    space_id = resolve_space_id(space, principal=principal, path=path)
    # A resolved space id is always a node id, so this transition is a node's —
    # including the structural refusal, which `_transition_row` owns so that
    # `archive <id>` cannot route around it.
    archived = transition(space_id, "archive", principal=principal, path=path)
    if not isinstance(archived, NodeOut):
        # Unreachable: a space id resolves to a space node, so the node branch
        # of the transition is the only one that can apply. Stated so the
        # declared NodeOut return is honest to the type checker.
        raise RuntimeError("unreachable: archiving a space node returns a node")
    return archived


def list_spaces(*, principal: Principal, path: str | Path | None = None) -> list[SpaceOut]:
    """Every active space, with its live node count and the agents granted on it.

    The ``/spaces`` screen's read, and the CLI's ``space-list``: the space
    nodes plus the two facts that make a space territory rather than a name —
    how much lives there, and who else may touch it.

    Human-only for the same reason :func:`list_grants` is: which agent holds
    what is governance information, and an agent learning the shape of the
    delegation around it is precisely what the grant model withholds.

    Args:
        principal: Who is asking; must be a human.
        path: Explicit database path.

    Returns:
        One :class:`SpaceOut` per active space, in creation order.

    Raises:
        GrantNotPermitted: If the principal is not a human.
    """
    conn = _connect(path)
    try:
        Store(conn, principal).require_human("list spaces")
        rows = conn.execute(
            "SELECT * FROM nodes WHERE type_id = 'space' AND state = 'active'"
            " ORDER BY created_at, rowid"
        ).fetchall()
        counts = {
            row["space_id"]: row["live_nodes"]
            for row in conn.execute(
                "SELECT space_id, COUNT(*) AS live_nodes FROM nodes"
                " WHERE state != 'archived' GROUP BY space_id"
            )
        }
        grants: dict[str, list[GrantOut]] = {}
        for row in conn.execute("SELECT * FROM grants ORDER BY agent_id"):
            grants.setdefault(row["space_id"], []).append(
                GrantOut(
                    agent_id=row["agent_id"],
                    space_id=row["space_id"],
                    level=row["level"],
                    created_at=row["created_at"],
                )
            )
        return [
            SpaceOut(
                **_node_out(row).model_dump(),
                node_count=counts.get(row["id"], 0),
                grants=grants.get(row["id"], []),
            )
            for row in rows
        ]
    finally:
        conn.close()


# ── Consolidation cycles (design §8.4) ────────────────────────────────────────
#
# A cycle groups a set of graph writes under one id so that a human can take the
# whole of it back in one action. The lifecycle is deliberately thin: these four
# functions own the `cycles` row and nothing else. The stamping is
# `in_cycle` + `_emit`, the writes are the ordinary public operations, and the
# per-cycle diff is `list_events(cycle_id=…)` — so a cycle adds a grouping and
# not a second way to write to the graph.

#: How a cycle came to exist. ``manual`` is a human asking for one, ``scheduled``
#: is the nightly runner, ``curative`` is a cycle opened to carry one curative
#: operation, and ``rollback`` is the cycle that takes another one back. The
#: schema carries the same four; this is what refuses a fifth with a sentence
#: instead of a bare ``IntegrityError``.
CYCLE_TRIGGERS = CYCLE_TRIGGERS

#: The statuses a cycle may be closed into. It opens ``running`` and leaves that
#: state exactly once (:data:`CYCLE_STATUSES` is the schema's full set).
CYCLE_CLOSED_STATUSES = CYCLE_CLOSED_STATUSES

#: Every status a ``cycles`` row may hold.
CYCLE_STATUSES = CYCLE_STATUSES

#: ``cycles.triggered_by`` for a scheduled cycle: nobody asked, the clock did.
#: Derived from the trigger rather than taken as an argument, so no caller can
#: put a name in the journal that is not the one that authenticated.
SCHEDULER_ACTOR = "scheduler"

#: The triggers a *consolidation run* opens, and the ones ``0014``'s partial
#: unique index serialises against each other. ``curative`` and ``rollback`` are
#: deliberately outside it: each is one short, human-driven operation, and
#: blocking them for the length of a nightly sweep would take the curative tier
#: offline every night.
CONSOLIDATION_TRIGGERS = CONSOLIDATION_TRIGGERS


def _cycle_out(row: sqlite3.Row) -> CycleOut:
    return CycleOut(
        id=row["id"],
        trigger=row["trigger"],
        triggered_by=row["triggered_by"],
        scope=row["scope"],
        dry_run=bool(row["dry_run"]),
        status=row["status"],
        report=json.loads(row["report"]) if row["report"] is not None else None,
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        rolled_back_by=row["rolled_back_by"],
        # Derived, never stored: `0015` writes the two stamps and no flag, so
        # the boolean the surfaces render cannot drift from the record it is
        # about. `CycleDetailOut.metrics` projects the report the same way.
        stop_requested=row["stop_requested_at"] is not None,
        stop_requested_by=row["stop_requested_by"],
        stop_requested_at=row["stop_requested_at"],
    )


def _no_such_cycle(cycle_id: str) -> RecordNotFound:
    """The single refusal a cycle id can meet, whoever asked and for whatever reason.

    One function owns the sentence because :func:`stop_requested` answers a
    principal it may not answer with **this** refusal rather than a permission
    one — the non-oracle rule — and two copies of a sentence that has to be
    identical are how it stops being identical.

    The message names the thing the caller asked for, not the table it lives
    in: ``no cycles row with id`` is this module's own vocabulary reaching a
    human who typed ``nodum cycle-get <id>`` and has no reason to know the
    schema. Every other lookup here says ``<thing> not found: <id>``.
    """
    return RecordNotFound(f"consolidation cycle not found: {cycle_id}")


def _find_cycle_row(conn: sqlite3.Connection, cycle_id: str) -> sqlite3.Row | None:
    """The ``cycles`` row with this id, or ``None``.

    One query, because there are three callers and they differ only in what they
    do with a miss: :func:`_get_cycle_row` raises, :func:`stop_requested` folds
    the miss into a check it makes afterwards, and :func:`_rollback_target`
    treats it as "this rollback reversed nothing that still exists" and walks no
    further. That was three copies of the ``SELECT``, which is the argument
    :func:`_no_such_cycle` itself is written from — a filter added to one of them
    is a filter the others silently do not have.
    """
    return conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()


def _get_cycle_row(conn: sqlite3.Connection, cycle_id: str) -> sqlite3.Row:
    row = _find_cycle_row(conn, cycle_id)
    if row is None:
        raise _no_such_cycle(cycle_id)
    return row


def _cycle_authority_spaces(principal: Principal, scope_id: str | None) -> set[str | None]:
    """The spaces a cycle's open/close is checked against by ``require_review``.

    A scoped cycle is checked against its scope, which is the plain reading.

    An **unscoped** cycle covers the whole file, and no grant confers that — so
    what is required instead is that the principal holds curative authority
    *somewhere*: a human (unfiltered, and passes ``require_review`` before this
    set is even looked at), or an agent with ``edit`` on at least one space. An
    agent with none gets the empty set and is refused. This is not a weaker rule
    than it looks: opening a cycle is authority to *group*, and every write made
    inside one is still gated space by space by the same store as any other
    write. The gardener holds ``read`` on ``meta`` and ``edit`` on ``main`` by
    migration ``0014``, so the set it brings here is ``{main}`` — which is
    exactly what lets it run the nightly cycle and nothing outside ``main``.
    ``read`` on ``meta`` resolves a type and never opens a cycle over the
    vocabulary, which is the point: no job writes ``meta``.
    """
    if scope_id is not None:
        return {scope_id}
    return {space for space in principal.grants if principal.level_on(space) >= EDIT}


def open_cycle(
    *,
    trigger: CycleTrigger,
    scope: str | None = None,
    dry_run: bool = False,
    principal: Principal,
    path: str | Path | None = None,
) -> CycleOut:
    """Open a consolidation cycle and return its journal entry.

    The returned ``id`` is what :func:`in_cycle` stamps onto every event the
    cycle's writes produce, and what :func:`list_events` filters by to show what
    it did.

    Opening one is curative-tier authority, gated by the same
    :meth:`~nodum.store.Store.require_review` check accept/reject use —
    a human, or ``edit`` on the space in question (see
    :func:`_cycle_authority_spaces` for what an unscoped cycle checks against).
    Humans hold it everywhere by construction.

    Args:
        trigger: One of :data:`CYCLE_TRIGGERS` — how the cycle came to exist.
        scope: A space id or name to confine the cycle to; ``None`` is the whole
            file. It resolves through the ordinary space rule, so a space that
            does not exist and one the principal holds no grant on answer
            identically — a cycle is not an existence oracle either.
        dry_run: Record that this cycle was a rehearsal. Nothing here enforces
            it; the runner does, and the journal has to say which it was.
        principal: Who is opening it. Recorded as ``triggered_by`` (who
            *asked*), which is deliberately not the ``actor`` on the events
            inside (who *acted*) — except for a ``scheduled`` cycle, where the
            honest answer is :data:`SCHEDULER_ACTOR`. A ``manual`` cycle is by
            definition a human asking for one, and that is **enforced**: see
            below.
        path: Explicit database path.

    Returns:
        The new cycle, ``running``.

    Raises:
        ValueError: If ``trigger`` is not one of :data:`CYCLE_TRIGGERS`.
        TypeNotFound: If ``scope`` resolves to no space the principal can see.
        GrantNotPermitted: If the principal may not open a cycle there, or is
            not a human and asked for a ``manual`` cycle.
        CycleInProgress: If ``trigger`` is a consolidation trigger and a
            consolidation cycle is already ``running`` — refused on the INSERT
            by ``0014``'s partial unique index, so the rule holds against a
            second *process* and not only a second caller here.
    """
    if trigger not in CYCLE_TRIGGERS:
        raise ValueError(f"trigger must be one of {CYCLE_TRIGGERS}, got {trigger!r}")
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        scope_id = None if scope is None else _resolve_space(conn, scope, principal)
        store.require_review(
            _cycle_authority_spaces(principal, scope_id), "open a consolidation cycle"
        )
        # `manual` means *a human asked for this run*, and the schema says so in
        # as many words (`triggered_by` is "'human:<id>', or 'scheduler'"). It
        # was only ever a convention: `nodum.consolidate.consolidate` takes
        # `triggered_by` as a **string** and re-mints a principal from it, so
        # nothing downstream had a `principal=` binding to check and a caller
        # able to reach that function could put `agent:builtin-gardener` in the
        # journal's "who asked" column — which is the one column that answers
        # "I did not ask for this". Refused here rather than there, because this
        # is where the value is written and every future caller passes through
        # it. `scheduled` needs no check (it records the clock whatever it is
        # handed) and `curative` genuinely records the principal running the
        # operation, which may be an `edit`-granted agent by design (T1).
        if trigger == "manual" and not principal.is_human:
            raise GrantNotPermitted(
                f"{principal.actor_string} may not open a 'manual' consolidation cycle: "
                "'manual' records that a human asked for this run, and the journal's answer to "
                "'who asked' is a human or the scheduler. A run nobody asked for is 'scheduled'; "
                "a cycle carrying one curative operation is 'curative'"
            )
        cycle_id = uuid.uuid4().hex
        triggered_by = SCHEDULER_ACTOR if trigger == "scheduled" else principal.actor_string
        try:
            conn.execute(
                "INSERT INTO cycles (id, trigger, triggered_by, scope, dry_run, status)"
                " VALUES (?, ?, ?, ?, ?, 'running')",
                (cycle_id, trigger, triggered_by, scope_id, int(dry_run)),
            )
        except sqlite3.IntegrityError as clash:
            # `0014`'s partial unique index is the cross-process lock: at most one
            # `running` consolidation row exists, whichever process opened it, and
            # the loser finds out on the INSERT rather than after a `SELECT` that
            # was true when it ran. Translated only when a running consolidation
            # is actually there — an `IntegrityError` from anything else is a bug
            # and must not be reported as a busy graph.
            running = _running_consolidation(conn)
            if running is None:
                raise
            raise CycleInProgress(_cycle_in_progress_message(running)) from clash
        row = _get_cycle_row(conn, cycle_id)
        conn.commit()
        return _cycle_out(row)
    finally:
        conn.close()


def _running_consolidation(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The consolidation cycle currently holding the file, or ``None``."""
    placeholders = ",".join("?" * len(CONSOLIDATION_TRIGGERS))
    return conn.execute(
        f"SELECT * FROM cycles WHERE status = 'running' AND trigger IN ({placeholders})",
        CONSOLIDATION_TRIGGERS,
    ).fetchone()


def _cycle_in_progress_message(running: sqlite3.Row) -> str:
    """Refuse a second consolidation, naming the one in the way and the door out.

    The door matters as much as the refusal. A cycle killed by a ``SIGKILL``, a
    power cut, or a shutdown cancelling a mid-run task never closes itself, and
    nothing else moves the row — so it blocks every later run, and "try again
    when it has finished" is advice about a run that will never finish.
    ``cycle-abandon`` is what closes it, and the refusal names the whole command.
    """
    asked_by = (
        "the nightly schedule" if running["trigger"] == "scheduled" else running["triggered_by"]
    )
    return (
        f"a consolidation cycle is already running: cycle {running['id']}, started "
        f"{running['started_at']} for {asked_by}. Cycles are serialised across every process "
        "that shares this file, so two runs cannot propose the same candidate twice, and this "
        "one was refused rather than queued behind it. Try again when it has finished — or, if "
        f"that run was interrupted and will never close itself, run: nodum cycle-abandon "
        f"{running['id']}"
    )


def close_cycle(
    cycle_id: str,
    *,
    status: str,
    report: dict[str, Any],
    principal: Principal,
    path: str | Path | None = None,
) -> CycleOut:
    """Close a running cycle with its outcome and the runner's report.

    Args:
        cycle_id: The cycle to close.
        status: One of :data:`CYCLE_CLOSED_STATUSES`. A cycle that crashed is
            ``failed`` and stays in the journal — a cycle that vanished on
            failure would be a cycle nobody could ask about.
        report: The runner's summary, stored as JSON. What the cycle *changed*
            is not in here: that is ``list_events(cycle_id=…)``.
        principal: Who is closing it, checked exactly as :func:`open_cycle`
            checks who opened it, against the cycle's recorded scope.
        path: Explicit database path.

    Returns:
        The closed cycle.

    Raises:
        ValueError: If ``status`` is not one of :data:`CYCLE_CLOSED_STATUSES`.
        RecordNotFound: If the cycle id does not resolve.
        InvalidTransition: If the cycle is not ``running``.
        GrantNotPermitted: If the principal may not close a cycle there.
    """
    if status not in CYCLE_CLOSED_STATUSES:
        raise ValueError(f"status must be one of {CYCLE_CLOSED_STATUSES}, got {status!r}")
    conn = _connect(path)
    try:
        store = Store(conn, principal)
        row = _get_cycle_row(conn, cycle_id)
        # The recorded scope, not a re-resolution of it: a space archived while
        # the cycle ran must not make its own cycle uncloseable, which would
        # leave a `running` row in the journal for good.
        store.require_review(
            _cycle_authority_spaces(principal, row["scope"]), "close a consolidation cycle"
        )
        if row["status"] != "running":
            raise InvalidTransition(
                f"cycle {cycle_id} is already {row['status']}: a cycle leaves 'running' once"
            )
        # `AND status = 'running'` is the atomic guard, not the read above: two
        # concurrent closes both pass the read, and the second one's UPDATE then
        # matches nothing — so exactly one close wins, and the rowcount is the
        # honest refusal rather than the pre-check (finding M8, the
        # `request_stop` shape).
        cursor = conn.execute(
            "UPDATE cycles SET status = ?, report = ?, finished_at = datetime('now')"
            " WHERE id = ? AND status = 'running'",
            (status, json.dumps(report, ensure_ascii=False), cycle_id),
        )
        if cursor.rowcount == 0:
            raise InvalidTransition(
                f"cycle {cycle_id} is already {_get_cycle_row(conn, cycle_id)['status']}: "
                "a cycle leaves 'running' once"
            )
        closed = _get_cycle_row(conn, cycle_id)
        conn.commit()
        return _cycle_out(closed)
    finally:
        conn.close()


def abandon_cycle(
    cycle_id: str, *, principal: Principal, path: str | Path | None = None
) -> CycleOut:
    """Close a ``running`` cycle nobody is going to finish, as ``failed`` (human-only).

    The door out of an **interrupted** run. A cycle that never closed is not a
    cosmetic wart in the journal: ``_rollback_plan`` refuses a ``running`` cycle
    because its event set is not closed yet, and :func:`undo` refuses every
    event it stamped — so the writes it made before it died are irreversible on
    every surface until somebody closes it. The runner closes its own cycle even
    on ``KeyboardInterrupt``, but a ``SIGKILL``, a power cut, or
    :meth:`nodum.scheduler.ConsolidationScheduler.stop` cancelling a mid-cycle
    task during a server shutdown all leave the row exactly there, and until now
    nothing on any surface could move it.

    It is deliberately **not** a general "close this cycle" verb — that is
    :func:`close_cycle`, which the runner owns and which takes the outcome and
    the report. This one takes neither: the outcome of an abandoned run is
    ``failed`` (what the run itself would have recorded had it survived), and
    the report says who abandoned it and that the run never finished, which is
    the whole of what is known. Nothing about the writes it already made is
    touched — they are real, and :func:`rollback_cycle` is what takes them back,
    which is exactly what this unlocks.

    Human-only. Declaring somebody else's run dead is a judgement about a
    process the caller cannot see, and it is the step that makes a whole cycle's
    writes reversible — an authority ``rollback_cycle`` itself does not delegate.

    Args:
        cycle_id: The cycle to abandon.
        principal: Who is abandoning it; must be a human, and is recorded in the
            report.
        path: Explicit database path.

    Returns:
        The cycle, now ``failed``.

    Raises:
        GrantNotPermitted: If the principal is not a human.
        RecordNotFound: If the cycle id does not resolve.
        InvalidTransition: If the cycle is not ``running`` — a cycle that has
            already said how it ended is not abandoned, and re-closing it would
            overwrite the record of what actually happened.
    """
    conn = _connect(path)
    try:
        Store(conn, principal).require_human("abandon a consolidation cycle")
        row = _get_cycle_row(conn, cycle_id)
        if row["status"] != "running":
            raise InvalidTransition(
                f"cycle {cycle_id} is already {row['status']}, not running: abandoning is for a "
                "run that was interrupted and left its journal entry open, and a cycle that has "
                "said how it ended is not one"
            )
    finally:
        conn.close()
    # Through `close_cycle`, so a cycle leaves `running` in exactly one place.
    # The read above is message-only: the race is closed by close_cycle's
    # `UPDATE ... WHERE status = 'running'` and its rowcount check, which makes
    # the second of two concurrent abandons a clean refusal instead of a
    # double-close (finding M8).
    return close_cycle(
        cycle_id,
        status="failed",
        report={
            # `abandoned` is the discriminator, and it exists because there was
            # none: an abandon wore the one-op curative report's shape exactly
            # (`op` + `error`), so an abandoned nightly sweep read back as "One
            # curative operation: abandon_cycle. It failed." — a consolidation
            # described as a curative op, and a failure that was the *run's*.
            # **And there is no `op` here**, deliberately: an abandon is not an
            # operation the cycle ran, so naming one is the misreading in field
            # form. It carried `op: "abandon_cycle"` for exactly one round,
            # because the journal view's reader returned nothing without that key
            # — a value on the server kept alive by a client, and the client
            # keys on `abandoned` now.
            "abandoned": True,
            "abandoned_by": principal.actor_string,
            # **Not `error`.** The abandon succeeded — it is the run that failed,
            # which `status = 'failed'` already says. A sentence explaining the
            # close, filed under a key that means "this raised", is what put "It
            # failed." on an operation that did exactly what was asked.
            "detail": (
                "the run was interrupted and never closed itself; a human closed its journal "
                "entry so that what it had already written could be rolled back"
            ),
        },
        principal=principal,
        path=path,
    )


def request_stop(
    cycle_id: str, *, principal: Principal, path: str | Path | None = None
) -> CycleOut:
    """Ask a ``running`` cycle to stop, and record who asked (human-only, K1–K3).

    The kill switch's write half. It sets ``0015``'s two columns and **nothing
    else**: the row stays ``running``, no event is emitted, and no write the
    cycle already made is touched. The run notices at its next check — between
    jobs, between items, or immediately before a provider call — and closes its
    own cycle ``failed`` with a report that says it was stopped. Worst-case
    latency is therefore one provider call, and honestly so: cancelling mid-call
    would buy seconds and cost a torn transaction.

    **What checks it today is one of those three points**: :meth:`nodum.agent.
    AgentRun.chat`, immediately before a provider call. The five deterministic
    jobs in :mod:`nodum.consolidate` make no provider call and read this switch
    nowhere, so a stop recorded against one of those runs is kept on the row and
    the run finishes — the abstraction job (5b-ii's first) is the exception: it
    reaches the model through ``AgentRun.chat``, so it obeys a stop recorded
    against its own run. Every human surface says so rather than
    promising a wind-down that would not arrive, and
    ``test_the_deterministic_runner_consults_no_stop_switch_and_the_copy_says_so``
    is what makes that copy fail when it stops being true.

    **Deliberately not** :func:`abandon_cycle`, and not a thin wrapper over it.
    That verb is a *repair* — a human declaring somebody else's dead process
    dead, closing the row from outside so its writes become rollback-able. This
    one expects the run to be alive and to wind down honestly, and the journal
    has to keep the two apart: a human reading a ``failed`` cycle at 09:00 needs
    to know whether the operator stopped it or the process died. Building this
    on top of the repair would erase exactly that distinction.

    It does **not** roll back. Stopping and undoing are two decisions, and a
    kill switch that also reverted would make "stop, look at what it did, then
    decide" impossible — which is the reason a human hits one. Every write the
    run made stays, stamped with the cycle id, and :func:`rollback_cycle` takes
    them back like any other cycle's.

    Human-only, for the reason :func:`abandon_cycle` is: it is an instruction
    aimed at a process the caller cannot see, and an agent that could stop the
    gardener's night could stop the review queue from ever being filled.

    **Asking twice is not an error, and the first asker keeps the record.** A
    kill switch that raised because the run was already stopping would make a
    human hitting it twice doubt whether it worked at all, which is the one
    moment that must not be ambiguous. The second call is a no-op that returns
    the cycle carrying who actually stopped it and when.

    Args:
        cycle_id: The cycle to stop.
        principal: Who is asking; must be a human, and is recorded in
            ``stop_requested_by`` as their actor string.
        path: Explicit database path.

    Returns:
        The cycle, now carrying the stop — or carrying the earlier one, if a
        stop was already recorded.

    Raises:
        GrantNotPermitted: If the principal is not a human.
        RecordNotFound: If the cycle id does not resolve.
        InvalidTransition: If the cycle is not ``running``. A cycle that has
            already said how it ended cannot be told to stop: there is nothing
            left to obey it, and stamping one would put a stop in the journal
            that no run ever saw.
    """
    conn = _connect(path)
    try:
        Store(conn, principal).require_human("stop a consolidation cycle")
        row = _get_cycle_row(conn, cycle_id)
        if row["status"] != "running":
            raise InvalidTransition(
                f"cycle {cycle_id} is already {row['status']}, not running: a stop is an "
                "instruction to a live run, and a cycle that has said how it ended has nothing "
                "left to obey it"
            )
        # `AND stop_requested_at IS NULL` is what makes the second asker a no-op
        # rather than an overwrite: who stopped the night is the fact, and the
        # first answer to it is the true one. It is also the whole of the race
        # guard — two humans hitting the switch at once leave one row, not a
        # torn one.
        conn.execute(
            "UPDATE cycles SET stop_requested_at = datetime('now'), stop_requested_by = ?"
            " WHERE id = ? AND stop_requested_at IS NULL",
            (principal.actor_string, cycle_id),
        )
        stopped = _get_cycle_row(conn, cycle_id)
        conn.commit()
        return _cycle_out(stopped)
    finally:
        conn.close()


def _may_watch_a_cycle(row: sqlite3.Row, principal: Principal) -> bool:
    """May this principal ask whether the cycle in ``row`` was told to stop?

    **The rule is: what would have admitted *you* to run this cycle's
    territory.** It is a question about the caller, not about the run — and the
    difference is not a nicety. ``cycles`` records ``triggered_by``, who *asked*
    for the run, and has no column at all for who is *running* it: the runner is
    a principal minted inside :func:`nodum.consolidate._run_cycle` and never
    written down. So "exactly what admitted this run" — which is how this was
    first stated — is not a question this row can answer, and a check that
    claimed to ask it would be describing a column that does not exist. What is
    checkable is the admission rule itself, applied to whoever is asking.

    A **scoped** cycle is admitted by :func:`nodum.consolidate.
    _require_gardener_scope`, which asks only that the runner can *resolve* the
    scope — any grant, ``read`` included — so that is what is asked here.
    An **unscoped** cycle covers the whole file, which no grant confers, and
    :func:`open_cycle` admits it on ``edit`` *somewhere*
    (:func:`_cycle_authority_spaces` with no scope); that half is unchanged,
    because nothing was ever wrong with it. Humans pass, as they pass every
    other check here.

    **Caller-relative is wider than run-relative, and the width is the delta.**
    Any agent holding *any* grant on space S can read the switch on every cycle
    ever scoped to S — cycles a human opened, cycles another agent opened, runs
    it has no part in. Concretely: the minimum grant set :func:`create_agent`
    gives a new agent, ``read`` on ``meta`` — the least anything needs to resolve
    a type — now watches every cycle scoped to ``meta``, and the parity set
    ``0010`` backfilled onto the agents that predate it, ``meta: read`` plus
    ``main: suggest``, watches every cycle over ``main`` too. ``require_review``
    refused both, since neither level reaches ``EDIT``. It is a boolean per
    cycle id and no id is reachable from anything an agent can call — cycle ids
    are ``uuid4``, both journal reads are human-only, and no agent-facing model
    carries one — but the width is real and stating a narrower rule than the code
    enforces is how a later reader grants themselves the narrower one.

    **The scoped half is not the rule that shipped, and the one it replaces was
    wrong.** :func:`stop_requested` asked ``Store.require_review`` over
    :func:`_cycle_authority_spaces` for *both* cases — the identical check
    ``open_cycle`` and ``close_cycle`` ask — justified as *obeying a stop is
    closing the cycle, so a principal that could not close this one has no use
    for the answer*. That justification is unsound on **both** triggers, in two
    different ways, and the widening is right on both:

    * On a ``manual`` run the gardener never closes the cycle at all.
      ``consolidate._run_cycle`` closes as the **opener**, and ``_opener``
      resolves a human-triggered run's opener to the human — so the check was
      demanding of the gardener an authority the gardener does not exercise.
    * On a ``scheduled`` run the gardener *is* the opener and *does* close its
      own cycle — but then ``open_cycle`` already required ``edit`` on the scope
      before the run could start, so the check was re-asking a question the door
      had already answered ``yes``. It could refuse nothing a scheduled run
      would ever meet.

    Either way the old check only ever bit where it was wrong: a scoped run
    needs no more than to resolve its scope, so a gardener holding ``read`` on a
    space is entitled to consolidate it and was then refused the switch over its
    own run — a night dying at the first provider call with
    ``GrantNotPermitted``, which is a kill switch killing the run by being
    unreadable. A check on the far side of a door must not be stricter than the
    door.
    """
    if principal.is_human:
        return True
    if row["scope"] is not None:
        return principal.level_on(row["scope"]) >= READ
    return bool(_cycle_authority_spaces(principal, None))


def stop_requested(cycle_id: str, *, principal: Principal, path: str | Path | None = None) -> bool:
    """Has this cycle been told to stop? — the read a run obeys (K3).

    One row read, and the runner calls it between jobs, between items, and
    immediately before every provider call
    (:func:`nodum.agent.cycle_stop_check`). Nothing caches it: a check answering
    from a value read at the top of the run would be a kill switch that cannot
    be hit after the run starts, which is the only time anyone hits one.

    **Deliberately not human-only**, unlike :func:`get_cycle` and
    :func:`list_cycles`. Those are human-only because a journal entry reports
    what the gardener did across every space in the file, and an agent reading
    one learns the shape of territory it holds no grant on. This returns a
    single boolean about a run, discloses no node, no space and no count, and a
    runner that cannot ask whether it was told to stop cannot obey.

    **What bounds it instead is** :func:`_may_watch_a_cycle` — the admission
    rule for this cycle's territory, asked of whoever is calling, which for a
    scoped cycle is the grant that resolves its scope and no longer the
    authority to close it. That rule changed here, and why — along with how much
    wider caller-relative is than run-relative — is written where the rule is.

    **And this refusal is no longer an existence oracle.** The two answers this
    function used to give a principal it turned away — ``RecordNotFound`` for an
    id that names nothing, ``GrantNotPermitted`` for a cycle it may not watch —
    told those two cases apart, so anything holding a single grant could probe a
    cycle id and learn whether it exists. Cycle ids are unguessable and both
    journal reads are human-only, which bounds the damage and does not close the
    class: the space-name check and the Q13 non-oracle rule are both settled the
    other way, and the rule they settle on is that **the refusal is one sentence
    for both cases**. The ordering trick the space-name check uses — ask the
    grant first, so the existence question is never reached — is unavailable
    here, because the grant to ask for is recorded *on the row*. So this takes
    ``_resolve_space``'s shape instead: one refusal, the not-found one, echoing
    nothing back but the id the caller supplied. A principal that may watch the
    cycle still gets the truthful answer, and a human — unfiltered, as everywhere
    — is never told a cycle it can see does not exist.

    **It is closed here and nowhere else, and that is the claim.** It is not
    "the class of cycle-id oracles, closed": :func:`close_cycle` takes a cycle
    id, is not human-only, and still answers ``GrantNotPermitted`` for a cycle
    the caller may not close against ``RecordNotFound`` for an id that names
    nothing — the only one of the seven cycle-id surfaces that still tells them
    apart. That is deliberate. ``close_cycle``'s refusal is the *same*
    ``require_review`` :func:`open_cycle` raises, on the same spaces, and
    ``open_cycle`` cannot be an oracle at all because it takes a scope and not an
    id; collapsing one half of that pair would leave a principal that opened a
    cycle and cannot close it reading *this cycle does not exist* about a row it
    is holding open, and would falsify the symmetry
    :meth:`~nodum.store.Store.require_review` documents. The exposure argument is
    this function's own, unchanged — an unguessable id, no agent-facing model
    carrying one — and it is an argument about reach, not a reason to widen the
    collapse onto a write.

    **What the collapse costs, said plainly.** The refusal a principal outside
    the run now meets is ``consolidation cycle not found``, and that sentence
    reaches a *legitimate* runner too. Grants are read when a principal is minted
    (``auth._grant_set``, which drops grants on archived spaces), so a run whose
    scope is archived under it keeps reading its switch — ``_run_cycle`` mints
    the gardener once and holds it — while the **next** principal minted for that
    cycle, in a later process or after a restart, is told its own cycle does not
    exist. It used to be told it needed ``edit`` on the item's space, which at
    least pointed at the cause. Both refusals are wrong for that reader; this one
    is quieter about the thing an outsider must not learn, which is the trade
    that was taken. The recorded scope is used rather than a re-resolution of it
    (as :func:`close_cycle` does) so that an archived space does not make the
    *lookup* fail on top of it — but it does not, and cannot, keep an archived
    space's cycle readable by a re-minted runner.

    Args:
        cycle_id: The cycle to ask about.
        principal: Who is asking — the principal the run acts as.
        path: Explicit database path.

    Returns:
        ``True`` once a stop has been recorded, for good: the stamp outlives the
        run, because a journal entry has to go on saying this night was stopped.

    Raises:
        RecordNotFound: If the cycle id names no cycle — **or** names one this
            principal may not watch. Word for word the same refusal, on purpose:
            see above.
    """
    conn = _connect(path)
    try:
        row = _find_cycle_row(conn, cycle_id)
        if row is None or not _may_watch_a_cycle(row, principal):
            raise _no_such_cycle(cycle_id)
        return row["stop_requested_at"] is not None
    finally:
        conn.close()


def get_cycle(cycle_id: str, *, principal: Principal, path: str | Path | None = None) -> CycleOut:
    """Return one cycle's journal entry (human-only).

    Human-only for the reason :func:`list_events` and :func:`list_spaces` are:
    the journal says what the gardener did across every space in the file, and
    an agent reading it would learn the shape of territory it holds no grant on.

    Raises:
        GrantNotPermitted: If the principal is not a human.
        RecordNotFound: If the cycle id does not resolve.
    """
    conn = _connect(path)
    try:
        Store(conn, principal).require_human("read the consolidation journal")
        return _cycle_out(_get_cycle_row(conn, cycle_id))
    finally:
        conn.close()


def list_cycles(
    *, limit: int = 50, principal: Principal, path: str | Path | None = None
) -> list[CycleOut]:
    """Return the most recent cycles, newest first (human-only).

    Raises:
        ValueError: If ``limit`` is below 1. SQLite reads a negative ``LIMIT``
            as *unbounded*, so ``--limit -3`` silently returned the whole
            journal — the opposite of what the caller asked for. The rule is
            :func:`subgraph`'s, said here for the same reason.
        GrantNotPermitted: If the principal is not a human.
    """
    require_positive_limit(limit)
    conn = _connect(path)
    try:
        Store(conn, principal).require_human("read the consolidation journal")
        rows = conn.execute(
            "SELECT * FROM cycles ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_cycle_out(row) for row in rows]
    finally:
        conn.close()


# ── The curative tier (design §8.2) ───────────────────────────────────────────
#
# Four operations that change structure rather than add to it. What they share
# is the reason they all sit behind one gate: each writes **several rows from
# one decision**, and :func:`undo` reverses exactly one row from one payload. A
# merge touches a tombstone, a redirect row and every edge that pointed at the
# merged-away node; undoing one of those on its own leaves the other half
# standing. So every curative operation runs inside a consolidation cycle —
# including the one a human invokes directly, which opens a one-op cycle of its
# own (`trigger='curative'`) and closes it (decision C2). Rollback is then the
# single reverse for the whole tier, and there is no second multi-row reversal
# mechanism to build and keep correct.
#
# **The op names are not free.** :mod:`nodum.projectors` dispatches on
# ``op.startswith("node.")`` to reproject the FTS index and the embeddings, and
# takes ``payload["after"]`` as the node to index. A curative op that changes a
# node's text or type and is named outside that namespace would silently
# desynchronise the search index — a search index that lies is worse than one
# that is missing. So ``node.merge`` and ``node.retype`` are node events with an
# ``after`` row shaped exactly like every other node event's, **one event per
# node** rather than one per call, and ``edge.supersede`` / ``edge.relink`` sit
# in the ``edge.`` namespace where the projectors correctly ignore them (an edge
# carries no node text).
#
# Authority is the review path's, unchanged (decision T1): ``edit`` on every
# space the operation touches, asked through
# :meth:`~nodum.store.Store.require_review`. Humans hold it everywhere by
# construction, the gardener holds it where it was granted, and an external
# agent cannot reach this tier at all — MCP never registers these tools and the
# HTTP surface is human-session-only. No new permission concept.

#: The most edges one :func:`bulk_relink` call may rewrite, in the spirit of
#: :data:`MAX_SUBGRAPH_LIMIT`: a server-side ceiling so that one call cannot
#: turn into an unbounded rewrite of the file. The value is what a human can
#: still review as a diff and what one transaction should carry, and a
#: selection that reaches it is reported through ``truncated`` rather than
#: quietly cut (the same rule ``subgraph`` and ingestion's page cap follow).
MAX_RELINK_EDGES = 500

#: Node types the curative tier refuses to touch on either side of an
#: operation. A ``space`` node *is* territory — merging or retyping one would
#: retire a space past :func:`_transition_row`'s structural guard while every
#: node in it kept pointing at it, and past the name rules in
#: :func:`_require_space_lives_in_meta` / :func:`_require_space_name_free`. A
#: ``type`` node is the vocabulary every other node is typed from. Both are
#: structural surgery with their own lifecycles; this tier curates knowledge.
STRUCTURAL_TYPE_IDS = ("space", "type")

#: The keys :func:`bulk_relink` accepts in its selector and in its changes.
#: Spelled out so an unknown key is a sentence rather than a filter that
#: silently matched everything.
RELINK_SELECTOR_KEYS = ("src_id", "dst_id", "type", "state")
RELINK_CHANGE_KEYS = ("type", "dst_id")

#: The keys a :func:`supersede_edge` replacement may name. Everything it does
#: not name is inherited from the edge being replaced.
REPLACEMENT_KEYS = ("src_id", "dst_id", "type", "props", "confidence")


def _readable_node(conn: sqlite3.Connection, store: Store, node_id: str) -> sqlite3.Row:
    """Fetch a node row the principal may read, or raise :class:`NodeNotFound`."""
    row = _get_node_row(conn, node_id)
    if not store.node_visible(row):
        raise NodeNotFound(f"node not found: {node_id}")
    return row


def _readable_edge(conn: sqlite3.Connection, store: Store, edge_id: str) -> sqlite3.Row:
    """Fetch an edge row the principal may read, or raise :class:`EdgeNotFound`.

    An edge is readable iff **both** endpoints are (:meth:`Store.edge_scope`);
    anything less leaks the other space's existence.
    """
    row = _get_edge_row(conn, edge_id)
    for endpoint in (row["src_id"], row["dst_id"]):
        node = _get_node_row(conn, endpoint)
        if node is None or not store.node_visible(node):
            raise EdgeNotFound(f"edge not found: {edge_id}")
    return row


def _node_space(conn: sqlite3.Connection, node_id: str) -> str | None:
    """The space a node lives in (used to collect what an operation touches)."""
    row = conn.execute("SELECT space_id FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return None if row is None else row["space_id"]


@contextmanager
def _curative_cycle(
    op: str, principal: Principal, path: str | Path | None
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Run a curative operation inside a cycle — the ambient one, or its own.

    Decision C2: a curative op is *always* cycle-stamped, because it is always
    several rows from one decision and :func:`undo` reverses one row from one
    payload. When a runner has already set an ambient cycle
    (:func:`in_cycle`), the op **joins** it and neither opens nor closes
    anything — the runner owns that cycle's lifecycle and its report. With no
    ambient cycle the op opens a one-op ``curative`` cycle attributed to the
    caller, runs inside it, and closes it ``completed`` — or ``failed``, since
    a cycle that vanished on failure is a cycle nobody could ask about.

    Args:
        op: The operation's name, recorded in the cycle's report.
        principal: Who is asking; ``open_cycle`` records it as ``triggered_by``
            and gates the open on the same authority the operation needs.
        path: Explicit database path.

    Yields:
        ``(cycle_id, report)`` — the id every event inside is stamped with, and
        a dict the body fills in as the cycle's report. On the ambient path the
        report is discarded, since the runner writes its own.
    """
    ambient = _CURRENT_CYCLE.get()
    if ambient is not None:
        yield ambient, {}
        return
    try:
        cycle = open_cycle(trigger="curative", principal=principal, path=path)
    except GrantNotPermitted as refusal:
        # The check is right — curative work is the review tier — but the
        # caller named an operation, not a cycle, and a refusal that talks
        # about machinery they never mentioned is a line nobody can act on.
        raise GrantNotPermitted(
            f"{principal.actor_string} may not run the curative operation {op!r}: it runs "
            "inside a consolidation cycle, which needs a human, or edit on a space"
        ) from refusal
    report: dict[str, Any] = {"op": op}
    with in_cycle(cycle.id):
        try:
            yield cycle.id, report
        except Exception as exc:
            close_cycle(
                cycle.id,
                status="failed",
                report={**report, "error": str(exc)},
                principal=principal,
                path=path,
            )
            raise
        close_cycle(cycle.id, status="completed", report=report, principal=principal, path=path)


def merge_nodes(
    ids: list[str],
    into: str,
    *,
    principal: Principal,
    path: str | Path | None = None,
) -> MergeOut:
    """Merge nodes into a survivor — soft, reversible, nothing destroyed (D9).

    Each merged-away node moves to ``archived`` and gains
    ``props.merged_into``; a ``merge_redirects`` row records
    ``(tombstone, survivor, event_seq)``; and every non-archived edge incident
    to a tombstone is repointed at the survivor, keeping its original endpoints
    in ``props.merged_from`` so a reversal is exact.

    **The read path is deliberately unchanged.** ``get_node`` on a tombstone
    returns the archived node, which says where it went in its own props. A
    redirect on the hottest and most-tested read in the system would buy
    nothing the props field does not already carry.

    Two kinds of incident edge cannot be repointed and are **archived** instead,
    each reported in ``retired`` carrying its own ``reason`` — the same sentence
    the event payload records: one whose repointing would produce a
    **self-loop** (both endpoints merged into the same survivor — which is
    exactly what happens to the ``duplicate_of`` edge that proposed the merge),
    and one that would **duplicate** an edge the survivor already carries.
    Neither is dropped silently, and neither is reported without saying which of
    the two rules bit.

    What it refuses, and why each is a refusal rather than a silent skip: a node
    merged **into itself** (nothing to do, and a ``tombstones`` list that did
    not match ``ids`` would misreport what happened); a node that is **already a
    tombstone**, on either side, since a redirect chain would break the promise
    that ``props.merged_into`` says where a node went; anything not ``active``,
    since a proposed duplicate is rejected rather than merged and an archived
    one is already retired; and a ``space`` or ``type`` node
    (:data:`STRUCTURAL_TYPE_IDS`).

    Nodes in **different spaces** merge fine — the authority check simply covers
    every space involved. A tombstone's **children keep their parent**: a node's
    parent must live in its own space (:func:`create_node`), so reparenting
    would be illegal for a cross-space merge and would in any case mutate rows
    nobody named; the tombstone stays readable and says where it went. A
    repointed ``mentions`` edge is likewise left as it is — the linking node's
    text still says what it says, and the next edit of that node
    re-materialises from the text as always.

    Args:
        ids: The nodes to merge away.
        into: The survivor's node id.
        principal: Who is merging; needs ``edit`` on every space touched —
            the survivor's, each tombstone's, and both endpoint spaces of every
            incident edge.
        path: Explicit database path.

    Returns:
        The survivor, the tombstones, the redirect rows, the repointed and the
        retired edges, and the cycle it all landed in.

    Raises:
        NodeNotFound: If an id does not resolve, or is not readable.
        GrantNotPermitted: If the principal may not edit every space touched.
        ValueError: For any of the refusals above.
    """
    if not ids:
        raise ValueError("merge_nodes needs at least one node to merge away")
    with _curative_cycle("merge_nodes", principal, path) as (cycle_id, report):
        conn = _connect(path)
        try:
            # The mergeability checks below are reads the writes would otherwise
            # race — two concurrent merges of the same tombstone both passing
            # `_require_mergeable` (finding M8).
            db.begin_immediate(conn)
            store = Store(conn, principal)
            actor = principal.actor_string
            survivor = _row_dict(_readable_node(conn, store, into))
            tombstones = [
                _row_dict(_readable_node(conn, store, node_id)) for node_id in dict.fromkeys(ids)
            ]
            _require_mergeable(survivor, tombstones)

            merged_ids = {node["id"] for node in tombstones}
            placeholders = ",".join("?" * len(merged_ids))
            incident = conn.execute(
                f"SELECT * FROM edges WHERE (src_id IN ({placeholders})"
                f" OR dst_id IN ({placeholders})) AND state != 'archived'"
                " ORDER BY created_at, rowid",
                (*sorted(merged_ids), *sorted(merged_ids)),
            ).fetchall()
            spaces: set[str | None] = {survivor["space_id"]}
            spaces.update(node["space_id"] for node in tombstones)
            for edge in incident:
                spaces.add(_node_space(conn, edge["src_id"]))
                spaces.add(_node_space(conn, edge["dst_id"]))
            store.require_review(spaces, "merge nodes")

            redirects: list[MergeRedirectOut] = []
            archived: list[NodeOut] = []
            for tombstone in tombstones:
                props = json.loads(tombstone["props"])
                props["merged_into"] = survivor["id"]
                conn.execute(
                    "UPDATE nodes SET state = 'archived', props = ?,"
                    " updated_at = datetime('now') WHERE id = ?",
                    (json.dumps(props, ensure_ascii=False), tombstone["id"]),
                )
                after = _row_dict(_get_node_row(conn, tombstone["id"]))
                # One event per node, with the ordinary before/after shape: this
                # is what keeps the FTS and vec projectors correct across a
                # merge (they read `payload["after"]` off any `node.` event).
                seq = _emit(
                    conn,
                    actor,
                    "node.merge",
                    {"before": tombstone, "after": after, "into_id": survivor["id"]},
                )
                _write_version(conn, after, actor, seq)
                conn.execute(
                    "INSERT INTO merge_redirects (tombstone_id, into_id, event_seq)"
                    " VALUES (?, ?, ?)",
                    (tombstone["id"], survivor["id"], seq),
                )
                redirect = conn.execute(
                    "SELECT * FROM merge_redirects WHERE tombstone_id = ?", (tombstone["id"],)
                ).fetchone()
                redirects.append(MergeRedirectOut(**_row_dict(redirect)))
                archived.append(_node_out(after))

            # Every repointed edge ends up incident to the survivor, so the
            # survivor's own live edges are the complete set a duplicate could
            # collide with.
            live_keys = {
                (row["src_id"], row["dst_id"], row["type_id"])
                for row in conn.execute(
                    "SELECT src_id, dst_id, type_id FROM edges"
                    " WHERE (src_id = ? OR dst_id = ?) AND state != 'archived'",
                    (survivor["id"], survivor["id"]),
                ).fetchall()
            }
            relinked: list[EdgeOut] = []
            retired: list[RetiredEdgeOut] = []
            for row in incident:
                before = _row_dict(row)
                new_src = survivor["id"] if before["src_id"] in merged_ids else before["src_id"]
                new_dst = survivor["id"] if before["dst_id"] in merged_ids else before["dst_id"]
                key = (new_src, new_dst, before["type_id"])
                reason = None
                if new_src == new_dst:
                    reason = (
                        "repointing this edge at the survivor would make it a self-loop: "
                        "both of its endpoints are being merged into it"
                    )
                elif key in live_keys:
                    reason = "the survivor already carries an identical edge"
                if reason is not None:
                    # A pending edge leaves `proposed`, so its op is `reject`
                    # rather than `archive` — the state machine allows one.
                    action = "archive" if before["state"] == "active" else "reject"
                    # The reason travels in the return value as well as in the
                    # event payload: a caller reading `retired` should not have
                    # to go to the event log to learn why an edge left the live
                    # graph, and the two rules read very differently to a human.
                    retired.append(
                        RetiredEdgeOut(
                            **_edge_out(
                                _set_edge_state(
                                    conn, before, "archived", action, actor, reason=reason
                                )
                            ).model_dump(),
                            reason=reason,
                        )
                    )
                    continue
                props = json.loads(before["props"])
                props["merged_from"] = {
                    "src_id": before["src_id"],
                    "dst_id": before["dst_id"],
                }
                conn.execute(
                    "UPDATE edges SET src_id = ?, dst_id = ?, props = ? WHERE id = ?",
                    (new_src, new_dst, json.dumps(props, ensure_ascii=False), before["id"]),
                )
                after_edge = _row_dict(_get_edge_row(conn, before["id"]))
                _emit(conn, actor, "edge.relink", {"before": before, "after": after_edge})
                live_keys.add(key)
                relinked.append(_edge_out(after_edge))

            conn.commit()
            report.update(
                {
                    "into": survivor["id"],
                    "merged": [node["id"] for node in tombstones],
                    "relinked": len(relinked),
                    "retired": len(retired),
                }
            )
            return MergeOut(
                into=_node_out(survivor),
                tombstones=archived,
                redirects=redirects,
                relinked=relinked,
                retired=retired,
                cycle_id=cycle_id,
            )
        finally:
            conn.close()


def _require_mergeable(survivor: dict[str, Any], tombstones: list[dict[str, Any]]) -> None:
    """Refuse a merge that cannot be reversed exactly, or means nothing.

    Every refusal here is deliberate rather than a skip: a merge reports the
    nodes it merged, and one that silently dropped an id from that list would
    tell the caller something happened that did not.
    """
    if survivor["type_id"] in STRUCTURAL_TYPE_IDS:
        raise ValueError(
            f"cannot merge into {survivor['id']}: it is a {survivor['type_id']!r} node, and "
            "spaces and types are structure with their own lifecycles rather than knowledge "
            "to curate"
        )
    # The chain check comes first: a tombstone is archived, so the state
    # refusal below would otherwise answer the more specific question with the
    # more general sentence.
    into_props = json.loads(survivor["props"])
    if "merged_into" in into_props:
        raise ValueError(
            f"cannot merge into {survivor['id']}: it was itself merged into "
            f"{into_props['merged_into']}. A redirect chain would break the promise that a "
            "tombstone's props say where it went — merge into that node instead"
        )
    if survivor["state"] != "active":
        raise ValueError(
            f"cannot merge into {survivor['id']}: it is {survivor['state']!r}, and pointing "
            "tombstones at a node that is not part of the live graph would leave them naming "
            "somewhere nobody arrives"
        )
    for node in tombstones:
        if node["id"] == survivor["id"]:
            raise ValueError(
                f"cannot merge {node['id']} into itself: drop the survivor from the list of "
                "nodes to merge away"
            )
        if node["type_id"] in STRUCTURAL_TYPE_IDS:
            raise ValueError(
                f"cannot merge away {node['id']}: it is a {node['type_id']!r} node, and spaces "
                "and types are structure with their own lifecycles rather than knowledge to "
                "curate"
            )
        props = json.loads(node["props"])
        if "merged_into" in props:
            raise ValueError(
                f"cannot merge away {node['id']}: it was already merged into {props['merged_into']}"
            )
        if node["state"] != "active":
            raise ValueError(
                f"cannot merge away {node['id']}: it is {node['state']!r}. A proposed duplicate "
                "is rejected rather than merged, and an archived one is already retired"
            )


def retype(
    ids: list[str],
    new_type: str,
    *,
    principal: Principal,
    path: str | Path | None = None,
) -> RetypeOut:
    """Change nodes' type — the one sanctioned exception to an immutable field.

    A node's ``type`` is fixed at creation **by design**: ``update_node`` takes
    ``title``/``content``/``props`` only, and ``AGENTS.md`` says in as many
    words not to add a ``type`` field to ``PATCH /api/nodes/{id}``. Curative
    ``retype`` (design §8.2) is where the exception lives, which is why it is
    here and neither on that route nor on MCP.

    **No props are transformed.** "Props migration" in the design's phrasing is
    a judgement call about what a property *means* — whether a ``source``'s
    ``url`` is a ``claim``'s citation — and judgement belongs to the LLM half
    of this phase (5b), not to the deterministic layer. This writes the type,
    a version, and a ``node.retype`` event, and stops.

    Retype is state-agnostic: an ``archived`` node may be retyped, since a type
    is what a node *is* rather than where it sits in the state machine. It
    refuses to retype a ``space`` or ``type`` node, and to retype anything
    *into* one (:data:`STRUCTURAL_TYPE_IDS`): a space node whose type changed
    would stop resolving as a space while every node in it kept pointing at it,
    and an ordinary node retyped into a space would be territory that never
    passed the "a space lives in meta, under a free name" rules.

    Per-item failures are collected rather than fatal, exactly as a batch
    accept/reject collects them — an unknown id, a node the caller may not
    edit, or one already of the target type lands in ``failed`` and the rest
    still change.

    Args:
        ids: The nodes to retype.
        new_type: The target node type's id or name; it must resolve to an
            active ``type`` node the principal can read, exactly as
            ``create_node`` resolves one.
        principal: Who is retyping; needs ``edit`` on each node's space.
        path: Explicit database path.

    Returns:
        A batch-transition-shaped result: the ids that changed, the ones that
        did not and why, the resolved target type, and the cycle.

    Raises:
        TypeNotFound: If ``new_type`` resolves to no readable node type.
        ValueError: If ``ids`` is empty, or the target type is structural.
    """
    if not ids:
        raise ValueError("retype needs at least one node")
    with _curative_cycle("retype", principal, path) as (cycle_id, report):
        conn = _connect(path)
        try:
            # Each id's already-a-type check is a read the UPDATE would
            # otherwise race — two concurrent retypes of one node both passing
            # it and both emitting an event (finding M8).
            db.begin_immediate(conn)
            store = Store(conn, principal)
            actor = principal.actor_string
            # Resolved once, outside the loop: the type is the same for every
            # id, so an unknown one is the whole call's failure and not a
            # per-item one.
            type_id = _resolve_node_type(conn, new_type, principal)
            if type_id in STRUCTURAL_TYPE_IDS:
                raise ValueError(
                    f"cannot retype anything into a {type_id!r} node: spaces and types are "
                    "structure with their own lifecycles, and one made this way would never "
                    "have passed the rules that govern it"
                )
            retyped: list[str] = []
            failed: list[TransitionFailure] = []
            for node_id in dict.fromkeys(ids):
                try:
                    before = _row_dict(_readable_node(conn, store, node_id))
                    store.require_review({before["space_id"]}, "retype a node")
                    if before["type_id"] in STRUCTURAL_TYPE_IDS:
                        raise ValueError(
                            f"cannot retype {node_id}: it is a {before['type_id']!r} node, and "
                            "spaces and types are structure with their own lifecycles"
                        )
                    if before["type_id"] == type_id:
                        raise ValueError(f"node {node_id} is already of type {type_id!r}")
                    conn.execute(
                        "UPDATE nodes SET type_id = ?, updated_at = datetime('now') WHERE id = ?",
                        (type_id, node_id),
                    )
                    after = _row_dict(_get_node_row(conn, node_id))
                    seq = _emit(
                        conn,
                        actor,
                        "node.retype",
                        {
                            "before": before,
                            "after": after,
                            "from_type": before["type_id"],
                            "to_type": type_id,
                        },
                    )
                    _write_version(conn, after, actor, seq)
                    retyped.append(node_id)
                except (RecordNotFound, InvalidTransition, GrantNotPermitted, ValueError) as exc:
                    failed.append(TransitionFailure(id=node_id, error=str(exc)))
            conn.commit()
            report.update({"new_type": type_id, "retyped": retyped, "failed": len(failed)})
            return RetypeOut(
                action="retype",
                actor=actor,
                transitioned=retyped,
                failed=failed,
                new_type=type_id,
                cycle_id=cycle_id,
            )
        finally:
            conn.close()


def supersede_edge(
    edge_id: str,
    *,
    replacement: dict[str, Any] | None = None,
    principal: Principal,
    path: str | Path | None = None,
) -> SupersedeOut:
    """Retire an edge that stopped being true, optionally naming its successor.

    Two facts are recorded, because they are two different facts: ``valid_to``
    is closed (*when* it stopped being true) **and** the edge is ``archived``
    (*it is no longer part of the live graph*). The closure is written by the
    shared edge-archive transition (:func:`_set_edge_state` — the same writer
    every other active→archived path uses, so a supersede and a plain archive
    cannot record different facts), with the ``superseded_by`` props write
    riding on the same UPDATE; the timestamps use SQLite's own
    ``datetime('now')`` like every other timestamp here, never a Python clock.

    When ``replacement`` is given it is created first and the two are linked
    through the ``supersedes``/``superseded_by`` vocabulary migration ``0001``
    seeded — **carried in each edge's props, not as an edge**. That is what
    using the seeded pair correctly means here: ``edges.src_id``/``dst_id``
    reference ``nodes``, so one edge cannot point at another, and the seeded
    pair's value is its two directions, which props record exactly (the
    replacement gains ``props.supersedes``, the original ``props.superseded_by``).

    Every field the replacement does not name — ``src_id``, ``dst_id``,
    ``type``, ``props``, ``confidence`` — is inherited from the edge it
    replaces, so a supersede that only changes a confidence says only that. The
    replacement is written through the ordinary edge-creation path, so it is
    validated, type-resolved and grant-checked exactly like any other edge and
    emits its own ``edge.create`` event.

    Args:
        edge_id: The edge to supersede; it must be ``active`` (a proposal is
            rejected rather than superseded, and an archived edge is already
            out of the live graph).
        replacement: Optional ``{src_id, dst_id, type, props, confidence}``.
        principal: Who is superseding; needs ``edit`` on both endpoint spaces.
        path: Explicit database path.

    Returns:
        The superseded edge, the replacement if there was one, and the cycle.

    Raises:
        EdgeNotFound: If the edge does not resolve, or is not readable.
        InvalidTransition: If the edge is not ``active``.
        GrantNotPermitted: If the principal may not edit both endpoint spaces.
        ValueError: If ``replacement`` names an unknown key.
    """
    with _curative_cycle("supersede_edge", principal, path) as (cycle_id, report):
        conn = _connect(path)
        try:
            store = Store(conn, principal)
            actor = principal.actor_string
            before = _row_dict(_readable_edge(conn, store, edge_id))
            store.require_review(
                {_node_space(conn, before["src_id"]), _node_space(conn, before["dst_id"])},
                "supersede an edge",
            )
            if before["state"] != "active":
                raise InvalidTransition(
                    f"cannot supersede an edge in state {before['state']!r}: a proposal is "
                    "rejected rather than superseded, and an archived edge has already left "
                    "the live graph"
                )
            new_edge: dict[str, Any] | None = None
            if replacement is not None:
                unknown = sorted(set(replacement) - set(REPLACEMENT_KEYS))
                if unknown:
                    raise ValueError(
                        f"unknown replacement key(s): {', '.join(unknown)};"
                        f" expected any of {', '.join(REPLACEMENT_KEYS)}"
                    )
                props = dict(replacement.get("props", json.loads(before["props"])))
                props["supersedes"] = before["id"]
                new_edge = _create_edge_in_conn(
                    conn,
                    replacement.get("src_id", before["src_id"]),
                    replacement.get("dst_id", before["dst_id"]),
                    replacement.get("type", before["type_id"]),
                    props=props,
                    confidence=replacement.get("confidence", before["confidence"]),
                    landing=None,
                    actor=actor,
                    store=store,
                )
            after_props = json.loads(before["props"])
            if new_edge is not None:
                after_props["superseded_by"] = new_edge["id"]
            # The retirement itself goes through the shared edge-archive
            # writer, so `valid_to` closes in exactly the same UPDATE shape —
            # and records the same fact — as every other active→archived
            # transition. The `superseded_by` props write rides on that same
            # UPDATE (the writer takes `props`), so the row is written once;
            # the `edge.archive` event it emits is the row move, and the
            # `edge.supersede` event below carries the link on top of it.
            after = _set_edge_state(conn, before, "archived", "archive", actor, props=after_props)
            payload: dict[str, Any] = {"before": before, "after": after}
            if new_edge is not None:
                payload["replacement_id"] = new_edge["id"]
            _emit(conn, actor, "edge.supersede", payload)
            conn.commit()
            report.update(
                {
                    "superseded": before["id"],
                    "replacement": None if new_edge is None else new_edge["id"],
                }
            )
            return SupersedeOut(
                superseded=_edge_out(after),
                replacement=None if new_edge is None else _edge_out(new_edge),
                cycle_id=cycle_id,
            )
        finally:
            conn.close()


def bulk_relink(
    selector: dict[str, Any],
    changes: dict[str, Any],
    *,
    dry_run: bool = False,
    principal: Principal,
    path: str | Path | None = None,
) -> BulkRelinkOut:
    """Repoint or retype many edges at once, behind a reviewable dry run.

    ``selector`` narrows by ``src_id``, ``dst_id``, ``type`` and/or ``state``;
    ``changes`` names a new ``type`` and/or a new ``dst_id``. An empty selector
    is refused rather than treated as "everything" — one call that rewrote every
    edge in the file is precisely what a blast-radius rule exists to prevent —
    and an unknown key in either dict is a sentence rather than a filter that
    silently matched nothing.

    **``dry_run=True`` writes nothing at all**: no cycle is opened, no event is
    emitted, and the result is the diff. That is §8.5's reviewable proposal for
    a large refactor; the reversal, once it is applied for real, is the cycle.
    Every check a real run makes runs on the dry run too, so the diff tells the
    truth about what would be skipped and why.

    Without an explicit ``state`` the selector excludes ``archived`` edges: a
    retired edge is history, and relinking it would rewrite what was once true.
    Naming ``state='archived'`` reaches one deliberately.

    A matched edge is **refused, with its reason reported in** ``skipped``, when
    the new destination is its own source (a self-loop), when the graph already
    carries an identical edge, or when the caller may not edit one of the spaces
    involved. An edge the change would not alter is a different thing and goes
    in ``unchanged`` as a bare id: it is a fact about the diff, not a refusal,
    and mixing the two under one ``error`` field left a script unable to tell
    them apart. At most :data:`MAX_RELINK_EDGES` edges are selected and
    ``truncated`` says whether that ceiling bit.

    Args:
        selector: Any of ``src_id``, ``dst_id``, ``type``, ``state``.
        changes: ``type`` and/or ``dst_id``.
        dry_run: Return the diff and write nothing.
        principal: Who is relinking; needs ``edit`` on the source's space, the
            old destination's, and the new one's.
        path: Explicit database path.

    Returns:
        The matched count, the per-edge diff, the edges nothing would change on,
        the refused edges with reasons, the truncation flag, and the cycle
        (``None`` on a dry run).

    Raises:
        NodeNotFound: If a new ``dst_id`` does not resolve, or is not readable.
        TypeNotFound: If a selector or change type does not resolve.
        ValueError: If either dict is empty or names an unknown key, or the
            selector's ``state`` is not a known state.
    """
    unknown = sorted(set(selector) - set(RELINK_SELECTOR_KEYS))
    if unknown:
        raise ValueError(
            f"unknown selector key(s): {', '.join(unknown)};"
            f" expected any of {', '.join(RELINK_SELECTOR_KEYS)}"
        )
    if not selector:
        raise ValueError(
            "bulk_relink needs a selector: an empty one would match every edge in the file, "
            f"and one call may rewrite at most {MAX_RELINK_EDGES}"
        )
    unknown = sorted(set(changes) - set(RELINK_CHANGE_KEYS))
    if unknown:
        raise ValueError(
            f"unknown change key(s): {', '.join(unknown)};"
            f" expected any of {', '.join(RELINK_CHANGE_KEYS)}"
        )
    if not changes:
        raise ValueError("bulk_relink needs changes: a new 'type' and/or a new 'dst_id'")
    if dry_run:
        return _relink(
            selector, changes, dry_run=True, cycle_id=None, principal=principal, path=path
        )
    with _curative_cycle("bulk_relink", principal, path) as (cycle_id, report):
        result = _relink(
            selector, changes, dry_run=False, cycle_id=cycle_id, principal=principal, path=path
        )
        report.update(
            {
                "matched": result.matched,
                "relinked": len(result.changes),
                "unchanged": len(result.unchanged),
                "skipped": len(result.skipped),
                "truncated": result.truncated,
            }
        )
        return result


def _relink(
    selector: dict[str, Any],
    changes: dict[str, Any],
    *,
    dry_run: bool,
    cycle_id: str | None,
    principal: Principal,
    path: str | Path | None,
) -> BulkRelinkOut:
    """Select, check and (unless ``dry_run``) apply one bulk relink.

    One body for both postures on purpose: a dry run that took a different path
    through the checks would be a diff of something other than what the real
    run does.
    """
    conn = _connect(path)
    try:
        # The duplicate/skip checks below are reads the relink writes would
        # otherwise race — two concurrent relinks both passing them (finding M8).
        db.begin_immediate(conn)
        store = Store(conn, principal)
        actor = principal.actor_string
        clauses: list[str] = []
        params: list[Any] = []
        scope, scope_params = store.edge_scope()
        if scope:
            clauses.append(scope.removeprefix(" AND "))
            params.extend(scope_params)
        for key in ("src_id", "dst_id"):
            if key in selector:
                clauses.append(f"{key} = ?")
                params.append(selector[key])
        if "type" in selector:
            clauses.append("type_id = ?")
            params.append(_resolve_edge_type(conn, selector["type"], principal)[0])
        if "state" in selector:
            if selector["state"] not in STATES:
                raise ValueError(f"state must be one of {STATES}, got {selector['state']!r}")
            clauses.append("state = ?")
            params.append(selector["state"])
        else:
            clauses.append("state != 'archived'")
        rows = conn.execute(
            f"SELECT * FROM edges WHERE {' AND '.join(clauses)} ORDER BY created_at, rowid LIMIT ?",
            (*params, MAX_RELINK_EDGES + 1),
        ).fetchall()
        truncated = len(rows) > MAX_RELINK_EDGES
        rows = rows[:MAX_RELINK_EDGES]

        new_type_id = (
            _resolve_edge_type(conn, changes["type"], principal)[0] if "type" in changes else None
        )
        new_dst = (
            _row_dict(_readable_node(conn, store, changes["dst_id"]))
            if "dst_id" in changes
            else None
        )
        diffs: list[RelinkDiff] = []
        unchanged: list[str] = []
        skipped: list[TransitionFailure] = []
        for row in rows:
            before = _row_dict(row)
            target_type = new_type_id or before["type_id"]
            target_dst = new_dst["id"] if new_dst is not None else before["dst_id"]
            if (target_type, target_dst) == (before["type_id"], before["dst_id"]):
                # A diff annotation, not a refusal: the edge already says what
                # the caller asked for. It has its own list because sharing one
                # with the refusals meant sharing a field called `error`, and a
                # script could then only tell them apart by the sentence.
                unchanged.append(before["id"])
                continue
            if target_dst == before["src_id"]:
                skipped.append(
                    TransitionFailure(
                        id=before["id"],
                        error="the new destination is this edge's own source (a self-loop)",
                    )
                )
                continue
            try:
                dst_space = (
                    new_dst["space_id"] if new_dst is not None else _node_space(conn, target_dst)
                )
                store.require_review(
                    {
                        _node_space(conn, before["src_id"]),
                        _node_space(conn, before["dst_id"]),
                        dst_space,
                    },
                    "relink an edge",
                )
                # Called for its structural refusal, not for the state it
                # returns: a cross-space edge's type node must live in meta, and
                # a relink can make an edge cross a space boundary it did not.
                store.edge_landing_state(
                    _node_space(conn, before["src_id"]), dst_space, _node_space(conn, target_type)
                )
            except GrantNotPermitted as exc:
                skipped.append(TransitionFailure(id=before["id"], error=str(exc)))
                continue
            duplicate = conn.execute(
                "SELECT id FROM edges WHERE src_id = ? AND dst_id = ? AND type_id = ?"
                " AND state != 'archived' AND id != ? LIMIT 1",
                (before["src_id"], target_dst, target_type, before["id"]),
            ).fetchone()
            if duplicate is not None:
                skipped.append(
                    TransitionFailure(
                        id=before["id"],
                        error=f"the graph already carries an identical edge ({duplicate['id']})",
                    )
                )
                continue
            if not dry_run:
                conn.execute(
                    "UPDATE edges SET type_id = ?, dst_id = ? WHERE id = ?",
                    (target_type, target_dst, before["id"]),
                )
                after = _row_dict(_get_edge_row(conn, before["id"]))
                _emit(conn, actor, "edge.relink", {"before": before, "after": after})
            diffs.append(
                RelinkDiff(
                    edge_id=before["id"],
                    src_id=before["src_id"],
                    from_dst_id=before["dst_id"],
                    to_dst_id=target_dst,
                    from_type=before["type_id"],
                    to_type=target_type,
                )
            )
        if not dry_run:
            conn.commit()
        return BulkRelinkOut(
            dry_run=dry_run,
            matched=len(rows),
            changes=diffs,
            unchanged=unchanged,
            skipped=skipped,
            truncated=truncated,
            cycle_id=cycle_id,
        )
    finally:
        conn.close()


# ── Rolling a cycle back (design §8.4, decisions C4 and C5) ───────────────────
#
# D7 promises a rollback "rolls back the whole cycle wholesale", so this is one
# transaction: all of it, or none of it.
#
# **It refuses rather than clobbers** (decision C4). The interesting case is the
# graph having moved on since the cycle ran, and three answers were possible.
# Reversing blindly overwrites the later edits — the failure shape this project
# has closed twice, and a promise the service would not be keeping. A best
# effort leaves the graph in a state nobody asked for and makes "wholesale"
# false. So: reverse only if nothing outside the cycle has touched an affected
# row since, and otherwise refuse and **name the rows and the events**, because
# a human told which four nodes are in the way can act.
#
# **A rollback's own writes carry the rollback's own cycle id** (decision C5).
# It is recorded as a new `cycles` row with `trigger='rollback'`, and the
# original's `rolled_back_by` points at it. That closes the recursion honestly:
# a rollback is reversed the way everything with a `cycle_id` is reversed — by
# rolling *it* back, which re-applies the original — and since `undo` already
# refuses cycle-stamped events, no new guard is needed to keep `undo` out of a
# rollback's own writes.

#: The op a rollback emits per row it reverses, by kind. The first two stay
#: inside the `node.`/`edge.` namespaces for the same reason the curative ops
#: do: :mod:`nodum.projectors` dispatches on ``op.startswith("node.")`` to
#: reproject FTS and the embeddings, and an op outside it would leave the search
#: index quietly describing a graph that no longer exists. ``version.rollback``
#: is outside it for the mirror of that reason — reversing a review decision
#: moves a ``versions`` row and no node text, so a projector reading it would
#: reindex a node nothing changed.
ROLLBACK_OPS = {
    "node": "node.rollback",
    "edge": "edge.rollback",
    "version": "version.rollback",
}

#: The rollback's own summary event. Deliberately *outside* the graph
#: namespaces: it changes no row, so a projector reading it as one would index
#: nothing, and :func:`undo` refuses it as the audit record it is.
ROLLBACK_SUMMARY_OP = "cycle.rollback"

#: Payload table names mapped to the kind of graph record they hold. This is the
#: **conflict** map, not the reversal map (:data:`_REVERSIBLE_TABLES`): it is
#: what reads an ``undo``'s reach out of its ``deleted`` list. ``versions`` is
#: absent because a version row's moves ride on the events themselves — an
#: accept's move is recorded under :data:`VERSION_STATE_KEY` on the ``node.update``
#: it caused, a reject is a ``version.`` event — and :func:`_touched_rows`
#: resolves those by op; a version row *is* a potential conflict when one of
#: those events moved it (finding M10), it just is not addressed by table name.
_TABLE_KIND: dict[str, RollbackKind] = {"nodes": "node", "edges": "edge"}

#: How many conflicts a refusal spells out before summarising the rest. The
#: full list is always on the exception's ``conflicts``.
MAX_NAMED_CONFLICTS = 5


class _RollbackPlan(NamedTuple):
    """What a rollback would do, computed without writing anything."""

    cycle: dict[str, Any]
    events: list[dict[str, Any]]
    skipped: list[int]
    conflicts: list[RollbackConflictOut]
    blockers: list[RollbackBlockerOut]


class _RollbackEffects(NamedTuple):
    """Which rows a reversal puts back, takes out and unlinks — its reported half.

    **One accounting, read by both paths.** :func:`_apply_rollback` fills it as
    it reverses and the ``dry_run`` preflight fills it from the same plan
    without writing, so ``RollbackOut``'s six lists mean the same thing on the
    verdict and on the outcome. They did not: the dry run returned every one of
    them empty and answered *"reversing 4 events"* about a rollback that was
    going to restore three nodes, delete one and drop a merge redirect — the
    confirm dialog a human presses is built on that response. It is the shape
    ``blockers`` was in one round earlier, and it has the same fix: model it in
    the plan rather than only in the run.

    The lists are mutable and the tuple is not, deliberately — a caller adds to
    an accounting it cannot re-point.
    """

    restored_nodes: list[str]
    restored_edges: list[str]
    restored_versions: list[int]
    deleted_nodes: list[str]
    deleted_edges: list[str]
    redirects_removed: list[str]

    @classmethod
    def nothing(cls) -> _RollbackEffects:
        """An empty accounting, for a walk that has not started."""
        return cls([], [], [], [], [], [])

    def record(self, conn: sqlite3.Connection, op: str, payload: dict[str, Any]) -> None:
        """Add what reversing one event accounts for, deciding nothing else.

        Read the payload, and the payload only, with one exception: whether a
        ``merge_redirects`` row is actually there to remove is a fact about the
        file. It is probed **before** the reversal touches anything, which is
        also when :func:`_apply_rollback` probes it — and no plan can hold both
        a merge of a tombstone and the un-merge that would put its redirect
        back, since a cycle merges a given node at most once and the reversal of
        that merge belongs to a different cycle.

        Args:
            conn: The open connection, read from and never written.
            op: The event's op, whose namespace is the row kind.
            payload: The event payload being reversed.
        """
        kind = op.split(".", 1)[0]
        before, after = payload["before"], payload["after"]
        if _applies_a_merge(kind, before, after):
            redirect = conn.execute(
                "SELECT 1 FROM merge_redirects WHERE tombstone_id = ?", (after["id"],)
            ).fetchone()
            # Defensive, and known to be: no test reaches the `None` arm and no
            # cycle shape produces it — at every involution depth tried, the
            # redirect is present exactly when `_applies_a_merge` is true, since
            # the merge that wrote the payload wrote the redirect in the same
            # transaction. The probe stays because this list is a *report* of
            # rows removed and `_apply_rollback`'s DELETE removes none when
            # there is nothing there; counting unconditionally would be the
            # accounting claiming a removal that did not happen the first time
            # anything does delete a redirect out from under a cycle.
            if redirect is not None:
                self.redirects_removed.append(after["id"])
        if before is None:
            # The event created the row, so reversing it takes the row out.
            (self.deleted_nodes if kind == "node" else self.deleted_edges).append(after["id"])
        else:
            {
                "node": self.restored_nodes,
                "edge": self.restored_edges,
                "version": self.restored_versions,
            }[kind].append(before["id"])
        # An accept's version move rides on the `node.update` it caused, so it
        # is counted from the same payload rather than from an event of its own.
        recorded = payload.get(VERSION_STATE_KEY)
        if recorded is not None:
            self.restored_versions.append(int(recorded["before"]["id"]))


def _planned_effects(conn: sqlite3.Connection, plan: _RollbackPlan) -> _RollbackEffects:
    """What reversing ``plan`` would restore, delete and unlink — nothing written.

    The preflight half of :class:`_RollbackEffects`, walking the plan in the
    order the run reverses it (newest first) so the lists come back in the order
    the run reports them.
    """
    effects = _RollbackEffects.nothing()
    for event in reversed(plan.events):
        effects.record(conn, event["op"], json.loads(event["payload"]))
    return effects


def _merged_into(row: dict[str, Any] | None) -> str | None:
    """``props.merged_into`` on a recorded node row, if it carries one."""
    if row is None:
        return None
    props = row.get("props")
    if isinstance(props, str):
        props = json.loads(props or "{}")
    return props.get("merged_into") if isinstance(props, dict) else None


def _applies_a_merge(
    kind: str, before: dict[str, Any] | None, after: dict[str, Any] | None
) -> bool:
    """Did this event's payload put a node *into* a merged-away state?

    The question a reversal has to ask before it deletes a ``merge_redirects``
    row, and it is a question about the **payload**, not about the op's name.
    Keying on ``op == 'node.merge'`` was right exactly twice: a rollback that
    re-applies a merge writes the same before/after pair under the name
    ``node.rollback``, so reversing *that* restored the tombstone and left the
    redirect standing — after which the tombstone's create was permanently
    un-undoable (``undo`` refuses a node a redirect names, and the merge that
    made it is cycle-stamped) and merging the node again died on the
    ``merge_redirects.tombstone_id`` primary key. The involution held for one
    rollback and broke on the third.

    Reading the payload covers both spellings and excludes the reversals that
    *un*-merge (``after`` has no ``merged_into``): those put the redirect back
    through the ordinary ``deleted`` re-insertion, which is where it belongs.
    """
    return kind == "node" and _merged_into(after) is not None and _merged_into(before) is None


def _touched_rows(op: str, payload: dict[str, Any]) -> set[tuple[RollbackKind, str]]:
    """Every ``(kind, row_id)`` an event's payload says it wrote.

    An ``undo``'s reach is read too — the row it restored *and* the rows it
    deleted — because an ordinary undo of an unstamped event can mutate a row a
    later cycle touched (undoing an ``edge.create`` deletes an edge a merge had
    relinked). Conflict detection that only read ``node.``/``edge.`` events
    would miss exactly that.

    **Version rows are covered by op, not by table.** A review moves one from
    one decision: the accept's move rides on the ``node.update`` it caused
    under :data:`VERSION_STATE_KEY` (a rollback mirrors it), a reject is a
    ``version.`` event of its own, and a rollback's removal of an accept's
    snapshot row lands in the reversal's ``deleted`` — all three name the
    ``versions`` row in a way this function reads, so a version row that moved
    outside the cycle is a conflict like any other (finding M10).
    """
    rows: set[tuple[RollbackKind, str]] = set()
    if op == "undo":
        reversed_kind = str(payload.get("reversed_op", "")).split(".", 1)[0]
        restored = payload.get("restored")
        if restored is not None:
            for kind in _TABLE_KIND.values():
                if kind == reversed_kind:
                    rows.add((kind, restored["id"]))
            if reversed_kind == "version":
                rows.add(("version", str(restored["id"])))
    else:
        prefix = op.split(".", 1)[0]
        for kind in _TABLE_KIND.values():
            if kind == prefix:
                for side in ("before", "after"):
                    row = payload.get(side)
                    if row is not None:
                        rows.add((kind, row["id"]))
                break
        else:
            if prefix == "version":
                for side in ("before", "after"):
                    row = payload.get(side)
                    if row is not None:
                        rows.add(("version", str(row["id"])))
            else:
                return rows
    # An accept moved the proposal's own row on the node.update it caused, and
    # a rollback mirrors that move — a `versions.state` change that would
    # otherwise ride on the event unseen.
    recorded = payload.get(VERSION_STATE_KEY)
    if recorded is not None:
        rows.add(("version", str(recorded["before"]["id"])))
    for entry in payload.get("deleted", []):
        kind = _TABLE_KIND.get(entry.get("table", ""))
        if kind is None and entry.get("table") == "versions":
            kind = "version"
        if kind is not None:
            rows.add((kind, entry["row"]["id"]))
    return rows


def _reverses(op: str, payload: dict[str, Any]) -> int | None:
    """The seq this event reversed, when it is itself a reversal.

    An ``undo`` names it ``reversed_seq``; a rollback's per-row event names it
    ``reverses_seq``. Both matter to conflict detection for the same reason: an
    event that has been taken back is not something the graph has moved on to.
    """
    if op == "undo":
        seq = payload.get("reversed_seq")
    elif op in ROLLBACK_OPS.values():
        seq = payload.get("reverses_seq")
    else:
        return None
    return None if seq is None else int(seq)


def _rollback_plan(conn: sqlite3.Connection, cycle_id: str) -> _RollbackPlan:
    """Work out what rolling this cycle back would reverse, and what stands in the way.

    Read-only, so it is both the preflight a ``dry_run`` caller gets and the
    check the real run makes inside its own transaction — a plan computed a
    different way from the one applied would be a diff of something other than
    what happens.

    **What a conflict is.** For each row the cycle touched, take the *first*
    event in the cycle that touched it. A conflict is any event after that one,
    belonging to a different cycle or to none, that touches the same row. Two
    things are deliberately not conflicts. An event another event has since
    taken back (an ``undo``, or a rollback's own reversal) is skipped, because
    the end state rollback wants is the state that row already has. And a
    *reversal* is judged by what it reversed rather than by its own seq: an undo
    that put a row back to where a post-cycle event left it is moving the row
    towards the cycle's end state, not away from it, while an undo reaching past
    the cycle — undoing the create of an edge a merge later relinked — is the
    genuine collision.

    Events from a **different cycle count**: they are still outside this one,
    and "another gardener's cycle touched it" is exactly as much of a
    later modification as a human's edit.

    **"Taken back" is a fixpoint, not a set.** A reversal can itself be
    reversed — rolling a rollback back re-applies what it reversed — so an event
    counted as "already taken back" the moment anything named it was wrong the
    moment that reversal was undone. A cycle whose write had been rolled back
    and then rolled back again is *live*, and was being skipped as reversed
    while an older cycle's rollback wrote straight over it. So a seq is reversed
    iff **some** reversal of it is not itself reversed, computed by recursion:
    reversals strictly increase in seq, so it terminates.

    **Blockers are the second refusal, and they belong here too** (finding S8).
    A conflict is the graph having *moved* a row the cycle wrote; a blocker is
    the graph having *grown something onto* a row the cycle created, which
    :func:`_delete_blocker` refuses. Both stop a rollback, and modelling only
    the first made the preflight disagree with the run — a dry run reported
    ``conflicts: []`` for a rollback that then died on the guard, which is the
    one answer a confirm dialog must not give. Rows the rollback itself removes
    are excluded, since it reverses newest first and a child a cycle created
    after its parent is gone before the parent's create is reached.

    Raises:
        RecordNotFound: If the cycle id does not resolve.
        InvalidTransition: If the cycle is still running, has already been
            rolled back, or wrote no graph events at all.
    """
    cycle = _row_dict(_get_cycle_row(conn, cycle_id))
    if cycle["status"] == "running":
        raise InvalidTransition(
            f"cycle {cycle_id} is still running: its event set is not closed yet, so reversing "
            "it would race the runner still writing into it. If the run is still going, wait "
            "for it to finish; if it was interrupted and will never close itself, close its "
            f"journal entry with: nodum cycle-abandon {cycle_id} — that records it 'failed', "
            "and a failed cycle rolls back like any other"
        )
    if cycle["status"] == "rolled_back":
        raise InvalidTransition(
            f"cycle {cycle_id} has already been rolled back by cycle {cycle['rolled_back_by']}. "
            "To put its writes back, roll *that* cycle back — a rollback is reversed the way "
            "everything else with a cycle id is"
        )
    events: list[dict[str, Any]] = []
    skipped: list[int] = []
    for row in conn.execute(
        "SELECT * FROM events WHERE cycle_id = ? ORDER BY seq", (cycle_id,)
    ).fetchall():
        # Non-graph events a cycle happens to contain are audit records — an
        # `asset.download` says a URL was redeemed, and there is no row behind
        # it to put back. A **version review inside a cycle** is not one of
        # them, and used to be read as one on both halves: the accept's
        # `node.update` came through here like any other event while the
        # `versions.state` flip it also made rode on no event at all, and the
        # reject's `version.reject` — which does carry the version rows — was
        # skipped as the audit record it looks like. Both are covered now: the
        # accept records the move in its own payload (:data:`VERSION_STATE_KEY`)
        # and `version.` joins the reversible kinds here.
        if _is_reversible(row["op"]):
            events.append(_row_dict(row))
        else:
            skipped.append(int(row["seq"]))
    if not events:
        rehearsal = (
            " — it is recorded as a dry run, and a rehearsal writes nothing"
            if cycle["dry_run"]
            else ""
        )
        raise InvalidTransition(
            f"cycle {cycle_id} wrote no graph events, so there is nothing to take back{rehearsal}"
        )

    # The cycle's *first* touch of each row is the line a later write crosses.
    # Not the last: a write that landed between two of the cycle's own writes is
    # a write the cycle clobbered, and restoring the pre-cycle row would lose it
    # just as completely.
    first_touch: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        for key in _touched_rows(event["op"], json.loads(event["payload"])):
            first_touch.setdefault(key, event)

    later = [
        _row_dict(row)
        for row in conn.execute(
            "SELECT * FROM events WHERE seq > ? ORDER BY seq", (events[0]["seq"],)
        ).fetchall()
    ]
    payloads = {event["seq"]: json.loads(event["payload"]) for event in later}
    # Anything that reverses a candidate must itself come after it, so the
    # window above holds every reversal that can matter.
    reversed_by: dict[int, list[int]] = {}
    for event in later:
        reversed_seq = _reverses(event["op"], payloads[event["seq"]])
        if reversed_seq is not None:
            reversed_by.setdefault(reversed_seq, []).append(int(event["seq"]))

    settled: dict[int, bool] = {}

    def is_reversed(seq: int) -> bool:
        """Is this event's effect currently undone? (a fixpoint, not a flag)."""
        answer = settled.get(seq)
        if answer is None:
            # A reversal that has itself been reversed has been re-applied, so
            # it stops counting. Recursion terminates because a reversal always
            # comes after what it reverses.
            settled[seq] = answer = any(
                not is_reversed(reversal) for reversal in reversed_by.get(seq, ())
            )
        return answer

    already_reversed = {seq for seq in reversed_by if is_reversed(seq)}

    conflicts: list[RollbackConflictOut] = []
    for event in later:
        if event["cycle_id"] == cycle_id or event["seq"] in already_reversed:
            continue
        payload = payloads[event["seq"]]
        reverses = _reverses(event["op"], payload)
        for kind, row_id in sorted(_touched_rows(event["op"], payload)):
            origin = first_touch.get((kind, row_id))
            if origin is None or event["seq"] <= origin["seq"]:
                continue
            if reverses is not None and reverses >= origin["seq"]:
                continue
            conflicts.append(
                RollbackConflictOut(
                    kind=kind,
                    row_id=row_id,
                    cycle_event_seq=int(origin["seq"]),
                    cycle_event_op=origin["op"],
                    conflicting_seq=int(event["seq"]),
                    conflicting_op=event["op"],
                    conflicting_actor=event["actor"],
                    conflicting_cycle_id=event["cycle_id"],
                )
            )
    return _RollbackPlan(
        cycle=cycle,
        events=events,
        skipped=skipped,
        conflicts=conflicts,
        blockers=_rollback_blockers(conn, events),
    )


def _rollback_blockers(
    conn: sqlite3.Connection, events: list[dict[str, Any]]
) -> list[RollbackBlockerOut]:
    """The delete guards a rollback of these events would hit, as data.

    Runs the same check :func:`_delete_created_row` raises on, over exactly the
    rows the reversal would have to delete — the ``node.`` events whose payload
    has no ``before``, which is what "the cycle created this" looks like in the
    log. Rows the reversal removes on its way are excluded: the pass goes newest
    first, so anything the cycle created *after* the row in question is already
    gone, as is any ``merge_redirects`` row the reversal of a merge takes with
    it — and, since ``edges.type_id`` became a guard, any edge the reversal
    deletes: one the cycle created (reversed before the type node's create) or
    one incident to a node it deletes (taken with it).
    """
    payloads = [(event, json.loads(event["payload"])) for event in events]
    created = [
        (event, payload["after"])
        for event, payload in payloads
        if event["op"].startswith("node.")
        and payload.get("before") is None
        and payload.get("after") is not None
    ]
    doomed_nodes = frozenset(after["id"] for _, after in created)
    doomed_redirects = frozenset(
        payload["after"]["id"]
        for event, payload in payloads
        if _applies_a_merge(
            event["op"].split(".", 1)[0], payload.get("before"), payload.get("after")
        )
    )
    # Edges the reversal removes, for the `edges.type_id` guard's benefit: the
    # ones the cycle created (their own create-reversal takes them out, newest
    # first) and the ones incident to a node it deletes (taken with it by
    # `_delete_created_row`). Both are gone before any doomed node's create is
    # reversed, so counting either would refuse a rollback that in fact works.
    doomed_edges = frozenset(
        payload["after"]["id"]
        for event, payload in payloads
        if event["op"].startswith("edge.")
        and payload.get("before") is None
        and payload.get("after") is not None
    )
    if doomed_nodes:
        marks = ", ".join("?" for _ in doomed_nodes)
        doomed_edges |= frozenset(
            str(row["id"])
            for row in conn.execute(
                f"SELECT id FROM edges WHERE src_id IN ({marks}) OR dst_id IN ({marks})",
                (*doomed_nodes, *doomed_nodes),
            ).fetchall()
        )
    blockers: list[RollbackBlockerOut] = []
    for event, after in created:
        blocker = _delete_blocker(
            conn,
            after["id"],
            doomed_nodes=doomed_nodes,
            doomed_redirects=doomed_redirects,
            doomed_edges=doomed_edges,
        )
        if blocker is not None:
            dependants, reason = blocker
            blockers.append(
                RollbackBlockerOut(
                    kind="node",
                    row_id=after["id"],
                    cycle_event_seq=int(event["seq"]),
                    cycle_event_op=event["op"],
                    dependants=dependants,
                    reason=reason,
                )
            )
    return blockers


def _conflict_message(cycle_id: str, conflicts: list[RollbackConflictOut]) -> str:
    """Name what stands between a cycle and its rollback, in one line."""
    named = "; ".join(
        f"{conflict.kind} {conflict.row_id} (cycle event {conflict.cycle_event_seq} "
        f"{conflict.cycle_event_op}, changed since by event {conflict.conflicting_seq} "
        f"{conflict.conflicting_op})"
        for conflict in conflicts[:MAX_NAMED_CONFLICTS]
    )
    rest = len(conflicts) - MAX_NAMED_CONFLICTS
    return (
        f"cannot roll back cycle {cycle_id}: {len(conflicts)} row(s) it wrote have been "
        f"changed since by work outside it, and a rollback reverses the whole cycle or none "
        f"of it — {named}" + (f"; and {rest} more" if rest > 0 else "")
    )


def _reversal_record(conn: sqlite3.Connection, cycle: dict[str, Any]) -> tuple[str, Any] | None:
    """What this rollback reversed and the status that cycle held, as recorded.

    Two records say it, and **neither is** ``cycles.rolled_back_by``: that mark
    is what :func:`_restate_reversal_chain` rewrites, so it cannot also be the
    thread the walk follows.

    The rollback's **report** is the first, written when the rollback closes.
    The rollback's own ``cycle.rollback`` **summary event** is the second, and it
    is the one that holds when the first is gone. A rollback whose process died
    between :func:`_apply_rollback`'s commit and :func:`close_cycle` is left
    ``running``, and :func:`abandon_cycle` is the door out of exactly that — but
    abandoning replaces the report wholesale (``{abandoned, abandoned_by,
    detail}``, naming no cycle), so a report-only walk stopped dead at the one
    rollback a human had to close by hand and left every cycle below it marked
    ``rolled_back`` by a cycle that had itself been taken back: writes standing
    while both the journal and ``rollback``'s own refusal said otherwise.

    The summary event is the right second record rather than merely an available
    one. It is emitted inside the transaction that applies the reversal, so it
    exists whenever the reversal does; it is an event, so nothing rewrites it at
    all; and it carries ``previous_status`` too, which ``rolled_back_by`` cannot
    — a ``failed`` cycle put back into force is ``failed`` again, and a fallback
    that only knew *which* cycle would have had to guess ``completed``.
    """
    if cycle["report"]:
        report = json.loads(cycle["report"])
        target_id = report.get("rolled_back")
        if isinstance(target_id, str):
            return target_id, report.get("previous_status")
    summary = conn.execute(
        "SELECT payload FROM events WHERE cycle_id = ? AND op = ? ORDER BY seq LIMIT 1",
        (cycle["id"], ROLLBACK_SUMMARY_OP),
    ).fetchone()
    if summary is None:
        return None
    payload = json.loads(summary["payload"])
    target_id = payload.get("cycle_id")
    if not isinstance(target_id, str):
        return None
    return target_id, payload.get("previous_status")


def _rollback_target(
    conn: sqlite3.Connection, cycle: dict[str, Any]
) -> tuple[dict[str, Any], str] | None:
    """The cycle ``cycle`` reversed and the status it held before, or ``None``.

    ``None`` when ``cycle`` is not a rollback, or nothing recorded a cycle that
    still exists (:func:`_reversal_record`).
    """
    if cycle["trigger"] != "rollback":
        return None
    recorded = _reversal_record(conn, cycle)
    if recorded is None:
        return None
    target_id, previous = recorded
    row = _find_cycle_row(conn, target_id)
    if row is None:
        return None
    if previous not in CYCLE_CLOSED_STATUSES:
        previous = "completed"
    return _row_dict(row), previous


def _restate_reversal_chain(conn: sqlite3.Connection, cycle: dict[str, Any]) -> str | None:
    """Restate the ``rolled_back`` mark down the whole chain below ``cycle``.

    ``cycle`` has just been marked ``rolled_back``, and if it is itself a
    rollback then everything it reversed has changed hands with it. The chain
    **alternates**, which is why one hop is not enough: a rollback that is taken
    back stops standing, so the cycle it reversed stands again and its mark
    comes off — but that cycle may itself be a rollback, and one that stands
    again is once more holding *its* target down, so that mark goes back on.
    Every step flips.

    Clearing exactly one hop was right for depth 2 and wrong from depth 3, where
    it left the journal asserting the mirror of the invariant it exists to keep:
    a cycle reported ``completed`` with no ``rolled_back_by`` while its writes
    were reversed and standing that way. That is not only a misread entry —
    :func:`_rollback_plan` refuses an already-``rolled_back`` cycle by reading
    exactly this column, so a stale ``completed`` hands it a row it will happily
    reverse a second time.

    Args:
        conn: The open write transaction.
        cycle: The cycle just marked ``rolled_back``, as a row dict.

    Returns:
        The cycle this rollback directly put back into force, or ``None``.
    """
    reapplied: str | None = None
    current = cycle
    # `current` was just marked `rolled_back`, so its own writes are not
    # standing; the first hop therefore *frees* whatever it was holding down.
    taken_back = True
    seen = {current["id"]}
    while (target := _rollback_target(conn, current)) is not None:
        row, previous = target
        # The chain is report data, not schema; a malformed one must not spin.
        if row["id"] in seen:
            break
        seen.add(row["id"])
        if taken_back:
            conn.execute(
                "UPDATE cycles SET status = ?, rolled_back_by = NULL WHERE id = ?",
                (previous, row["id"]),
            )
            if reapplied is None:
                reapplied = row["id"]
        else:
            conn.execute(
                "UPDATE cycles SET status = 'rolled_back', rolled_back_by = ? WHERE id = ?",
                (current["id"], row["id"]),
            )
        current = row
        taken_back = not taken_back
    return reapplied


def _apply_rollback(
    conn: sqlite3.Connection,
    plan: _RollbackPlan,
    rollback_cycle_id: str,
    principal: Principal,
) -> RollbackOut:
    """Reverse every event of a planned rollback, newest first, in one transaction.

    Newest first is the only order in which a create and the updates layered on
    top of it come apart. Each event is reversed by the same three primitives
    :func:`undo` uses, and each reversal is emitted as its own ``node.rollback``
    / ``edge.rollback`` event stamped with the rollback's cycle id — so the
    whole thing is itself a cycle, reversible by exactly this function.

    The reversal payloads are the mirror image of the events they reverse
    (``before`` and ``after`` swapped), which is what makes rolling a rollback
    back re-apply the original rather than needing a second, inverse code path.
    That holds for the ``versions`` row a review moves too: an accept's move
    rides on the ``node.update`` it caused (:data:`VERSION_STATE_KEY`) and is
    mirrored onto the ``node.rollback``, while a reject is a ``version.reject``
    reversed like any other recorded row move.
    """
    actor = principal.actor_string
    cycle_id = plan.cycle["id"]
    reversed_events: list[int] = []
    effects = _RollbackEffects.nothing()

    for event in reversed(plan.events):
        payload = json.loads(event["payload"])
        kind = event["op"].split(".", 1)[0]
        table = _REVERSIBLE_TABLES[kind]
        context = f"cannot roll back event {event['seq']} ({event['op']})"
        before, after = payload["before"], payload["after"]
        removed: list[dict[str, Any]] = []

        # Rows the event took away come back first — a previous rollback's own
        # reversal records them, and the node has to exist again before the
        # versions and edges that reference it.
        if payload.get("deleted"):
            _reinsert_rows(conn, payload["deleted"])

        # What this reversal accounts for, decided before it changes anything —
        # through the same function the `dry_run` verdict is built from, so the
        # preflight a confirm dialog reads cannot disagree with the run.
        effects.record(conn, event["op"], payload)

        # `merge_redirects` is covered by no event of its own. It is derivable
        # (the tombstone and the survivor are both in the payload) but it has to
        # be removed explicitly, because the foreign key it holds into `nodes`
        # otherwise makes the tombstone's original create permanently
        # un-undoable.
        if _applies_a_merge(kind, before, after):
            redirect = conn.execute(
                "SELECT * FROM merge_redirects WHERE tombstone_id = ?", (after["id"],)
            ).fetchone()
            if redirect is not None:
                removed.append({"table": "merge_redirects", "row": _row_dict(redirect)})
                conn.execute("DELETE FROM merge_redirects WHERE tombstone_id = ?", (after["id"],))

        if before is None:
            # The event created the row: take it back out. The live row is what
            # is recorded, not the payload's — they are equal (nothing outside
            # the cycle has touched it, or there would be no rollback) and the
            # live one is the one that has to go back on a reversal of this.
            live = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (after["id"],)).fetchone()
            if live is None:
                raise UndoNotPossible(f"{context}: {kind} {after['id']} no longer exists")
            current = _row_dict(live)
            removed.extend(_delete_created_row(conn, kind, table, current, context))
            reversal: dict[str, Any] = {"before": current, "after": None}
            restored: dict[str, Any] | None = None
        elif after is None:
            # The event removed the row — only a previous rollback emits that
            # shape — and the re-insertion above has already put it back.
            if not payload.get("deleted"):
                _reinsert_rows(conn, [{"table": table, "row": before}])
            reversal = {"before": None, "after": before}
            restored = before
        else:
            restored = _restore_row(conn, kind, table, before, principal, context)
            reversal = {"before": after, "after": before}

        # An accept moved a version row on top of the node it rewrote, recorded
        # on this same event. The mirror goes on the reversal so that rolling
        # *this* back re-applies the accept — the involution the rest of these
        # payloads already hold, at every depth.
        version_state = _restore_version_state(conn, payload, principal, context)
        # The snapshot the accept wrote of the accepted node goes with it,
        # recorded in `deleted` so that reversing *this* reversal re-inserts it
        # (finding M9).
        accept_snapshot = _accept_snapshot_row(conn, int(event["seq"]), payload)
        if accept_snapshot is not None:
            removed.append({"table": "versions", "row": accept_snapshot})
            conn.execute("DELETE FROM versions WHERE id = ?", (accept_snapshot["id"],))

        reversal.update(
            {
                "reverses_seq": int(event["seq"]),
                "reverses_op": event["op"],
                "rolled_back_cycle": cycle_id,
            }
        )
        if version_state is not None:
            reversal[VERSION_STATE_KEY] = version_state
        if removed:
            reversal["deleted"] = removed
        seq = _emit(conn, actor, ROLLBACK_OPS[kind], reversal, cycle_id=rollback_cycle_id)
        # A node mutation writes a version here as everywhere else. The version
        # rows the *cycle* wrote are left alone: like the events themselves they
        # are history, and history is what this file never rewrites — a rollback
        # adds the record of a reversal rather than erasing the record of what
        # it reversed. The one exception is forced by a foreign key, above:
        # deleting a node takes its versions with it, and they are recorded so
        # that reversing *this* puts them back.
        if restored is not None and kind == "node":
            _write_version(conn, restored, actor, seq)
        # Wikilinks are deliberately not re-materialised. Every `mentions` edge
        # the cycle wrote is a cycle event in its own right, so reversing those
        # events *is* the re-materialisation; running it again would race the
        # edge reversals and could recreate what one of them just took out.
        reversed_events.append(int(event["seq"]))

    conn.execute(
        "UPDATE cycles SET status = 'rolled_back', rolled_back_by = ? WHERE id = ?",
        (rollback_cycle_id, cycle_id),
    )
    # Rolling back a rollback re-applies what it reversed, so the cycle it
    # reversed stops being rolled back. Leaving the mark would make the journal
    # say a cycle is taken back while its writes are live again, and would leave
    # it unrollbackable behind a status that is no longer true. It is the whole
    # chain and not one hop: see :func:`_restate_reversal_chain`.
    reapplied = _restate_reversal_chain(conn, plan.cycle)

    _emit(
        conn,
        actor,
        ROLLBACK_SUMMARY_OP,
        {
            "cycle_id": cycle_id,
            "rollback_cycle_id": rollback_cycle_id,
            "previous_status": plan.cycle["status"],
            "reversed_seqs": reversed_events,
            "skipped_seqs": plan.skipped,
            "reapplied_cycle_id": reapplied,
        },
        cycle_id=rollback_cycle_id,
    )
    conn.commit()
    return RollbackOut(
        cycle_id=cycle_id,
        rollback_cycle_id=rollback_cycle_id,
        dry_run=False,
        reversed_events=reversed_events,
        skipped_events=plan.skipped,
        conflicts=[],
        **effects._asdict(),
    )


def rollback_cycle(
    cycle_id: str,
    *,
    dry_run: bool = False,
    principal: Principal,
    path: str | Path | None = None,
) -> RollbackOut:
    """Take a consolidation cycle back whole — all of it, or none of it (D7).

    Every event the cycle emitted is reversed newest first inside one
    transaction, using the same primitives :func:`undo` uses on a single event:
    a create is reversed by deleting the row it made, anything else by writing
    the recorded ``before`` row back. The reversals are emitted as
    ``node.rollback`` / ``edge.rollback`` events — inside the namespaces
    :mod:`nodum.projectors` dispatches on, so FTS and the embeddings follow —
    plus one ``cycle.rollback`` summary event, and **every one of them carries
    the rollback's own cycle id** (decision C5). The original cycle is marked
    ``rolled_back`` with ``rolled_back_by`` naming the new one.

    **It refuses rather than clobbers** (decision C4). If anything outside the
    cycle has touched a row the cycle touched since, nothing is written and
    :class:`RollbackConflict` names the rows and the events. See
    :func:`_rollback_plan` for exactly what counts as a conflict and what
    deliberately does not.

    **A rollback is itself rollable back**, which is how its own writes are
    reversed: ``undo`` refuses a cycle-stamped event, and a rollback's events
    are cycle-stamped like any other cycle's. Rolling one back re-applies the
    original and clears the mark on it, so the journal never says a cycle is
    taken back while its writes are live.

    Human-only, and for a stronger version of :func:`undo`'s own reason: it
    writes prior payloads back verbatim, ``state = 'active'`` included, across
    spaces — and does so for a whole cycle at once. An operation strictly more
    powerful than ``undo`` cannot be gated more weakly than ``undo``.

    Args:
        cycle_id: The cycle to take back.
        dry_run: Compute the plan and return it without writing anything — no
            rollback cycle is opened and no event is emitted. This is the "would
            this succeed?" a UI needs, so conflicts come back in ``conflicts``
            and the delete guards in ``blockers`` rather than as exceptions;
            every other refusal is raised on both paths, because they are
            refusals to plan rather than results of one. A dry run reporting
            either list is a rollback that would fail. **And it reports what the
            run would report**: the six outcome lists are filled from the same
            :class:`_RollbackEffects` accounting the run fills, because a
            preflight answering *"reversing 4 events"* and nothing else about a
            reversal that deletes a node is the disagreement ``blockers``
            already had to be fixed for.
        principal: Who is rolling it back. Must be a human.
        path: Explicit database path.

    Returns:
        What was reversed, restored and deleted — or, on a dry run, what would
        be, plus the conflicts that would stop it.

    Raises:
        GrantNotPermitted: If the principal is not a human.
        RecordNotFound: If the cycle id does not resolve.
        InvalidTransition: If the cycle is still running, has already been
            rolled back, or wrote no graph events.
        RollbackConflict: If the graph has moved on (never on a dry run).
        UndoNotPossible: If a row the reversal needs is gone or has grown
            something the reversal was never asked to delete.
    """
    conn = _connect(path)
    try:
        Store(conn, principal).require_human("roll back a consolidation cycle")
        plan = _rollback_plan(conn, cycle_id)
        # Read on the planning connection, which is the only one a dry run
        # opens: the verdict must describe the graph the plan was computed
        # against, not one a second connection might have seen move.
        planned = _planned_effects(conn, plan) if dry_run else None
    finally:
        conn.close()
    if planned is not None:
        return RollbackOut(
            cycle_id=cycle_id,
            rollback_cycle_id=None,
            dry_run=True,
            reversed_events=[int(event["seq"]) for event in reversed(plan.events)],
            skipped_events=plan.skipped,
            conflicts=plan.conflicts,
            blockers=plan.blockers,
            **planned._asdict(),
        )
    if plan.conflicts:
        raise RollbackConflict(_conflict_message(cycle_id, plan.conflicts), plan.conflicts)

    # The plan above is a preflight: it runs before any cycle is opened, so a
    # refused rollback leaves nothing behind at all — not even a journal entry
    # for something that never happened.
    rollback = open_cycle(trigger="rollback", principal=principal, path=path)
    report: dict[str, Any] = {
        "op": "rollback_cycle",
        "rolled_back": cycle_id,
        # What to put back on the original if *this* rollback is ever rolled
        # back. It lives in the journal entry because that is the record that
        # survives; the cycles row itself only ever holds the current status.
        "previous_status": plan.cycle["status"],
    }
    try:
        conn = _connect(path)
        try:
            # Re-planned inside the write transaction: the preflight ran on a
            # connection that has since been closed, and SQLite's one writer is
            # not one *reader*.
            plan = _rollback_plan(conn, cycle_id)
            if plan.conflicts:
                raise RollbackConflict(_conflict_message(cycle_id, plan.conflicts), plan.conflicts)
            # The rollback covers exactly the cycle's territory, recorded rather
            # than resolved: `open_cycle(scope=…)` re-resolves a space name and
            # an archived space no longer resolves, which would make a cycle
            # unrollbackable for the entirely unrelated reason that its space
            # was retired (the trap `close_cycle` documents).
            conn.execute(
                "UPDATE cycles SET scope = ? WHERE id = ?", (plan.cycle["scope"], rollback.id)
            )
            result = _apply_rollback(conn, plan, rollback.id, principal)
        finally:
            conn.close()
    except Exception as exc:
        # A cycle that vanished on failure is a cycle nobody could ask about —
        # the rule every other cycle here follows.
        close_cycle(
            rollback.id,
            status="failed",
            report={**report, "error": str(exc)},
            principal=principal,
            path=path,
        )
        raise
    close_cycle(
        rollback.id,
        status="completed",
        report={
            **report,
            "reversed": len(result.reversed_events),
            "restored": len(result.restored_nodes) + len(result.restored_edges),
            "deleted": len(result.deleted_nodes) + len(result.deleted_edges),
        },
        principal=principal,
        path=path,
    )
    return result
