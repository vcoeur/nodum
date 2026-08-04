"""Cycle rollback (design §8.4, decisions C4 and C5).

D7 promises a rollback takes a cycle back *wholesale*, so what is tested here is
that promise and the two things it costs. It is **atomic** — a reversal that
cannot finish writes nothing at all — and it **refuses rather than clobbers**:
if anything outside the cycle has touched a row the cycle touched, the rollback
names the rows and the events instead of overwriting them.

The recursion is tested too, because it is what makes a rollback safe to
perform: a rollback carries its own ``cycle_id``, so ``undo`` refuses its events
like any other cycle's, and the way back from a rollback is to roll *it* back —
which re-applies the original.
"""

from __future__ import annotations

import inspect
import json
import re

import pytest
from helpers import agent, owner

from nodum import assets, auth, db, embeddings, projectors, service
from nodum.service import (
    InvalidTransition,
    NodeNotFound,
    RecordNotFound,
    RollbackConflict,
    UndoNotPossible,
)
from nodum.store import GrantNotPermitted

#: Tables that make up "the graph" for an identical-to-before comparison, with
#: the column each is read in order by. `versions` is deliberately absent: it is
#: history, and a rollback adds to history rather than rewriting it (see
#: `test_a_rollback_adds_to_a_nodes_history_rather_than_erasing_it`).
GRAPH_TABLES = {"nodes": "id", "edges": "id", "merge_redirects": "tombstone_id"}


def _events(cycle_id=None):
    """The event log oldest-first, optionally narrowed to one cycle."""
    return list(reversed(service.list_events(owner(), limit=1000, cycle_id=cycle_id)))


def _rows(table, order="id"):
    conn = db.connect()
    try:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
    finally:
        conn.close()


def _graph():
    """Every graph row, in a form two moments in time can be compared by."""
    return {table: _rows(table, order) for table, order in GRAPH_TABLES.items()}


def _journal():
    """What the journal says about each cycle's own writes, keyed by cycle id.

    `cycles` cannot be in `GRAPH_TABLES`: a rollback writes a journal entry of
    its own, so the table grows by construction and no two moments compare whole.
    `(status, rolled_back_by)` is the part that must come back identical — it is
    the journal's answer to *are this cycle's writes standing?*, and the graph
    rows are the truth it has to agree with. A reversal that restores the rows
    and leaves the journal saying the opposite is a divergence `_graph()` alone
    cannot see.
    """
    return {row["id"]: (row["status"], row["rolled_back_by"]) for row in _rows("cycles")}


def _journal_of(recorded):
    """`_journal()` narrowed to the cycles `recorded` already knew about."""
    return {cycle_id: verdict for cycle_id, verdict in _journal().items() if cycle_id in recorded}


def _edge(edge_id):
    """One edge row, read past every filter (an archived edge is still a row)."""
    conn = db.connect()
    try:
        return dict(conn.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone())
    finally:
        conn.close()


def _node(title, *, type="claim", **kwargs):
    return service.create_node(type=type, title=title, principal=owner(), **kwargs)


def _cycle_with(*, title="Made by the cycle"):
    """A closed one-node cycle, returned as ``(cycle_id, node)``."""
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        node = service.create_node(type="claim", title=title, principal=owner())
    service.close_cycle(cycle.id, status="completed", report={}, principal=owner())
    return cycle.id, node


def _seq_of(op, *, row_id=None):
    for event in _events():
        if event.op != op:
            continue
        if row_id is None:
            return event.seq
        for side in ("before", "after"):
            row = event.payload.get(side)
            if row is not None and row["id"] == row_id:
                return event.seq
    raise AssertionError(f"no {op} event for {row_id}")


# ── The whole cycle, or none of it ───────────────────────────────────────────


def test_a_whole_cycle_comes_back_out_and_the_graph_is_what_it_was(fresh_db):
    """D7's promise, stated over the rows rather than over the report.

    Four writes in one cycle — a create, an update *on top of that create*, an
    edge, and an update to a node that existed before the cycle — reversed
    newest first, which is the only order in which a create and the updates
    layered on top of it come apart.

    The cycle writing the same node twice is the case that says a cycle does not
    collide with *itself*: its second write to `made` is a later event touching a
    row it already touched, which is exactly the shape a conflict has, and the
    only thing telling the two apart is whose cycle the later event belongs to.
    """
    kept, edited = _node("Kept"), _node("Edited by the cycle")
    before = _graph()

    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        made = service.create_node(type="claim", title="Made", principal=owner())
        service.update_node(made.id, content="second thoughts", principal=owner())
        service.create_edge(made.id, kept.id, "supports", principal=owner())
        service.update_node(edited.id, title="Renamed", principal=owner())
    service.close_cycle(cycle.id, status="completed", report={}, principal=owner())
    assert _graph() != before

    result = service.rollback_cycle(cycle.id, principal=owner())

    assert _graph() == before
    assert result.deleted_nodes == [made.id]
    # Newest first: the last write in the cycle is the first one taken back.
    assert result.restored_nodes == [edited.id, made.id]
    assert len(result.reversed_events) == 4
    with pytest.raises(NodeNotFound):
        service.get_node(made.id, principal=owner())


def test_a_rollback_that_cannot_finish_writes_nothing_at_all(fresh_db):
    """Atomic means atomic: the reversal that already succeeded goes back too.

    A node the cycle created has since been given a child, which the create's
    reversal refuses to cascade into. The update layered on top of that create
    is reversed *first* (newest first), so a rollback that were merely
    best-effort would leave the node holding its pre-cycle title while the cycle
    that set it is still recorded as completed.
    """
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        page = service.create_node(type="page", title="P", principal=owner())
        service.update_node(page.id, title="P2", principal=owner())
    service.close_cycle(cycle.id, status="completed", report={}, principal=owner())
    service.create_node(type="block", content="b", parent_id=page.id, principal=owner())
    before, events_before = _graph(), len(_events())

    with pytest.raises(UndoNotPossible, match="child node"):
        service.rollback_cycle(cycle.id, principal=owner())

    assert _graph() == before
    assert service.get_node(page.id, principal=owner()).title == "P2"
    assert len(_events()) == events_before, "a failed rollback left events behind"
    assert service.get_cycle(cycle.id, principal=owner()).status == "completed"
    # The attempt is in the journal, failed — a cycle that vanished on failure
    # is a cycle nobody could ask about.
    rollbacks = [
        entry for entry in service.list_cycles(principal=owner()) if entry.trigger == "rollback"
    ]
    assert [entry.status for entry in rollbacks] == ["failed"]


# ── The curative tier, taken back ─────────────────────────────────────────────


def test_a_merge_comes_back_whole(fresh_db):
    """Tombstone active again, edges repointed back, and the redirect row gone.

    The redirect is the part no event covers: it is derivable from the
    `node.merge` payload, but rollback has to delete it explicitly or the
    foreign key it holds into `nodes` outlives the merge that made it.
    """
    survivor, duplicate, other = _node("Alpha"), _node("Alpha (dup)"), _node("Gamma")
    incoming = service.create_edge(other.id, duplicate.id, "supports", principal=owner())
    before = _graph()

    merge = service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())
    assert _edge(incoming.id)["dst_id"] == survivor.id

    result = service.rollback_cycle(merge.cycle_id, principal=owner())

    tombstone = service.get_node(duplicate.id, principal=owner())
    assert tombstone.state == "active"
    assert "merged_into" not in tombstone.props
    assert _edge(incoming.id)["dst_id"] == duplicate.id
    assert _rows("merge_redirects", "tombstone_id") == []
    assert result.redirects_removed == [duplicate.id]
    assert _graph() == before


def test_removing_the_redirect_lets_the_tombstones_create_be_undone_again(fresh_db):
    """The point of deleting it: without this, the create is un-undoable for good.

    `undo` refuses to delete a node a `merge_redirects` row names, and the merge
    itself is cycle-stamped and so not undoable either — so if the rollback left
    the redirect standing, the node's own creation would have no route back
    through any mechanism at all.
    """
    survivor, duplicate = _node("Alpha"), _node("Alpha (dup)")
    merge = service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())
    create_seq = _seq_of("node.create", row_id=duplicate.id)
    with pytest.raises(UndoNotPossible, match="merge redirect"):
        service.undo(create_seq, principal=owner())

    service.rollback_cycle(merge.cycle_id, principal=owner())
    service.undo(create_seq, principal=owner())

    with pytest.raises(NodeNotFound):
        service.get_node(duplicate.id, principal=owner())


