"""Event log contents, node versions, and undo semantics."""

from __future__ import annotations

import pytest
from helpers import OWNER_ACTOR, agent, owner

from nodum import service
from nodum.service import EventNotFound, NodeNotFound, RecordNotFound, UndoNotPossible
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


def test_undo_with_no_seq_names_the_cycle_instead_of_reaching_past_it(fresh_db):
    """A cycle is the most recent thing that happened; reaching past it is never meant.

    The no-`seq` path used to filter cycle-stamped events out of the search
    entirely, on the reading that undo cannot reverse one row of a merge so it
    should not offer to. But `nodum undo` means *take back the last thing that
    happened*, and stepping over a consolidation to an older event silently
    reversed something the human never named — see the curative repro in
    `test_curative.py`, where it cost an edge and then left the merge itself
    permanently unrollbackable. The refusal names the cycle instead.
    """
    ordinary = service.create_node(type="note", title="mine", principal=owner())
    cycle = service.open_cycle(trigger="scheduled", principal=owner())
    with service.in_cycle(cycle.id):
        gardened = service.create_node(type="note", title="gardened", principal=owner())

    with pytest.raises(UndoNotPossible, match=f"consolidation cycle {cycle.id}"):
        service.undo(principal=owner())

    # Neither write moved, and no `undo` event claims otherwise.
    assert service.get_node(gardened.id, principal=owner()).title == "gardened"
    assert service.get_node(ordinary.id, principal=owner()).title == "mine"
    assert [event for event in _events() if event.op == "undo"] == []
    # And the refusal is followable: it names the command that does work.
    with pytest.raises(UndoNotPossible, match=f"nodum rollback {cycle.id}"):
        service.undo(principal=owner())


def test_a_bare_undo_still_skips_what_a_previous_undo_reversed(fresh_db):
    """The one skip that stays: a reversed event has a reversal, a cycle has none."""
    first = service.create_node(type="note", title="one", principal=owner())
    second = service.create_node(type="note", title="two", principal=owner())

    service.undo(principal=owner())  # takes `second` back
    result = service.undo(principal=owner())  # walks past it to `first`

    assert result.deleted[-1]["row"]["id"] == first.id
    with pytest.raises(NodeNotFound):
        service.get_node(second.id, principal=owner())


def test_list_events_narrows_to_one_cycle(fresh_db):
    cycle = service.open_cycle(trigger="manual", principal=owner())
    service.create_node(type="note", title="outside", principal=owner())
    with service.in_cycle(cycle.id):
        inside = service.create_node(type="note", title="inside", principal=owner())

    narrowed = service.list_events(owner(), cycle_id=cycle.id)
    assert [event.payload["after"]["id"] for event in narrowed] == [inside.id]
    assert len(service.list_events(owner())) == 2


def test_an_unknown_cycle_id_is_a_not_found_and_not_an_empty_diff(fresh_db):
    """An empty list here is what a *dry run* looks like, so a typo must not fake one.

    `AGENTS.md` leans on `events --cycle <id>` coming back empty as the
    machine-checkable proof that a `consolidate --dry-run` changed nothing. An
    id naming no cycle answering with the same empty list — and exit 0 — makes
    that proof unreadable: the caller cannot tell "the rehearsal wrote nothing"
    from "you mistyped the id".
    """
    with pytest.raises(RecordNotFound, match="consolidation cycle not found"):
        service.list_events(owner(), cycle_id="no-such-cycle")

    # A real dry run still answers with the empty list the claim rests on.
    rehearsal = service.open_cycle(trigger="manual", dry_run=True, principal=owner())
    service.close_cycle(rehearsal.id, status="completed", report={}, principal=owner())
    assert service.list_events(owner(), cycle_id=rehearsal.id) == []


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


def test_undo_create_of_a_space_an_agent_is_granted_on_is_refused(fresh_db):
    """`grants.space_id` is a foreign key into `nodes` too, and nothing guarded it.

    Three guards existed for exactly this shape — a bare `IntegrityError` is a
    500 over HTTP and `database error: FOREIGN KEY constraint failed` on a CLI
    whose contract promises to name what the graph has grown. This one is
    reachable in two commands: create a space, grant an agent on it. The graph
    is never corrupted (the transaction rolls back whole); what was lost was the
    ability to read the answer, and the fix is a sentence naming the grant.
    """
    agent("reader-bot", grants={"meta": "read"})
    space = service.create_space("delegated", principal=owner())
    create_space_seq = _events()[0].seq
    service.grant("reader-bot", space.id, "read", principal=owner())

    with pytest.raises(UndoNotPossible, match="still carries 1 grant"):
        service.undo(create_space_seq, principal=owner())

    assert "reader-bot" in str(
        pytest.raises(UndoNotPossible, service.undo, create_space_seq, principal=owner()).value
    )
    assert service.get_node(space.id, principal=owner()).state == "active"
    assert [event for event in _events() if event.op == "undo"] == []

    # Revoking is the follow-through the message names, and it clears the way.
    service.revoke("reader-bot", space.id, principal=owner())
    service.undo(create_space_seq, principal=owner())
    with pytest.raises(NodeNotFound):
        service.get_node(space.id, principal=owner())


def test_undo_create_of_a_type_node_something_is_typed_by_is_refused(fresh_db):
    """`nodes.type_id` became a foreign key into `nodes` at 0009 — a type is a node.

    So a type node that has since been used to type anything is held down by
    every node wearing it, and the delete served the same bare `IntegrityError`
    the other guards exist to prevent.
    """
    widget = service.create_node(
        type="type", title="widget", space="meta", props={"type_kind": "node"}, principal=owner()
    )
    create_type_seq = _events()[0].seq
    typed = service.create_node(type="widget", title="a widget", principal=owner())

    with pytest.raises(UndoNotPossible, match="still types 1 node"):
        service.undo(create_type_seq, principal=owner())

    assert service.get_node(widget.id, principal=owner()).state == "active"
    assert service.get_node(typed.id, principal=owner()).type == widget.id
    assert [event for event in _events() if event.op == "undo"] == []

    # Taking the typed node back first clears the way.
    service.undo(principal=owner())
    service.undo(create_type_seq, principal=owner())
    with pytest.raises(NodeNotFound):
        service.get_node(widget.id, principal=owner())
