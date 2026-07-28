"""The curative tier (design §8.2): merge, retype, supersede, bulk relink.

Four operations that change structure rather than add to it. What is tested
here is what makes them one tier rather than four functions: every one of them
runs inside a consolidation cycle (decision C2), every event they emit is
stamped with it and therefore refused by :func:`nodum.service.undo`, and every
op name stays inside the ``node.``/``edge.`` namespaces the projectors dispatch
on — which is the single detail whose failure mode is a search index that
quietly lies.
"""

from __future__ import annotations

import json

import pytest
from helpers import agent, owner

from nodum import assets, auth, db, embeddings, projectors, service
from nodum.service import (
    EdgeNotFound,
    InvalidTransition,
    NodeNotFound,
    TypeNotFound,
    UndoNotPossible,
)
from nodum.store import GrantNotPermitted


def _events(cycle_id=None):
    """The event log oldest-first, optionally narrowed to one cycle."""
    return list(reversed(service.list_events(owner(), limit=1000, cycle_id=cycle_id)))


def _cycles():
    return service.list_cycles(principal=owner())


def _edge(edge_id):
    """One edge row, read past every filter (an archived edge is still a row)."""
    conn = db.connect()
    try:
        return dict(conn.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone())
    finally:
        conn.close()


def _redirects():
    conn = db.connect()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM merge_redirects")]
    finally:
        conn.close()


def _node(title, *, type="claim", **kwargs):
    return service.create_node(type=type, title=title, principal=owner(), **kwargs)


# ── Every curative op runs inside a cycle (decision C2) ───────────────────────


def test_a_human_invoked_merge_opens_and_closes_its_own_curative_cycle(fresh_db):
    """No ambient cycle means the op opens a one-op cycle of its own.

    A merge is several rows from one decision and `undo` reverses one row from
    one payload, so the cycle is not bookkeeping — it is the only thing that can
    take the merge back whole.
    """
    survivor, duplicate = _node("Alpha"), _node("Alpha (dup)")
    result = service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())

    (cycle,) = _cycles()
    assert cycle.id == result.cycle_id
    assert cycle.trigger == "curative"
    assert cycle.triggered_by == "human:owner"
    assert cycle.status == "completed"
    assert cycle.report["op"] == "merge_nodes"
    assert {event.cycle_id for event in _events(cycle_id=cycle.id)} == {cycle.id}


@pytest.mark.parametrize("op", ["merge_nodes", "retype", "supersede_edge", "bulk_relink"])
def test_every_curative_op_stamps_every_event_it_emits(fresh_db, op):
    """One unstamped event is one row rollback would leave behind."""
    first, second, third = _node("One"), _node("Two"), _node("Three")
    edge = service.create_edge(first.id, second.id, "supports", principal=owner())
    before = len(_events())

    if op == "merge_nodes":
        result = service.merge_nodes([second.id], into=third.id, principal=owner())
    elif op == "retype":
        result = service.retype([first.id], "concept", principal=owner())
    elif op == "supersede_edge":
        result = service.supersede_edge(edge.id, replacement={}, principal=owner())
    else:
        result = service.bulk_relink({"src_id": first.id}, {"dst_id": third.id}, principal=owner())

    emitted = _events()[before:]
    assert emitted, "the operation emitted nothing at all"
    assert {event.cycle_id for event in emitted} == {result.cycle_id}


def test_a_curative_op_inside_an_ambient_cycle_joins_it(fresh_db):
    """The runner owns that cycle's lifecycle; the op neither opens nor closes."""
    node = _node("Alpha")
    runner = service.open_cycle(trigger="scheduled", principal=auth.internal_principal())
    with service.in_cycle(runner.id):
        result = service.retype([node.id], "concept", principal=owner())

    assert result.cycle_id == runner.id
    assert [cycle.id for cycle in _cycles()] == [runner.id]
    assert service.get_cycle(runner.id, principal=owner()).status == "running"
    assert {event.cycle_id for event in _events(cycle_id=runner.id)} == {runner.id}


def test_a_curative_op_that_fails_closes_its_cycle_failed(fresh_db):
    """A cycle that vanished on failure is a cycle nobody could ask about."""
    node = _node("Alpha")
    with pytest.raises(ValueError, match="merge .* into itself"):
        service.merge_nodes([node.id], into=node.id, principal=owner())

    (cycle,) = _cycles()
    assert cycle.status == "failed"
    assert cycle.trigger == "curative"
    assert "into itself" in cycle.report["error"]


def test_undo_refuses_a_curative_event_and_points_at_rollback(fresh_db):
    """C3: the ops keep the `node.`/`edge.` prefix, so undo needs the guard."""
    survivor, duplicate = _node("Alpha"), _node("Alpha (dup)")
    service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())
    later = _node("Written afterwards")
    merge_event = next(event for event in _events() if event.op == "node.merge")

    with pytest.raises(UndoNotPossible, match="Roll the cycle back instead"):
        service.undo(merge_event.seq, principal=owner())
    # And the no-seq search skips it rather than reaching for it: the newest
    # undoable event is the ordinary write that followed the merge.
    undone = service.undo(principal=owner())
    assert undone.undone_op == "node.create"
    assert undone.deleted[-1]["row"]["id"] == later.id


