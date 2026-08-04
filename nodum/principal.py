"""The unforgeable principal (Q13 R1).

A :class:`Principal` is the identity every service function works against.
It is minted **only** by :mod:`nodum.auth` — from a verified credential (HTTP
session, MCP token) or from the trusted-local path (the CLI, where local
access is the trust boundary and ``--as`` names the human for attribution).
Service code never constructs one and never re-derives identity from a
string; ``tests/test_principal_guards.py`` keeps it that way, as AST
properties over every module in the package.

Human principals are unfiltered: human accounts are identity, credentials
and attribution, never a permission scope — the file is the only isolation
boundary. Agent principals carry a grant set (space id → level) loaded from
the ``grants`` table; anything outside it does not exist for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nodum.migrations import META_SPACE_ID
from nodum.vocab import GRANT_LEVELS, PrincipalKind

#: Grant levels, ordered: read ⊂ suggest ⊂ edit (design §5.2 as amended).
GRANT_LEVELS = GRANT_LEVELS

#: Level integers for the checks below.
READ, SUGGEST, EDIT = GRANT_LEVELS["read"], GRANT_LEVELS["suggest"], GRANT_LEVELS["edit"]


@dataclass(frozen=True)
class Principal:
    """One authenticated identity: a human, or an agent plus its grant set.

    Attributes:
        kind: ``human``, ``external`` or ``internal`` (the two agent kinds).
        id: The account id — ``humans.id`` or ``agents.id``.
        grants: Space id → level name (``read``/``suggest``/``edit``); empty
            for humans, who need none. The values stay plain ``str`` because
            they arrive from the ``grants`` table through
            :func:`nodum.auth._grant_set` and the database CHECK is the gate;
            the Literal lives at the service boundary.
        meta_space_id: The meta space's node id, for the cross-space edge
            type rule.
    """

    kind: PrincipalKind
    id: str
    grants: dict[str, str] = field(default_factory=dict)
    meta_space_id: str = META_SPACE_ID

    @property
    def is_human(self) -> bool:
        """Humans are full-rights everywhere: no grants, no filters."""
        return self.kind == "human"

    @property
    def actor_string(self) -> str:
        """The structured actor written to events, versions and created_by."""
        prefix = "human" if self.is_human else "agent"
        return f"{prefix}:{self.id}"

    def level_on(self, space_id: str | None) -> int:
        """The principal's grant level on a space (0 = none; humans: EDIT).

        A ``None`` space is a legacy/pre-migration artifact; treat it as no
        grant for agents (humans are unfiltered regardless).
        """
        if self.is_human:
            return EDIT
        if space_id is None:
            return 0
        level = self.grants.get(space_id, "")
        if level not in GRANT_LEVELS:
            return 0
        return GRANT_LEVELS[level]

    @property
    def read_spaces(self) -> frozenset[str] | None:
        """The spaces whose nodes the principal may see; ``None`` = unfiltered."""
        if self.is_human:
            return None
        return frozenset(self.grants)
