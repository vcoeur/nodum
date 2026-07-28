"""Event log contents, node versions, and undo semantics."""

from __future__ import annotations

import pytest
from helpers import OWNER_ACTOR, agent, owner

from nodum import service
from nodum.service import EventNotFound, NodeNotFound, UndoNotPossible
from nodum.store import GrantNotPermitted


def _events():
    return list(reversed(service.list_events(limit=1000, principal=owner())))  # chronological


def test_create_appends_event_with_full_after_payload(fresh_db):
    node = service.create_node(type="note", title="T", content="body", principal=owner())
    (event,) = _events()
    assert event.op == "node.create"
    assert event.actor == OWNER_ACTOR
    assert event.cycle_id is None
    assert event.payload["before"] is None
    assert event.payload["after"]["id"] == node.id
    assert event.payload["after"]["content"] == "body"


def test_agent_create_logs_propose_op(fresh_db):
    service.create_node(type="note", title="T", principal=agent("x"))
    (event,) = _events()
    assert event.op == "node.propose"
    assert event.actor == "agent:x"


def test_update_event_carries_before_and_after(fresh_db):
    node = service.create_node(type="note", title="T", content="v1", principal=owner())
    service.update_node(node.id, content="v2", principal=owner())
    event = _events()[-1]
    assert event.op == "node.update"
    assert event.payload["before"]["content"] == "v1"
    assert event.payload["after"]["content"] == "v2"


def test_transition_events(fresh_db):
    node = service.create_node(type="note", title="T", principal=agent("x"))
    service.transition(node.id, "accept", principal=owner())
    service.transition(node.id, "archive", principal=owner())
    ops = [event.op for event in _events()]
    assert ops == ["node.propose", "node.accept", "node.archive"]


def test_every_node_mutation_writes_a_version(fresh_db):
    node = service.create_node(type="note", title="T", content="v1", principal=agent("x"))
    service.update_node(node.id, content="v2", principal=owner())
    service.transition(node.id, "accept", principal=owner())
    versions = service.history(node.id, principal=owner())
    assert [v.content for v in versions] == ["v1", "v2", "v2"]
    assert [v.actor for v in versions] == ["agent:x", OWNER_ACTOR, OWNER_ACTOR]
    # Each version points at the event that caused it.
    seqs = [event.seq for event in _events()]
    assert [v.event_seq for v in versions] == seqs


def test_undo_create_removes_the_node(fresh_db):
    node = service.create_node(type="note", title="T", principal=owner())
    result = service.undo(principal=owner())
    assert result.undone_op == "node.create"
    assert result.restored is None
    # The created row goes, along with the version snapshot written at create.
    assert {entry["table"] for entry in result.deleted} == {"nodes", "versions"}
    assert result.deleted[-1]["row"]["id"] == node.id
    with pytest.raises(NodeNotFound):
        service.get_node(node.id, principal=owner())
    # The undo itself is logged.
    assert _events()[-1].op == "undo"


def test_undo_update_restores_prior_content(fresh_db):
    node = service.create_node(type="note", title="T", content="v1", principal=owner())
    service.update_node(node.id, content="v2", props={"x": 1}, principal=owner())
    result = service.undo(principal=owner())
    assert result.undone_op == "node.update"
    restored = service.get_node(node.id, principal=owner())
    assert restored.content == "v1"
    assert restored.props == {}
    # The restore is itself versioned.
    assert service.history(node.id, principal=owner())[-1].content == "v1"


def test_undo_archive_restores_active_state(fresh_db):
    node = service.create_node(type="note", title="T", principal=owner())
    service.transition(node.id, "archive", principal=owner())
    service.undo(principal=owner())
    assert service.get_node(node.id, principal=owner()).state == "active"


def test_undo_by_explicit_seq(fresh_db):
    first = service.create_node(type="note", title="one", principal=owner())
    second = service.create_node(type="note", title="two", principal=owner())
    create_seq = _events()[0].seq  # node.create of `first`
    result = service.undo(create_seq, principal=owner())
    assert result.undone_seq == create_seq
    with pytest.raises(NodeNotFound):
        service.get_node(first.id, principal=owner())
    assert service.get_node(second.id, principal=owner()).title == "two"


