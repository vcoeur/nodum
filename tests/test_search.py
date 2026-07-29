"""BM25 keyword search over the FTS5 projector index."""

from __future__ import annotations

import pytest
from helpers import OWNER_ACTOR, agent, owner

from nodum import db, projectors, search, service


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


def test_query_punctuation_and_operators_are_literal_terms(fresh_db):
    """Every token is quoted into a term — the raw string never becomes syntax.

    Each of the strings below is a **syntax error** when handed to FTS5
    unquoted (verified against the engine: `OR`, `AND OR NOT NEAR`, `c++` and a
    lone `"` all raise `fts5: syntax error` or `unterminated string`), so a
    query reaching MATCH unquoted would be a 500 rather than a result — and
    `OR`, were it read as an operator, would silently widen the query into a
    disjunction over terms nobody typed.
    """
    target = service.create_node(type="note", title="T", content="c++ pitfalls", principal=owner())
    service.create_node(
        type="note",
        title="Other",
        content="OR operators everywhere",
        principal=owner(),
    )

    # A bare `OR` is the *word*: it finds the node that says it, and raises
    # nothing. As an operator it is a syntax error with no left-hand side.
    assert [hit.title for hit in search.search("OR", principal=owner()).hits] == ["Other"]
    # Punctuation that is FTS5 syntax is a term, and still finds the node.
    assert [hit.node_id for hit in search.search("c++ pitfalls", principal=owner()).hits] == [
        target.id
    ]
    # A query that is nothing but operator names and stray quotes answers
    # rather than raising.
    for hostile in ("AND OR NOT NEAR", '"', "NEAR(", "title:zebra", 'c++ "pitfalls" OR', "("):
        search.search(hostile, principal=owner())


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


# ── The quorum ────────────────────────────────────────────────────────────────
# The rule under test: a node is a keyword candidate when the query terms it
# carries are worth at least half the query's inverse-document-frequency
# weight. Two properties of the corpus decide whether these tests measure that
# rule or something else, so both are made explicit rather than assumed:
#
#   * **Size.** A term is dropped as ubiquitous above half the indexed rows, so
#     on a three-node graph "the" is dropped and the query that reaches FTS5
#     bears no relation to the one a real graph would compile. Every test below
#     runs against `_seed_prose`'s 36 notes plus its own.
#   * **Function words.** They have to occur in most rows, or their weight is
#     the same as a topic word's and the quorum has nothing to discount.
#
# `_assert_quorum_applies` closes the loop: a test that claims to exercise the
# quorum asserts first that its fixture actually compiles one. Without it a
# fixture that quietly fell back to a bare disjunction would keep passing while
# testing FTS5 instead of this module.


#: Ordinary prose, heavy on the small words. Every sentence carries "the", "a",
#: "is", "to", "that", "as", "how", "does", "work" and "let"; none carries the
#: topic vocabulary the tests query for. Every third note also says "framework",
#: which makes it a *moderately* common term — common enough to weigh less than
#: a rare one, rare enough to survive the ubiquity cut.
_PROSE = (
    "This is a note about how the work gets done, and it does not say much"
    " that a reader could not work out for themselves as they go.",
    "The point here is to let the reader see how a thing is put together,"
    " which is work that does not fit anywhere else as such.",
    "It is worth writing down how this does and does not work, so that the"
    " next reader is let off the work of finding it out as I did.",
)


def _seed_prose(count: int = 36) -> None:
    """Fill the graph with prose notes carrying none of the query vocabulary."""
    principal = owner()
    for index in range(count):
        extra = " The framework is mentioned here." if index % 3 == 0 else ""
        service.create_node(
            type="note",
            title=f"Ordinary note {index}",
            content=_PROSE[index % len(_PROSE)] + extra,
            principal=principal,
        )


def _assert_quorum_applies(db_path, query: str) -> None:
    """Fail unless this corpus and query actually compile a quorum restriction.

    The guard against the failure mode this repo keeps shipping: a fixture too
    small (or a query too short) compiles to a bare disjunction, and the
    assertion that follows then measures FTS5 rather than the rule.
    """
    projectors.run_projectors(names=["fts"], path=db_path)
    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        plan = search._compile_match(conn, search._query_terms(query))
    finally:
        conn.close()
    assert plan.clause, f"no quorum compiled for {query!r} — the fixture is too small"


