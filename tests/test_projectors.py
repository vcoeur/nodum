"""Projector infrastructure: checkpoints, incremental apply, rebuild equivalence."""

from __future__ import annotations

import json

import pytest
from helpers import owner

from nodum import assets, db, projectors, search, service


def _fts_rows(fresh_db):
    """Dump the FTS index as {node_id: (title, content)} for comparisons."""
    conn = db.connect(fresh_db)
    try:
        rows = conn.execute("SELECT node_id, title, content FROM node_fts").fetchall()
        return {row["node_id"]: (row["title"], row["content"]) for row in rows}
    finally:
        conn.close()


def _fts_extracted(fresh_db):
    """Dump the FTS index's `extracted_text` column as {node_id: text}."""
    conn = db.connect(fresh_db)
    try:
        rows = conn.execute("SELECT node_id, extracted_text FROM node_fts").fetchall()
        return {row["node_id"]: row["extracted_text"] for row in rows}
    finally:
        conn.close()


def _register_bytes(tmp_path, name, payload=b"some bytes"):
    """Register an asset from a scratch file; the bytes themselves never matter here."""
    source = tmp_path / name
    source.write_bytes(payload)
    return assets.register_asset(source)


def _emit_raw_node_event(fresh_db, node_id, props):
    """Append a `node.create` event by hand, carrying an arbitrary `props` value.

    The service can only ever write JSON objects there, so a payload this
    malformed has to be injected: the point is that a replay meeting one does
    not stop, whatever wrote it.
    """
    payload = {
        "before": None,
        "after": {"id": node_id, "title": "raw", "content": "body", "props": props},
    }
    conn = db.connect(fresh_db)
    try:
        conn.execute(
            "INSERT INTO events (actor, op, payload) VALUES ('human:owner', 'node.create', ?)",
            (json.dumps(payload),),
        )
        conn.commit()
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


# ── The asset join: `assets.extracted_text` reaches the FTS row ───────────────


def test_a_node_carrying_an_asset_hash_indexes_that_assets_text(fresh_db, tmp_path):
    asset = _register_bytes(tmp_path, "paper.pdf")
    assets.set_extracted_text(asset.hash, "the quokka is a small macropod")
    node = service.create_node(
        type="asset_ref",
        title="Paper",
        props={"asset_hash": asset.hash},
        principal=owner(),
    )
    projectors.run_projectors()

    assert _fts_extracted(fresh_db)[node.id] == "the quokka is a small macropod"
    # The node's own columns are untouched by the join.
    assert _fts_rows(fresh_db)[node.id] == ("Paper", "")


def test_only_an_asset_ref_node_gets_the_join(fresh_db, tmp_path):
    """Ingestion records `asset_hash` on the source node and on every per-page
    block as well — provenance, and what lets a page id resolve to a raster.
    Joining on the prop alone therefore gave every page of a document the whole
    document's text, so a word from one page matched all of them equally."""
    asset = _register_bytes(tmp_path, "paper.pdf")
    assets.set_extracted_text(asset.hash, "the quokka is a small macropod")
    describing = service.create_node(
        type="asset_ref", props={"asset_hash": asset.hash}, principal=owner()
    )
    page = service.create_node(
        type="block",
        title="Page 1",
        content="its own page text",
        props={"asset_hash": asset.hash, "page": 1},
        principal=owner(),
    )
    projectors.run_projectors()

    indexed = _fts_extracted(fresh_db)
    assert indexed[describing.id] == "the quokka is a small macropod"
    assert indexed[page.id] == ""


def test_a_word_only_in_the_extracted_text_finds_the_node(fresh_db, tmp_path):
    """The point of the join: an asset's body text is searchable through its node.

    `quokka` appears nowhere in the node's title or content — only in the
    asset row — so a hit proves the extracted column is really being indexed
    and really being matched, not merely written.
    """
    asset = _register_bytes(tmp_path, "paper.pdf")
    assets.set_extracted_text(asset.hash, "field notes on the quokka population")
    node = service.create_node(
        type="asset_ref",
        title="Scanned field notes",
        content="a scan, nothing more",
        props={"asset_hash": asset.hash},
        principal=owner(),
    )
    service.create_node(type="note", title="Unrelated", content="wombat", principal=owner())

    result = search.search("quokka", principal=owner())
    assert [hit.node_id for hit in result.hits] == [node.id]
    assert "bm25" in result.hits[0].signals