def test_undo_create_removes_dependent_edges_and_versions(fresh_db):
    a = service.create_node(type="claim", title="A", principal=owner())
    b = service.create_node(type="claim", title="B", principal=owner())
    service.create_edge(a.id, b.id, "supports", principal=owner())
    # Undo the creation of b: its incident edge and versions go too.
    create_b_seq = _events()[1].seq
    result = service.undo(create_b_seq, principal=owner())
    deleted_tables = {entry["table"] for entry in result.deleted}
    assert deleted_tables == {"nodes", "edges", "versions"}
    assert service.list_edges(principal=owner()) == []
    with pytest.raises(NodeNotFound):
        service.get_node(b.id, principal=owner())


def test_undo_edge_create(fresh_db):
    a = service.create_node(type="claim", title="A", principal=owner())
    b = service.create_node(type="claim", title="B", principal=owner())
    edge = service.create_edge(a.id, b.id, "supports", principal=owner())
    result = service.undo(principal=owner())
    assert result.undone_op == "edge.create"
    assert service.list_edges(principal=owner()) == []
    assert result.deleted[-1]["row"]["id"] == edge.id


def test_cannot_undo_an_undo(fresh_db):
    service.create_node(type="note", title="T", principal=owner())
    service.undo(principal=owner())
    with pytest.raises(EventNotFound):
        service.undo(principal=owner())  # latest non-undo event search skips undo rows entirely
    undo_seq = _events()[-1].seq
    with pytest.raises(ValueError, match="undo event"):
        service.undo(undo_seq, principal=owner())


def test_undo_empty_log_raises(fresh_db):
    with pytest.raises(EventNotFound):
        service.undo(principal=owner())


def test_undo_of_a_row_a_previous_undo_deleted_fails(fresh_db):
    """Restoring a row that is gone is a failure, not a success with no effect."""
    a = service.create_node(type="claim", title="A", principal=owner())
    b = service.create_node(type="claim", title="B", principal=owner())
    edge = service.create_edge(a.id, b.id, "supports", principal=owner())  # seq 3
    service.transition(edge.id, "archive", principal=owner())  # seq 4
    create_seq, archive_seq = _events()[2].seq, _events()[3].seq

    service.undo(create_seq, principal=owner())  # the edge row is deleted
    assert service.list_edges(principal=owner()) == []

    with pytest.raises(UndoNotPossible, match="no longer exists"):
        service.undo(archive_seq, principal=owner())
    # The archive is still open for a genuine undo — it was never marked
    # reversed, and no phantom `undo` event claims it was.
    undo_events = [event for event in _events() if event.op == "undo"]
    assert [event.payload["reversed_seq"] for event in undo_events] == [create_seq]
    assert service.list_edges(principal=owner()) == []


def test_undo_create_of_a_node_with_children_is_refused(fresh_db):
    """Children are later creates: reversing one event must not delete them."""
    page = service.create_node(type="page", title="P", principal=owner())
    child = service.create_node(type="block", content="b1", parent_id=page.id, principal=owner())
    create_page_seq = _events()[0].seq

    with pytest.raises(UndoNotPossible, match="child node"):
        service.undo(create_page_seq, principal=owner())

    assert service.get_node(page.id, principal=owner()).id == page.id
    assert service.get_node(child.id, principal=owner()).parent_id == page.id
    assert [event.op for event in _events()] == ["node.create", "node.create"]

    # Undoing the child's create first clears the way.
    service.undo(principal=owner())
    service.undo(create_page_seq, principal=owner())
    with pytest.raises(NodeNotFound):
        service.get_node(page.id, principal=owner())


