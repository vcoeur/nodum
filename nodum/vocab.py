"""Closed vocabularies for the graph's state/kind/level sets — one source of truth.

Every Literal type alias and every tuple/dict constant defining the same sets
lives here, so models.py, service.py, store.py, search.py and the adapters all
name the same vocabulary the web mirror (``web/src/api/types.ts``) names. The
alias names match that mirror exactly (``NodeState``, ``VersionState``,
``ProposalKind``, ``GrantLevel``, ``CycleTrigger``, ``CycleStatus``), which is
what lets the M31 contract test compare the two sides directly.

The tuple/dict constants are re-exported from their historical modules
(``nodum.service``, ``nodum.store``, ``nodum.principal``) so existing
``from nodum.service import STATES`` imports keep resolving. The Literals are
the contract; the runtime checks that validate adapter-provided strings
against the constants are the backstop and stay in place.
"""

from typing import Literal

#: Node/edge lifecycle state.
NodeState = Literal["proposed", "active", "archived"]

#: ``versions`` row state: an applied snapshot, a pending proposal, a rejection.
VersionState = Literal["applied", "proposed", "archived"]

#: What a review-queue entry proposes.
ProposalKind = Literal["node", "edge", "update"]

#: What a state transition (or an annotation) touches.
TransitionKind = Literal["node", "edge", "version"]

#: The state-machine actions, plus ``retype`` — a curative batch op that reuses
#: :class:`~nodum.models.BatchTransitionOut`'s shape (its ``action`` field is
#: always ``retype``) and so must be part of the same vocabulary. The service's
#: runtime check still refuses ``retype`` for the state-machine
#: :func:`nodum.service.transition`, whose action set is ``TRANSITIONS``.
TransitionAction = Literal["accept", "reject", "archive", "retype"]

#: Grant levels, weakest to strongest.
GrantLevel = Literal["read", "suggest", "edit"]

#: The two states a write can land in (``archived`` is a transition, not a landing).
LandingState = Literal["proposed", "active"]

#: Agent account kinds.
AgentKind = Literal["external", "internal"]

#: Principal kinds: a human, or the two agent kinds.
PrincipalKind = Literal["human", "external", "internal"]

#: Capability-URL kinds.
UrlGrantKind = Literal["download", "upload"]

#: Traversal directions.
Direction = Literal["out", "in", "both"]

#: The row kinds a rollback conflict/blocker can name (a version row carries no
#: conflict of its own — see ``service._REVERSIBLE_TABLES``).
RollbackKind = Literal["node", "edge"]

#: How a consolidation cycle came to exist.
CycleTrigger = Literal["manual", "scheduled", "curative", "rollback"]

#: Every status a ``cycles`` row may hold.
CycleStatus = Literal["running", "completed", "failed", "rolled_back"]

#: Allowed state values shared by nodes and edges (was ``service.STATES``).
STATES: tuple[NodeState, ...] = ("proposed", "active", "archived")

#: State transitions: action → (required current state, resulting state).
TRANSITIONS: dict[TransitionAction, tuple[NodeState, NodeState]] = {
    "accept": ("proposed", "active"),
    "reject": ("proposed", "archived"),
    "archive": ("active", "archived"),
}

#: The transitions that *review* a proposal (was ``service.REVIEW_ACTIONS``).
REVIEW_ACTIONS: tuple[TransitionAction, ...] = ("accept", "reject")

#: Node states :func:`nodum.service.suggest_links` draws link targets from.
SUGGEST_STATES: tuple[NodeState, ...] = ("active", "proposed")

#: Edge states :func:`nodum.service.subgraph` follows when none are named.
DEFAULT_EDGE_STATES: tuple[NodeState, ...] = ("active",)

#: The two states a write can land in (was ``store.LANDING_STATES``).
LANDING_STATES: tuple[LandingState, ...] = ("proposed", "active")

#: The three proposal kinds a review-queue filter may name.
PROPOSAL_KINDS: tuple[ProposalKind, ...] = ("node", "edge", "update")

#: Grant levels, ordered: read ⊂ suggest ⊂ edit (was ``principal.GRANT_LEVELS``).
GRANT_LEVELS: dict[GrantLevel, int] = {"read": 1, "suggest": 2, "edit": 3}

#: Grant level names (was ``service.GRANT_LEVEL_NAMES``).
GRANT_LEVEL_NAMES: tuple[GrantLevel, ...] = ("read", "suggest", "edit")

#: How a cycle came to exist (was ``service.CYCLE_TRIGGERS``).
CYCLE_TRIGGERS: tuple[CycleTrigger, ...] = ("manual", "scheduled", "curative", "rollback")

#: The statuses a cycle may be closed into (was ``service.CYCLE_CLOSED_STATUSES``).
CYCLE_CLOSED_STATUSES: tuple[CycleStatus, ...] = ("completed", "failed", "rolled_back")

#: Every status a ``cycles`` row may hold (was ``service.CYCLE_STATUSES``).
CYCLE_STATUSES: tuple[CycleStatus, ...] = ("running", *CYCLE_CLOSED_STATUSES)

#: The triggers a *consolidation run* opens (was ``service.CONSOLIDATION_TRIGGERS``).
CONSOLIDATION_TRIGGERS: tuple[CycleTrigger, ...] = ("manual", "scheduled")

#: Valid traversal directions (was ``service.DIRECTIONS``).
DIRECTIONS: tuple[Direction, ...] = ("out", "in", "both")
