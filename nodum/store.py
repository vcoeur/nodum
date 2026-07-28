"""The scope-bound store (Q13 R1): the single data-path choke point.

A :class:`Store` wraps one connection plus the :class:`Principal` it was
minted for, and service code touches the database through it. Reads get the
principal's read-set baked into their SQL; writes pass one validation path
that owns the level check, the both-endpoints edge rules and the meta-space
type rule. A human principal gets an unfiltered store — no ``WHERE`` clause
at all — so the human path pays nothing.

The leak rule is default-deny: an agent with no grant on a space cannot
tell that space exists, so reads answer *not found* (never *permission
denied*) for rows outside the read set.
"""

from __future__ import annotations

import sqlite3

from nodum.migrations import META_SPACE_ID
from nodum.principal import EDIT, READ, SUGGEST, Principal


class GrantNotPermitted(PermissionError):
    """Raised when a principal's grants do not cover the attempted write."""


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
        if not spaces:
            return " AND 1 = 0", []
        placeholders = ",".join("?" * len(spaces))
        return f" AND {alias}space_id IN ({placeholders})", sorted(spaces)

    def edge_scope(self, alias: str = "") -> tuple[str, list[str]]:
        """``(sql, params)`` restricting an edges query to readable edges.

        An edge is visible iff **both** endpoints are readable — anything
        less leaks the other space's existence (design-pass note 03).
        """
        spaces = self.principal.read_spaces
        if spaces is None:
            return "", []
        if not spaces:
            return " AND 1 = 0", []
        placeholders = ",".join("?" * len(spaces))
        sql = (
            f" AND EXISTS (SELECT 1 FROM nodes s WHERE s.id = {alias}src_id"
            f" AND s.space_id IN ({placeholders}))"
            f" AND EXISTS (SELECT 1 FROM nodes d WHERE d.id = {alias}dst_id"
            f" AND d.space_id IN ({placeholders}))"
        )
        return sql, sorted(spaces) * 2

    def node_visible(self, row: sqlite3.Row | dict) -> bool:
        """Is this nodes row inside the principal's read set?"""
        spaces = self.principal.read_spaces
        return spaces is None or row["space_id"] in spaces

    # ── Writes ────────────────────────────────────────────────────────────

    def landing_state(self, space_id: str | None) -> str:
        """The state a node create lands in on ``space_id`` (``active``/``proposed``).

        Raises:
            GrantNotPermitted: If the principal may not write the space at all.
        """
        level = self.principal.level_on(space_id)
        if level >= EDIT:
            return "active"
        if level >= SUGGEST:
            return "proposed"
        raise GrantNotPermitted(
            f"{self.principal.actor_string} has no write grant on space {space_id!r}"
        )

    def edge_landing_state(
        self, src_space: str | None, dst_space: str | None, type_space: str | None
    ) -> str:
        """The state an edge create lands in, from both endpoint grants.

        Creating an edge needs the matching level on **both** endpoint
        spaces, and a cross-space edge's type node must live in meta — the
        one structural rule (design-pass note 05).
        """
        if src_space != dst_space and type_space != META_SPACE_ID:
            raise GrantNotPermitted("a cross-space edge's type node must live in the meta space")
        level = min(self.principal.level_on(src_space), self.principal.level_on(dst_space))
        if level >= EDIT:
            return "active"
        if level >= SUGGEST:
            return "proposed"
        raise GrantNotPermitted(
            f"{self.principal.actor_string} needs the matching grant on both endpoint spaces"
        )

    def require_review(self, spaces: set[str | None], action: str) -> None:
        """Gate review and curative work: human, or ``edit`` on every space touched.

        Q13 note 03 Q1: an ``edit`` grant carries full in-space state-machine
        authority. Humans pass unconditionally.

        Its callers are accept/reject/archive and the consolidation cycle
        lifecycle (:func:`nodum.service.open_cycle` /
        :func:`nodum.service.close_cycle`), which asks the identical question —
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
        """Gate what only a human may do (undo, grant administration).

        Undo writes an event's prior payload back verbatim — ``state =
        'active'`` included, across spaces — so it is exactly the live-state
        back door an ``edit`` grant must not become.
        """
        if not self.principal.is_human:
            raise GrantNotPermitted(f"only a human may {action}")

    # Keep the level constants importable from the choke point's consumers.
    READ = READ
    SUGGEST = SUGGEST
    EDIT = EDIT
