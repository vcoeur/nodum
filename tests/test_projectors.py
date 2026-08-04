"""Projector infrastructure: checkpoints, incremental apply, rebuild equivalence."""

from __future__ import annotations

import json

import pytest
from conftest import HashEmbedder
from helpers import owner

from nodum import assets, db, embeddings, projectors, search, service


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


def _stored_chunks(fresh_db, node_id):
    """The chunk texts the vec projector wrote for one node, in sequence order."""
    conn = db.connect(fresh_db)
    try:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE node_id = ? ORDER BY seq", (node_id,)
        ).fetchall()
        return [row["text"] for row in rows]
    finally:
        conn.close()


def test_the_projector_and_the_cycle_chunk_a_node_the_same_way(fresh_db, fake_embedder):
    """The parity that stops a node having two different vectors.

    The projector stores one vector per chunk (search wants the best chunk) and
    the consolidation cycle needs one vector per node, so the two cannot store
    the same thing — but they must *derive* from the same chunking. Before this
    was fixed the cycle embedded the node's whole text in one call, so a node
    longer than one window was compared on its opening window alone while the
    projector held the whole of it.
    """
    content = " ".join(f"w{index}" for index in range(3 * embeddings.CHUNK_WORDS))
    node = service.create_node(type="note", title="Long", content=content, principal=owner())
    projectors.run_projectors(names=["vec"])

    stored = _stored_chunks(fresh_db, node.id)
    assert len(stored) > 1
    assert stored == embeddings.node_chunks({"title": node.title, "content": node.content})


def test_the_cycles_node_vector_is_recoverable_from_the_stored_chunks(fresh_db, fake_embedder):
    """The node vector is a pure function of the projector's rows, not a rival embedding."""
    content = " ".join(f"w{index}" for index in range(3 * embeddings.CHUNK_WORDS))
    node = service.create_node(type="note", title="Long", content=content, principal=owner())
    projectors.run_projectors(names=["vec"])

    from_projector = embeddings._pool(
        fake_embedder.embed(_stored_chunks(fresh_db, node.id)), fake_embedder.dimensions
    )
    (from_cycle,) = embeddings.node_vectors(
        fake_embedder, [{"title": node.title, "content": node.content}]
    )

    assert from_cycle == pytest.approx(from_projector)


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

    assets.set_extracted_text(asset.hash, None)  # clears, and logs asset.extract
    projectors.run_projectors()  # the event re-projects the node
    assert _fts_extracted(fresh_db)[node.id] == ""

    projectors.rebuild_projector("fts")
    assert _fts_extracted(fresh_db)[node.id] == ""


def test_text_stored_after_a_node_is_projected_is_indexed_by_the_extract_event(fresh_db, tmp_path):
    """Finding M14: `set_extracted_text` logs `asset.extract`, so the `fts`
    projector re-projects the describing node — no rebuild needed.

    This is what makes a rebuild from event 0 equal to an incremental replay:
    the log itself records that the extraction happened, so a replay from
    event 0 folds the same text in at the same point.
    """
    asset = _register_bytes(tmp_path, "paper.pdf")
    node = service.create_node(
        type="asset_ref", props={"asset_hash": asset.hash}, principal=owner()
    )
    projectors.run_projectors()
    assert _fts_extracted(fresh_db)[node.id] == ""

    assets.set_extracted_text(asset.hash, "quokka")
    projectors.run_projectors()  # the asset.extract event re-projects the node
    assert _fts_extracted(fresh_db)[node.id] == "quokka"

    # A rebuild replays the same chain to the same index.
    projectors.rebuild_projector("fts")
    assert _fts_extracted(fresh_db)[node.id] == "quokka"


def test_an_extract_event_before_the_describing_node_does_not_wedge_the_projector(
    fresh_db, tmp_path
):
    """The pipeline stores text before it creates the `asset_ref` node (M14).

    At replay the event finds no node for the hash and is a skip; the node's
    own create then does the live join and picks the text up in the same run.
    """
    asset = _register_bytes(tmp_path, "paper.pdf")
    assets.set_extracted_text(asset.hash, "quokka")  # event written; no node yet
    run = _runs()["fts"]
    assert run.applied == 1  # the asset.extract event alone
    assert _fts_extracted(fresh_db) == {}

    node = service.create_node(
        type="asset_ref", props={"asset_hash": asset.hash}, principal=owner()
    )
    projectors.run_projectors()
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


# ── M11: per-projector transactions; M12: the quarantine ─────────────────────


def _corrupt_payload(fresh_db, seq, payload="{not json"):
    """Rewrite one event's payload column to text `apply` cannot parse.

    The service layer can only write valid JSON objects there, so this has to
    be injected: the point is that a replay meeting one quarantines it and
    moves on, whatever wrote it (finding M12).
    """
    conn = db.connect(fresh_db)
    try:
        conn.execute("UPDATE events SET payload = ? WHERE seq = ?", (payload, seq))
        conn.commit()
    finally:
        conn.close()


