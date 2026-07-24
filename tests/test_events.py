"""Event log contents, node versions, and undo semantics."""

from __future__ import annotations

import pytest

from nodum import service
from nodum.service import EventNotFound, NodeNotFound, UndoNotPossible


def _events():
    return list(reversed(service.list_events(limit=1000)))  # chronological


def test_create_appends_event_with_full_after_payload(fresh_db):
    node = service.create_node(type="note", title="T", content="body")
    (event,) = _events()
    assert event.op == "node.create"
    assert event.actor == "human"
    assert event.cycle_id is None
    assert event.payload["before"] is None
    assert event.payload["after"]["id"] == node.id
    assert event.payload["after"]["content"] == "body"


def test_agent_create_logs_propose_op(fresh_db):
    service.create_node(type="note", title="T", actor="agent:x")
    (event,) = _events()
    assert event.op == "node.propose"
    assert event.actor == "agent:x"


def test_update_event_carries_before_and_after(fresh_db):
    node = service.create_node(type="note", title="T", content="v1")
    service.update_node(node.id, content="v2")
    event = _events()[-1]
    assert event.op == "node.update"
    assert event.payload["before"]["content"] == "v1"
    assert event.payload["after"]["content"] == "v2"


def test_transition_events(fresh_db):
    node = service.create_node(type="note", title="T", actor="agent:x")
    service.transition(node.id, "accept")
    service.transition(node.id, "archive")
    ops = [event.op for event in _events()]
    assert ops == ["node.propose", "node.accept", "node.archive"]


def test_every_node_mutation_writes_a_version(fresh_db):
    node = service.create_node(type="note", title="T", content="v1", actor="agent:x")
    service.update_node(node.id, content="v2")
    service.transition(node.id, "accept")
    versions = service.history(node.id)
    assert [v.content for v in versions] == ["v1", "v2", "v2"]
    assert [v.actor for v in versions] == ["agent:x", "human", "human"]
    # Each version points at the event that caused it.
    seqs = [event.seq for event in _events()]
    assert [v.event_seq for v in versions] == seqs


def test_undo_create_removes_the_node(fresh_db):
    node = service.create_node(type="note", title="T")
    result = service.undo()
    assert result.undone_op == "node.create"
    assert result.restored is None
    # The created row goes, along with the version snapshot written at create.
    assert {entry["table"] for entry in result.deleted} == {"nodes", "versions"}
    assert result.deleted[-1]["row"]["id"] == node.id
    with pytest.raises(NodeNotFound):
        service.get_node(node.id)
    # The undo itself is logged.
    assert _events()[-1].op == "undo"


def test_undo_update_restores_prior_content(fresh_db):
    node = service.create_node(type="note", title="T", content="v1")
    service.update_node(node.id, content="v2", props={"x": 1})
    result = service.undo()
    assert result.undone_op == "node.update"
    restored = service.get_node(node.id)
    assert restored.content == "v1"
    assert restored.props == {}
    # The restore is itself versioned.
    assert service.history(node.id)[-1].content == "v1"


def test_undo_archive_restores_active_state(fresh_db):
    node = service.create_node(type="note", title="T")
    service.transition(node.id, "archive")
    service.undo()
    assert service.get_node(node.id).state == "active"


def test_undo_by_explicit_seq(fresh_db):
    first = service.create_node(type="note", title="one")
    second = service.create_node(type="note", title="two")
    create_seq = _events()[0].seq  # node.create of `first`
    result = service.undo(create_seq)
    assert result.undone_seq == create_seq
    with pytest.raises(NodeNotFound):
        service.get_node(first.id)
    assert service.get_node(second.id).title == "two"


def test_undo_create_removes_dependent_edges_and_versions(fresh_db):
    a = service.create_node(type="claim", title="A")
    b = service.create_node(type="claim", title="B")
    service.create_edge(a.id, b.id, "supports")
    # Undo the creation of b: its incident edge and versions go too.
    create_b_seq = _events()[1].seq
    result = service.undo(create_b_seq)
    deleted_tables = {entry["table"] for entry in result.deleted}
    assert deleted_tables == {"nodes", "edges", "versions"}
    assert service.list_edges() == []
    with pytest.raises(NodeNotFound):
        service.get_node(b.id)


def test_undo_edge_create(fresh_db):
    a = service.create_node(type="claim", title="A")
    b = service.create_node(type="claim", title="B")
    edge = service.create_edge(a.id, b.id, "supports")
    result = service.undo()
    assert result.undone_op == "edge.create"
    assert service.list_edges() == []
    assert result.deleted[-1]["row"]["id"] == edge.id


def test_cannot_undo_an_undo(fresh_db):
    service.create_node(type="note", title="T")
    service.undo()
    with pytest.raises(EventNotFound):
        service.undo()  # latest non-undo event search skips undo rows entirely
    undo_seq = _events()[-1].seq
    with pytest.raises(ValueError, match="undo event"):
        service.undo(undo_seq)


def test_undo_empty_log_raises(fresh_db):
    with pytest.raises(EventNotFound):
        service.undo()


def test_undo_of_a_row_a_previous_undo_deleted_fails(fresh_db):
    """Restoring a row that is gone is a failure, not a success with no effect."""
    a = service.create_node(type="claim", title="A")
    b = service.create_node(type="claim", title="B")
    edge = service.create_edge(a.id, b.id, "supports")  # seq 3
    service.transition(edge.id, "archive")  # seq 4
    create_seq, archive_seq = _events()[2].seq, _events()[3].seq

    service.undo(create_seq)  # the edge row is deleted
    assert service.list_edges() == []

    with pytest.raises(UndoNotPossible, match="no longer exists"):
        service.undo(archive_seq)
    # The archive is still open for a genuine undo — it was never marked
    # reversed, and no phantom `undo` event claims it was.
    undo_events = [event for event in _events() if event.op == "undo"]
    assert [event.payload["reversed_seq"] for event in undo_events] == [create_seq]
    assert service.list_edges() == []


def test_undo_create_of_a_node_with_children_is_refused(fresh_db):
    """Children are later creates: reversing one event must not delete them."""
    page = service.create_node(type="page", title="P")
    child = service.create_node(type="block", content="b1", parent_id=page.id)
    create_page_seq = _events()[0].seq

    with pytest.raises(UndoNotPossible, match="child node"):
        service.undo(create_page_seq)

    assert service.get_node(page.id).id == page.id
    assert service.get_node(child.id).parent_id == page.id
    assert [event.op for event in _events()] == ["node.create", "node.create"]

    # Undoing the child's create first clears the way.
    service.undo()
    service.undo(create_page_seq)
    with pytest.raises(NodeNotFound):
        service.get_node(page.id)