def test_an_annotation_never_blocks_undo(fresh_db):
    """An annotation is derived judgement, so it can never refuse a node's undo.

    Migration 0016's `annotations.target_node_id` is the one foreign key into
    `nodes(id)` deliberately absent from `_delete_blocker` — it cascades — so
    an undone create takes its annotation with it instead of standing in the
    way, which is what the annotation's `cycle_id` already implies: the cycle
    that wrote the night's annotations rolls back with them.
    """
    node = _node("Annotated")
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO annotations (id, target_node_id, target_edge_id,"
            " target_version_id, body, actor)"
            " VALUES ('a1', ?, NULL, NULL, '{\"rate\": 0.9}',"
            " 'agent:builtin-gardener')",
            (node.id,),
        )
        conn.commit()
    finally:
        conn.close()

    service.undo(_seq_of("node.create", row_id=node.id), principal=owner())

    conn = db.connect()
    try:
        remaining = conn.execute("SELECT 1 FROM annotations WHERE id = 'a1'").fetchone()
    finally:
        conn.close()
    assert remaining is None
    with pytest.raises(NodeNotFound):
        service.get_node(node.id, principal=owner())


def test_an_annotation_on_an_edge_never_blocks_undo(fresh_db):
    """The edge leg of the annotation cascade: an undone edge.create takes its
    annotation with it, exactly as an undone node create does.

    `annotations.target_edge_id` is the same derived-judgement cascade as
    `target_node_id`, so the edge's create is undoable with the annotation
    standing — the row goes with the edge, and the undo is never refused.
    """
    src, dst = _node("A"), _node("B")
    edge = service.create_edge(src.id, dst.id, "supports", principal=owner())
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO annotations (id, target_node_id, target_edge_id,"
            " target_version_id, body, actor)"
            " VALUES ('a2', NULL, ?, NULL, '{\"rate\": 0.7}',"
            " 'agent:builtin-gardener')",
            (edge.id,),
        )
        conn.commit()
    finally:
        conn.close()

    service.undo(_seq_of("edge.create", row_id=edge.id), principal=owner())

    conn = db.connect()
    try:
        remaining = conn.execute("SELECT 1 FROM annotations WHERE id = 'a2'").fetchone()
    finally:
        conn.close()
    assert remaining is None
    assert edge.id not in {row["id"] for row in _rows("edges")}


def test_a_retype_comes_back(fresh_db):
    node = _node("Alpha")
    before = _graph()
    result = service.retype([node.id], "concept", principal=owner())
    assert service.get_node(node.id, principal=owner()).type == "concept"

    service.rollback_cycle(result.cycle_id, principal=owner())

    assert service.get_node(node.id, principal=owner()).type == "claim"
    assert _graph() == before


def test_a_supersede_comes_back_including_the_valid_to_it_closed(fresh_db):
    """Two facts were recorded, so two have to come back out.

    `valid_to` (when the edge stopped being true) and `archived` (it left the
    live graph) are different facts, and a reversal that restored only the state
    would leave an active edge claiming it stopped being true.
    """
    first, second = _node("A"), _node("B")
    edge = service.create_edge(first.id, second.id, "supports", principal=owner())
    before = _graph()

    result = service.supersede_edge(edge.id, replacement={"confidence": 0.4}, principal=owner())
    assert _edge(edge.id)["valid_to"] is not None
    assert result.replacement is not None

    service.rollback_cycle(result.cycle_id, principal=owner())

    assert _edge(edge.id)["state"] == "active"
    assert _edge(edge.id)["valid_to"] is None
    assert _graph() == before, "the replacement edge outlived the cycle that created it"


# ── Refusing rather than clobbering (decision C4) ─────────────────────────────


def test_a_row_changed_since_refuses_the_rollback_and_names_what_is_in_the_way(fresh_db):
    node = _node("Alpha")
    result = service.retype([node.id], "concept", principal=owner())
    service.update_node(node.id, title="Edited after the cycle", principal=owner())
    before = _graph()

    with pytest.raises(RollbackConflict) as refusal:
        service.rollback_cycle(result.cycle_id, principal=owner())

    (conflict,) = refusal.value.conflicts
    assert (conflict.kind, conflict.row_id) == ("node", node.id)
    assert conflict.cycle_event_op == "node.retype"
    assert conflict.conflicting_op == "node.update"
    assert conflict.conflicting_actor == "human:owner"
    assert conflict.conflicting_cycle_id is None
    # Renderable from the message alone, too: a human told "rollback failed"
    # cannot act, and one told which row and which event can.
    assert node.id in str(refusal.value)
    assert str(conflict.conflicting_seq) in str(refusal.value)
    # And nothing at all was written — not even a journal entry.
    assert _graph() == before
    assert [entry.trigger for entry in service.list_cycles(principal=owner())] == ["curative"]


def test_another_cycles_later_write_is_a_conflict_too(fresh_db):
    """Outside this cycle is outside this cycle, gardener or not."""
    node = _node("Alpha")
    first = service.retype([node.id], "concept", principal=owner())
    second = service.retype([node.id], "person", principal=owner())

    with pytest.raises(RollbackConflict) as refusal:
        service.rollback_cycle(first.cycle_id, principal=owner())

    (conflict,) = refusal.value.conflicts
    assert conflict.conflicting_cycle_id == second.cycle_id
    # The newer cycle rolls back fine — nothing came after it.
    service.rollback_cycle(second.cycle_id, principal=owner())
    assert service.get_node(node.id, principal=owner()).type == "concept"


def test_an_undo_reaching_past_the_cycle_is_a_conflict(fresh_db):
    """The case only reading `undo` payloads catches.

    Undoing the *create* of an edge a merge later relinked deletes that edge —
    an unstamped event moving a row the cycle owns. The undo's own op is `undo`,
    so conflict detection that only read `node.`/`edge.` events would call this
    graph untouched and then reverse a merge onto a row that is gone.
    """
    survivor, duplicate, other = _node("Alpha"), _node("Alpha (dup)"), _node("Gamma")
    edge = service.create_edge(other.id, duplicate.id, "supports", principal=owner())
    merge = service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())
    service.undo(_seq_of("edge.create", row_id=edge.id), principal=owner())

    with pytest.raises(RollbackConflict) as refusal:
        service.rollback_cycle(merge.cycle_id, principal=owner())

    conflicts = {(conflict.kind, conflict.row_id) for conflict in refusal.value.conflicts}
    assert ("edge", edge.id) in conflicts
    assert all(conflict.conflicting_op == "undo" for conflict in refusal.value.conflicts)


def test_a_later_change_that_has_itself_been_undone_is_not_a_conflict(fresh_db):
    """Skipped, not refused: the end state rollback wants is the one the row has.

    The edit after the cycle has been taken back, so the row already stands
    exactly as the cycle left it. The `undo` that took it back touches the same
    row and is *not* a conflict either, because what it reversed came after the
    cycle — it moved the row towards the cycle's end state, not away from it.
    """
    node = _node("Alpha")
    result = service.retype([node.id], "concept", principal=owner())
    service.update_node(node.id, title="Edited after the cycle", principal=owner())
    service.undo(principal=owner())
    assert service.get_node(node.id, principal=owner()).title == "Alpha"

    service.rollback_cycle(result.cycle_id, principal=owner())

    assert service.get_node(node.id, principal=owner()).type == "claim"


def test_a_reversal_that_was_itself_reversed_stops_counting_as_one(fresh_db):
    """ "Reversed" is a fixpoint, and treating it as a flat set let a rollback clobber.

    C2's write was rolled back and then rolled back again, so it is **live**:
    the node reads `source`, and C2's journal entry says `completed`. The old
    code added every seq anything named to one set, so C2's own event counted as
    "already reversed" and C1's rollback sailed straight over it — reporting no
    conflicts on the dry run and then silently destroying a live write.
    """
    node = _node("Alpha")
    first = service.retype([node.id], "note", principal=owner())
    second = service.retype([node.id], "source", principal=owner())

    reversal = service.rollback_cycle(second.cycle_id, principal=owner())
    assert service.get_node(node.id, principal=owner()).type == "note"
    service.rollback_cycle(reversal.rollback_cycle_id, principal=owner())
    assert service.get_node(node.id, principal=owner()).type == "source"
    assert service.get_cycle(second.cycle_id, principal=owner()).status == "completed"

    plan = service.rollback_cycle(first.cycle_id, dry_run=True, principal=owner())
    assert [conflict.row_id for conflict in plan.conflicts] == [node.id]
    assert [conflict.conflicting_cycle_id for conflict in plan.conflicts] == [second.cycle_id]
    with pytest.raises(RollbackConflict):
        service.rollback_cycle(first.cycle_id, principal=owner())
    assert service.get_node(node.id, principal=owner()).type == "source"