def test_a_failing_projector_rolls_back_only_its_own_batch(fresh_db):
    """Finding M11: one projector's failure must not discard another's work.

    `fts` and `vec` share one run call and (before this) one transaction: the
    vec projector failing mid-batch rolled back fts's applied events and
    checkpoints with it. Each projector's batch is its own transaction now —
    vec's failure rolls back vec's own writes and checkpoint, while fts's
    committed work survives.
    """

    class FailingEmbedder(HashEmbedder):
        def embed(self, texts):
            if any("boom" in text for text in texts):
                raise RuntimeError("embedding provider fell over")
            return super().embed(texts)

    embeddings.set_provider(FailingEmbedder())
    ok = service.create_node(type="note", title="ok", content="safe text", principal=owner())
    boom = service.create_node(type="note", title="boom", content="boom text", principal=owner())
    with pytest.raises(RuntimeError, match="fell over"):
        projectors.run_projectors(names=["fts", "vec"])

    # fts's batch committed: both nodes indexed, checkpoint at the end.
    rows = _fts_rows(fresh_db)
    assert ok.id in rows and boom.id in rows
    fts_status = _statuses()["fts"]
    assert fts_status.last_event_seq == 2
    assert fts_status.pending_events == 0

    # vec's batch rolled back: nothing in the store, checkpoint unmoved.
    vec_status = _statuses()["vec"]
    assert vec_status.last_event_seq == 0
    assert vec_status.rows == 0

    # The next run retries vec from the same checkpoint (and fails again —
    # a provider failure is systemic, never quarantined as skips).
    with pytest.raises(RuntimeError, match="fell over"):
        projectors.run_projectors(names=["vec"])
    assert _statuses()["vec"].last_event_seq == 0


def test_a_malformed_event_is_quarantined_and_the_checkpoint_moves_on(fresh_db):
    """Finding M12: one bad row must not wedge the projector forever.

    A replay that meets an unparseable payload skips the event, records the
    skip, and keeps applying what comes after — the checkpoint advances past
    the bad row, so the next run starts after it instead of failing on it.
    """
    before = service.create_node(type="note", title="before", principal=owner())
    bad = service.create_node(type="note", title="bad", principal=owner())
    after = service.create_node(type="note", title="after", principal=owner())
    _corrupt_payload(fresh_db, 2)  # the middle event: `bad`'s create

    run = _runs()["fts"]
    assert (run.applied, run.skipped, run.from_seq, run.to_seq) == (2, 1, 0, 3)
    rows = _fts_rows(fresh_db)
    assert before.id in rows and after.id in rows
    assert bad.id not in rows

    # The next run starts after the bad event: nothing left to do.
    run = _runs()["fts"]
    assert (run.applied, run.skipped) == (0, 0)

    # The skip is recorded with the event's op and the failure reason.
    conn = db.connect(fresh_db)
    try:
        (skip,) = conn.execute(
            "SELECT projector, seq, op, error FROM projector_skips WHERE projector = 'fts'"
        ).fetchall()
    finally:
        conn.close()
    assert (skip["seq"], skip["op"]) == (2, "node.create")
    assert "JSONDecodeError" in skip["error"]

    # A rebuild replays the bad event and re-skips it: the record is
    # upserted, not duplicated.
    run = projectors.rebuild_projector("fts")
    assert (run.applied, run.skipped) == (2, 1)
    conn = db.connect(fresh_db)
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM projector_skips").fetchone()["n"]
    finally:
        conn.close()
    assert count == 1

    # The quarantine is visible through status.
    assert _statuses()["fts"].skipped == 1


def test_a_backlog_whose_every_event_is_malformed_aborts_instead_of_hiding(fresh_db):
    """The all-failed bound: an all-malformed backlog is a writer bug, not N
    independent corruptions, so it must not be recorded as N skips.

    A single bad event is quarantined and the projector moves on; a batch
    where *every* event raises is a systemic signal, so the batch aborts, the
    checkpoint stays behind, and the failure surfaces to the caller.
    """
    service.create_node(type="note", title="one", principal=owner())
    service.create_node(type="note", title="two", principal=owner())
    _corrupt_payload(fresh_db, 1)
    _corrupt_payload(fresh_db, 2)
    with pytest.raises(json.JSONDecodeError):
        projectors.run_projectors(names=["fts"])

    # Nothing committed: checkpoint unmoved, and the skip rows rolled back
    # with the batch.
    assert _statuses()["fts"].last_event_seq == 0
    conn = db.connect(fresh_db)
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM projector_skips").fetchone()["n"]
    finally:
        conn.close()
    assert count == 0
