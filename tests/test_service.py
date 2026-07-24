"""Service-layer CRUD, validation, structure, and the state machine."""

from __future__ import annotations

import pytest

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
    node = service.create_node(type="note", title="Hello", content="body")
    assert node.state == "active"
    assert node.created_by == "human"
    assert node.type == "note"
    assert node.props == {}


def test_create_node_proposed_for_agent(fresh_db):
    node = service.create_node(type="note", title="Bot note", actor="agent:researcher")
    assert node.state == "proposed"
    assert node.created_by == "agent:researcher"


def test_create_node_unknown_type_rejected(fresh_db):
    with pytest.raises(TypeNotFound):
        service.create_node(type="no-such-type", title="x")


def test_create_node_unknown_parent_rejected(fresh_db):
    with pytest.raises(NodeNotFound):
        service.create_node(type="note", parent_id="missing")


def test_get_node_roundtrip(fresh_db):
    created = service.create_node(type="concept", title="C", content="text", props={"k": 1})
    fetched = service.get_node(created.id)
    assert fetched == created
    assert fetched.props == {"k": 1}


def test_get_missing_node_raises(fresh_db):
    with pytest.raises(NodeNotFound):
        service.get_node("missing")


def test_update_node_changes_only_given_fields(fresh_db):
    node = service.create_node(type="note", title="old", content="old body", props={"a": 1})
    updated = service.update_node(node.id, content="new body")
    assert updated.content == "new body"
    assert updated.title == "old"
    assert updated.props == {"a": 1}


def test_children_positions_increment(fresh_db):
    page = service.create_node(type="page", title="Page")
    first = service.create_node(type="block", parent_id=page.id, content="one")
    second = service.create_node(type="block", parent_id=page.id, content="two")
    third = service.create_node(type="block", parent_id=page.id, content="three")
    assert first.position == 1.0
    assert second.position == 2.0
    assert third.position == 3.0
    children = service.list_children(page.id)
    assert [child.id for child in children] == [first.id, second.id, third.id]


def test_list_nodes_filters(fresh_db):
    service.create_node(type="note", title="n1")
    service.create_node(type="claim", title="c1")
    agent_node = service.create_node(type="note", title="n2", actor="agent:x")
    assert len(service.list_nodes()) == 3
    assert [n.title for n in service.list_nodes(type="note")] == ["n1", "n2"]
    assert [n.id for n in service.list_nodes(state="proposed")] == [agent_node.id]


def test_list_nodes_rejects_bad_state(fresh_db):
    with pytest.raises(ValueError, match="state"):
        service.list_nodes(state="bogus")


def test_create_edge_and_list(fresh_db):
    a = service.create_node(type="claim", title="A")
    b = service.create_node(type="claim", title="B")
    edge = service.create_edge(a.id, b.id, "supports", confidence=0.8, props={"note": "x"})
    assert edge.state == "active"
    assert edge.confidence == 0.8
    assert edge.props == {"note": "x"}
    # list_edges matches incidents in either direction
    assert [e.id for e in service.list_edges(node_id=b.id)] == [edge.id]
    assert service.list_edges(type="contradicts") == []


def test_create_edge_proposed_for_agent(fresh_db):
    a = service.create_node(type="claim", title="A")
    b = service.create_node(type="claim", title="B")
    edge = service.create_edge(a.id, b.id, "supports", actor="agent:researcher")
    assert edge.state == "proposed"


def test_create_edge_validates(fresh_db):
    a = service.create_node(type="claim", title="A")
    b = service.create_node(type="claim", title="B")
    with pytest.raises(TypeNotFound):
        service.create_edge(a.id, b.id, "no-such-edge-type")
    with pytest.raises(NodeNotFound):
        service.create_edge(a.id, "missing", "supports")
    with pytest.raises(ValueError, match="confidence"):
        service.create_edge(a.id, b.id, "supports", confidence=1.5)


def test_accept_reject_archive_transitions(fresh_db):
    node = service.create_node(type="note", title="p", actor="agent:x")
    assert node.state == "proposed"
    accepted = service.transition(node.id, "accept")
    assert accepted.state == "active"
    archived = service.transition(node.id, "archive")
    assert archived.state == "archived"

    other = service.create_node(type="note", title="q", actor="agent:x")
    rejected = service.transition(other.id, "reject")
    assert rejected.state == "archived"


def test_invalid_transitions_rejected(fresh_db):
    node = service.create_node(type="note", title="active one")  # active
    with pytest.raises(InvalidTransition):
        service.transition(node.id, "accept")
    with pytest.raises(InvalidTransition):
        service.transition(node.id, "reject")

    proposed = service.create_node(type="note", title="p", actor="agent:x")
    with pytest.raises(InvalidTransition):
        service.transition(proposed.id, "archive")


def test_transition_applies_to_edges_too(fresh_db):
    a = service.create_node(type="claim", title="A")
    b = service.create_node(type="claim", title="B")
    edge = service.create_edge(a.id, b.id, "supports", actor="agent:x")
    accepted = service.transition(edge.id, "accept")
    assert accepted.state == "active"
    assert accepted.id == edge.id


def test_transition_unknown_id_raises_the_kind_agnostic_base(fresh_db):
    """A bare id names no kind, so an unresolvable one is not a *node* miss.

    Reporting `NodeNotFound` here told every caller the wrong thing: the id
    may equally have been an edge or a proposed-version id.
    """
    with pytest.raises(RecordNotFound, match="no node, edge, or version") as raised:
        service.transition("missing", "accept")
    assert not isinstance(raised.value, NodeNotFound | EdgeNotFound | VersionNotFound)


def test_kind_specific_misses_keep_their_own_type(fresh_db):
    """A caller that named a kind still gets that kind's exception…"""
    with pytest.raises(NodeNotFound):
        service.get_node("missing")
    with pytest.raises(VersionNotFound):
        service.diff_versions(1, 2)


def test_every_not_found_is_catchable_through_one_base(fresh_db):
    """…and one `except RecordNotFound` still covers all of them."""
    for call in (
        lambda: service.get_node("missing"),
        lambda: service.diff_versions(1, 2),
        lambda: service.transition("missing", "accept"),
    ):
        with pytest.raises(RecordNotFound):
            call()


def test_transition_unknown_action(fresh_db):
    node = service.create_node(type="note", title="x")
    with pytest.raises(ValueError, match="unknown transition"):
        service.transition(node.id, "explode")