def test_a_reversal_that_is_still_standing_still_clears_the_way(fresh_db):
    """The other half of the fixpoint: one reversal deep is still reversed.

    Rolled back *once*, C2's write is genuinely gone, so C1's rollback proceeds
    — the fix must not turn every reversal into a permanent conflict.
    """
    node = _node("Alpha")
    first = service.retype([node.id], "note", principal=owner())
    second = service.retype([node.id], "source", principal=owner())
    service.rollback_cycle(second.cycle_id, principal=owner())

    service.rollback_cycle(first.cycle_id, principal=owner())

    assert service.get_node(node.id, principal=owner()).type == "claim"


def test_a_write_to_another_row_is_not_a_conflict(fresh_db):
    """Neither over- nor under-sensitive: the graph moving on elsewhere is fine."""
    node, bystander = _node("Alpha"), _node("Bystander")
    result = service.retype([node.id], "concept", principal=owner())
    service.update_node(bystander.id, title="Busy elsewhere", principal=owner())

    service.rollback_cycle(result.cycle_id, principal=owner())

    assert service.get_node(node.id, principal=owner()).type == "claim"
    assert service.get_node(bystander.id, principal=owner()).title == "Busy elsewhere"


# ── The rollback is a cycle, and is itself rollable back (decision C5) ────────


def test_the_rollback_is_a_cycle_of_its_own_and_stamps_every_event_it_writes(fresh_db):
    space = service.create_space("research", principal=owner())
    cycle = service.open_cycle(trigger="manual", scope=space.id, principal=owner())
    with service.in_cycle(cycle.id):
        service.create_node(type="claim", title="X", space=space.id, principal=owner())
    service.close_cycle(cycle.id, status="completed", report={}, principal=owner())

    result = service.rollback_cycle(cycle.id, principal=owner())

    rollback = service.get_cycle(result.rollback_cycle_id, principal=owner())
    assert rollback.trigger == "rollback"
    assert rollback.triggered_by == "human:owner"
    assert rollback.status == "completed"
    assert rollback.scope == space.id
    assert rollback.report["rolled_back"] == cycle.id
    assert rollback.report["previous_status"] == "completed"
    assert service.get_cycle(cycle.id, principal=owner()).rolled_back_by == rollback.id
    assert service.get_cycle(cycle.id, principal=owner()).status == "rolled_back"
    emitted = _events(cycle_id=rollback.id)
    assert emitted, "the rollback emitted nothing"
    assert {event.cycle_id for event in emitted} == {rollback.id}
    assert {event.op for event in emitted} == {"node.rollback", "cycle.rollback"}


def test_undo_will_not_touch_a_rollbacks_own_events(fresh_db):
    """No new guard: a rollback's events are cycle-stamped like any other's."""
    node = _node("Alpha")
    result = service.retype([node.id], "concept", principal=owner())
    rollback = service.rollback_cycle(result.cycle_id, principal=owner())
    reversal = next(event for event in _events() if event.op == "node.rollback")
    summary = next(event for event in _events() if event.op == "cycle.rollback")

    assert reversal.cycle_id == rollback.rollback_cycle_id
    with pytest.raises(UndoNotPossible, match="Roll the cycle back instead"):
        service.undo(reversal.seq, principal=owner())
    # And the summary is an audit record, refused for the other reason.
    with pytest.raises(ValueError, match="not a graph event"):
        service.undo(summary.seq, principal=owner())
    assert service.get_node(node.id, principal=owner()).type == "claim"


def test_rolling_a_rollback_back_re_applies_the_original(fresh_db):
    """The recursion, closed honestly: the way back from a rollback is a rollback."""
    survivor, duplicate, other = _node("Alpha"), _node("Alpha (dup)"), _node("Gamma")
    service.create_edge(other.id, duplicate.id, "supports", principal=owner())
    merge = service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())
    merged = _graph()

    rollback = service.rollback_cycle(merge.cycle_id, principal=owner())
    assert _graph() != merged

    service.rollback_cycle(rollback.rollback_cycle_id, principal=owner())

    assert _graph() == merged, "re-applying the merge did not reproduce it exactly"
    original = service.get_cycle(merge.cycle_id, principal=owner())
    # The journal stops claiming the merge is taken back, because it is not.
    assert original.status == "completed"
    assert original.rolled_back_by is None
    assert service.get_cycle(rollback.rollback_cycle_id, principal=owner()).status == "rolled_back"


def test_rolling_a_rollback_back_puts_a_deleted_node_back_with_its_versions(fresh_db):
    """A node deleted by a rollback comes back with the rows the delete took.

    Its `versions` had to go — they hold a foreign key into the row — so the
    reversal recorded them, and this is what makes that recording load-bearing
    rather than decorative.
    """
    cycle_id, made = _cycle_with()
    before_versions = service.history(made.id, principal=owner())
    assert before_versions

    rollback = service.rollback_cycle(cycle_id, principal=owner())
    with pytest.raises(NodeNotFound):
        service.get_node(made.id, principal=owner())

    service.rollback_cycle(rollback.rollback_cycle_id, principal=owner())

    assert service.get_node(made.id, principal=owner()).title == "Made by the cycle"
    restored = service.history(made.id, principal=owner())
    assert [version.id for version in restored][: len(before_versions)] == [
        version.id for version in before_versions
    ]


def test_the_involution_holds_past_the_second_rollback(fresh_db):
    """Roll a merge back and forward five times; every state must be bit-identical.

    Depths 1 and 2 were correct and depth 3 was not, because the redirect
    removal was keyed on the **op name** `node.merge` (and on the merge's own
    `event_seq`). A rollback that *re-applies* a merge emits `node.rollback`
    carrying the same before/after pair, so reversing that restored the node and
    left the `merge_redirects` row behind — a divergence invisible in `nodes`
    and `edges`, which is exactly why `GRAPH_TABLES` includes the third table.

    Two rollbacks were never enough to catch it: the involution has to be
    exercised past the point where a rollback is reversing another rollback's
    reversal.

    The **journal** is compared beside the rows for the same reason the third
    table is compared beside the first two. Clearing the `rolled_back` mark ran
    exactly one hop up the chain, which is right at depth 2 and wrong from depth
    3: reversing a rollback puts the rollback *it* reversed back into force, so
    the cycle that one took back is taken back again. Leaving that mark off says
    a cycle's writes are standing while they are not — the mirror of the
    invariant `_apply_rollback` documents, and invisible in `nodes`, `edges` and
    `merge_redirects` alike.
    """
    survivor, duplicate, other = _node("Alpha"), _node("Alpha (dup)"), _node("Gamma")
    service.create_edge(other.id, duplicate.id, "supports", principal=owner())
    merge = service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())
    merged, merged_journal = _graph(), _journal()

    first = service.rollback_cycle(merge.cycle_id, principal=owner())
    unmerged, unmerged_journal = _graph(), _journal()
    assert unmerged != merged
    assert unmerged_journal != merged_journal

    next_cycle = first.rollback_cycle_id
    for depth in range(2, 6):
        result = service.rollback_cycle(next_cycle, principal=owner())
        next_cycle = result.rollback_cycle_id
        taken_back = depth % 2 == 1
        expected = unmerged if taken_back else merged
        expected_journal = unmerged_journal if taken_back else merged_journal
        assert _graph() == expected, f"rollback #{depth} diverged from the state it must reproduce"
        assert _journal_of(expected_journal) == expected_journal, (
            f"rollback #{depth} left the journal disagreeing with the rows it restored"
        )


def test_a_cycle_taken_back_from_depth_three_still_refuses_a_second_rollback(fresh_db):
    """What the missing mark costs a human, rather than what it looks like in SQL.

    `rollback_cycle` refuses a cycle that is already `rolled_back` — that guard
    is the only thing standing between one cycle's writes and being reversed
    twice. It reads `cycles.status`, so a mark cleared one hop too far does not
    merely mislead the dream journal: it hands the guard a `completed` row and
    the refusal never happens.

    At depth 3 the retype has been taken back (the node is a `claim` again) by
    the *first* rollback, which reversing the second put back into force. So the
    journal has to name that rollback, and asking to roll the retype back again
    has to be refused by name.
    """
    node = _node("Alpha")
    retype = service.retype([node.id], "concept", principal=owner())
    first = service.rollback_cycle(retype.cycle_id, principal=owner())

    cycle = first.rollback_cycle_id
    for _ in range(2):
        cycle = service.rollback_cycle(cycle, principal=owner()).rollback_cycle_id

    # Depth 3: the retype is reversed again, so the journal must say so.
    assert service.get_node(node.id, principal=owner()).type == "claim"
    taken_back = service.get_cycle(retype.cycle_id, principal=owner())
    assert taken_back.status == "rolled_back"
    assert taken_back.rolled_back_by == first.rollback_cycle_id
    with pytest.raises(InvalidTransition, match="already been rolled back"):
        service.rollback_cycle(retype.cycle_id, principal=owner())


