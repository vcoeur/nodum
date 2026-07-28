"""Service-layer CRUD, validation, structure, and the state machine."""

from __future__ import annotations

import pytest
from helpers import OWNER_ACTOR, agent, owner

from nodum import service
from nodum.migrations import GARDENER_AGENT_ID
from nodum.service import (
    EdgeNotFound,
    InvalidTransition,
    NodeNotFound,
    RecordNotFound,
    TypeNotFound,
    VersionNotFound,
)
from nodum.store import GrantNotPermitted


def test_create_node_defaults_active_for_human(fresh_db):
    node = service.create_node(type="note", title="Hello", content="body", principal=owner())
    assert node.state == "active"
    assert node.created_by == OWNER_ACTOR
    assert node.type == "note"
    assert node.props == {}


def test_create_node_proposed_for_agent(fresh_db):
    node = service.create_node(type="note", title="Bot note", principal=agent("researcher"))
    assert node.state == "proposed"
    assert node.created_by == "agent:researcher"


def test_create_node_unknown_type_rejected(fresh_db):
    with pytest.raises(TypeNotFound):
        service.create_node(type="no-such-type", title="x", principal=owner())


def test_create_node_unknown_parent_rejected(fresh_db):
    with pytest.raises(NodeNotFound):
        service.create_node(type="note", parent_id="missing", principal=owner())


def test_get_node_roundtrip(fresh_db):
    created = service.create_node(
        type="concept",
        title="C",
        content="text",
        props={"k": 1},
        principal=owner(),
    )
    fetched = service.get_node(created.id, principal=owner())
    assert fetched == created
    assert fetched.props == {"k": 1}


def test_get_missing_node_raises(fresh_db):
    with pytest.raises(NodeNotFound):
        service.get_node("missing", principal=owner())


def test_update_node_changes_only_given_fields(fresh_db):
    node = service.create_node(
        type="note",
        title="old",
        content="old body",
        props={"a": 1},
        principal=owner(),
    )
    updated = service.update_node(node.id, content="new body", principal=owner())
    assert updated.content == "new body"
    assert updated.title == "old"
    assert updated.props == {"a": 1}


def test_children_positions_increment(fresh_db):
    page = service.create_node(type="page", title="Page", principal=owner())
    first = service.create_node(type="block", parent_id=page.id, content="one", principal=owner())
    second = service.create_node(type="block", parent_id=page.id, content="two", principal=owner())
    third = service.create_node(type="block", parent_id=page.id, content="three", principal=owner())
    assert first.position == 1.0
    assert second.position == 2.0
    assert third.position == 3.0
    children = service.list_children(page.id, principal=owner())
    assert [child.id for child in children] == [first.id, second.id, third.id]


def test_list_nodes_filters(fresh_db):
    service.create_node(type="note", title="n1", principal=owner())
    service.create_node(type="claim", title="c1", principal=owner())
    agent_node = service.create_node(type="note", title="n2", principal=agent("x"))
    assert len(service.list_nodes(principal=owner())) == 3
    assert [n.title for n in service.list_nodes(type="note", principal=owner())] == ["n1", "n2"]
    assert [n.id for n in service.list_nodes(state="proposed", principal=owner())] == [
        agent_node.id
    ]


def test_list_nodes_rejects_bad_state(fresh_db):
    with pytest.raises(ValueError, match="state"):
        service.list_nodes(state="bogus", principal=owner())


def test_create_edge_and_list(fresh_db):
    a = service.create_node(type="claim", title="A", principal=owner())
    b = service.create_node(type="claim", title="B", principal=owner())
    edge = service.create_edge(
        a.id,
        b.id,
        "supports",
        confidence=0.8,
        props={"note": "x"},
        principal=owner(),
    )
    assert edge.state == "active"
    assert edge.confidence == 0.8
    assert edge.props == {"note": "x"}
    # list_edges matches incidents in either direction
    assert [e.id for e in service.list_edges(node_id=b.id, principal=owner())] == [edge.id]
    assert service.list_edges(type="contradicts", principal=owner()) == []


def test_create_edge_proposed_for_agent(fresh_db):
    a = service.create_node(type="claim", title="A", principal=owner())
    b = service.create_node(type="claim", title="B", principal=owner())
    edge = service.create_edge(a.id, b.id, "supports", principal=agent("researcher"))
    assert edge.state == "proposed"


def test_create_edge_validates(fresh_db):
    a = service.create_node(type="claim", title="A", principal=owner())
    b = service.create_node(type="claim", title="B", principal=owner())
    with pytest.raises(TypeNotFound):
        service.create_edge(a.id, b.id, "no-such-edge-type", principal=owner())
    with pytest.raises(NodeNotFound):
        service.create_edge(a.id, "missing", "supports", principal=owner())
    with pytest.raises(ValueError, match="confidence"):
        service.create_edge(a.id, b.id, "supports", confidence=1.5, principal=owner())