def test_undoing_the_create_of_a_merged_node_is_refused_by_name(fresh_db):
    """`merge_redirects` is a third foreign key into `nodes`, and this is its first
    writer: without the guard the delete served a bare `FOREIGN KEY constraint
    failed` — a 500 on `/api/undo` — for the ordinary "the graph has grown past
    this" case the contract promises to name."""
    survivor, duplicate = _node("Alpha"), _node("Alpha (dup)")
    service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())
    creates = [
        event
        for event in _events()
        if event.op == "node.create" and event.payload["after"]["id"] in (survivor.id, duplicate.id)
    ]

    for event in creates:
        with pytest.raises(UndoNotPossible, match="merge redirect"):
            service.undo(event.seq, principal=owner())


# ── The projectors see every curative op (the highest-risk detail) ────────────


def test_the_fts_projector_reindexes_a_retyped_node(fresh_db, tmp_path):
    """`node.retype` must reproject, and this is where a missed one shows.

    The FTS row's `extracted_text` is joined from the asset store **for
    `asset_ref` nodes only**, so retyping a node into that type is a change to
    what the index must contain — visible, and impossible to fake. An op named
    outside the `node.` namespace would leave the old row standing.
    """
    source = tmp_path / "notes.txt"
    source.write_bytes(b"quokka field notes")
    asset = assets.register_asset(source)
    assets.set_extracted_text(asset.hash, "the quokka is a small macropod")
    node = service.create_node(
        type="note", title="Notes", props={"asset_hash": asset.hash}, principal=owner()
    )
    projectors.run_projectors()
    assert _fts_extracted()[node.id] == ""

    service.retype([node.id], "asset_ref", principal=owner())
    projectors.run_projectors()
    assert _fts_extracted()[node.id] == "the quokka is a small macropod"


def test_the_fts_and_vec_projectors_reindex_a_merged_node(fresh_db, fake_embedder):
    """`node.merge` must reproject too, and a merge changes no node text.

    So the derived rows are dropped first: what puts them back can only be the
    merge event itself. If `node.merge` were named outside the namespace the
    projectors dispatch on, both stores would stay empty and the tombstone would
    be unfindable while still being in the graph.
    """
    survivor, duplicate = _node("Alpha"), _node("Alpha (dup)")
    projectors.run_projectors()
    _drop_derived(duplicate.id)
    assert duplicate.id not in _fts_rows()

    service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())
    projectors.run_projectors()

    assert _fts_rows()[duplicate.id] == "Alpha (dup)"
    assert _chunk_count(duplicate.id) > 0
    assert embeddings.get_provider() is fake_embedder


def test_every_op_the_curative_tier_emits_is_in_a_namespace_the_projectors_read(fresh_db):
    """The rule, stated over what the tier actually wrote rather than restated.

    `projectors.apply` dispatches on `op.startswith("node.")`, and an op outside
    that namespace is not an error anywhere — the replay skips it, the
    checkpoint still advances, and the index silently stops matching the graph.
    Nothing else in the system would notice, which is why the namespace is
    asserted directly.
    """
    first, second, third = _node("One"), _node("Two"), _node("Three")
    edge = service.create_edge(first.id, second.id, "supports", principal=owner())
    service.supersede_edge(edge.id, replacement={"confidence": 0.4}, principal=owner())
    service.retype([first.id], "concept", principal=owner())
    service.bulk_relink({"src_id": first.id}, {"dst_id": third.id}, principal=owner())
    service.merge_nodes([second.id], into=third.id, principal=owner())

    ops = {event.op for event in _events() if event.cycle_id is not None}
    assert ops, "the curative tier emitted nothing"
    outside = sorted(op for op in ops if not op.startswith(("node.", "edge.")))
    assert outside == [], f"these ops are invisible to the projectors: {outside}"

    projectors.run_projectors(names=["fts"])
    status = {entry.name: entry for entry in projectors.projector_status()}["fts"]
    assert status.pending_events == 0


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


def _drop_derived(node_id):
    """Remove one node's derived rows, so only a later event can put them back."""
    conn = db.connect()
    try:
        conn.execute("DELETE FROM node_fts WHERE node_id = ?", (node_id,))
        conn.execute(
            "DELETE FROM node_vec WHERE rowid IN (SELECT id FROM chunks WHERE node_id = ?)",
            (node_id,),
        )
        conn.execute("DELETE FROM chunks WHERE node_id = ?", (node_id,))
        conn.commit()
    finally:
        conn.close()


# ── merge_nodes ──────────────────────────────────────────────────────────────


def test_a_merge_archives_the_duplicate_and_says_where_it_went(fresh_db):
    survivor = _node("Alpha")
    duplicate = _node("Alpha (dup)")
    result = service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())

    (tombstone,) = result.tombstones
    assert tombstone.state == "archived"
    assert tombstone.props["merged_into"] == survivor.id
    # The read path is unchanged: `get_node` on a tombstone answers with the
    # tombstone, which carries the redirect in its own props.
    read_back = service.get_node(duplicate.id, principal=owner())
    assert read_back.id == duplicate.id
    assert read_back.props["merged_into"] == survivor.id
    # The survivor comes back untouched, and is reported as it stands.
    assert service.get_node(survivor.id, principal=owner()) == result.into
    assert result.into.id == survivor.id and result.into.state == "active"


