"""The scope-bound store (Q13 R1): the single data-path choke point.

A :class:`Store` wraps one connection plus the :class:`Principal` it was
minted for, and service code touches the database through it. Reads get the
principal's read-set baked into their SQL; writes pass one validation path
that owns the level check, the both-endpoints edge rules, the meta-space
type rule, and the writer's own ceiling on where a write lands
(:meth:`Store.cap_landing`). A human principal gets an unfiltered store — no
``WHERE`` clause at all — so the human path pays nothing.

The leak rule is default-deny: an agent with no grant on a space cannot
tell that space exists, so reads answer *not found* (never *permission
denied*) for rows outside the read set. A ``space``-typed node is scoped to
its **own id** rather than to the space it sits in: space nodes live in the
meta space, which every agent reads for the type vocabulary, so filtering a
space node on ``space_id`` alone would hand every space in the file to any
meta reader (M3). A space node is visible iff the principal holds a grant
on that space — the one rule is computed in :meth:`Store.node_scope` (SQL)
and :meth:`Store.node_visible` (rows), so every node read inherits it.
"""

from __future__ import annotations

import sqlite3

from nodum.migrations import META_SPACE_ID
from nodum.principal import EDIT, READ, SUGGEST, Principal
from nodum.vocab import LANDING_STATES, LandingState

#: The two states a write can land in. ``archived`` is not one of them: a write
#: lands live or as a proposal, and retiring it is a transition afterwards.
LANDING_STATES = LANDING_STATES


class GrantNotPermitted(PermissionError):
    """Raised when a principal's grants do not cover the attempted write."""


def require_landing_state(landing: LandingState | None) -> None:
    """Reject a requested landing state that is not one a write can land in.

    Args:
        landing: The caller's requested state, or ``None`` for "my grant's".

    Raises:
        ValueError: If ``landing`` is not in :data:`LANDING_STATES`.
    """
    if landing is not None and landing not in LANDING_STATES:
        raise ValueError(f"landing must be one of {LANDING_STATES}, got {landing!r}")


def node_scope_clause(spaces: frozenset[str], alias: str = "") -> tuple[str, list[str]]:
    """The read-set clause for one agent's node reads (never ``None`` spaces).

    A non-space node is visible iff its space is in the set; a ``space``-typed
    node is visible iff its **own id** is. Space nodes live in the meta space,
    which every agent reads for the type vocabulary, so a filter on ``space_id``
    alone would hand every space in the file to any meta reader (M3): the node
    itself is the scope, not the space it sits in. A grant on a space is the
    proof of acquaintance with it, so a space node resolves through its own id
    in the set — and never through another space's grant.

    Args:
        spaces: The principal's read set (``None`` — the unfiltered human case
            — is the caller's to skip, exactly as the empty set is: the latter
            is a boundary that must make the query match nothing).
        alias: Column prefix, e.g. ``"n."`` when the query aliases the table.

    Returns:
        ``(clause, params)``: the ANDed boundary, and the params in the order
        the clause's placeholders stand. The empty set yields ``"1 = 0"``.
    """
    if not spaces:
        return "1 = 0", []
    placeholders = ",".join("?" * len(spaces))
    ordered = sorted(spaces)
    # The whole disjunction is parenthesised, not just each arm: the clause is
    # ANDed onto other filters (and an FTS ``MATCH``), and `A OR B AND C` binds
    # as `A OR (B AND C)` — the trailing filters would apply to the space-node
    # arm alone, and an FTS MATCH beside a top-level OR is refused outright.
    clause = (
        f"(({alias}type_id != 'space' AND {alias}space_id IN ({placeholders}))"
        f" OR ({alias}type_id = 'space' AND {alias}id IN ({placeholders})))"
    )
    return clause, ordered * 2