# ── The landing seam: a grant is a ceiling, not a mandate (design §8.3) ───────


def _pair():
    """Two claim nodes in `main`, written by the owner, to hang an edge between."""
    return (
        service.create_node(type="claim", title="A", principal=owner()),
        service.create_node(type="claim", title="B", principal=owner()),
    )


def test_an_edit_grant_may_file_a_write_proposed_instead(fresh_db):
    """§8.3's self-governing writer: confident writes go active, uncertain ones wait."""
    a, b = _pair()
    curator = agent("curator", grants={"meta": "read", "main": "edit"})

    edge = service.create_edge(a.id, b.id, "supports", landing="proposed", principal=curator)

    assert edge.state == "proposed"
    # And it is in the queue, which is the whole point of asking for it.
    assert [item.id for item in service.list_proposals(principal=owner())] == [edge.id]


def test_a_human_may_file_a_write_proposed_too(fresh_db):
    """No human special case: a human is `edit` everywhere, so the same ceiling applies."""
    a, b = _pair()

    edge = service.create_edge(a.id, b.id, "supports", landing="proposed", principal=owner())

    assert edge.state == "proposed"


def test_asking_to_land_active_on_a_suggest_grant_is_refused(fresh_db):
    """The seam only ever lowers — it is never a way around the grant."""
    a, b = _pair()
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest"})

    with pytest.raises(GrantNotPermitted, match="ceiling on the grant"):
        service.create_edge(a.id, b.id, "supports", landing="active", principal=proposer)

    assert service.list_edges(principal=owner()) == []


def test_a_suggest_grant_lands_proposed_whatever_it_asks_for(fresh_db):
    """The ceiling property: no argument value can raise where a write lands."""
    a, b = _pair()
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest"})

    for landing in (None, "proposed"):
        edge = service.create_edge(a.id, b.id, "supports", landing=landing, principal=proposer)
        assert edge.state == "proposed"


def test_landing_active_under_an_edit_grant_is_the_default_said_out_loud(fresh_db):
    a, b = _pair()
    curator = agent("curator", grants={"meta": "read", "main": "edit"})

    edge = service.create_edge(a.id, b.id, "supports", landing="active", principal=curator)

    assert edge.state == "active"


def test_propose_edges_applies_the_landing_to_the_whole_batch(fresh_db):
    a, b = _pair()
    c = service.create_node(type="claim", title="C", principal=owner())
    curator = agent("curator", grants={"meta": "read", "main": "edit"})

    result = service.propose_edges(
        [
            {"src": a.id, "dst": b.id, "edge_type": "supports"},
            {"src": b.id, "dst": c.id, "edge_type": "supports"},
        ],
        landing="proposed",
        principal=curator,
    )

    assert result.failed == []
    assert [edge.state for edge in result.created] == ["proposed", "proposed"]


def test_propose_edges_reports_an_escalation_as_a_failed_suggestion(fresh_db):
    """A refusal that depends on the endpoints is per suggestion, like every other."""
    a, b = _pair()
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest"})

    result = service.propose_edges(
        [{"src": a.id, "dst": b.id, "edge_type": "supports"}],
        landing="active",
        principal=proposer,
    )

    assert result.created == []
    assert "ceiling on the grant" in result.failed[0].error
    assert service.list_edges(principal=owner()) == []


def test_a_landing_state_a_write_cannot_land_in_is_refused_once(fresh_db):
    """`archived` is not a landing state, and a batch-level argument fails once."""
    a, b = _pair()

    with pytest.raises(ValueError, match="landing must be one of"):
        service.create_edge(a.id, b.id, "supports", landing="archived", principal=owner())
    with pytest.raises(ValueError, match="landing must be one of"):
        service.propose_edges(
            [{"src": a.id, "dst": b.id, "edge_type": "supports"}] * 3,
            landing="archived",
            principal=owner(),
        )


def test_accept_reject_archive_transitions(fresh_db):
    node = service.create_node(type="note", title="p", principal=agent("x"))
    assert node.state == "proposed"
    accepted = service.transition(node.id, "accept", principal=owner())
    assert accepted.state == "active"
    archived = service.transition(node.id, "archive", principal=owner())
    assert archived.state == "archived"

    other = service.create_node(type="note", title="q", principal=agent("x"))
    rejected = service.transition(other.id, "reject", principal=owner())
    assert rejected.state == "archived"


def test_invalid_transitions_rejected(fresh_db):
    node = service.create_node(type="note", title="active one", principal=owner())  # active
    with pytest.raises(InvalidTransition):
        service.transition(node.id, "accept", principal=owner())
    with pytest.raises(InvalidTransition):
        service.transition(node.id, "reject", principal=owner())

    proposed = service.create_node(type="note", title="p", principal=agent("x"))
    with pytest.raises(InvalidTransition):
        service.transition(proposed.id, "archive", principal=owner())