def test_a_thrice_rolled_back_merge_leaves_the_node_mergeable_again(fresh_db):
    """The consequence, stated as the two things a human actually hits.

    A stranded `merge_redirects` row makes the tombstone's *creating* cycle
    permanently unrollbackable — the guard names the redirect and tells you to
    roll back the cycle that merged it, which reads `rolled_back` and refuses —
    and merging the node again dies on the primary key with a bare
    `sqlite3.IntegrityError`: a 500 over HTTP, and precisely the shape the three
    guards in `_delete_created_row` exist to prevent.
    """
    survivor, duplicate = _node("Alpha"), _node("Alpha (dup)")
    merge = service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())

    cycle = merge.cycle_id
    for _ in range(3):
        cycle = service.rollback_cycle(cycle, principal=owner()).rollback_cycle_id

    # Three rollbacks from a merge is an un-merged graph, so no redirect stands.
    assert _rows("merge_redirects", "tombstone_id") == []
    assert service.get_node(duplicate.id, principal=owner()).state == "active"
    # Which means the node can be merged again — and its create can be reversed.
    again = service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())
    assert [row.tombstone_id for row in again.redirects] == [duplicate.id]
    service.rollback_cycle(again.cycle_id, principal=owner())
    service.undo(_seq_of("node.create", row_id=duplicate.id), principal=owner())
    with pytest.raises(NodeNotFound):
        service.get_node(duplicate.id, principal=owner())


def test_a_cycle_is_rolled_back_once(fresh_db):
    node = _node("Alpha")
    result = service.retype([node.id], "concept", principal=owner())
    service.rollback_cycle(result.cycle_id, principal=owner())

    with pytest.raises(InvalidTransition, match="already been rolled back"):
        service.rollback_cycle(result.cycle_id, principal=owner())


class _Killed(BaseException):
    """Stands in for a `SIGKILL`: not an `Exception`, so no handler tidies up."""


def _strand_the_rollback(cycle_id, monkeypatch):
    """Roll ``cycle_id`` back with the process dying before the cycle closes.

    Exactly the state a `SIGKILL` between `_apply_rollback`'s commit and
    `close_cycle` leaves: the reversal is on disk and the rollback's own journal
    entry is still `running`. Returns the stranded rollback's cycle id.
    """
    real_open, real_close = service.open_cycle, service.close_cycle
    opened: list[str] = []

    def remember(**kwargs):
        cycle = real_open(**kwargs)
        opened.append(cycle.id)
        return cycle

    def die(*args, **kwargs):
        raise _Killed("the process died before the rollback cycle closed")

    monkeypatch.setattr(service, "open_cycle", remember)
    monkeypatch.setattr(service, "close_cycle", die)
    try:
        with pytest.raises(_Killed):
            service.rollback_cycle(cycle_id, principal=owner())
    finally:
        monkeypatch.setattr(service, "open_cycle", real_open)
        monkeypatch.setattr(service, "close_cycle", real_close)
    stranded = opened[-1]
    assert service.get_cycle(stranded, principal=owner()).status == "running", (
        "the fixture did not reach the state it exists to build"
    )
    return stranded


def test_a_rollback_a_human_abandoned_is_still_found_by_the_chain(fresh_db, monkeypatch):
    """An abandoned rollback replaces its report, and the chain has to survive that.

    The walk follows each rollback's recorded target, and the record it read was
    the **report** — written by the `close_cycle` at the end of `rollback_cycle`.
    A rollback a crash stranded never reaches that line, and `abandon_cycle`, the
    door this branch advertises for exactly that state, closes it with a report
    of its own: `{abandoned, abandoned_by, detail}`, naming no cycle. So the
    report was a dead end, and reversing such a rollback left the cycle *below*
    it marked `rolled_back` by a cycle that had itself been taken back.

    What that costs the human is the point. The retype's writes are standing
    again, so `rollback` on the retype refuses by name ("roll *that* cycle
    back") — and the cycle it names is `rolled_back` too, so the advice is
    refused as well. Both routes closed on a cycle whose writes are live.

    The `cycle.rollback` summary event is the record that holds: it is emitted
    inside the transaction that applies the reversal, so it exists whenever the
    reversal does, and nothing rewrites an event.
    """
    node = _node("Alpha")
    retype = service.retype([node.id], "concept", principal=owner())
    assert service.get_node(node.id, principal=owner()).type == "concept"

    stranded = _strand_the_rollback(retype.cycle_id, monkeypatch)
    assert service.get_node(node.id, principal=owner()).type == "claim"
    assert service.get_cycle(retype.cycle_id, principal=owner()).rolled_back_by == stranded

    service.abandon_cycle(stranded, principal=owner())
    # The abandon really did replace the report, so the walk's first record is
    # gone rather than merely assumed to be.
    assert "rolled_back" not in (service.get_cycle(stranded, principal=owner()).report or {})

    service.rollback_cycle(stranded, principal=owner())

    # The retype is back in force, so the journal must say its writes stand.
    assert service.get_node(node.id, principal=owner()).type == "concept"
    reapplied = service.get_cycle(retype.cycle_id, principal=owner())
    assert reapplied.status == "completed"
    assert reapplied.rolled_back_by is None
    # And the human's route back is open rather than closed behind two refusals.
    service.rollback_cycle(retype.cycle_id, principal=owner())
    assert service.get_node(node.id, principal=owner()).type == "claim"


def test_a_failed_cycle_put_back_into_force_is_failed_again(fresh_db):
    """The restated status is the one that was recorded, not a constant.

    A cycle the runner closed `failed` still wrote rows, and rolling it back and
    then rolling *that* back puts those rows — and the entry describing them —
    back exactly as they were. Restating it as `completed` would have the journal
    claim a run succeeded because it was reversed twice.
    """
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        node = service.create_node(type="claim", title="Half-written", principal=owner())
    service.close_cycle(cycle.id, status="failed", report={"error": "boom"}, principal=owner())
    assert service.get_cycle(cycle.id, principal=owner()).status == "failed"

    first = service.rollback_cycle(cycle.id, principal=owner())
    service.rollback_cycle(first.rollback_cycle_id, principal=owner())

    assert service.get_node(node.id, principal=owner()).title == "Half-written"
    restated = service.get_cycle(cycle.id, principal=owner())
    assert restated.status == "failed"
    assert restated.rolled_back_by is None


def test_a_chain_that_loops_back_on_itself_stops_instead_of_spinning(fresh_db, monkeypatch):
    """The `seen` guard, over the malformed data it exists for.

    The walk follows a record — a report, or the reversal's own summary event —
    and a record is data, not schema. Correct writes cannot make a ring (a
    rollback always comes after what it reverses), so the loop below is forged:
    the *reversed* cycle is rewritten to claim it is a rollback of the very
    cycle that reversed it. Without the guard the walk alternates between the
    two rows forever, holding the write transaction open — a hang, not an error.

    The watchdog is a counter on the step function rather than a clock, so the
    failure is deterministic and the suite cannot hang waiting for it. And the
    steps record what each one *returned*, because "the walk stopped after two"
    is also what a forgery that did not take looks like: if the second step
    found no target the walk would end there for the ordinary reason and this
    test would pass while covering nothing.
    """
    cycle_id, node = _cycle_with()
    first = service.rollback_cycle(cycle_id, principal=owner())
    forged = json.dumps(
        {
            "op": "rollback_cycle",
            "rolled_back": first.rollback_cycle_id,
            "previous_status": "completed",
        }
    )
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE cycles SET trigger = 'rollback', report = ? WHERE id = ?", (forged, cycle_id)
        )
        conn.commit()
    finally:
        conn.close()

    real_target = service._rollback_target
    steps: list[tuple[str, bool]] = []

    def counted(conn, cycle):
        if len(steps) >= 8:
            raise AssertionError(f"the chain walk did not terminate: {steps}")
        found = real_target(conn, cycle)
        steps.append((cycle["id"], found is not None))
        return found

    monkeypatch.setattr(service, "_rollback_target", counted)
    service.rollback_cycle(first.rollback_cycle_id, principal=owner())

    # Two steps, each cycle read once, and the *second* one found a target — so
    # the walk stopped because the guard saw a repeat, not because the ring was
    # never built. And the reversal it was part of still landed.
    assert steps == [(first.rollback_cycle_id, True), (cycle_id, True)], (
        f"the walk did not stop at the ring it was given: {steps}"
    )
    assert service.get_node(node.id, principal=owner()).title == "Made by the cycle"