def test_a_merge_snapshots_the_tombstone_as_a_version(fresh_db):
    """Every node mutation in this file writes one; a merge is a node mutation."""
    survivor, duplicate = _node("Alpha"), _node("Alpha (dup)")
    before = len(service.history(duplicate.id, principal=owner()))

    service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())

    history = service.history(duplicate.id, principal=owner())
    assert len(history) == before + 1
    merge_event = next(event for event in _events() if event.op == "node.merge")
    assert history[-1].event_seq == merge_event.seq
    assert history[-1].props["merged_into"] == survivor.id


def test_a_merge_records_a_redirect_row_naming_its_event(fresh_db):
    """`merge_redirects` has existed since 0001 with no writer; this is it."""
    survivor, duplicate = _node("Alpha"), _node("Alpha (dup)")
    result = service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())

    (row,) = _redirects()
    assert row["tombstone_id"] == duplicate.id
    assert row["into_id"] == survivor.id
    merge_event = next(event for event in _events() if event.op == "node.merge")
    assert row["event_seq"] == merge_event.seq
    assert [redirect.model_dump() for redirect in result.redirects] == [row]


def test_incident_edges_are_repointed_and_keep_their_original_endpoints(fresh_db):
    survivor, duplicate, other = _node("Alpha"), _node("Alpha (dup)"), _node("Gamma")
    incoming = service.create_edge(other.id, duplicate.id, "supports", principal=owner())
    outgoing = service.create_edge(duplicate.id, other.id, "cites", principal=owner())

    result = service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())

    assert {edge.id for edge in result.relinked} == {incoming.id, outgoing.id}
    assert _edge(incoming.id)["dst_id"] == survivor.id
    assert _edge(outgoing.id)["src_id"] == survivor.id
    assert json.loads(_edge(incoming.id)["props"])["merged_from"] == {
        "src_id": other.id,
        "dst_id": duplicate.id,
    }
    assert [event.op for event in _events() if event.cycle_id == result.cycle_id].count(
        "edge.relink"
    ) == 2


def test_an_edge_that_would_become_a_self_loop_is_retired_with_its_reason(fresh_db):
    """The `duplicate_of` edge that proposed the merge is exactly this case."""
    survivor, duplicate = _node("Alpha"), _node("Alpha (dup)")
    proposal = service.create_edge(duplicate.id, survivor.id, "duplicate_of", principal=owner())

    result = service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())

    assert [edge.id for edge in result.retired] == [proposal.id]
    assert _edge(proposal.id)["state"] == "archived"
    # The endpoints are untouched: it never moved, it was retired where it stood.
    assert (_edge(proposal.id)["src_id"], _edge(proposal.id)["dst_id"]) == (
        duplicate.id,
        survivor.id,
    )
    archive_event = next(event for event in _events() if event.op == "edge.archive")
    assert "self-loop" in archive_event.payload["reason"]


def test_a_repointed_edge_that_would_duplicate_one_the_survivor_has_is_retired(fresh_db):
    survivor, duplicate, other = _node("Alpha"), _node("Alpha (dup)"), _node("Gamma")
    kept = service.create_edge(survivor.id, other.id, "supports", principal=owner())
    doomed = service.create_edge(duplicate.id, other.id, "supports", principal=owner())

    result = service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())

    assert [edge.id for edge in result.retired] == [doomed.id]
    assert _edge(doomed.id)["state"] == "archived"
    assert _edge(kept.id)["state"] == "active"
    archive_event = next(event for event in _events() if event.op == "edge.archive")
    assert "already carries an identical edge" in archive_event.payload["reason"]


def test_two_merged_nodes_carrying_the_same_edge_leave_only_one(fresh_db):
    """The duplicate rule covers edges the merge itself creates collisions between."""
    survivor, first, second, other = (
        _node("Alpha"),
        _node("Alpha (a)"),
        _node("Alpha (b)"),
        _node("Gamma"),
    )
    from_first = service.create_edge(first.id, other.id, "supports", principal=owner())
    from_second = service.create_edge(second.id, other.id, "supports", principal=owner())

    result = service.merge_nodes([first.id, second.id], into=survivor.id, principal=owner())

    assert [edge.id for edge in result.relinked] == [from_first.id]
    assert [edge.id for edge in result.retired] == [from_second.id]


def test_a_proposed_incident_edge_leaves_proposed_as_a_reject(fresh_db):
    """`proposed → archived` is a reject; the state machine allows only one."""
    survivor, duplicate = _node("Alpha"), _node("Alpha (dup)")
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest"})
    proposal = service.create_edge(duplicate.id, survivor.id, "duplicate_of", principal=proposer)
    assert proposal.state == "proposed"

    service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())

    assert _edge(proposal.id)["state"] == "archived"
    assert any(event.op == "edge.reject" for event in _events())


def test_an_archived_incident_edge_is_left_alone(fresh_db):
    """A retired edge is history; repointing it would rewrite what was true."""
    survivor, duplicate, other = _node("Alpha"), _node("Alpha (dup)"), _node("Gamma")
    retired = service.create_edge(duplicate.id, other.id, "supports", principal=owner())
    service.transition(retired.id, "archive", principal=owner())

    result = service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())

    assert result.relinked == [] and result.retired == []
    assert _edge(retired.id)["src_id"] == duplicate.id