def test_transition_applies_to_edges_too(fresh_db):
    a = service.create_node(type="claim", title="A", principal=owner())
    b = service.create_node(type="claim", title="B", principal=owner())
    edge = service.create_edge(a.id, b.id, "supports", principal=agent("x"))
    accepted = service.transition(edge.id, "accept", principal=owner())
    assert accepted.state == "active"
    assert accepted.id == edge.id


def test_transition_unknown_id_raises_the_kind_agnostic_base(fresh_db):
    """A bare id names no kind, so an unresolvable one is not a *node* miss.

    Reporting `NodeNotFound` here told every caller the wrong thing: the id
    may equally have been an edge or a proposed-version id.
    """
    with pytest.raises(RecordNotFound, match="no node, edge, or version") as raised:
        service.transition("missing", "accept", principal=owner())
    assert not isinstance(raised.value, NodeNotFound | EdgeNotFound | VersionNotFound)


def test_kind_specific_misses_keep_their_own_type(fresh_db):
    """A caller that named a kind still gets that kind's exception…"""
    with pytest.raises(NodeNotFound):
        service.get_node("missing", principal=owner())
    with pytest.raises(VersionNotFound):
        service.diff_versions(1, 2, principal=owner())


def test_every_not_found_is_catchable_through_one_base(fresh_db):
    """…and one `except RecordNotFound` still covers all of them."""
    for call in (
        lambda: service.get_node("missing", principal=owner()),
        lambda: service.diff_versions(1, 2, principal=owner()),
        lambda: service.transition("missing", "accept", principal=owner()),
    ):
        with pytest.raises(RecordNotFound):
            call()


def test_transition_unknown_action(fresh_db):
    node = service.create_node(type="note", title="x", principal=owner())
    with pytest.raises(ValueError, match="unknown transition"):
        service.transition(node.id, "explode", principal=owner())


# ── Space lifecycle: a space is a node, so its lifecycle is a node's ──────────


def test_create_space_is_a_node_of_type_space_in_meta(fresh_db):
    space = service.create_space("research", principal=owner())

    assert space.type == "space"
    assert space.space_id == "meta"
    assert space.title == "research"
    assert space.state == "active"
    # And it is immediately usable as a write target, by name or by id.
    written = service.create_node(type="note", title="n", space="research", principal=owner())
    assert written.space_id == space.id


def test_rename_and_archive_round_trip_by_name_or_id(fresh_db):
    space = service.create_space("draft", principal=owner())

    renamed = service.rename_space("draft", "reference", principal=owner())
    assert renamed.id == space.id
    assert renamed.title == "reference"
    # The rename is an ordinary node update, so it is versioned and logged.
    assert [version.title for version in service.history(space.id, principal=owner())] == [
        "draft",
        "reference",
    ]

    archived = service.archive_space(space.id, principal=owner())
    assert archived.state == "archived"
    assert [row.title for row in service.list_spaces(principal=owner())] == ["meta", "main"]
    # An archived space has left the vocabulary: it no longer resolves at all.
    with pytest.raises(TypeNotFound):
        service.create_node(type="note", title="n", space="reference", principal=owner())


def test_the_lifecycle_refuses_a_node_that_is_not_a_space(fresh_db):
    """The route/command says "space", so a note id must not be renamed by it."""
    note = service.create_node(type="note", title="not a space", principal=owner())

    with pytest.raises(TypeNotFound):
        service.rename_space(note.id, "hijacked", principal=owner())
    with pytest.raises(TypeNotFound):
        service.archive_space(note.id, principal=owner())
    assert service.get_node(note.id, principal=owner()).title == "not a space"


def test_list_spaces_carries_live_node_counts_and_grant_holders(fresh_db):
    space = service.create_space("research", principal=owner())
    service.create_space("sandbox", principal=owner())
    service.create_node(type="note", title="live", space="research", principal=owner())
    proposed = service.create_node(type="note", title="draft", space="research", principal=owner())
    service.transition(proposed.id, "archive", principal=owner())
    researcher = agent("researcher", grants={"meta": "read"})
    service.grant(researcher.id, "research", "suggest", principal=owner())

    listed = {row.title: row for row in service.list_spaces(principal=owner())}

    # One live node: the archived one is retired, not territory.
    assert listed["research"].node_count == 1
    assert listed["research"].id == space.id
    assert [(g.agent_id, g.level) for g in listed["research"].grants] == [
        (researcher.id, "suggest")
    ]
    # The gardener's seeded grants (migration 0014) are ordinary rows and list
    # here beside every other agent's — that is what makes them revocable.
    assert [(g.agent_id, g.level) for g in listed["main"].grants] == [(GARDENER_AGENT_ID, "edit")]
    # A space nobody was granted reports an empty list rather than omitting the key.
    assert listed["sandbox"].grants == []


def test_list_spaces_is_human_only(fresh_db):
    """Which agent holds what is governance information, exactly as `grants` is."""
    service.create_space("research", principal=owner())

    with pytest.raises(GrantNotPermitted):
        service.list_spaces(principal=agent("researcher"))