def test_undo_refuses_an_event_that_belongs_to_a_consolidation_cycle(fresh_db):
    """The guard that makes the curative tier safe to name under `node.`/`edge.`.

    Curative ops *have* to live in those namespaces — `projectors` dispatches on
    `op.startswith("node.")` to reproject FTS and embeddings, so an op outside
    them desynchronises the search index silently. Which means undo's "undoable"
    filter would otherwise happily reverse one row of a multi-row merge and
    leave the other half standing.
    """
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        node = service.create_node(type="note", title="merged", principal=owner())
    seq = _events()[-1].seq

    with pytest.raises(UndoNotPossible, match=f"consolidation cycle {cycle.id}"):
        service.undo(seq, principal=owner())
    # Nothing moved, and no `undo` event claims otherwise.
    assert service.get_node(node.id, principal=owner()).title == "merged"
    assert [event for event in _events() if event.op == "undo"] == []


def test_undo_with_no_seq_skips_past_a_cycle_event_to_the_one_below(fresh_db):
    """Same treatment as an event a previous undo already reversed.

    Without the skip, `nodum undo` on a graph whose last write was a
    consolidation would refuse rather than reach the human's own last edit —
    and the refusal would be for something the human never did.
    """
    ordinary = service.create_node(type="note", title="mine", principal=owner())
    cycle = service.open_cycle(trigger="scheduled", principal=owner())
    with service.in_cycle(cycle.id):
        gardened = service.create_node(type="note", title="gardened", principal=owner())

    result = service.undo(principal=owner())

    assert result.undone_op == "node.create"
    assert result.deleted[-1]["row"]["id"] == ordinary.id
    # The cycle's own write is untouched: it is taken back by rolling the cycle
    # back, never by an undo that happened to walk past it.
    assert service.get_node(gardened.id, principal=owner()).title == "gardened"
    with pytest.raises(NodeNotFound):
        service.get_node(ordinary.id, principal=owner())


def test_list_events_narrows_to_one_cycle(fresh_db):
    cycle = service.open_cycle(trigger="manual", principal=owner())
    service.create_node(type="note", title="outside", principal=owner())
    with service.in_cycle(cycle.id):
        inside = service.create_node(type="note", title="inside", principal=owner())

    narrowed = service.list_events(owner(), cycle_id=cycle.id)
    assert [event.payload["after"]["id"] for event in narrowed] == [inside.id]
    assert len(service.list_events(owner())) == 2
    # An id no event carries is an empty list, not everything.
    assert service.list_events(owner(), cycle_id="no-such-cycle") == []


def test_the_cycle_filter_keeps_the_human_only_guard(fresh_db):
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with pytest.raises(GrantNotPermitted, match="read the event log"):
        service.list_events(agent("x"), cycle_id=cycle.id)


def test_undo_create_of_a_space_that_now_holds_nodes_is_refused(fresh_db):
    """`nodes.space_id` is the other foreign key into a node, and `/spaces` grows it.

    The create-reversal branch guarded `parent_id` children and nothing else,
    so undoing the create of a space that had since been written into hit the
    FK: a bare `IntegrityError`, served as a **500** by `/api/undo` and as
    `database error: FOREIGN KEY constraint failed` by the CLI — for the plain
    "an undo the graph has grown past" case the CLI contract promises to name.
    Creating a space is one click away on `/spaces` now.
    """
    space = service.create_space("temp", principal=owner())
    create_space_seq = _events()[0].seq
    inside = service.create_node(type="note", title="in", space=space.id, principal=owner())

    with pytest.raises(UndoNotPossible, match="still holds 1 node"):
        service.undo(create_space_seq, principal=owner())

    # Nothing half-written: the rollback left the space, its occupant and the
    # event log exactly as they were, and no `undo` event claims otherwise.
    assert service.get_node(space.id, principal=owner()).state == "active"
    assert service.get_node(inside.id, principal=owner()).space_id == space.id
    assert [event for event in _events() if event.op == "undo"] == []

    # Emptying the space first clears the way, exactly like the children case.
    service.undo(principal=owner())
    service.undo(create_space_seq, principal=owner())
    with pytest.raises(NodeNotFound):
        service.get_node(space.id, principal=owner())