def _ids(result) -> set[str]:
    return {hit.node_id for hit in result.hits}


def test_a_question_shaped_query_is_not_zeroed_by_its_function_words(fresh_db):
    """The carried finding: FTS5 ANDs the terms, so a question matched nothing.

    Eight of this question's words are ordinary English the graph holds
    everywhere and the answering sentence does not; under the conjunctive rule
    that emptied the result set, and on the default install — no embedding
    provider — the vector signal is not there to carry it either.

    The question also names *Kafka*, which the graph knows and the answering
    sentence does not say. That word is the difference between this test and
    one the common-term drop alone would satisfy: it is rare, so it survives
    into the compiled query and has to be *outvoted* rather than discarded.
    Requiring every surviving term still answers nothing here.
    """
    _seed_prose()
    for index in range(3):
        service.create_node(
            type="note",
            title=f"Kafka operations {index}",
            content="Notes on running Kafka, which is a thing that does not work by itself.",
            principal=owner(),
        )
    target = service.create_node(
        type="claim",
        title="Compaction and state stores",
        content=(
            "A compacted topic can act as a durable state store because"
            " replaying it rebuilds the latest value for every key."
        ),
        principal=owner(),
    )
    query = "How does compaction let a Kafka topic work as a state store?"
    _assert_quorum_applies(fresh_db, query)
    assert [hit.node_id for hit in search.search(query, principal=owner()).hits] == [target.id]


def test_a_term_the_graph_has_never_seen_does_not_empty_the_result(fresh_db):
    """The E3 prerequisite: one hallucinated term must not zero the query.

    A model rewriting a query invents terms — the design pass measured this one
    producing `"once-once semantics"` — and under the conjunctive rule a single
    invented word made the whole search answer nothing. A term the corpus has
    never seen discriminates nothing, so it is dropped rather than required.
    """
    _seed_prose()
    target = service.create_node(
        type="claim",
        title="Roman concrete in seawater",
        content="Seawater percolating through pozzolana concrete grows interlocking crystals.",
        principal=owner(),
    )
    honest = search.search("pozzolana seawater crystals", principal=owner())
    invented = search.search("pozzolana seawater crystals concretoid", principal=owner())
    assert [hit.node_id for hit in honest.hits] == [target.id]
    assert [hit.node_id for hit in invented.hits] == [target.id]


def test_a_two_term_query_requires_the_rarer_term(fresh_db):
    """Two terms: half the weight is the rarer one, so it is the one required.

    This is the commonest query shape, and the rule has to say something
    definite about it. It fails in both directions: under the old conjunction
    the rare-only node is not a hit at all, and under a bare disjunction the
    common-only node is.
    """
    _seed_prose()
    rare_only = service.create_node(
        type="note", title="Rare", content="Pozzolana reacts with lime.", principal=owner()
    )
    common_only = service.create_node(
        type="note", title="Common", content="The framework is described.", principal=owner()
    )
    both = service.create_node(
        type="note", title="Both", content="The pozzolana framework is here.", principal=owner()
    )
    query = "pozzolana framework"
    _assert_quorum_applies(fresh_db, query)
    found = _ids(search.search(query, k=20, principal=owner()))
    assert both.id in found
    assert rare_only.id in found  # fails under the conjunctive rule
    assert common_only.id not in found  # fails under a bare OR


def test_a_node_sharing_only_a_common_word_is_not_a_hit(fresh_db):
    """The precision half — the reason this is a quorum and not a disjunction.

    Nothing about the conjunctive rule made this fail, so this test does not
    fail without the change: it fails if the change is ever *simplified* into
    the bare OR that the quorum was chosen over.
    """
    _seed_prose()
    target = service.create_node(
        type="claim",
        title="Compaction and state stores",
        content="A compacted topic can act as a durable state store.",
        principal=owner(),
    )
    decoy = service.create_node(
        type="note",
        title="Decoy",
        content="The state of the framework is a matter of opinion.",
        principal=owner(),
    )
    query = "compacted topic state store"
    _assert_quorum_applies(fresh_db, query)
    found = _ids(search.search(query, k=20, principal=owner()))
    assert target.id in found
    assert decoy.id not in found


