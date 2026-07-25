"""The sqlite-vec projector: chunk embeddings fed by the event log."""

from __future__ import annotations

import sqlite3

import pytest
from conftest import HashEmbedder
from helpers import owner

from nodum import db, embeddings, projectors, service


def _vec_state(fresh_db):
    """Dump the derived vector store for comparisons.

    Returns (chunks, vectors): chunks as {node_id: [(seq, text, model_id)]}
    and vectors as {(node_id, seq): serialized bytes} — keyed by content, not
    chunk rowid, so an incremental run and a rebuild (which re-keys chunks)
    compare equal when the embedded content matches.
    """
    conn = db.connect(fresh_db)
    try:
        chunk_rows = conn.execute(
            "SELECT id, node_id, seq, text, model_id FROM chunks ORDER BY node_id, seq"
        ).fetchall()
        chunks: dict[str, list] = {}
        for row in chunk_rows:
            chunks.setdefault(row["node_id"], []).append((row["seq"], row["text"], row["model_id"]))
        vectors = {
            (row["node_id"], row["seq"]): row["embedding"]
            for row in conn.execute(
                """
                SELECT c.node_id, c.seq, v.embedding
                FROM chunks c JOIN node_vec v ON v.rowid = c.id
                """
            ).fetchall()
        }
        return chunks, vectors
    finally:
        conn.close()


def test_incremental_run_embeds_nodes_with_model_id(fresh_db, fake_embedder):
    node = service.create_node(
        type="note",
        title="Photosynthesis",
        content="sunlight to energy",
        principal=owner(),
    )
    title_only = service.create_node(type="note", title="Empty", principal=owner())
    (run,) = [r for r in projectors.run_projectors(names=["vec"])]
    assert run.applied == 2
    assert run.detail is None

    chunks, vectors = _vec_state(fresh_db)
    assert set(chunks) == {node.id, title_only.id}
    ((seq, text, model_id),) = chunks[node.id]
    assert seq == 0
    assert text == "Photosynthesis sunlight to energy"  # chunking normalizes whitespace
    assert model_id == fake_embedder.model_id
    # A title-only node still embeds: the title is meaningful text.
    assert chunks[title_only.id][0][1] == "Empty"
    assert len(vectors) == 2

    status = {s.name: s for s in projectors.projector_status()}["vec"]
    assert status.available is True
    assert status.rows == 2
    assert status.pending_events == 0


def test_update_reembeds_and_replaces_chunks(fresh_db, fake_embedder):
    node = service.create_node(type="note", title="T", content="v1 body", principal=owner())
    projectors.run_projectors(names=["vec"])
    service.update_node(node.id, content="v2 body", principal=owner())
    projectors.run_projectors(names=["vec"])

    chunks, vectors = _vec_state(fresh_db)
    ((_, text, _),) = chunks[node.id]
    assert text == "T v2 body"
    assert len(vectors) == 1  # replaced, not duplicated


def test_undone_create_drops_chunks_and_vectors(fresh_db, fake_embedder):
    keep = service.create_node(type="note", title="keep", content="xylem", principal=owner())
    service.create_node(type="note", title="gone", content="phloem", principal=owner())
    projectors.run_projectors(names=["vec"])

    service.undo(principal=owner())  # reverses the create of `gone`
    projectors.run_projectors(names=["vec"])
    chunks, vectors = _vec_state(fresh_db)
    assert set(chunks) == {keep.id}
    assert len(vectors) == 1


def test_rebuild_replays_to_identical_vectors(fresh_db, fake_embedder):
    a = service.create_node(type="note", title="A", content="alpha", principal=owner())
    service.update_node(a.id, content="alpha v2", principal=owner())
    service.create_node(type="claim", title="B", content="beta", principal=owner())
    projectors.run_projectors(names=["vec"])
    before = _vec_state(fresh_db)

    run = projectors.rebuild_projector("vec")
    assert run.from_seq == 0
    assert run.applied == 3
    assert _vec_state(fresh_db) == before


def test_rebuild_equivalence_through_undo_events(fresh_db, fake_embedder):
    a = service.create_node(type="note", title="A", content="v1", principal=owner())
    service.update_node(a.id, content="v2", principal=owner())
    service.undo(principal=owner())  # back to v1
    service.create_node(type="note", title="B", content="transient", principal=owner())
    service.undo(principal=owner())  # B's create reverted
    projectors.run_projectors(names=["vec"])
    before = _vec_state(fresh_db)

    projectors.rebuild_projector("vec")
    assert _vec_state(fresh_db) == before
    chunks, _ = _vec_state(fresh_db)
    assert set(chunks) == {a.id}
    assert chunks[a.id][0][1] == "A v1"


def test_long_content_produces_overlapping_chunks(fresh_db, fake_embedder):
    words = " ".join(f"w{i}" for i in range(1200))
    node = service.create_node(type="note", title=None, content=words, principal=owner())
    projectors.run_projectors(names=["vec"])
    chunks, vectors = _vec_state(fresh_db)
    assert len(chunks[node.id]) == 3
    assert [seq for seq, _, _ in chunks[node.id]] == [0, 1, 2]
    assert len(vectors) == 3


def test_vectors_of_the_wrong_width_are_what_the_dimension_guard_prevents(fresh_db):
    """`node_vec` is fixed at 384, so a wider model cannot be stored at all.

    `set_provider` is the configuration seam and skips the check that
    `embeddings._resolve_default` applies to a `NODUM_EMBED_MODEL` override —
    which makes this the failure that guard exists to keep out of a run.
    """

    class WideEmbedder:
        model_id = "test-768-dim"
        dimensions = 768

        def embed(self, texts):
            return [[0.1] * 768 for _ in texts]

    embeddings.set_provider(WideEmbedder())
    service.create_node(type="note", title="T", content="body", principal=owner())
    with pytest.raises(sqlite3.OperationalError, match="Expected 384 dimensions"):
        projectors.run_projectors(names=["vec"])


def test_unavailable_provider_is_a_noop_not_a_crash(fresh_db):
    # No fake embedder: the autouse fixture forces the provider unavailable.
    service.create_node(type="note", title="T", content="body", principal=owner())
    (run,) = [r for r in projectors.run_projectors(names=["vec"])]
    assert run.applied == 0
    assert run.from_seq == run.to_seq == 0  # checkpoint unmoved: backlog waits
    assert run.detail

    status = {s.name: s for s in projectors.projector_status()}["vec"]
    assert status.available is False
    assert status.detail
    assert status.pending_events == 1

    # Rebuilding while unavailable is refused (it would empty the store).
    with pytest.raises(ValueError, match="cannot rebuild"):
        projectors.rebuild_projector("vec")

    # Once a provider appears, the backlog is picked up.
    embeddings.set_provider(HashEmbedder())
    (run,) = [r for r in projectors.run_projectors(names=["vec"])]
    assert run.applied == 1
    chunks, _ = _vec_state(fresh_db)
    assert len(chunks) == 1