def test_a_failed_cycle_is_failed_again_through_an_abandoned_rollback(fresh_db, monkeypatch):
    """The recorded status has to survive the crash path too, not only the report.

    This is why the fallback reads the `cycle.rollback` summary event rather than
    `cycles.rolled_back_by`: the mark says *which* cycle a rollback reversed and
    nothing about the status it held, so a walk threaded through it would have to
    guess — and the guess is `completed`, which is wrong here.
    """
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        node = service.create_node(type="claim", title="Half-written", principal=owner())
    service.close_cycle(cycle.id, status="failed", report={"error": "boom"}, principal=owner())
    assert service.get_cycle(cycle.id, principal=owner()).status == "failed"

    stranded = _strand_the_rollback(cycle.id, monkeypatch)
    service.abandon_cycle(stranded, principal=owner())
    service.rollback_cycle(stranded, principal=owner())

    assert service.get_node(node.id, principal=owner()).title == "Half-written"
    restated = service.get_cycle(cycle.id, principal=owner())
    assert restated.status == "failed"
    assert restated.rolled_back_by is None


# ── History is added to, never rewritten ─────────────────────────────────────


def test_a_rollback_adds_to_a_nodes_history_rather_than_erasing_it(fresh_db):
    """The `versions` decision, stated where it is visible.

    A version row names the event that produced it, and this file never deletes
    an event — the cycle's own events stay in the log after it is rolled back.
    So the snapshots pointing at them stay too: a rollback records the reversal
    rather than erasing the record of what it reversed. The one exception is
    forced by a foreign key (deleting a node takes its versions), and that one
    is recorded so it can be put back.
    """
    node = _node("Alpha")
    result = service.retype([node.id], "concept", principal=owner())
    after_retype = service.history(node.id, principal=owner())

    service.rollback_cycle(result.cycle_id, principal=owner())

    history = service.history(node.id, principal=owner())
    assert [version.id for version in history][: len(after_retype)] == [
        version.id for version in after_retype
    ]
    assert len(history) == len(after_retype) + 1
    assert history[-1].event_seq == _seq_of("node.rollback", row_id=node.id)


# ── A version review comes back too (decisions V1/V2) ────────────────────────
#
# A review moves two rows from one decision and only one of them is a graph
# record, which is how both halves ended up outside the reversal by two
# different mechanisms: an accept's flip of `versions.state` to `applied` rode
# on no event at all, and a reject's `version.reject` — which does carry the
# rows — was skipped as an audit record. The tests below compare **every table
# in the file**, because the shape being tested is a row nobody thought to look
# at.

#: Tables a rollback appends to by construction, so no two moments of them
#: compare whole: the append-only log, the journal (a rollback is a cycle of its
#: own), and `versions` — history, which a reversal adds a snapshot to rather
#: than rewriting (see the section above).
_APPENDED_TO = {"events", "cycles", "versions"}


def _every_table():
    """Every row of every ordinary table, enumerated from `sqlite_master`.

    Not from a list of table names. `GRAPH_TABLES` names three, and a version
    row is exactly the kind of thing a named list leaves out — which is how the
    hole these tests close survived a suite that already compared "the graph"
    before and after a rollback. Derived indexes (`node_fts*`, `node_vec*`) are
    out because they are a projector's business and are rebuilt from the log,
    and `sqlite_*` is SQLite's own bookkeeping.
    """
    conn = db.connect()
    try:
        names = [
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not row["name"].startswith(("node_fts", "node_vec", "sqlite_"))
        ]
        return {
            name: sorted(
                (dict(row) for row in conn.execute(f"SELECT * FROM {name}")),
                key=lambda row: json.dumps(row, sort_keys=True, default=str),
            )
            for name in sorted(names)
        }
    finally:
        conn.close()


def _assert_every_row_came_back(before, after):
    """Every table identical, and every history row that existed still exact.

    The two guards are the point of the helper as much as the comparison is: a
    "compares every table" that enumerated nothing, or a history loop over an
    empty list, would pass on any graph at all. Both are the shape this project
    has shipped four times — an assertion about a universal it does not check.
    """
    assert {"nodes", "edges", "versions", "merge_redirects", "grants", "agents"} <= set(before), (
        "the comparison is not reading the tables it claims to"
    )
    assert before["versions"], "no version rows to check: the fixture staged nothing"
    assert set(before) == set(after), "a table appeared or vanished"
    for table in sorted(before):
        if table not in _APPENDED_TO:
            assert after[table] == before[table], f"{table} did not come back"
    for row in before["versions"]:
        assert row in after["versions"], f"versions row {row['id']} did not come back: {row}"


def _proposal(node_id, *, content="a second thought"):
    """An agent's proposed update to a node, as the pending `versions` row."""
    service.update_node(node_id, content=content, principal=agent("proposer"))
    return next(
        version
        for version in service.history(node_id, principal=owner())
        if version.state == "proposed"
    )


def _version_state(version_id):
    """One version row's state, read past every filter."""
    conn = db.connect()
    try:
        row = conn.execute("SELECT state FROM versions WHERE id = ?", (version_id,)).fetchone()
        return None if row is None else row["state"]
    finally:
        conn.close()


def _reviewed_in_a_cycle(version_id, action):
    """Review one proposal inside a closed cycle, as the gardener; return the id."""
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        service.transition(str(version_id), action, principal=auth.internal_principal())
    service.close_cycle(cycle.id, status="completed", report={}, principal=owner())
    return cycle.id


def test_rolling_back_an_accepted_proposal_puts_the_proposal_back(fresh_db):
    """The half note 03 recorded: state changed outside the log, again.

    Accepting emits an ordinary `node.update`, which reverses correctly — and
    flips `versions.state` to `applied` through no event of its own, so the
    version's own state used to survive the rollback of the node change it
    caused. That is not a cosmetic leftover: a version leaves `proposed` exactly
    once, so a proposal left `applied` over a node whose content came back can
    never be accepted or rejected again.
    """
    node = _node("Alpha", content="first")
    version = _proposal(node.id)
    before = _every_table()

    cycle_id = _reviewed_in_a_cycle(version.id, "accept")
    assert service.get_node(node.id, principal=owner()).content == "a second thought"
    assert _version_state(version.id) == "applied"

    result = service.rollback_cycle(cycle_id, principal=owner())

    assert service.get_node(node.id, principal=owner()).content == "first"
    assert _version_state(version.id) == "proposed"
    assert result.restored_versions == [version.id]
    _assert_every_row_came_back(before, _every_table())
    # And the proposal is a proposal again rather than a row wearing the word:
    # it is back in the queue, and accepting it a second time works.
    queued = service.list_proposals(kind="update", principal=owner())
    assert [item.version.id for item in queued] == [version.id]
    service.transition(str(version.id), "accept", principal=owner())
    assert service.get_node(node.id, principal=owner()).content == "a second thought"


def test_rolling_back_a_rejected_proposal_puts_the_proposal_back(fresh_db):
    """The half note 03 missed: an event nothing was reading.

    `version.reject` carries the version rows as `before`/`after` — a proper
    event — but `version.` was in neither reversal verb's set, so the rollback
    plan filed it with `asset.download` as an audit record with no row behind
    it. A cycle that did nothing but reject was therefore a cycle that "wrote no
    graph events", and the refusal said so.
    """
    node = _node("Alpha", content="first")
    version = _proposal(node.id)
    before = _every_table()

    cycle_id = _reviewed_in_a_cycle(version.id, "reject")
    assert _version_state(version.id) == "archived"

    result = service.rollback_cycle(cycle_id, principal=owner())

    assert _version_state(version.id) == "proposed"
    assert result.restored_versions == [version.id]
    assert result.skipped_events == [], "the reject is still being read as an audit record"
    _assert_every_row_came_back(before, _every_table())
    queued = service.list_proposals(kind="update", principal=owner())
    assert [item.version.id for item in queued] == [version.id]


def test_a_version_review_is_re_applied_by_rolling_its_rollback_back(fresh_db):
    """The involution, at the depth this file has already been wrong at twice.

    The version's move is recorded on the reversal too, mirrored, so each hop
    flips it. Clearing it one way only would be right at depth 1 and wrong from
    depth 2 — which is exactly the shape of the `merge_redirects` and
    `rolled_back` bugs this suite already carries tests for.
    """
    node = _node("Alpha", content="first")
    version = _proposal(node.id)
    accepted_cycle = _reviewed_in_a_cycle(version.id, "accept")
    accepted = _every_table()

    first = service.rollback_cycle(accepted_cycle, principal=owner())
    assert _version_state(version.id) == "proposed"

    second = service.rollback_cycle(first.rollback_cycle_id, principal=owner())
    assert _version_state(version.id) == "applied", "the accept was not re-applied"
    assert service.get_node(node.id, principal=owner()).content == "a second thought"
    _assert_every_row_came_back(accepted, _every_table())

    third = service.rollback_cycle(second.rollback_cycle_id, principal=owner())
    assert _version_state(version.id) == "proposed"
    assert third.restored_versions == [version.id]
    assert service.get_node(node.id, principal=owner()).content == "first"