def test_extracted_text_is_dropped_when_the_asset_text_is_cleared(fresh_db, tmp_path):
    asset = _register_bytes(tmp_path, "paper.pdf")
    assets.set_extracted_text(asset.hash, "quokka")
    node = service.create_node(
        type="asset_ref", props={"asset_hash": asset.hash}, principal=owner()
    )
    projectors.run_projectors()
    assert _fts_extracted(fresh_db)[node.id] == "quokka"

    assets.set_extracted_text(asset.hash, None)
    projectors.rebuild_projector("fts")
    assert _fts_extracted(fresh_db)[node.id] == ""


def test_text_stored_after_a_node_is_projected_waits_for_the_next_projection(fresh_db, tmp_path):
    """The honest limitation: `assets` is not event-logged, so the join is a
    live read taken *at projection time*.

    Storing the text after a node has been projected leaves the index stale
    until that node is projected again or `fts` is rebuilt — which is exactly
    why the ingestion pipeline stores the text before it creates the node.
    """
    asset = _register_bytes(tmp_path, "paper.pdf")
    node = service.create_node(
        type="asset_ref", props={"asset_hash": asset.hash}, principal=owner()
    )
    projectors.run_projectors()
    assert _fts_extracted(fresh_db)[node.id] == ""

    assets.set_extracted_text(asset.hash, "quokka")
    projectors.run_projectors()  # no new events, so nothing is re-projected
    assert _fts_extracted(fresh_db)[node.id] == ""

    projectors.rebuild_projector("fts")
    assert _fts_extracted(fresh_db)[node.id] == "quokka"


def test_a_node_with_no_asset_hash_indexes_cleanly(fresh_db):
    node = service.create_node(type="note", title="T", content="v1", principal=owner())
    projectors.run_projectors()
    assert _fts_extracted(fresh_db)[node.id] == ""


def test_a_hash_with_no_asset_row_contributes_nothing(fresh_db):
    """A node may name bytes nobody ever registered; that is not an error."""
    node = service.create_node(
        type="asset_ref",
        title="Dangling",
        props={"asset_hash": "0" * 64},
        principal=owner(),
    )
    run = _runs()["fts"]
    assert run.applied == 1
    assert _fts_extracted(fresh_db)[node.id] == ""


@pytest.mark.parametrize(
    ("label", "props"),
    [
        ("not json", "not json at all"),
        ("json but not an object", "[1, 2, 3]"),
        ("absent", None),
        ("hash is not a string", '{"asset_hash": 12}'),
        ("hash is empty", '{"asset_hash": ""}'),
    ],
)
def test_a_malformed_props_value_does_not_break_a_replay(fresh_db, label, props):
    """One unreadable payload must not stop the projector for every node after it."""
    _emit_raw_node_event(fresh_db, "raw-node", props)
    later = service.create_node(
        type="note", title="after", content="still indexed", principal=owner()
    )

    run = _runs()["fts"]
    assert run.applied == 2
    assert _fts_extracted(fresh_db)["raw-node"] == ""
    assert _fts_rows(fresh_db)[later.id] == ("after", "still indexed")


def test_rebuild_replays_the_asset_join_from_event_zero(fresh_db, tmp_path):
    asset = _register_bytes(tmp_path, "paper.pdf")
    assets.set_extracted_text(asset.hash, "quokka")
    node = service.create_node(
        type="asset_ref", props={"asset_hash": asset.hash}, principal=owner()
    )
    projectors.run_projectors()
    before = _fts_extracted(fresh_db)

    projectors.rebuild_projector("fts")
    assert _fts_extracted(fresh_db) == before
    assert _fts_extracted(fresh_db)[node.id] == "quokka"
