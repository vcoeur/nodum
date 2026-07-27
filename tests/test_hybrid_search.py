"""Hybrid search: RRF fusion of BM25 + vector, then graph expansion."""

from __future__ import annotations

from helpers import agent, owner

from nodum import search, service


def test_vector_only_hit_surfaces_with_vector_signal(fresh_db, fake_embedder):
    target = service.create_node(
        type="note",
        title="Target",
        content="xylem vessels carry water",
        principal=owner(),
    )
    vector_only = service.create_node(
        type="note", title="Unrelated", content="quantum entanglement qubits", principal=owner()
    )

    result = search.search("xylem", k=5, principal=owner())
    by_id = {hit.node_id: hit for hit in result.hits}
    assert result.hits[0].node_id == target.id
    assert "bm25" in by_id[target.id].signals
    # The disjoint node still surfaces: the ANN list is k-deep with no
    # similarity threshold, so it fuses with a tiny contribution from its
    # (poor) vector rank.
    assert set(by_id[vector_only.id].signals) == {"vector"}
    assert by_id[target.id].score > by_id[vector_only.id].score
    # The vector list builds its own row shape, so it has to carry the space
    # too — a hit reached through it is scanned in the same result list.
    assert by_id[vector_only.id].space_id == vector_only.space_id == "main"


def test_fused_beats_single_signal_where_lists_agree(fresh_db, fake_embedder):
    # "both" shares the query terms and little else → top of both lists.
    # "weak" matches BM25 too (terms are ANDed) but is diluted with disjoint
    # vocabulary → second in both lists. "disjoint" shares no token with the
    # query → vector-only hit via the threshold-free ANN list.
    both = service.create_node(
        type="note", title="Both", content="xylem vessels transport water upward", principal=owner()
    )
    weak = service.create_node(
        type="note",
        title="Weak",
        content="xylem vessels quantum entanglement qubit photon flux",
        principal=owner(),
    )
    disjoint = service.create_node(
        type="note",
        title="Disjoint",
        content="avocado toast sunrise",
        principal=owner(),
    )

    result = search.search("xylem vessels", k=5, principal=owner())
    by_id = {hit.node_id: hit for hit in result.hits}
    assert result.hits[0].node_id == both.id
    assert set(result.hits[0].signals) == {"bm25", "vector"}
    assert set(by_id[disjoint.id].signals) == {"vector"}
    # Agreement across signals beats any single-signal hit on the fused score…
    assert result.hits[0].score > by_id[disjoint.id].score
    # …and the fused order keeps the stronger BM25+vector match ahead.
    assert by_id[both.id].score > by_id[weak.id].score
    # The breakdown sums to the fused score exactly.
    assert result.hits[0].score == sum(result.hits[0].signals.values())


def test_rrf_contribution_uses_rank_not_raw_score(fresh_db, fake_embedder):
    service.create_node(type="note", title="T", content="lichen symbiosis", principal=owner())
    result = search.search("lichen", k=5, principal=owner())
    (hit,) = result.hits
    # Rank 1 in both lists: 1/(60+1) per signal.
    assert hit.signals["bm25"] == 1 / 61
    assert hit.signals["vector"] == 1 / 61
    assert hit.score == 2 / 61


def test_vector_signal_respects_filters(fresh_db, fake_embedder):
    note = service.create_node(
        type="note",
        title="N",
        content="mycelium networks",
        principal=owner(),
    )
    service.create_node(type="concept", title="C", content="mycelium concept", principal=owner())
    result = search.search("mycelium", type="note", k=5, principal=owner())
    assert [hit.node_id for hit in result.hits] == [note.id]

    proposed = service.create_node(
        type="note", title="P", content="mycelium draft", principal=agent("x")
    )
    result = search.search("mycelium", k=10, principal=owner())
    assert proposed.id not in {hit.node_id for hit in result.hits}


def test_graph_expansion_applies_after_fusion(fresh_db, fake_embedder):
    target = service.create_node(
        type="note",
        title="T",
        content="xylem carries sap",
        principal=owner(),
    )
    service.create_node(type="note", title="F", content="unrelated filler text", principal=owner())
    neighbor = service.create_node(
        type="concept",
        title="N",
        content="vascular plants",
        principal=owner(),
    )
    service.create_edge(target.id, neighbor.id, "relates_to", principal=owner())

    result = search.search("xylem", k=1, expand=True, principal=owner())
    # k=1 keeps only the fused winner; the neighbor is not a direct match, so
    # it arrives purely through post-fusion graph expansion.
    assert [hit.node_id for hit in result.hits] == [target.id, neighbor.id]
    assert set(result.hits[0].signals) >= {"bm25"}
    assert result.hits[1].signals == {"graph": 0.5}  # relates_to weight × 1.0


def test_degrades_to_bm25_when_no_provider(fresh_db):
    # No fake embedder: search must not crash and stays BM25-only.
    target = service.create_node(type="note", title="T", content="xylem vessels", principal=owner())
    result = search.search("xylem", principal=owner())
    assert [hit.node_id for hit in result.hits] == [target.id]
    assert set(result.hits[0].signals) == {"bm25"}