def test_a_proposal_staged_and_reviewed_inside_one_cycle_comes_back(fresh_db):
    """The node's create and the review of a proposal on it, in one reversal.

    The interaction the two tests above do not reach: the version row is deleted
    by the create's reversal (a foreign key forces it) *after* the accept's
    reversal has already put it back to `proposed`, and rolling the rollback
    back has to re-insert it and then move it to `applied` again. It is also the
    fixture that caught a prefix match: `version.propose` lives in the same
    namespace as `version.reject` and records the *creation* of a version rather
    than a move of one, so sweeping the namespace into the plan died here on
    `KeyError: 'after'`.
    """
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        node = service.create_node(type="claim", title="Made", content="first", principal=owner())
        version = _proposal(node.id)
        service.transition(str(version.id), "accept", principal=auth.internal_principal())
    service.close_cycle(cycle.id, status="completed", report={}, principal=owner())
    reviewed = _every_table()
    assert service.get_node(node.id, principal=owner()).content == "a second thought"

    first = service.rollback_cycle(cycle.id, principal=owner())

    assert first.deleted_nodes == [node.id]
    assert first.restored_versions == [version.id]
    assert _version_state(version.id) is None, "the node's versions went with it"
    with pytest.raises(NodeNotFound):
        service.get_node(node.id, principal=owner())

    service.rollback_cycle(first.rollback_cycle_id, principal=owner())

    assert service.get_node(node.id, principal=owner()).content == "a second thought"
    assert _version_state(version.id) == "applied"
    _assert_every_row_came_back(reviewed, _every_table())


def test_a_proposals_own_event_is_not_swept_into_a_reversal_by_its_namespace(fresh_db):
    """`version.` is three ops with three payload shapes, so it is not a filter.

    `version.propose` records the *creation* of a version row: its `before` is
    the **node** row, it carries no `after` at all, and it does not name the
    version it made (the event is emitted before the insert so the row can point
    back at it). Matching the namespace instead of naming the ops put it in both
    reversal verbs. The enumeration is read out of the module so a fourth
    `version.` op is a decision somebody has to make rather than one a prefix
    makes for them.
    """
    emitted = set(re.findall(r'"(version\.[a-z_]+)"', inspect.getsource(service)))
    assert emitted == {"version.propose", "version.reject", "version.rollback"}, (
        "a new `version.` op: decide whether a reversal can read its payload"
    )
    assert {op for op in emitted if service._is_reversible(op)} == set(
        service._REVERSIBLE_VERSION_OPS
    )

    # And the behaviour under it. A proposal is stepped over by the bare search
    # and refused when named, both exactly as before this change — reversing a
    # proposal is a different operation with a different payload, and rejecting
    # it is the one that exists.
    node = _node("Alpha", content="first")
    _proposal(node.id)
    with pytest.raises(ValueError, match="not a graph event"):
        service.undo(_seq_of("version.propose"), principal=owner())
    assert service.undo(principal=owner()).undone_op == "node.create"


def test_a_rejects_reversal_is_outside_the_projector_namespaces_on_purpose(fresh_db):
    """`version.rollback` changes no node text, so no projector should read it.

    The mirror of the rule `ROLLBACK_OPS` states for the other two kinds: a
    curative op that changes a node has to be `node.*` or the search index
    desynchronises, and an op that changes *only* a `versions` row has to stay
    out of it or the index reprojects a node nothing touched.
    """
    node = _node("Alpha", content="first")
    version = _proposal(node.id)
    cycle_id = _reviewed_in_a_cycle(version.id, "reject")
    projectors.run_projectors()
    indexed = _fts_rows()
    assert indexed, "nothing is indexed, so an unchanged index would prove nothing"

    result = service.rollback_cycle(cycle_id, principal=owner())

    ops = {event.op for event in _events(cycle_id=result.rollback_cycle_id)}
    assert ops == {"version.rollback", "cycle.rollback"}
    assert not any(op.startswith(("node.", "edge.")) for op in ops)
    # The claim, not just the op name: replaying the reversal changes no index
    # row, because no node's text moved.
    projectors.run_projectors()
    assert _fts_rows() == indexed
    assert service.get_node(node.id, principal=owner()).content == "first"


# ── The projectors follow a rollback (the highest-risk detail) ────────────────


def test_the_fts_and_vec_projectors_drop_a_node_a_rollback_deleted(fresh_db, fake_embedder):
    """A `node.` event with no `after` is a removal, and the index has to mirror it.

    Nothing else in the system notices an index that stopped matching the graph:
    the replay skips what it does not understand and the checkpoint still
    advances, so a search index that lies is the silent failure here.
    """
    cycle_id, made = _cycle_with()
    projectors.run_projectors()
    assert made.id in _fts_rows()
    assert _chunk_count(made.id) > 0

    service.rollback_cycle(cycle_id, principal=owner())
    projectors.run_projectors()

    assert made.id not in _fts_rows()
    assert _chunk_count(made.id) == 0
    assert embeddings.get_provider() is fake_embedder
    assert {entry.name: entry.pending_events for entry in projectors.projector_status()} == {
        "fts": 0,
        "vec": 0,
    }


def test_the_fts_index_follows_a_retype_back(fresh_db, tmp_path):
    """A reversal named outside the `node.` namespace would leave the old row.

    The FTS row's `extracted_text` is joined from the asset store for
    `asset_ref` nodes only, so retyping into and back out of that type is a
    change to what the index must contain — visible, and impossible to fake.
    """
    source = tmp_path / "notes.txt"
    source.write_bytes(b"quokka field notes")
    asset = assets.register_asset(source)
    assets.set_extracted_text(asset.hash, "the quokka is a small macropod")
    node = service.create_node(
        type="note", title="Notes", props={"asset_hash": asset.hash}, principal=owner()
    )
    result = service.retype([node.id], "asset_ref", principal=owner())
    projectors.run_projectors()
    assert _fts_extracted()[node.id] == "the quokka is a small macropod"

    service.rollback_cycle(result.cycle_id, principal=owner())
    projectors.run_projectors()

    assert _fts_extracted()[node.id] == ""


def test_every_op_a_rollback_emits_is_readable_by_a_projector_or_deliberately_not(fresh_db):
    """The namespace rule, asserted over what a rollback actually wrote."""
    survivor, duplicate = _node("Alpha"), _node("Alpha (dup)")
    merge = service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())
    result = service.rollback_cycle(merge.cycle_id, principal=owner())

    ops = {event.op for event in _events(cycle_id=result.rollback_cycle_id)}
    assert ops, "the rollback emitted nothing"
    # Every row-level reversal is inside the namespaces the projectors read;
    # the only op outside them is the summary, which changes no row.
    assert {op for op in ops if not op.startswith(("node.", "edge."))} == {"cycle.rollback"}


def _fts_rows():
    conn = db.connect()
    try:
        return {
            row["node_id"]: row["title"]
            for row in conn.execute("SELECT node_id, title FROM node_fts")
        }
    finally:
        conn.close()


def _fts_extracted():
    conn = db.connect()
    try:
        return {
            row["node_id"]: row["extracted_text"]
            for row in conn.execute("SELECT node_id, extracted_text FROM node_fts")
        }
    finally:
        conn.close()


def _chunk_count(node_id):
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE node_id = ?", (node_id,)
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()


# ── Who may roll back, and what cannot be rolled back ────────────────────────


def test_rollback_is_human_only(fresh_db):
    """Strictly more powerful than `undo`, so it cannot be gated more weakly.

    A rollback writes prior payloads back verbatim — `state = 'active'`
    included, across spaces — for a whole cycle at once. The gardener is
    included on purpose: `edit` is authority to *run* a cycle, never authority
    to write live state back across the file.
    """
    node = _node("Alpha")
    result = service.retype([node.id], "concept", principal=owner())

    for principal in (
        agent("bot", grants={"meta": "read", "main": "edit"}),
        auth.internal_principal(),
    ):
        with pytest.raises(GrantNotPermitted, match="only a human may roll back"):
            service.rollback_cycle(result.cycle_id, principal=principal)
    assert service.get_node(node.id, principal=owner()).type == "concept"


