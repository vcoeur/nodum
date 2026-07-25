"""Service-layer CRUD, validation, structure, and the state machine."""

from __future__ import annotations

import pytest
from helpers import OWNER_ACTOR, agent, owner

from nodum import service
from nodum.service import (
    EdgeNotFound,
    InvalidTransition,
    NodeNotFound,
    RecordNotFound,
    TypeNotFound,
    VersionNotFound,
)


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