def test_a_repeated_term_is_weighed_once(fresh_db):
    """A word said twice must not buy twice the say in the quorum.

    Not a hypothetical: the design pass measured the local model rewriting a
    query as `["semantics", "once-once semantics", "Kafka", …]`, which
    whitespace-splits to *semantics* twice. Without the fold the repeated term
    carries two shares of the query's weight, and here that is enough to let a
    node holding nothing but the common word over the bar — the fixture is
    sized so that two shares of `framework` (2 × 1.14) just clear one share of
    `pozzolana` (2.23), which is the only arrangement in which the fold is
    observable at all.
    """
    _seed_prose()
    for index in range(4):
        service.create_node(
            type="note",
            title=f"Rare {index}",
            content="Pozzolana reacts with lime.",
            principal=owner(),
        )
    common_only = service.create_node(
        type="note", title="Common", content="The framework is described.", principal=owner()
    )
    once = _ids(search.search("pozzolana framework", k=30, principal=owner()))
    twice = _ids(search.search("pozzolana Framework framework", k=30, principal=owner()))
    assert common_only.id not in once
    assert once == twice


def test_a_one_term_query_compiles_no_quorum(fresh_db):
    """Matching the only term *is* the quorum, so the statement stays as it was.

    Asserted on the compiled plan rather than on results, because a redundant
    restriction would be invisible in the hits and visible only in the cost.
    """
    _seed_prose()
    service.create_node(
        type="claim",
        title="Roman concrete",
        content="Pozzolana and lime set under seawater.",
        principal=owner(),
    )
    projectors.run_projectors(names=["fts"], path=fresh_db)
    conn = db.connect(fresh_db)
    try:
        db.init_db(conn)
        single = search._compile_match(conn, search._query_terms("framework"))
        several = search._compile_match(conn, search._query_terms("pozzolana lime seawater"))
    finally:
        conn.close()
    assert (single.cte, single.clause) == ("", "")
    assert several.cte and several.clause


def test_a_term_in_most_of_the_graph_is_dropped_from_the_match_expression(fresh_db):
    """The cost rule: an everywhere-term is not worth a doclist-sized walk.

    Its weight is near zero either way, so this changes what search *costs*
    rather than what it returns — which is why it is asserted on the expression
    and not on the hits.
    """
    _seed_prose()
    projectors.run_projectors(names=["fts"], path=fresh_db)
    conn = db.connect(fresh_db)
    try:
        db.init_db(conn)
        plan = search._compile_match(conn, search._query_terms("the framework"))
    finally:
        conn.close()
    assert '"the"' not in plan.match
    assert '"framework"' in plan.match


def test_bm25_length_normalisation_offsets_a_long_document(fresh_db):
    """Finding 2, pinned: the sentence beats the book that contains it.

    The carried note blamed a source node outranking a claim on length
    normalisation failing to offset a whole document's text. It does offset it —
    measured here, and measured directly in the design pass at 112 chars versus
    60 KB with term coverage held fixed. This test exists so that a later
    change to `_BM25_WEIGHTS` cannot quietly make the recorded explanation true.
    """
    _seed_prose()
    sentence = "A compacted topic can act as a durable state store."
    # One title, so the 5× title weight cannot be what decides this: the two
    # nodes differ in length and in nothing else the ranker sees.
    claim = service.create_node(
        type="claim", title="Compaction and state stores", content=sentence, principal=owner()
    )
    long_body = "\n\n".join([sentence, *(_PROSE[index % len(_PROSE)] for index in range(400))])
    source = service.create_node(
        type="source", title="Compaction and state stores", content=long_body, principal=owner()
    )
    assert len(long_body) > 20_000  # the size the carried finding names
    ranked = [
        hit.node_id for hit in search.search("compacted topic state store", principal=owner()).hits
    ]
    assert claim.id in ranked and source.id in ranked
    assert ranked.index(claim.id) < ranked.index(source.id)
