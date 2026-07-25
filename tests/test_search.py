"""BM25 keyword search over the FTS5 projector index."""

from __future__ import annotations

import pytest
from helpers import OWNER_ACTOR, agent, owner

from nodum import db, search, service


def test_search_returns_ranked_hits_with_snippet_and_signals(fresh_db):
    target = service.create_node(
        type="note",
        title="Photosynthesis",
        content="Photosynthesis converts sunlight into chemical energy in plants.",
        principal=owner(),
    )
    service.create_node(
        type="note",
        title="Quantum",
        content="Entanglement and qubits.",
        principal=owner(),
    )

    result = search.search("photosynthesis", principal=owner())
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
    service.create_node(
        type="note",
        title="T",
        content="xylem vessels carry water",
        principal=owner(),
    )
    assert [hit.title for hit in search.search("xylem", principal=owner()).hits] == ["T"]


def test_bm25_ranks_the_stronger_match_first(fresh_db):
    service.create_node(
        type="note",
        title="mentioned",
        content="chromatography appears once here among other text",
        principal=owner(),
    )
    strong = service.create_node(
        type="note",
        title="Chromatography",
        content="chromatography separates mixtures",
        principal=owner(),
    )
    result = search.search("chromatography", principal=owner())
    assert [hit.node_id for hit in result.hits][:1] == [strong.id]
    assert result.hits[0].score >= result.hits[1].score


def test_multiple_terms_are_anded(fresh_db):
    both = service.create_node(
        type="note",
        title="both",
        content="alpha beta gamma",
        principal=owner(),
    )
    service.create_node(type="note", title="one", content="alpha only", principal=owner())
    result = search.search("beta gamma", principal=owner())
    assert [hit.node_id for hit in result.hits] == [both.id]


def test_query_punctuation_and_operators_are_literal_terms(fresh_db):
    """Every token is quoted into a required term — never FTS5 syntax.

    Unquoted, `OR` would widen the query into a disjunction and `c++` (or the
    stray quote) would be a syntax error.
    """
    target = service.create_node(type="note", title="T", content="c++ pitfalls", principal=owner())
    service.create_node(
        type="note",
        title="Other",
        content="OR operators everywhere",
        principal=owner(),
    )

    # `OR` is ANDed in as a term the target lacks, so the query matches nothing…
    assert search.search('c++ "pitfalls" OR', principal=owner()).hits == []
    # …while the same punctuation without it still finds the node.
    assert [hit.node_id for hit in search.search("c++ pitfalls", principal=owner()).hits] == [
        target.id
    ]
    # And a bare `OR` searches for the word, never joining the two documents.
    assert [hit.title for hit in search.search("OR", principal=owner()).hits] == ["Other"]


def test_proposed_and_archived_nodes_are_filtered_by_default(fresh_db):
    proposed = service.create_node(
        type="note", title="P", content="zebra stripes", principal=agent("x")
    )
    archived = service.create_node(
        type="note",
        title="A",
        content="zebra hooves",
        principal=owner(),
    )
    service.transition(archived.id, "archive", principal=owner())
    active = service.create_node(type="note", title="N", content="zebra mane", principal=owner())

    result = search.search("zebra", principal=owner())
    assert [hit.node_id for hit in result.hits] == [active.id]

    everything = search.search("zebra", state=None, principal=owner())
    assert {hit.node_id for hit in everything.hits} == {proposed.id, archived.id, active.id}

    only_proposed = search.search("zebra", state="proposed", principal=owner())
    assert [hit.node_id for hit in only_proposed.hits] == [proposed.id]


def test_type_filter(fresh_db):
    note = service.create_node(
        type="note",
        title="N",
        content="mycelium networks",
        principal=owner(),
    )
    service.create_node(type="concept", title="C", content="mycelium concept", principal=owner())
    result = search.search("mycelium", type="note", principal=owner())
    assert [hit.node_id for hit in result.hits] == [note.id]
    with pytest.raises(ValueError, match="unknown node type"):
        search.search("mycelium", type="nope", principal=owner())