def test_a_running_cycle_is_refused_and_says_what_to_do(fresh_db):
    """Its event set is not closed, so reversing it would race the writer.

    A crashed run is indistinguishable from a live one from the outside, so the
    honest move is to make the runner (or a human) close it `failed` first —
    and a failed cycle rolls back like any other.
    """
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        node = service.create_node(type="claim", title="Half-written", principal=owner())

    with pytest.raises(InvalidTransition, match="still running"):
        service.rollback_cycle(cycle.id, principal=owner())

    service.close_cycle(cycle.id, status="failed", report={"error": "boom"}, principal=owner())
    assert service.get_cycle(cycle.id, principal=owner()).status == "failed"
    service.rollback_cycle(cycle.id, principal=owner())
    with pytest.raises(NodeNotFound):
        service.get_node(node.id, principal=owner())


def test_a_cycle_that_wrote_nothing_is_refused_and_a_dry_run_says_why(fresh_db):
    rehearsal = service.open_cycle(trigger="manual", dry_run=True, principal=owner())
    service.close_cycle(rehearsal.id, status="completed", report={}, principal=owner())
    empty = service.open_cycle(trigger="manual", principal=owner())
    service.close_cycle(empty.id, status="completed", report={}, principal=owner())

    with pytest.raises(InvalidTransition, match="rehearsal writes nothing"):
        service.rollback_cycle(rehearsal.id, principal=owner())
    with pytest.raises(InvalidTransition, match="nothing to take back"):
        service.rollback_cycle(empty.id, principal=owner())


def test_rolling_back_an_unknown_cycle_is_a_not_found(fresh_db):
    with pytest.raises(RecordNotFound, match="consolidation cycle not found"):
        service.rollback_cycle("nope", principal=owner())


def test_a_cycles_non_graph_events_are_skipped_rather_than_reversed(fresh_db):
    """An audit record has no row behind it to put back."""
    agent("bot", grants={"main": "read"})
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        service.create_node(type="claim", title="X", principal=owner())
        service.grant("bot", "main", "suggest", principal=owner())
    service.close_cycle(cycle.id, status="completed", report={}, principal=owner())
    grant_seq = _seq_of("grant.set")

    result = service.rollback_cycle(cycle.id, principal=owner())

    assert result.skipped_events == [grant_seq]
    assert grant_seq not in result.reversed_events
    levels = {entry.agent_id: entry.level for entry in service.list_grants(principal=owner())}
    assert levels["bot"] == "suggest"


# ── The dry run: "would this work?", answered without writing ────────────────


def test_a_dry_run_plans_the_rollback_and_writes_nothing(fresh_db):
    node = _node("Alpha")
    result = service.retype([node.id], "concept", principal=owner())
    before, events_before = _graph(), len(_events())

    plan = service.rollback_cycle(result.cycle_id, dry_run=True, principal=owner())

    assert plan.dry_run is True
    assert plan.rollback_cycle_id is None
    assert plan.conflicts == []
    assert plan.reversed_events == [_seq_of("node.retype", row_id=node.id)]
    assert _graph() == before
    assert len(_events()) == events_before
    assert [entry.trigger for entry in service.list_cycles(principal=owner())] == ["curative"]


def _one_cycle_over_every_reversal_shape(fresh_db_unused=None):
    """A cycle whose rollback fills all six of `RollbackOut`'s outcome lists.

    A node create and an edge create (rows the reversal deletes), an update (a
    row it restores), a merge (a tombstone plus the `merge_redirects` row it
    unlinks and the edge it repointed), an accepted proposal (a `versions` row
    moved on the back of a `node.update`) and a rejected one (a `version.reject`
    of its own). Anything narrower leaves a list empty on both paths and proves
    nothing about whether the two agree.
    """
    kept, merged = _node("Kept"), _node("Merged away")
    edited = _node("Edited", content="first")
    other = _node("Other")
    service.create_edge(merged.id, other.id, "supports", principal=owner())
    accepted, rejected = _node("Accepted", content="first"), _node("Rejected", content="first")
    to_accept, to_reject = _proposal(accepted.id), _proposal(rejected.id)

    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        service.create_node(type="claim", title="Created inside", principal=owner())
        service.update_node(edited.id, content="second", principal=owner())
        service.merge_nodes([merged.id], into=kept.id, principal=owner())
        service.create_edge(edited.id, other.id, "supports", principal=owner())
        service.transition(str(to_accept.id), "accept", principal=owner())
        service.transition(str(to_reject.id), "reject", principal=owner())
    service.close_cycle(cycle.id, status="completed", report={}, principal=owner())
    return cycle.id


def test_a_dry_run_reports_what_the_run_reports_rather_than_six_empty_lists(fresh_db):
    """The preflight and the run have to agree, and on these six they did not.

    `blockers` was this exact shape one round earlier: a fact the run knew and
    the plan did not model, so the confirm dialog a human presses answered with
    something other than what pressing it would do. Here the plan modelled none
    of the *outcome* — the dry run returned `restored_nodes`, `restored_edges`,
    `restored_versions`, `deleted_nodes`, `deleted_edges` and `redirects_removed`
    all empty, whatever the rollback was about to do — so a verdict built on them
    understates a reversal that is going to put five rows back and take one out.

    Asserted as *the same lists*, not as expected values: the claim is that one
    accounting answers both paths, and hand-written expectations would let the
    two drift apart the moment either grew a case.
    """
    cycle_id = _one_cycle_over_every_reversal_shape()

    plan = service.rollback_cycle(cycle_id, dry_run=True, principal=owner())
    outcome = service.rollback_cycle(cycle_id, principal=owner())

    reported = {
        "restored_nodes": plan.restored_nodes,
        "restored_edges": plan.restored_edges,
        "restored_versions": plan.restored_versions,
        "deleted_nodes": plan.deleted_nodes,
        "deleted_edges": plan.deleted_edges,
        "redirects_removed": plan.redirects_removed,
    }
    assert reported == {
        "restored_nodes": outcome.restored_nodes,
        "restored_edges": outcome.restored_edges,
        "restored_versions": outcome.restored_versions,
        "deleted_nodes": outcome.deleted_nodes,
        "deleted_edges": outcome.deleted_edges,
        "redirects_removed": outcome.redirects_removed,
    }
    # And every one of them is non-empty, or the agreement above is an agreement
    # about nothing — which is precisely how six empty lists passed for a year.
    assert all(reported.values()), f"a shape this cycle covers reported nothing: {reported}"


def test_a_dry_run_reports_the_conflicts_instead_of_raising_them(fresh_db):
    """The preflight exists to *report*, so the verdict is data rather than a raise.

    Every check the real run makes runs here — only what happens to the answer
    differs, which is what a UI asking "would this work?" needs.
    """
    node = _node("Alpha")
    result = service.retype([node.id], "concept", principal=owner())
    service.update_node(node.id, title="Edited after the cycle", principal=owner())

    plan = service.rollback_cycle(result.cycle_id, dry_run=True, principal=owner())

    assert [conflict.row_id for conflict in plan.conflicts] == [node.id]
    with pytest.raises(RollbackConflict):
        service.rollback_cycle(result.cycle_id, principal=owner())


def test_a_dry_run_reports_the_delete_guards_it_used_to_call_clean(fresh_db):
    """The preflight and the run have to agree, and on these two they did not.

    A conflict is the graph having *moved* a row the cycle wrote; a blocker is
    the graph having *grown something onto* a row the cycle created. The plan
    modelled only the first, so a created node that has since gained a child —
    and a created space that has since been granted on — both answered
    `conflicts: []` and then died on the guard mid-rollback. A confirm dialog
    that says "clean" and then fails is worse than one that says nothing.
    """
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        page = service.create_node(type="page", title="P", principal=owner())
        space = service.create_space("delegated", principal=owner())
    service.close_cycle(cycle.id, status="completed", report={}, principal=owner())
    child = service.create_node(type="block", content="b", parent_id=page.id, principal=owner())
    agent("reader-bot", grants={"meta": "read"})
    service.grant("reader-bot", space.id, "read", principal=owner())

    plan = service.rollback_cycle(cycle.id, dry_run=True, principal=owner())

    assert plan.conflicts == []
    blocked = {blocker.row_id: blocker for blocker in plan.blockers}
    assert set(blocked) == {page.id, space.id}
    assert blocked[page.id].dependants == [child.id]
    assert "child node" in blocked[page.id].reason
    assert blocked[space.id].dependants == ["reader-bot"]
    assert "grant" in blocked[space.id].reason
    assert blocked[page.id].cycle_event_op == "node.create"
    # And the verdict is honest: the real run refuses on one of the two guards
    # the plan named — the space's, since a rollback reverses newest first.
    with pytest.raises(UndoNotPossible) as refused:
        service.rollback_cycle(cycle.id, principal=owner())
    assert str(refused.value).endswith(blocked[space.id].reason)


