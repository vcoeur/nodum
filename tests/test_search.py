"""BM25 keyword search over the FTS5 projector index."""

from __future__ import annotations

import pytest

from nodum import search, service


def test_search_returns_ranked_hits_with_snippet_and_signals(fresh_db):
    target = service.create_node(
        type="note",
        title="Photosynthesis",
        content="Photosynthesis converts sunlight into chemical energy in plants.",
    )
    service.create_node(type="note", title="Quantum", content="Entanglement and qubits.")

    result = search.search("photosynthesis")
    assert result.query == "photosynthesis"
    assert result.k == 10
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.node_id == target.id
    assert hit.type == "note"
    assert hit.title == "Photosynthesis"
    assert "**Photosynthesis**" in hit.snippet  # matched terms are marked
    assert hit.score > 0
    assert hit.signals == {"bm25": hit.score}


def test_search_catches_the_projector_up_implicitly(fresh_db):
    # No explicit projector run — search syncs the index itself.
    service.create_node(type="note", title="T", content="xylem vessels carry water")
    assert [hit.title for hit in search.search("xylem").hits] == ["T"]


def test_bm25_ranks_the_stronger_match_first(fresh_db):
    service.create_node(
        type="note", title="mentioned", content="chromatography appears once here among other text"
    )
    strong = service.create_node(
        type="note", title="Chromatography", content="chromatography separates mixtures"
    )
    result = search.search("chromatography")
    assert [hit.node_id for hit in result.hits][:1] == [strong.id]
    assert result.hits[0].score >= result.hits[1].score


def test_multiple_terms_are_anded(fresh_db):
    both = service.create_node(type="note", title="both", content="alpha beta gamma")
    service.create_node(type="note", title="one", content="alpha only")
    result = search.search("beta gamma")
    assert [hit.node_id for hit in result.hits] == [both.id]


def test_query_punctuation_cannot_break_the_match(fresh_db):
    service.create_node(type="note", title="T", content="c++ pitfalls")
    result = search.search('c++ "pitfalls" OR')
    assert isinstance(result.hits, list)  # no OperationalError; quoting is inert


def test_proposed_and_archived_nodes_are_filtered_by_default(fresh_db):
    proposed = service.create_node(type="note", title="P", content="zebra stripes", actor="agent:x")
    archived = service.create_node(type="note", title="A", content="zebra hooves")
    service.transition(archived.id, "archive")
    active = service.create_node(type="note", title="N", content="zebra mane")

    result = search.search("zebra")
    assert [hit.node_id for hit in result.hits] == [active.id]

    everything = search.search("zebra", state=None)
    assert {hit.node_id for hit in everything.hits} == {proposed.id, archived.id, active.id}

    only_proposed = search.search("zebra", state="proposed")
    assert [hit.node_id for hit in only_proposed.hits] == [proposed.id]


def test_type_filter(fresh_db):
    note = service.create_node(type="note", title="N", content="mycelium networks")
    service.create_node(type="concept", title="C", content="mycelium concept")
    result = search.search("mycelium", type="note")
    assert [hit.node_id for hit in result.hits] == [note.id]
    with pytest.raises(ValueError, match="unknown node type"):
        search.search("mycelium", type="nope")


def test_k_limits_hits(fresh_db):
    for i in range(5):
        service.create_node(type="note", title=f"n{i}", content="lichen symbiosis")
    assert len(search.search("lichen", k=3).hits) == 3


def test_empty_query_raises(fresh_db):
    with pytest.raises(ValueError, match="at least one term"):
        search.search("   ")


def test_no_match_returns_no_hits(fresh_db):
    service.create_node(type="note", title="T", content="something")
    assert search.search("nonexistentterm").hits == []