def node_readable(spaces: frozenset[str], row: sqlite3.Row | dict) -> bool:
    """The read rule for one already-fetched row — :func:`node_scope_clause` as a predicate.

    Args:
        spaces: The principal's read set.
        row: A ``nodes`` row.

    Returns:
        Whether the row is inside the read set.
    """
    if row["type_id"] == "space":
        return row["id"] in spaces
    return row["space_id"] in spaces


class Store:
    """One principal's scoped handle on one connection."""

    def __init__(self, conn: sqlite3.Connection, principal: Principal) -> None:
        self.conn = conn
        self.principal = principal

    # ── Reads ─────────────────────────────────────────────────────────────

    def node_scope(self, alias: str = "") -> tuple[str, list[str]]:
        """``(sql, params)`` restricting a nodes query to the read set.

        Empty for humans. ``alias`` prefixes the column (e.g. ``"n."``).
        """
        spaces = self.principal.read_spaces
        if spaces is None:
            return "", []
        clause, params = node_scope_clause(spaces, alias)
        return f" AND {clause}", params

    def edge_scope(self, alias: str = "") -> tuple[str, list[str]]:
        """``(sql, params)`` restricting an edges query to readable edges.

        An edge is visible iff **both** endpoints are readable — anything
        less leaks the other space's existence (design-pass note 03).

        "Readable" is :func:`node_scope_clause`'s rule, applied to each
        endpoint, and it is reused rather than restated for the reason M3
        exists: this clause used to test ``space_id`` alone, which is the
        rule *before* the space-node fix. A ``space`` node's ``space_id`` is
        meta and every agent reads meta, so an edge touching one passed —
        and :func:`nodum.service._walk` returns both endpoints of every edge
        it follows, handing over the id and title of a space
        :meth:`node_scope` correctly refuses. One rule, two call sites, no
        second copy to fall behind.
        """
        spaces = self.principal.read_spaces
        if spaces is None:
            return "", []
        if not spaces:
            return " AND 1 = 0", []
        src_clause, src_params = node_scope_clause(spaces, "s.")
        dst_clause, dst_params = node_scope_clause(spaces, "d.")
        sql = (
            f" AND EXISTS (SELECT 1 FROM nodes s WHERE s.id = {alias}src_id"
            f" AND {src_clause})"
            f" AND EXISTS (SELECT 1 FROM nodes d WHERE d.id = {alias}dst_id"
            f" AND {dst_clause})"
        )
        return sql, [*src_params, *dst_params]

    def node_visible(self, row: sqlite3.Row | dict) -> bool:
        """Is this nodes row inside the principal's read set?"""
        spaces = self.principal.read_spaces
        return spaces is None or node_readable(spaces, row)

    # ── Writes ────────────────────────────────────────────────────────────

    def landing_state(
        self, space_id: str | None, landing: LandingState | None = None
    ) -> LandingState:
        """The state a node create lands in on ``space_id`` (``active``/``proposed``).

        Args:
            landing: The writer's own ceiling on the result (see
                :meth:`cap_landing`); ``None`` takes the grant's own level.

        Raises:
            ValueError: If ``landing`` is not in :data:`LANDING_STATES`.
            GrantNotPermitted: If the principal may not write the space at all.
        """
        level = self.principal.level_on(space_id)
        granted: LandingState
        if level >= EDIT:
            granted = "active"
        elif level >= SUGGEST:
            granted = "proposed"
        else:
            raise GrantNotPermitted(
                f"{self.principal.actor_string} has no write grant on space {space_id!r}"
            )
        return self.cap_landing(granted, landing)

    def edge_landing_state(
        self,
        src_space: str | None,
        dst_space: str | None,
        type_space: str | None,
        landing: LandingState | None = None,
    ) -> LandingState:
        """The state an edge create lands in, from both endpoint grants.

        Creating an edge needs the matching level on **both** endpoint
        spaces, and a cross-space edge's type node must live in meta — the
        one structural rule (design-pass note 05).

        Args:
            src_space: The source node's space.
            dst_space: The destination node's space.
            type_space: The edge type node's space.
            landing: The writer's own ceiling on the result (see
                :meth:`cap_landing`); ``None`` takes the grant's own level.
        """
        if src_space != dst_space and type_space != META_SPACE_ID:
            raise GrantNotPermitted("a cross-space edge's type node must live in the meta space")
        level = min(self.principal.level_on(src_space), self.principal.level_on(dst_space))
        granted: LandingState
        if level >= EDIT:
            granted = "active"
        elif level >= SUGGEST:
            granted = "proposed"
        else:
            raise GrantNotPermitted(
                f"{self.principal.actor_string} needs the matching grant on both endpoint spaces"
            )
        return self.cap_landing(granted, landing)

    def cap_landing(self, granted: LandingState, landing: LandingState | None) -> LandingState:
        """Lower a granted landing state to the writer's own ceiling.

        Design §8.3: *"``edit`` = the agent writes ``active`` directly and
        self-governs with its own confidence — confident writes go active,
        uncertain ones are filed ``proposed``."* A grant is a **ceiling, not a
        mandate**, so a writer holding ``edit`` may file a write it is unsure
        of as a proposal and put it in front of a human.

        It only ever lowers. Asking for ``active`` on a ``suggest`` grant is
        refused rather than quietly downgraded: a caller that named a state and
        silently got another one has been told nothing, and a refusal here is
        the same refusal the grant already gives that write today.

        Args:
            granted: The state the principal's grants alone would land in.
            landing: The requested state, or ``None`` to take ``granted``.

        Returns:
            The state to write.

        Raises:
            ValueError: If ``landing`` is not in :data:`LANDING_STATES`.
            GrantNotPermitted: If ``landing`` is above what the grant allows.
        """
        require_landing_state(landing)
        if landing is None or landing == granted:
            return granted
        if granted == "proposed":
            raise GrantNotPermitted(
                f"{self.principal.actor_string} may not land {landing!r} here: the landing "
                "state is a ceiling on the grant, never an escalation of it"
            )
        return landing

    def require_review(self, spaces: set[str | None], action: str) -> None:
        """Gate review and curative work: human, or ``edit`` on every space touched.

        Q13 note 03 Q1: an ``edit`` grant carries full in-space state-machine
        authority. Humans pass unconditionally. The ``archive`` transition is
        the exception to the exception: at the review choke point it is the
        human tier (:meth:`require_human`), because it retires live state —
        except inside a consolidation cycle, where the transition is part of
        the cycle's work and stays here at the cycle's own bar.

        Its callers are accept/reject and the consolidation cycle
        lifecycle (:func:`nodum.service.open_cycle` /
        :func:`nodum.service.close_cycle`), which ask the identical question —
        may this principal exercise state-machine authority over these spaces?
        — and so must not grow a second copy of the answer. What a cycle passes
        as ``spaces`` is where the two differ, and that decision lives at the
        call site, in :func:`nodum.service._cycle_authority_spaces`.
        """
        if self.principal.is_human:
            return
        if spaces and all(self.principal.level_on(space) >= EDIT for space in spaces):
            return
        raise GrantNotPermitted(
            f"{self.principal.actor_string} may not {action}: "
            "that needs a human, or edit on the item's space"
        )

    def require_human(self, action: str) -> None:
        """Gate what only a human may do (undo, archive, grant administration).

        The line is live state. Undo writes an event's prior payload back
        verbatim — ``state = 'active'`` included, across spaces — so it is
        exactly the live-state back door an ``edit`` grant must not become.
        Archive is the same line from the other side: it retires live state,
        and an ``edit`` grant is in-space authority, not the right to retire
        it. Accepting within a space the grant covers is a different act from
        either and stays open (see :meth:`require_review`).
        """
        if not self.principal.is_human:
            raise GrantNotPermitted(f"only a human may {action}")

    # Keep the level constants importable from the choke point's consumers.
    READ = READ
    SUGGEST = SUGGEST
    EDIT = EDIT