def test_a_blocked_dry_run_still_describes_the_reversal_it_says_cannot_run(fresh_db):
    """The six lists answer "what is this rollback", not "would it go through".

    Filling them from the plan bought the preflight its agreement with the run,
    and it also made a combination nothing covered: `_planned_effects` walks the
    payloads and never looks at `blockers` or `conflicts`, so a refused verdict
    now says *"this would delete node X"* in the same object that says *"it
    cannot, X has a child"*. That is the honest shape rather than a defect — a
    blocked rollback is still a rollback with a description — but it is only
    readable as a contradiction by a client that renders the six without
    checking the two, so the model docstring says which to read first and this
    pins the shape both statements are about.

    Both refusals are covered, because they arrive by different routes: a
    blocker is `UndoNotPossible` out of the guard, a conflict is
    `RollbackConflict` off the plan.
    """
    blocked_cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(blocked_cycle.id):
        page = service.create_node(type="page", title="P", principal=owner())
    service.close_cycle(blocked_cycle.id, status="completed", report={}, principal=owner())
    child = service.create_node(type="block", content="b", parent_id=page.id, principal=owner())

    plan = service.rollback_cycle(blocked_cycle.id, dry_run=True, principal=owner())
    # It describes the delete...
    assert plan.deleted_nodes == [page.id]
    # ...and refuses it, in the same response.
    assert [blocker.row_id for blocker in plan.blockers] == [page.id]
    with pytest.raises(UndoNotPossible):
        service.rollback_cycle(blocked_cycle.id, principal=owner())
    # The graph is untouched by either call — a preflight writes nothing and a
    # refused rollback is all of it or none of it.
    assert service.get_node(page.id, principal=owner()).id == page.id
    assert service.get_node(child.id, principal=owner()).id == child.id

    # The conflict half, which refuses for the other reason.
    node = _node("Alpha")
    conflicted = service.retype([node.id], "concept", principal=owner())
    service.update_node(node.id, title="Edited after the cycle", principal=owner())

    verdict = service.rollback_cycle(conflicted.cycle_id, dry_run=True, principal=owner())
    assert verdict.restored_nodes == [node.id]
    assert [conflict.row_id for conflict in verdict.conflicts] == [node.id]
    with pytest.raises(RollbackConflict):
        service.rollback_cycle(conflicted.cycle_id, principal=owner())


def test_a_dry_run_does_not_call_the_cycles_own_rows_blockers(fresh_db):
    """A rollback reverses newest first, so what it deletes cannot block it.

    A cycle that creates a page and then a block child of it rolls back
    perfectly — the child is gone before the parent's create is reached — and a
    preflight that counted the child would refuse a rollback that in fact works.
    """
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        page = service.create_node(type="page", title="P", principal=owner())
        service.create_node(type="block", content="b", parent_id=page.id, principal=owner())
    service.close_cycle(cycle.id, status="completed", report={}, principal=owner())
    before = _graph()

    plan = service.rollback_cycle(cycle.id, dry_run=True, principal=owner())
    assert plan.blockers == []
    assert _graph() == before

    service.rollback_cycle(cycle.id, principal=owner())
    with pytest.raises(NodeNotFound):
        service.get_node(page.id, principal=owner())


def test_a_rollback_is_blocked_by_edges_typed_by_the_type_node_the_cycle_created(fresh_db):
    """`edges.type_id` is a foreign key into `nodes(id)` since 0009 — an edge's
    type is a node — and it was the one the guard used to miss.

    The guard that missed it answered `blockers: []` and then died on a bare
    `IntegrityError` mid-rollback: the 500 the refusals exist to prevent (B9).
    The preflight names the edge, and the run refuses on the same sentence
    rather than dying on the constraint.
    """
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        link = service.create_node(
            type="type",
            title="link",
            space="meta",
            props={"type_kind": "edge"},
            principal=owner(),
        )
    service.close_cycle(cycle.id, status="completed", report={}, principal=owner())
    a, b = _node("A"), _node("B")
    edge = service.create_edge(a.id, b.id, link.id, principal=owner())

    plan = service.rollback_cycle(cycle.id, dry_run=True, principal=owner())

    assert plan.conflicts == []
    blocked = {blocker.row_id: blocker for blocker in plan.blockers}
    assert set(blocked) == {link.id}
    assert blocked[link.id].dependants == [edge.id]
    assert "still types 1 edge" in blocked[link.id].reason
    with pytest.raises(UndoNotPossible) as refused:
        service.rollback_cycle(cycle.id, principal=owner())
    assert str(refused.value).endswith(blocked[link.id].reason)
    # A refused rollback is all of it or none of it: the type node and the edge
    # wearing it both still stand.
    assert service.get_node(link.id, principal=owner()).id == link.id
    assert _edge(edge.id)["type_id"] == link.id


def test_a_dry_run_does_not_call_edges_the_reversal_removes_blockers(fresh_db):
    """The reversal deletes every edge wearing a doomed type node, so none block.

    An edge the cycle typed with its own type node is gone before the type's
    create is reversed — a rollback reverses newest first, and the edge had to
    come after its type — and an edge incident to a doomed node goes with the
    node. A preflight that counted either would refuse a rollback that in fact
    works, which is the exact false positive the `doomed_*` sets exist to
    prevent.
    """
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        link = service.create_node(
            type="type",
            title="link",
            space="meta",
            props={"type_kind": "edge"},
            principal=owner(),
        )
        a = service.create_node(type="claim", title="A", principal=owner())
        service.create_edge(a.id, link.id, link.id, principal=owner())
    service.close_cycle(cycle.id, status="completed", report={}, principal=owner())
    before = _graph()

    plan = service.rollback_cycle(cycle.id, dry_run=True, principal=owner())
    assert plan.blockers == []
    assert _graph() == before

    service.rollback_cycle(cycle.id, principal=owner())
    with pytest.raises(NodeNotFound):
        service.get_node(link.id, principal=owner())


def test_every_non_cascading_foreign_key_into_nodes_is_guarded(fresh_db):
    """The delete guards' completeness is pinned to the schema, not to a docstring.

    `_delete_blocker`'s claim is only as good as the list it was written from,
    and a hand-written list rots the day a migration adds a foreign key into
    `nodes(id)` — the guard that missed it answers `blockers: []` and the run
    dies on a bare `IntegrityError`, which is exactly what `edges.type_id` did
    until this phase added it (B9). So the schema is the source of truth: every
    non-cascading foreign key into `nodes(id)` must be owned by a guard, and
    the one cascade is exempted *by name*, so a future migration that adds an
    unguarded reference fails here on the commit that adds it.

    The ownership split, as the code implements it:

    - `_delete_blocker` refuses on what survives the delete: `nodes.parent_id`
      (children), `nodes.space_id` (occupants), `nodes.type_id` (typed nodes),
      `merge_redirects.tombstone_id`/`into_id` (redirects), `grants.space_id`
      (grants) and `edges.type_id` (typed edges).
    - `_delete_created_row` deletes what goes with the row: `edges.src_id`/
      `dst_id` (incident edges) and `versions.node_id` — so no guard over them,
      or a delete that in fact succeeds would be refused.
    - `annotations.target_node_id` cascades (migration 0016): derived judgement
      can never hold a node's undo.
    """
    conn = db.connect()
    try:
        live = db.foreign_keys_into(conn, "nodes")
    finally:
        conn.close()

    refused_by_blocker = {
        ("nodes", "parent_id"),
        ("nodes", "space_id"),
        ("nodes", "type_id"),
        ("merge_redirects", "tombstone_id"),
        ("merge_redirects", "into_id"),
        ("grants", "space_id"),
        ("edges", "type_id"),
    }
    deleted_with_the_row = {("edges", "src_id"), ("edges", "dst_id"), ("versions", "node_id")}
    cascades = {("annotations", "target_node_id")}

    assert live == refused_by_blocker | deleted_with_the_row | cascades, (
        "a migration changed the foreign keys into `nodes(id)` without updating the "
        "delete guards: every non-cascading reference must be refused by "
        "`_delete_blocker` or deleted by `_delete_created_row`, and only "
        "`annotations.target_node_id` may be exempt (it cascades)"
    )


def test_a_dry_run_still_refuses_what_cannot_be_planned(fresh_db):
    """A refusal to *plan* is not a verdict about the graph, so both paths raise."""
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        service.create_node(type="claim", title="Half-written", principal=owner())

    with pytest.raises(InvalidTransition, match="still running"):
        service.rollback_cycle(cycle.id, dry_run=True, principal=owner())
    with pytest.raises(GrantNotPermitted):
        service.rollback_cycle(cycle.id, dry_run=True, principal=agent("bot"))