def test_created_by_filter(fresh_db):
    human = service.create_node(
        type="note",
        title="H",
        content="anaerobic digestion",
        principal=owner(),
    )
    service.create_node(
        type="note", title="B", content="anaerobic fermentation", principal=agent("x")
    )
    mine = search.search("anaerobic", created_by=OWNER_ACTOR, principal=owner())
    assert [hit.node_id for hit in mine.hits] == [human.id]


# ── Date-range filters ────────────────────────────────────────────────────────


def _backdate(db_path, node_id, timestamp):
    """Move a node's ``created_at`` so date filters are testable without sleeping.

    ``datetime('now')`` has one-second resolution, so two nodes written by a
    test share a timestamp; the filter compares against `nodes.created_at`, so
    rewriting it is exactly what a node created earlier would look like.
    """
    conn = db.connect(db_path)
    try:
        conn.execute("UPDATE nodes SET created_at = ? WHERE id = ?", (timestamp, node_id))
        conn.commit()
    finally:
        conn.close()


def test_created_after_and_before_split_the_corpus(fresh_db):
    old = service.create_node(
        type="note", title="old", content="mycorrhiza networks", principal=owner()
    )
    recent = service.create_node(
        type="note", title="recent", content="mycorrhiza symbiosis", principal=owner()
    )
    _backdate(fresh_db, old.id, "2020-01-01 00:00:00")
    cut = "2021-01-01 00:00:00"

    assert {hit.node_id for hit in search.search("mycorrhiza", principal=owner()).hits} == {
        old.id,
        recent.id,
    }
    assert [
        hit.node_id
        for hit in search.search("mycorrhiza", created_after=cut, principal=owner()).hits
    ] == [recent.id]
    assert [
        hit.node_id
        for hit in search.search("mycorrhiza", created_before=cut, principal=owner()).hits
    ] == [old.id]
    # Both bounds together select the window between them.
    windowed = search.search(
        "mycorrhiza",
        created_after="2019-01-01 00:00:00",
        created_before=cut,
        principal=owner(),
    )
    assert [hit.node_id for hit in windowed.hits] == [old.id]


def test_date_bounds_are_exclusive(fresh_db):
    """`>` / `<`, not `>=` / `<=`: a node's own timestamp excludes it from both."""
    node = service.create_node(
        type="note",
        title="T",
        content="tardigrade cryptobiosis",
        principal=owner(),
    )
    stamp = service.get_node(node.id, principal=owner()).created_at

    assert search.search("tardigrade", created_after=stamp, principal=owner()).hits == []
    assert search.search("tardigrade", created_before=stamp, principal=owner()).hits == []
    everything = search.search("tardigrade", created_after="2000-01-01 00:00:00", principal=owner())
    assert [hit.node_id for hit in everything.hits] == [node.id]


def test_date_filters_apply_to_the_vector_signal_too(fresh_db, fake_embedder):
    """Both ranked lists share one filter set, so a filtered-out node cannot fuse."""
    node = service.create_node(
        type="note",
        title="T",
        content="tardigrade cryptobiosis",
        principal=owner(),
    )
    hit = search.search("tardigrade cryptobiosis", principal=owner()).hits[0]
    assert set(hit.signals) == {"bm25", "vector"}  # it is in both lists unfiltered

    assert hit.node_id == node.id
    assert search.search(
        "tardigrade cryptobiosis",
        created_before="2000-01-01 00:00:00",
        principal=owner(),
    )


def test_k_limits_hits(fresh_db):
    for i in range(5):
        service.create_node(
            type="note",
            title=f"n{i}",
            content="lichen symbiosis",
            principal=owner(),
        )
    assert len(search.search("lichen", k=3, principal=owner()).hits) == 3


def test_empty_query_raises(fresh_db):
    with pytest.raises(ValueError, match="at least one term"):
        search.search("   ", principal=owner())


def test_no_match_returns_no_hits(fresh_db):
    service.create_node(type="note", title="T", content="something", principal=owner())
    assert search.search("nonexistentterm", principal=owner()).hits == []