def test_a_tombstones_children_keep_their_parent(fresh_db):
    """Reparenting would be illegal across spaces and mutates rows nobody named."""
    survivor, duplicate = _node("Alpha"), _node("Alpha (dup)")
    child = service.create_node(
        type="block", title="child", parent_id=duplicate.id, principal=owner()
    )

    service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())

    assert service.get_node(child.id, principal=owner()).parent_id == duplicate.id
    assert [node.id for node in service.list_children(survivor.id, principal=owner())] == []


def test_merging_a_node_into_itself_is_refused(fresh_db):
    node = _node("Alpha")
    with pytest.raises(ValueError, match="drop the survivor from the list"):
        service.merge_nodes([node.id], into=node.id, principal=owner())


def test_merging_a_node_that_is_already_a_tombstone_is_refused(fresh_db):
    first, second, third = _node("A"), _node("B"), _node("C")
    service.merge_nodes([second.id], into=first.id, principal=owner())
    with pytest.raises(ValueError, match="already merged into"):
        service.merge_nodes([second.id], into=third.id, principal=owner())


def test_merging_into_a_tombstone_is_refused_rather_than_chained(fresh_db):
    """A redirect chain would break the promise that props say where a node went."""
    first, second, third = _node("A"), _node("B"), _node("C")
    service.merge_nodes([second.id], into=first.id, principal=owner())
    with pytest.raises(ValueError, match="redirect chain"):
        service.merge_nodes([third.id], into=second.id, principal=owner())


def test_merging_a_node_that_is_not_active_is_refused(fresh_db):
    survivor, duplicate = _node("Alpha"), _node("Alpha (dup)")
    service.transition(duplicate.id, "archive", principal=owner())
    with pytest.raises(ValueError, match="already retired"):
        service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())


def test_merging_into_a_node_that_is_not_active_is_refused(fresh_db):
    """A survivor outside the live graph would leave its tombstones naming
    somewhere nobody arrives — and a `proposed` one may yet be rejected."""
    duplicate = _node("Alpha (dup)")
    retired = _node("Retired")
    service.transition(retired.id, "archive", principal=owner())
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest"})
    pending = service.create_node(type="claim", title="Pending", principal=proposer)
    assert pending.state == "proposed"

    for survivor in (retired.id, pending.id):
        with pytest.raises(ValueError, match="nobody arrives"):
            service.merge_nodes([duplicate.id], into=survivor, principal=owner())


@pytest.mark.parametrize("structural", ["main", "space"])
def test_a_space_or_type_node_cannot_be_merged_on_either_side(fresh_db, structural):
    """Structure has its own lifecycle; this tier curates knowledge."""
    ordinary = _node("Alpha")
    with pytest.raises(ValueError, match="structure with their own lifecycles"):
        service.merge_nodes([structural], into=ordinary.id, principal=owner())
    with pytest.raises(ValueError, match="structure with their own lifecycles"):
        service.merge_nodes([ordinary.id], into=structural, principal=owner())


def test_a_merge_across_spaces_needs_edit_on_every_space_it_touches(fresh_db):
    research = service.create_space("research", principal=owner())
    survivor = _node("Alpha")
    duplicate = service.create_node(
        type="claim", title="Alpha (dup)", space=research.id, principal=owner()
    )

    # A human holds it everywhere by construction.
    result = service.merge_nodes([duplicate.id], into=survivor.id, principal=owner())
    assert result.tombstones[0].space_id == research.id

    other, another = _node("B"), _node("C")
    service.create_edge(another.id, other.id, "supports", principal=owner())
    half = agent("half", grants={"meta": "read", "main": "edit"})
    service.grant(half.id, research.id, "read", principal=owner())
    half = auth.agent_principal(half.id)
    outside = service.create_node(
        type="claim", title="Elsewhere", space=research.id, principal=owner()
    )
    with pytest.raises(GrantNotPermitted, match="merge nodes"):
        service.merge_nodes([outside.id], into=other.id, principal=half)


def test_a_merge_needs_edit_on_the_far_space_of_every_incident_edge(fresh_db):
    """An edge is repointed, so its far endpoint's space is touched too."""
    research = service.create_space("research", principal=owner())
    survivor, duplicate = _node("Alpha"), _node("Alpha (dup)")
    far = service.create_node(type="claim", title="Far", space=research.id, principal=owner())
    service.create_edge(far.id, duplicate.id, "supports", principal=owner())

    writer = agent("writer", grants={"meta": "read", "main": "edit", research.id: "read"})
    with pytest.raises(GrantNotPermitted, match="merge nodes"):
        service.merge_nodes([duplicate.id], into=survivor.id, principal=writer)


def test_an_unreadable_node_is_not_found_rather_than_refused(fresh_db):
    """On **either** side. A refusal that said "you may not" where the answer
    should be "no such node" would be an existence oracle over every space in
    the file — the leak rule the whole store is built on."""
    research = service.create_space("research", principal=owner())
    hidden = service.create_node(type="claim", title="Hidden", space=research.id, principal=owner())
    survivor = _node("Alpha")
    stranger = agent("stranger", grants={"meta": "read", "main": "edit"})

    with pytest.raises(NodeNotFound, match="node not found"):
        service.merge_nodes([hidden.id], into=survivor.id, principal=stranger)
    with pytest.raises(NodeNotFound, match="node not found"):
        service.merge_nodes([survivor.id], into=hidden.id, principal=stranger)


