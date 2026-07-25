"""Projector infrastructure: checkpoints, incremental apply, rebuild equivalence."""

from __future__ import annotations

import pytest
from helpers import owner

from nodum import db, projectors, service


def _fts_rows(fresh_db):
    """Dump the FTS index as {node_id: (title, content)} for comparisons."""
    conn = db.connect(fresh_db)
    try:
        rows = conn.execute("SELECT node_id, title, content FROM node_fts").fetchall()
        return {row["node_id"]: (row["title"], row["content"]) for row in rows}
    finally:
        conn.close()


def _statuses():
    """Projector statuses keyed by name."""
    return {status.name: status for status in projectors.projector_status()}


def _runs(**kwargs):
    """Projector runs keyed by name."""
    return {run.name: run for run in projectors.run_projectors(**kwargs)}


def test_checkpoint_starts_at_zero_with_backlog(fresh_db):
    service.create_node(type="note", title="T", principal=owner())
    statuses = _statuses()
    assert set(statuses) == {"fts", "vec"}
    fts = statuses["fts"]
    assert fts.last_event_seq == 0
    assert fts.pending_events == 1
    assert fts.rows == 0


def test_run_applies_pending_events_incrementally(fresh_db):
    service.create_node(type="note", title="one", principal=owner())
    run = _runs()["fts"]
    assert run.applied == 1
    assert run.from_seq == 0
    assert run.to_seq == 1

    service.create_node(type="note", title="two", principal=owner())
    service.create_node(type="note", title="three", principal=owner())
    run = _runs()["fts"]
    assert (run.applied, run.from_seq, run.to_seq) == (2, 1, 3)

    # Up to date: a further run applies nothing.
    run = _runs()["fts"]
    assert (run.applied, run.from_seq, run.to_seq) == (0, 3, 3)
    status = _statuses()["fts"]
    assert status.pending_events == 0
    assert status.rows == 3


def test_run_only_the_named_projectors(fresh_db):
    service.create_node(type="note", title="T", principal=owner())
    runs = projectors.run_projectors(names=["fts"])
    assert [run.name for run in runs] == ["fts"]
    with pytest.raises(ValueError, match="unknown projector"):
        projectors.run_projectors(names=["nope"])


def test_rebuild_replays_from_event_zero(fresh_db):
    a = service.create_node(type="note", title="A", content="alpha", principal=owner())
    service.update_node(a.id, content="alpha v2", principal=owner())
    service.create_node(type="claim", title="B", content="beta", principal=owner())
    projectors.run_projectors()
    before = _fts_rows(fresh_db)

    run = projectors.rebuild_projector("fts")
    assert run.from_seq == 0
    assert run.applied == 3
    assert _fts_rows(fresh_db) == before
    assert _fts_rows(fresh_db)[a.id] == ("A", "alpha v2")

    with pytest.raises(ValueError, match="unknown projector"):
        projectors.rebuild_projector("nope")


def test_fts_tracks_update_archive_and_undo(fresh_db):
    node = service.create_node(type="note", title="T", content="v1", principal=owner())
    projectors.run_projectors()
    assert _fts_rows(fresh_db)[node.id] == ("T", "v1")

    service.update_node(node.id, content="v2", principal=owner())
    service.transition(node.id, "archive", principal=owner())
    projectors.run_projectors()
    # Archived nodes stay in the index; the search layer filters by state.
    assert _fts_rows(fresh_db)[node.id] == ("T", "v2")

    # Undo the archive: the restored active row is re-indexed.
    service.undo(principal=owner())
    projectors.run_projectors()
    assert _fts_rows(fresh_db)[node.id] == ("T", "v2")


def test_fts_drops_a_node_when_its_create_is_undone(fresh_db):
    keep = service.create_node(type="note", title="keep", principal=owner())
    gone = service.create_node(type="note", title="gone", principal=owner())
    projectors.run_projectors()
    assert set(_fts_rows(fresh_db)) == {keep.id, gone.id}

    service.undo(principal=owner())  # reverses the create of `gone`
    run = _runs()["fts"]
    assert run.applied == 1  # the undo event
    assert set(_fts_rows(fresh_db)) == {keep.id}


def test_rebuild_equivalence_after_undo_events(fresh_db):
    """Replaying from event 0 through undo events lands on the same index."""
    a = service.create_node(type="note", title="A", content="v1", principal=owner())
    service.update_node(a.id, content="v2", principal=owner())
    service.undo(principal=owner())  # back to v1
    service.create_node(type="note", title="B", principal=owner())
    service.undo(principal=owner())  # B's create reverted
    projectors.run_projectors()
    before = _fts_rows(fresh_db)

    projectors.rebuild_projector("fts")
    assert _fts_rows(fresh_db) == before
    assert set(_fts_rows(fresh_db)) == {a.id}
    assert _fts_rows(fresh_db)[a.id] == ("A", "v1")