def test_merge_nodes_needs_something_to_merge(fresh_db):
    survivor = _node("Alpha")
    with pytest.raises(ValueError, match="at least one node"):
        service.merge_nodes([], into=survivor.id, principal=owner())


# ── retype ───────────────────────────────────────────────────────────────────


def test_retype_changes_the_type_writes_a_version_and_touches_no_props(fresh_db):
    node = service.create_node(
        type="note", title="Alpha", content="body", props={"origin": "inbox"}, principal=owner()
    )
    result = service.retype([node.id], "concept", principal=owner())

    assert result.transitioned == [node.id] and result.failed == []
    assert result.new_type == "concept"
    after = service.get_node(node.id, principal=owner())
    assert after.type == "concept"
    # Props migration is a judgement call about what a property *means* — 5b.
    assert after.props == {"origin": "inbox"}
    assert after.title == "Alpha" and after.content == "body"
    retype_event = next(event for event in _events() if event.op == "node.retype")
    assert (retype_event.payload["from_type"], retype_event.payload["to_type"]) == (
        "note",
        "concept",
    )
    history = service.history(node.id, principal=owner())
    assert history[-1].event_seq == retype_event.seq


def test_retype_resolves_its_target_the_way_create_node_does(fresh_db):
    node = _node("Alpha")
    with pytest.raises(TypeNotFound, match="unknown node type"):
        service.retype([node.id], "nonesuch", principal=owner())


def test_a_type_outside_the_principals_read_scope_does_not_resolve(fresh_db):
    """The type catalog is not a leak channel for a curative op either."""
    node = _node("Alpha")
    blind = agent("blind", grants={"main": "edit"})
    with pytest.raises(TypeNotFound, match="unknown node type"):
        service.retype([node.id], "concept", principal=blind)


@pytest.mark.parametrize("structural", ["space", "type"])
def test_nothing_may_be_retyped_into_a_space_or_a_type(fresh_db, structural):
    node = _node("Alpha")
    with pytest.raises(ValueError, match="structure with their own lifecycles"):
        service.retype([node.id], structural, principal=owner())


def test_a_space_node_may_not_be_retyped(fresh_db):
    """It would stop resolving as a space while its nodes kept pointing at it."""
    space = service.create_space("research", principal=owner())
    result = service.retype([space.id], "concept", principal=owner())
    assert result.transitioned == []
    assert "structure with their own lifecycles" in result.failed[0].error
    assert service.get_node(space.id, principal=owner()).type == "space"


def test_retype_collects_per_item_failures_and_still_changes_the_rest(fresh_db):
    good, already = (
        _node("Alpha"),
        service.create_node(type="concept", title="Beta", principal=owner()),
    )
    result = service.retype([good.id, already.id, "nope"], "concept", principal=owner())

    assert result.transitioned == [good.id]
    assert {failure.id for failure in result.failed} == {already.id, "nope"}
    assert "already of type" in next(f.error for f in result.failed if f.id == already.id)


def test_retype_needs_edit_on_the_nodes_space(fresh_db):
    """A `suggest` grant holds `edit` nowhere, so it never reaches the tier."""
    node = _node("Alpha")
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest"})
    with pytest.raises(GrantNotPermitted, match="curative operation 'retype'"):
        service.retype([node.id], "concept", principal=proposer)
    assert service.get_node(node.id, principal=owner()).type == "claim"


def test_retype_is_refused_per_item_in_a_space_the_caller_cannot_edit(fresh_db):
    """Holding `edit` somewhere opens the cycle; the item's own space is checked
    per node, so one unreachable id never fails the whole batch."""
    research = service.create_space("research", principal=owner())
    mine = _node("Alpha")
    theirs = service.create_node(type="claim", title="Theirs", space=research.id, principal=owner())
    writer = agent("writer", grants={"meta": "read", "main": "edit", research.id: "read"})

    result = service.retype([mine.id, theirs.id], "concept", principal=writer)

    assert result.transitioned == [mine.id]
    assert result.failed[0].id == theirs.id
    assert "retype a node" in result.failed[0].error


def test_a_node_the_caller_cannot_read_fails_a_retype_as_not_found(fresh_db):
    """Not as a refusal: an unreadable space does not exist for its principal,
    and the per-item error string is where that would leak."""
    research = service.create_space("research", principal=owner())
    hidden = service.create_node(type="claim", title="Hidden", space=research.id, principal=owner())
    stranger = agent("stranger", grants={"meta": "read", "main": "edit"})

    result = service.retype([hidden.id], "concept", principal=stranger)

    assert result.transitioned == []
    assert result.failed[0].error == f"node not found: {hidden.id}"


def test_retype_needs_something_to_retype(fresh_db):
    with pytest.raises(ValueError, match="at least one node"):
        service.retype([], "concept", principal=owner())


# ── supersede_edge ───────────────────────────────────────────────────────────


def test_supersede_closes_valid_to_and_archives_the_edge(fresh_db):
    """Two different facts: when it stopped being true, and that it is gone."""
    first, second = _node("A"), _node("B")
    edge = service.create_edge(first.id, second.id, "supports", principal=owner())
    assert edge.valid_to is None

    result = service.supersede_edge(edge.id, principal=owner())

    assert result.superseded.state == "archived"
    assert result.superseded.valid_to is not None
    assert result.replacement is None
    assert any(event.op == "edge.supersede" for event in _events())


def test_a_replacement_is_created_and_the_two_are_linked_both_ways(fresh_db):
    """`supersedes`/`superseded_by` is a seeded *edge type* pair, and an edge's
    endpoints are nodes — so the link between two edges lives in their props."""
    first, second, third = _node("A"), _node("B"), _node("C")
    edge = service.create_edge(first.id, second.id, "supports", principal=owner())

    result = service.supersede_edge(
        edge.id, replacement={"dst_id": third.id, "confidence": 0.9}, principal=owner()
    )

    assert result.replacement is not None
    assert result.replacement.props["supersedes"] == edge.id
    assert result.superseded.props["superseded_by"] == result.replacement.id
    assert result.replacement.state == "active"
    supersede_event = next(event for event in _events() if event.op == "edge.supersede")
    assert supersede_event.payload["replacement_id"] == result.replacement.id


def test_a_replacement_inherits_every_field_it_does_not_name(fresh_db):
    """A supersede that only moves the destination says only that."""
    first, second, third = _node("A"), _node("B"), _node("C")
    edge = service.create_edge(
        first.id, second.id, "supports", props={"note": "kept"}, confidence=0.3, principal=owner()
    )

    result = service.supersede_edge(edge.id, replacement={"dst_id": third.id}, principal=owner())

    assert result.replacement.dst_id == third.id
    assert result.replacement.src_id == first.id
    assert result.replacement.type == "supports"
    assert result.replacement.props["note"] == "kept"
    assert result.replacement.confidence == 0.3


def test_superseding_an_edge_that_is_not_active_is_refused(fresh_db):
    first, second = _node("A"), _node("B")
    edge = service.create_edge(first.id, second.id, "supports", principal=owner())
    service.transition(edge.id, "archive", principal=owner())

    with pytest.raises(InvalidTransition, match="already left the live graph"):
        service.supersede_edge(edge.id, principal=owner())


def test_a_replacement_naming_an_unknown_key_is_refused(fresh_db):
    first, second = _node("A"), _node("B")
    edge = service.create_edge(first.id, second.id, "supports", principal=owner())
    with pytest.raises(ValueError, match="unknown replacement key"):
        service.supersede_edge(edge.id, replacement={"state": "active"}, principal=owner())


def test_an_unreadable_edge_is_not_found(fresh_db):
    research = service.create_space("research", principal=owner())
    inside = service.create_node(type="claim", title="Inside", space=research.id, principal=owner())
    other = service.create_node(type="claim", title="Other", space=research.id, principal=owner())
    edge = service.create_edge(inside.id, other.id, "supports", principal=owner())
    stranger = agent("stranger", grants={"meta": "read", "main": "edit"})

    with pytest.raises(EdgeNotFound):
        service.supersede_edge(edge.id, principal=stranger)


def test_supersede_needs_edit_on_both_endpoint_spaces(fresh_db):
    """The far endpoint's space counts even when the caller owns the near one."""
    research = service.create_space("research", principal=owner())
    near = _node("A")
    far = service.create_node(type="claim", title="Far", space=research.id, principal=owner())
    edge = service.create_edge(near.id, far.id, "supports", principal=owner())
    writer = agent("writer", grants={"meta": "read", "main": "edit", research.id: "read"})

    with pytest.raises(GrantNotPermitted, match="supersede an edge"):
        service.supersede_edge(edge.id, principal=writer)
    assert _edge(edge.id)["state"] == "active"


def test_a_suggest_grant_never_reaches_the_curative_tier(fresh_db):
    """The tier is the review tier's authority; `suggest` holds `edit` nowhere."""
    first, second = _node("A"), _node("B")
    edge = service.create_edge(first.id, second.id, "supports", principal=owner())
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest"})

    with pytest.raises(GrantNotPermitted, match="curative operation 'supersede_edge'"):
        service.supersede_edge(edge.id, principal=proposer)
    with pytest.raises(GrantNotPermitted, match="curative operation 'merge_nodes'"):
        service.merge_nodes([second.id], into=first.id, principal=proposer)
    with pytest.raises(GrantNotPermitted, match="curative operation 'bulk_relink'"):
        service.bulk_relink({"src_id": first.id}, {"type": "cites"}, principal=proposer)
    assert _cycles() == []


# ── bulk_relink ──────────────────────────────────────────────────────────────


def test_a_dry_run_returns_the_diff_and_writes_nothing_at_all(fresh_db):
    """No cycle, no event, no row: the diff *is* the reviewable proposal."""
    first, second, third = _node("A"), _node("B"), _node("C")
    edge = service.create_edge(first.id, second.id, "supports", principal=owner())
    before = len(_events())

    result = service.bulk_relink(
        {"src_id": first.id}, {"dst_id": third.id}, dry_run=True, principal=owner()
    )

    assert result.dry_run is True and result.cycle_id is None
    assert result.matched == 1
    assert [change.edge_id for change in result.changes] == [edge.id]
    assert (result.changes[0].from_dst_id, result.changes[0].to_dst_id) == (second.id, third.id)
    assert _cycles() == []
    assert len(_events()) == before
    assert _edge(edge.id)["dst_id"] == second.id


def test_a_real_run_rewrites_the_edges_and_emits_one_relink_each(fresh_db):
    first, second, third = _node("A"), _node("B"), _node("C")
    one = service.create_edge(first.id, second.id, "supports", principal=owner())
    two = service.create_edge(first.id, second.id, "cites", principal=owner())

    result = service.bulk_relink({"src_id": first.id}, {"dst_id": third.id}, principal=owner())

    assert {change.edge_id for change in result.changes} == {one.id, two.id}
    assert _edge(one.id)["dst_id"] == third.id and _edge(two.id)["dst_id"] == third.id
    relinks = [event for event in _events() if event.op == "edge.relink"]
    assert len(relinks) == 2
    assert {event.cycle_id for event in relinks} == {result.cycle_id}


def test_a_relink_can_change_the_type_as_well_as_the_destination(fresh_db):
    first, second = _node("A"), _node("B")
    edge = service.create_edge(first.id, second.id, "supports", principal=owner())

    result = service.bulk_relink({"type": "supports"}, {"type": "relates_to"}, principal=owner())

    assert result.changes[0].to_type == "relates_to"
    assert _edge(edge.id)["type_id"] == "relates_to"


def test_an_empty_selector_is_refused_rather_than_meaning_everything(fresh_db):
    with pytest.raises(ValueError, match="needs a selector"):
        service.bulk_relink({}, {"type": "relates_to"}, principal=owner())


@pytest.mark.parametrize(
    ("selector", "changes", "message"),
    [
        ({"nonsense": 1}, {"type": "relates_to"}, "unknown selector key"),
        ({"type": "supports"}, {"nonsense": 1}, "unknown change key"),
        ({"type": "supports"}, {}, "needs changes"),
        ({"state": "sideways"}, {"type": "relates_to"}, "state must be one of"),
    ],
)
def test_a_malformed_relink_is_a_sentence(fresh_db, selector, changes, message):
    with pytest.raises(ValueError, match=message):
        service.bulk_relink(selector, changes, principal=owner())


def test_archived_edges_are_excluded_unless_the_selector_names_them(fresh_db):
    first, second, third = _node("A"), _node("B"), _node("C")
    retired = service.create_edge(first.id, second.id, "supports", principal=owner())
    service.transition(retired.id, "archive", principal=owner())

    default = service.bulk_relink(
        {"src_id": first.id}, {"dst_id": third.id}, dry_run=True, principal=owner()
    )
    assert default.matched == 0

    named = service.bulk_relink(
        {"src_id": first.id, "state": "archived"},
        {"dst_id": third.id},
        dry_run=True,
        principal=owner(),
    )
    assert [change.edge_id for change in named.changes] == [retired.id]


def test_a_relink_that_would_make_a_self_loop_is_skipped_with_its_reason(fresh_db):
    first, second = _node("A"), _node("B")
    edge = service.create_edge(first.id, second.id, "supports", principal=owner())

    result = service.bulk_relink({"src_id": first.id}, {"dst_id": first.id}, principal=owner())

    assert result.changes == []
    assert result.skipped[0].id == edge.id
    assert "self-loop" in result.skipped[0].error
    assert _edge(edge.id)["dst_id"] == second.id


def test_a_relink_onto_an_edge_the_graph_already_carries_is_skipped(fresh_db):
    first, second, third = _node("A"), _node("B"), _node("C")
    doomed = service.create_edge(first.id, second.id, "supports", principal=owner())
    service.create_edge(first.id, third.id, "supports", principal=owner())

    result = service.bulk_relink({"dst_id": second.id}, {"dst_id": third.id}, principal=owner())

    assert result.changes == []
    assert result.skipped[0].id == doomed.id
    assert "already carries an identical edge" in result.skipped[0].error


def test_an_edge_nothing_would_change_on_is_skipped_not_rewritten(fresh_db):
    first, second = _node("A"), _node("B")
    edge = service.create_edge(first.id, second.id, "supports", principal=owner())

    result = service.bulk_relink({"src_id": first.id}, {"dst_id": second.id}, principal=owner())

    assert result.matched == 1 and result.changes == []
    assert result.skipped[0].id == edge.id
    assert "nothing would change" in result.skipped[0].error
    assert [event for event in _events() if event.op == "edge.relink"] == []


def test_a_dry_run_by_a_principal_who_could_not_apply_it_says_so(fresh_db):
    """ "What would happen if I ran this" and the honest answer is "nothing".

    A dry run opens no cycle, so it is not refused up front; it runs the same
    per-edge checks a real run does and reports every edge as skipped, which is
    exactly what would happen. It still reveals nothing the caller cannot
    already read — the selection carries the store's edge scope.
    """
    first, second, third = _node("A"), _node("B"), _node("C")
    edge = service.create_edge(first.id, second.id, "supports", principal=owner())
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest"})

    result = service.bulk_relink(
        {"src_id": first.id}, {"dst_id": third.id}, dry_run=True, principal=proposer
    )

    assert result.changes == []
    assert result.skipped[0].id == edge.id
    assert "relink an edge" in result.skipped[0].error


def test_the_selection_is_capped_and_says_so(fresh_db, monkeypatch):
    """The ceiling is in the spirit of MAX_SUBGRAPH_LIMIT; truncation is reported."""
    monkeypatch.setattr(service, "MAX_RELINK_EDGES", 2)
    first, second, third = _node("A"), _node("B"), _node("C")
    for edge_type in ("supports", "cites", "relates_to"):
        service.create_edge(first.id, second.id, edge_type, principal=owner())

    result = service.bulk_relink({"src_id": first.id}, {"dst_id": third.id}, principal=owner())

    assert result.matched == 2
    assert result.truncated is True
    assert len(result.changes) == 2
    assert service.MAX_RELINK_EDGES == 2  # the patch, not the shipped value


def test_the_shipped_relink_ceiling_is_a_real_bound(fresh_db):
    assert 0 < service.MAX_RELINK_EDGES <= service.MAX_SUBGRAPH_LIMIT


def test_a_relink_into_a_space_the_caller_cannot_edit_is_skipped(fresh_db):
    research = service.create_space("research", principal=owner())
    first, second = _node("A"), _node("B")
    far = service.create_node(type="claim", title="Far", space=research.id, principal=owner())
    edge = service.create_edge(first.id, second.id, "supports", principal=owner())
    writer = agent("writer", grants={"meta": "read", "main": "edit", research.id: "read"})

    result = service.bulk_relink({"src_id": first.id}, {"dst_id": far.id}, principal=writer)

    assert result.changes == []
    assert result.skipped[0].id == edge.id
    assert "relink an edge" in result.skipped[0].error
    assert _edge(edge.id)["dst_id"] == second.id


def test_a_relink_that_would_cross_spaces_needs_a_meta_typed_edge(fresh_db):
    """The one structural edge rule survives a relink that *creates* a crossing.

    An edge is written inside one space with a type node living in that space,
    which is legal — and then relinked so that it leaves the space. The rule
    that a cross-space edge's type must live in meta is checked here rather
    than only at creation, or a relink would be the way around it. Every seeded
    type does live in meta, so the type node has to be made by hand.
    """
    research = service.create_space("research", principal=owner())
    _seed_local_edge_type("local_link")
    first, second = _node("A"), _node("B")
    far = service.create_node(type="claim", title="Far", space=research.id, principal=owner())
    local = service.create_edge(first.id, second.id, "local_link", principal=owner())
    crossing = service.create_edge(first.id, second.id, "supports", principal=owner())

    result = service.bulk_relink({"src_id": first.id}, {"dst_id": far.id}, principal=owner())

    # The meta-typed edge crosses; the locally typed one is skipped and says so.
    assert [change.edge_id for change in result.changes] == [crossing.id]
    assert result.skipped[0].id == local.id
    assert "type node must live in the meta space" in result.skipped[0].error
    assert _edge(local.id)["dst_id"] == second.id


def _seed_local_edge_type(type_id):
    """Insert an edge-type node in `main` — something no seeded type is."""
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO nodes (id, space_id, type_id, title, props, state, created_by)"
            " VALUES (?, 'main', 'type', ?, '{\"type_kind\":\"edge\"}', 'active', 'human:owner')",
            (type_id, type_id),
        )
        conn.commit()
    finally:
        conn.close()
    return type_id


def test_a_new_destination_the_caller_cannot_read_is_not_found(fresh_db):
    research = service.create_space("research", principal=owner())
    first, second = _node("A"), _node("B")
    hidden = service.create_node(type="claim", title="Hidden", space=research.id, principal=owner())
    service.create_edge(first.id, second.id, "supports", principal=owner())
    stranger = agent("stranger", grants={"meta": "read", "main": "edit"})

    with pytest.raises(NodeNotFound):
        service.bulk_relink({"src_id": first.id}, {"dst_id": hidden.id}, principal=stranger)


# ── The gardener can run the tier it was seeded for ───────────────────────────


def test_the_gardener_may_run_a_curative_op_where_it_holds_edit(fresh_db):
    """0014 grants it `edit` on meta and main; that is the whole authority."""
    survivor, duplicate = _node("Alpha"), _node("Alpha (dup)")
    result = service.merge_nodes(
        [duplicate.id], into=survivor.id, principal=auth.internal_principal()
    )

    assert result.tombstones[0].props["merged_into"] == survivor.id
    merge_event = next(event for event in _events() if event.op == "node.merge")
    assert merge_event.actor == "agent:builtin-gardener"
    (cycle,) = _cycles()
    assert cycle.triggered_by == "agent:builtin-gardener"


def test_the_gardener_cannot_curate_a_space_it_was_not_granted(fresh_db):
    research = service.create_space("research", principal=owner())
    inside = service.create_node(type="claim", title="Inside", space=research.id, principal=owner())
    survivor = _node("Alpha")

    with pytest.raises(NodeNotFound):
        service.merge_nodes([inside.id], into=survivor.id, principal=auth.internal_principal())
