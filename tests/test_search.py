"""BM25 keyword search over the FTS5 projector index."""

from __future__ import annotations

import asyncio
import re
import threading

import httpx
import pytest
from helpers import OWNER_ACTOR, agent, owner

from nodum import db, http_api, projectors, search, service


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


def _compile(db_path, query: str):
    """Compile one query the way a default `search()` call would.

    The matcher counts document frequencies through the same filters the ranked
    query applies, so a plan compiled with no filters at all is not the plan
    search runs — hence the default filter set here rather than an empty one.
    """
    projectors.run_projectors(names=["fts"], path=db_path)
    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        filters, params = search._node_filters(
            "active", None, None, None, None, False, None, owner()
        )
        return search._compile_match(
            conn, search._query_terms(query), filters=filters, filter_params=params
        )
    finally:
        conn.close()


def _assert_quorum_applies(db_path, query: str) -> None:
    """Fail unless this corpus and query actually compile a quorum restriction.

    The guard against the failure mode this repo keeps shipping: a fixture too
    small (or a query too short) compiles to a bare disjunction, and the
    assertion that follows then measures FTS5 rather than the rule.
    """
    plan = _compile(db_path, query)
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
    single = _compile(fresh_db, "framework")
    several = _compile(fresh_db, "pozzolana lime seawater")
    assert (single.cte, single.clause) == ("", "")
    assert several.cte and several.clause


def test_a_term_in_most_of_the_graph_is_dropped_from_the_match_expression(fresh_db):
    """The cost rule: an everywhere-term is not worth a doclist-sized walk.

    Its weight is near zero either way, so this changes what search *costs*
    rather than what it returns — which is why it is asserted on the expression
    and not on the hits.

    The term is `reader`, not `the`: `the` is on the function-word list, so it
    would be dropped whatever the document frequency said and this test would
    pass without exercising the ceiling at all. `reader` is an ordinary word
    that happens to be in every one of these notes, which is the case the
    ceiling exists for.
    """
    _seed_prose()
    assert not search._is_function_word('"reader"'), "the fixture term must reach the df ceiling"
    plan = _compile(fresh_db, "reader framework")
    assert '"reader"' not in plan.match
    assert '"framework"' in plan.match


# ── The quorum on a young graph ───────────────────────────────────────────────
# Everything above runs against `_seed_prose`, where function words sit in
# nearly every row and the df ceiling therefore drops them. That is what a
# *prose-heavy* graph looks like. A young graph does not look like that: it is
# mostly short factual sentences, and "what", "does" and "how" then sit in a
# small minority of rows — under the ceiling, with more inverse-document-
# frequency weight than half the topic vocabulary. Document frequency cannot
# tell a function word from a term on a graph that size, which is why the
# fixture below is claim-heavy and why these tests exist separately.

#: Short factual sentences, deliberately telegraphic: the shape of a graph
#: nobody has written prose into yet. None of them says "what", "does" or "how".
_CLAIMS = (
    "Log compaction retains the newest value for each key.",
    "A tombstone record removes a key after the retention period.",
    "Rebalancing reassigns partitions across the members of a group.",
    "The group coordinator drives the join and sync phases.",
    "Idempotent producers deduplicate retries by sequence number.",
    "Transactions commit records across partitions atomically.",
    "Raft elects one leader per term.",
    "An entry is committed once a majority of servers store it.",
    "Randomised election timeouts make split votes rare.",
    "Joint consensus covers a membership change safely.",
    "Vector clocks stamp events with a per-process counter.",
    "Version vectors compare replicas of one data item.",
    "Consistent hashing moves only the keys next to a departing node.",
    "Virtual nodes even out the imbalance of a hash ring.",
    "Two-phase commit blocks when the coordinator fails.",
    "PACELC extends CAP with the case of a healthy network.",
    "A watermark asserts that no earlier event will arrive.",
    "Event-time windows replay reproducibly.",
    "Backpressure slows an upstream stage instead of buffering.",
    "Checkpoint barriers snapshot the state of an operator.",
    "Write-ahead logging appends frames before a commit.",
    "A checkpoint copies frames back into the main database.",
    "Vacuum reclaims the space held by dead row versions.",
    "A visibility map allows a scan to skip a clean page.",
    "The porter tokenizer stems English words to a common root.",
    "BM25 weights each indexed column separately.",
    "Length normalisation penalises a long document.",
    "Chunking splits a document before it is embedded.",
    "Cosine similarity ignores the magnitude of a vector.",
    "A sourdough starter needs feeding once a day.",
    "Espresso extraction rises as the grind gets finer.",
    "Oil paint hardens by oxidation rather than evaporation.",
)

#: The only rows carrying the question words — eight of forty-four, which is
#: well under the half-the-graph ceiling, so `what`, `does` and `how` reach the
#: compiled query carrying more weight than `compaction` does.
_QUESTION_PROSE = (
    "What I write here is what the thing does and how it behaves, because that"
    " is what I forget first.",
    "What follows does not explain how any of it is implemented; it is the"
    " version I would say out loud.",
    "How much of this does anybody need? What I keep is what I have had to look"
    " up more than twice.",
    "What is worth writing down is how a thing fails, and what it does when it"
    " is pushed against its limits.",
    "How this does or does not hold up against a real load is a separate note,"
    " and what is here is only the shape.",
    "What I do against a flaky component is write down how it fails and what"
    " that does to everything downstream.",
    "How does anybody remember what these settings do? What is here is the short version.",
    "What does the reader need? How the thing is put together, and what it is for.",
)


def _seed_claim_graph() -> None:
    """Seed the claim-heavy graph: 32 short claims plus 8 question-word notes."""
    principal = owner()
    for text in _CLAIMS:
        service.create_node(
            type="claim",
            title=" ".join(text.split()[:4]),
            content=text,
            principal=principal,
        )
    for index, text in enumerate(_QUESTION_PROSE):
        service.create_node(
            type="note", title=f"Loose notes {index}", content=text, principal=principal
        )


def _document_frequencies(db_path, *words: str) -> dict[str, int]:
    """Index rows carrying each word, and the total — the numbers the rule uses."""
    projectors.run_projectors(names=["fts"], path=db_path)
    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        counts = {
            word: conn.execute(
                "SELECT count(*) AS n FROM node_fts WHERE node_fts MATCH ?", (f'"{word}"',)
            ).fetchone()["n"]
            for word in words
        }
        counts["*rows*"] = conn.execute("SELECT count(*) AS n FROM node_fts").fetchone()["n"]
    finally:
        conn.close()
    return counts


def _assert_function_words_survive_the_df_ceiling(db_path, *words: str) -> None:
    """Fail unless the fixture reaches the branch these tests are about.

    The df ceiling drops a term in more than half the rows. If the fixture put
    the question words in more than half of them, the *old* cost rule would
    already be dropping them and the assertion below would pass without the
    fix — a fixture that cannot reach its branch, which is the shape this
    project keeps shipping. So: assert the question words are rare enough to
    survive, and heavy enough to matter.
    """
    counts = _document_frequencies(db_path, *words)
    rows = counts.pop("*rows*")
    ceiling = max(1, int(rows * search._COMMON_TERM_DF_FRACTION))
    for word, count in counts.items():
        assert 0 < count <= ceiling, (
            f"{word!r} is in {count} of {rows} rows (ceiling {ceiling}) — this fixture"
            " tests the common-term drop, not the rule under test"
        )


def test_the_node_carrying_the_querys_only_rare_term_is_excluded_by_its_question_words(fresh_db):
    """The blocker: the one node that answers the question is voted out by it.

    `min.insync.replicas` is in one row of forty-four and is the only term in
    this query that discriminates anything. The target carries it and nothing
    else the query says; `What`, `does` and `against` are in 9, 8 and 3 rows,
    all under the ceiling, and together they outweigh it. Measured on the real
    47-row graph this fixture is modelled on: the same query answered with
    nothing at all, while dropping the two question words answered with the
    right node.

    What comes back instead is the sharper half of the harm: three prose notes
    that share only the question's phrasing, ranked and returned as if they
    were answers.
    """
    _seed_claim_graph()
    target = service.create_node(
        type="note",
        title="ISR and min.insync.replicas",
        content=(
            "The in-sync replica set is the set of replicas caught up with the leader."
            " With replication factor 3 and min.insync.replicas=2, a partition whose"
            " in-sync set has shrunk to one refuses a write with acks=all."
        ),
        principal=owner(),
    )
    _assert_function_words_survive_the_df_ceiling(fresh_db, "what", "does", "against")
    query = "What does min.insync.replicas protect against?"
    assert [hit.node_id for hit in search.search(query, k=10, principal=owner()).hits] == [
        target.id
    ]


def test_a_question_word_does_not_let_a_draft_displace_the_canonical_claim(fresh_db):
    """The sibling harm: a plausible list with the authoritative note missing.

    Two claims say the same thing; the draft happens to phrase it with `let`,
    which this graph holds in one row and therefore weighs heavier than
    `compaction`. Under the shipped rule the draft clears the bar, the
    canonical claim does not, and the caller gets a ranked list with nothing
    saying the better node was dropped. That is worse than the conjunctive
    rule's silence, which was at least obviously useless.
    """
    _seed_claim_graph()
    canonical = service.create_node(
        type="claim",
        title="Log compaction and state stores",
        content=(
            "A compacted topic retains at least the most recent value for every key,"
            " which is the property that makes it a durable state store."
        ),
        principal=owner(),
    )
    draft = service.create_node(
        type="claim",
        title="Log compaction and state stores (draft)",
        content=(
            "A compacted topic keeps the latest value per key and throws the rest"
            " away, and that is what let it act as a state store in the first place."
        ),
        principal=owner(),
    )
    service.create_node(
        type="note",
        title="Pipeline notes",
        content="A pull-based pipeline asks for the work it can handle, so it never buffers.",
        principal=owner(),
    )
    _assert_function_words_survive_the_df_ceiling(fresh_db, "how", "does", "let", "work")
    query = "How does compaction let a topic work as a state store?"
    found = _ids(search.search(query, k=10, principal=owner()))
    assert canonical.id in found
    assert draft.id in found  # the draft was never the problem


def test_a_question_about_a_word_the_graph_has_never_seen_answers_with_nothing(fresh_db):
    """Question words are not an answer when the graph knows none of the rest.

    The bare term answers with silence, which is correct and obviously useless.
    Wrapping it in a question used to answer with three prose notes that share
    only the phrasing — a plausible ranked list for a word the graph has never
    heard of, and nothing saying so. That is the same conversion this phase
    keeps finding: a visibly empty result turned into a confidently wrong one.

    The fallback that did it is right for the query it was written for — *"what
    is it"*, nothing but function words — and wrong the moment the caller typed
    a content word too. So the fallback to phrasing is gated on the query
    having had no content word at all.
    """
    _seed_claim_graph()
    _assert_function_words_survive_the_df_ceiling(fresh_db, "what", "does", "against")
    assert search.search("zarquon", principal=owner()).hits == []
    assert search.search("What does zarquon protect against?", k=10, principal=owner()).hits == []


def test_a_decorated_question_word_does_not_re_open_the_refusal(fresh_db):
    """One stray character on a function word used to undo the whole rule.

    The refusal reads the query's *content* words, and a word is a function
    word by lookup in `_QUERY_STOPWORDS` after `_bare_word`. So a fold that
    misses a decoration promotes the decorated stopword to a content word —
    and because FTS5 tokenizes the decoration away, that content word arrives
    carrying the *undecorated* word's real document frequency. `known_content`
    is non-empty, `kept` is non-empty, and the early return never fires.

    Measured on this fixture with the pre-fix fold, which trimmed a list of
    fifteen ASCII characters: the bare question refused correctly with **0**
    hits, and `**What** does zarquon protect against?`, `“What” …`, `What- …`,
    `#What …` and `What_ …` each answered with **8** — every one of them
    `Loose notes N`, which is verbatim the harm the test above exists to
    prevent. `… protect against—` answered with **3**, the same three prose
    notes that test's docstring names one by one. Pasted markdown and a smart
    quote are not exotic input, so this is the shape the rule actually meets.

    Asserted over :data:`_SAME_WORD_DECORATIONS` on *every* word of the
    question rather than on a sampled few: the decoration can land anywhere,
    and a fold that handles the trailing one and not the leading one is the
    fold this replaced.
    """
    _seed_claim_graph()
    _assert_function_words_survive_the_df_ceiling(fresh_db, "what", "does", "against")
    words = ["What", "does", "zarquon", "protect", "against?"]
    for decoration in _SAME_WORD_DECORATIONS:
        for position, word in enumerate(words):
            if search._bare_word(f'"{word}"') == "zarquon":
                continue  # the subject is the invented word, not a stopword
            decorated = [*words]
            decorated[position] = decoration.format(word=word.rstrip("?"))
            query = " ".join(decorated)
            assert search.search(query, k=10, principal=owner()).hits == [], query


def test_the_word_a_query_is_about_is_not_dropped_for_being_everywhere(fresh_db):
    """The ubiquity cut is a cost rule, so it is the first thing given up.

    A young graph is usually about one subject, so its subject is in most of
    its rows and lands over the ceiling. Dropped, the query was left with its
    question words, the fallback restored *those*, and *"What is kafka?"*
    answered with every note that says "what" and not one that says "kafka" —
    the exact inverse of the answer, on the most ordinary question there is.

    Saving a doclist walk is worth less than the word the query is about, so
    the ceiling is relaxed before the function-word fallback is reached.
    """
    for index in range(30):
        service.create_node(
            type="claim",
            title=f"Kafka note {index}",
            content=f"Kafka retains the segment while the broker holds offset {index}.",
            principal=owner(),
        )
    for index, text in enumerate(_QUESTION_PROSE):
        service.create_node(
            type="note", title=f"Loose notes {index}", content=text, principal=owner()
        )
    counts = _document_frequencies(fresh_db, "kafka")
    rows = counts.pop("*rows*")
    ceiling = max(1, int(rows * search._COMMON_TERM_DF_FRACTION))
    assert counts["kafka"] > ceiling, "the fixture must put the subject over the ubiquity ceiling"
    assert _ids(search.search("What is kafka?", k=40, principal=owner())) == _ids(
        search.search("kafka", k=40, principal=owner())
    )


#: Twelve questions built around a subject the graph has never heard of, each
#: paired with the invented word (or words) it is built around. Their *other*
#: content words are ordinary English on purpose — `_QUERY_STOPWORDS`
#: deliberately holds no `store`, `work`, `long`, `node` or `first` — so this
#: suite measures the gate as it really is rather than as it reads. The
#: pairing is what lets the test below delete the subject; deriving it by
#: filtering function words instead keeps the invented word (it is a content
#: word) and measures nothing.
_INVENTED_SUBJECT_QUESTIONS = (
    ("What does zarquon protect against?", ("zarquon",)),
    ("How does blorptide work?", ("blorptide",)),
    ("What is a frimble?", ("frimble",)),
    ("Does quixolate replace vantrium?", ("quixolate", "vantrium")),
    ("What are the tradeoffs of snarfblat?", ("snarfblat",)),
    ("How do I configure gribblewatt?", ("gribblewatt",)),
    ("What happens when plerkins fail?", ("plerkins",)),
    ("Is thrumbolt safe to enable?", ("thrumbolt",)),
    ("What does zarquon store?", ("zarquon",)),
    ("How long does blorptide take?", ("blorptide",)),
    ("What zarquon events arrive first?", ("zarquon",)),
    ("Is frimble a node or a space?", ("frimble",)),
)


def test_the_refusal_closes_half_the_invented_subject_shape_and_the_number_is_the_claim(
    fresh_db,
):
    """What the gate tests is *knownness*, not discrimination — measured.

    `AGENTS.md` claims the refusal for "a query whose content words the graph
    has simply never seen", and a reader takes that to cover every question
    built around an invented subject. It does not. The gate fires only when
    **no** content word of the query is in the index, and the questions people
    actually type carry ordinary English nouns and verbs that a technical graph
    really does hold — which `_QUERY_STOPWORDS`' own docstring defends keeping
    on the content side, because `state`, `store` and `log` carry topic meaning
    here.

    So the honest number: **six of these twelve** answer with nothing, and the
    six that still answer are the six whose non-invented content words the
    graph genuinely holds. That count is the number `AGENTS.md` quotes, which
    is the whole reason it is asserted exactly rather than as a floor: change
    `_QUERY_STOPWORDS` or the ordering and this fails, and the prose has to be
    re-derived rather than left to drift. Against 0 of 12 silent under the
    pre-`47c867e` ordering.

    The count carried a robustness claim — "stable at 40, 72, 136 and 264 rows"
    — which is **withdrawn**, because it was measured by repeating this one
    fixture and duplication *cannot* move it: the gate reads only whether a
    content word has `df > 0`, and copying every row preserves that for every
    word while scaling `df` and the ceiling by the same factor. It varied
    corpus size while holding corpus composition fixed, and composition is the
    only input the gate has. The number is a measurement on **this** corpus,
    and nothing here claims more.
    """
    _seed_claim_graph()
    _assert_function_words_survive_the_df_ceiling(fresh_db, "what", "does", "how")
    answered = {
        query: [hit.node_id for hit in search.search(query, k=10, principal=owner()).hits]
        for query, _ in _INVENTED_SUBJECT_QUESTIONS
    }
    silent = {query for query, hits in answered.items() if not hits}
    assert len(silent) == 6, f"AGENTS.md says six of twelve; measured {len(silent)}: {answered}"
    # Deleting the invented subject leaves the same ranked list. Read that for
    # what it is — a **fixture guard**, not evidence about the gate. A term
    # with `df == 0` is dropped before any weight is computed and `match` is
    # built only from what survives, so `_compile_match` returns a
    # bit-identical plan for the query and the sub-query; that holds on any
    # corpus, for any word the corpus does not contain, and appending a
    # brand-new invented word to an answering query returns the identical list
    # too. What it *can* catch is the fixture going stale: if one of these
    # "invented" subjects ever turns out to be in the corpus, the lists diverge
    # and this reddens — worth pinning, because every assertion above reads
    # them as unknown. (An earlier version of this loop filtered out
    # `_is_function_word` tokens instead, which keeps the invented subject — it
    # is a content word — and so asserted only that the content words of a
    # query that answered still answer, which the gate already guarantees.)
    for query, invented in _INVENTED_SUBJECT_QUESTIONS:
        if query in silent:
            continue
        without_subject = " ".join(
            word for word in query.split() if search._bare_word(f'"{word}"') not in invented
        )
        assert len(without_subject.split()) == len(query.split()) - len(invented), query
        assert [
            hit.node_id for hit in search.search(without_subject, k=10, principal=owner()).hits
        ] == answered[query], query


def test_a_hallucinated_term_beside_the_subject_still_answers_on_a_single_subject_graph(
    fresh_db,
):
    """The E3 prerequisite at the one shape a wider refusal gate would break.

    A tempting reading of the rule is that it should fire on "no content word
    that *discriminates*" rather than "no content word *known*", since a term
    over the ubiquity ceiling separates nothing. On a graph about one subject
    that reading takes the subject with it, and this is the test that says so:
    `kafka` is over the ceiling here, `concretoid` is invented, and the query
    still has to answer with the subject's rows.

    Measured over 38, 56, 120 and 308 rows, rebinding `_compile_match` to each
    variant: refusing when no content word is at or under the ceiling takes
    `What is kafka?` from 30 hits to **0** at every size; refusing only when an
    unknown content word sits beside an over-ceiling one keeps that but takes
    `kafka concretoid` from 30 to **0**, which is the hallucinated-term
    guarantee `test_a_term_the_graph_has_never_seen_does_not_empty_the_result`
    exists for. Neither variant closes one extra invented-subject query —
    silence stays at 0.83 for all three gates at all four sizes. They cost the
    graph's own subject and buy nothing, so the gate stays knownness.
    """
    for index in range(30):
        service.create_node(
            type="claim",
            title=f"Kafka note {index}",
            content=f"Kafka retains the segment while the broker holds offset {index}.",
            principal=owner(),
        )
    for index, text in enumerate(_QUESTION_PROSE):
        service.create_node(
            type="note", title=f"Loose notes {index}", content=text, principal=owner()
        )
    counts = _document_frequencies(fresh_db, "kafka")
    rows = counts.pop("*rows*")
    ceiling = max(1, int(rows * search._COMMON_TERM_DF_FRACTION))
    assert counts["kafka"] > ceiling, "the fixture must put the subject over the ubiquity ceiling"
    subject_rows = _ids(search.search("kafka", k=40, principal=owner()))
    assert _ids(search.search("kafka concretoid", k=40, principal=owner())) == subject_rows
    assert _ids(search.search("What does kafka do about zarquon?", k=40, principal=owner())) == (
        subject_rows
    )


def test_a_term_only_in_an_unreadable_space_does_not_change_what_an_agent_sees(fresh_db):
    """The df probes must not answer questions about rows outside the read set.

    The probe counted `node_fts` with no principal and no space filter, so a
    term's weight — and therefore the bar every readable node had to clear —
    was computed from rows the caller cannot see. One planted word in a private
    space turned six hits into none, which is a one-bit existence oracle over
    the whole file, and repeating it with words planted at chosen frequencies
    brackets the private term's document frequency. `search` is in
    `mcp_server.READ_TOOLS`, so an external agent has this.
    """
    public = service.create_space("public", principal=owner())
    private = service.create_space("private", principal=owner())
    for index in range(6):
        service.create_node(
            type="note",
            title=f"Apple note {index}",
            content="An apple is a fruit that keeps for a long time in a cold room.",
            space=public.id,
            principal=owner(),
        )
    reader = agent("reader", grants={public.id: "read"})
    without = _ids(search.search("apple zarquon", k=10, principal=reader))
    service.create_node(
        type="note",
        title="Codename",
        content="The project codename is zarquon and it must not leave this space.",
        space=private.id,
        principal=owner(),
    )
    with_planted = _ids(search.search("apple zarquon", k=10, principal=reader))
    assert without == _ids(search.search("apple", k=10, principal=reader))
    assert with_planted == without, "a term in an unreadable space changed the agent's results"


def test_a_query_with_more_terms_than_the_cap_is_refused_as_a_caller_error(fresh_db):
    """500 usable terms compile 500 `UNION ALL` branches; 501 is a SQLite error.

    Measured: `GET /api/search` answered **503 "database error: too many terms
    in compound SELECT"** for a 4 508-byte query, `POST /api/ask` the same, and
    the CLI exited 1 in the storage voice. Three contract breaks for what is
    plainly a caller-input problem — and 503 is reserved for retryable lock
    contention, so a client retries it forever.

    A term the index has never seen is dropped before the quorum is compiled,
    so a query of 700 *invented* words never reaches the limit at all: the
    fixture has to plant the vocabulary, or it cannot reach its branch.
    """
    vocabulary = [f"lexeme{index:04d}" for index in range(700)]
    for index in range(3):
        service.create_node(
            type="note",
            title=f"Vocabulary {index}",
            content=" ".join(vocabulary),
            principal=owner(),
        )
    counts = _document_frequencies(fresh_db, vocabulary[0], vocabulary[-1])
    counts.pop("*rows*")
    assert all(count > 0 for count in counts.values()), "the fixture must plant real terms"

    assert search.search(" ".join(vocabulary[: search._MAX_QUERY_TERMS]), principal=owner()).hits
    with pytest.raises(ValueError, match="at most"):
        search.search(" ".join(vocabulary[:501]), principal=owner())


def _seed_equal_df_pair():
    """Seed a 40-row graph where `kafka` and `postgres` sit at the same df.

    One node carries both; five carry each alone; the rest carry neither. Equal
    document frequency is what makes "half the weight" ambiguous for two terms,
    so this is the fixture both of the tests below have to run against.

    Returns:
        The one node carrying both words.
    """
    for index in range(29):
        service.create_node(
            type="note",
            title=f"Ordinary note {index}",
            content="This is a note about the way a thing is written down for the next reader.",
            principal=owner(),
        )
    for index in range(5):
        service.create_node(
            type="note",
            title=f"Kafka note {index}",
            content="A note about kafka brokers and the log they are written to.",
            principal=owner(),
        )
        service.create_node(
            type="note",
            title=f"Postgres note {index}",
            content="A note about postgres vacuum and the dead row it reclaims.",
            principal=owner(),
        )
    return service.create_node(
        type="note",
        title="Together",
        content="Comparing kafka topics with postgres tables, and what each of them is for.",
        principal=owner(),
    )


def test_two_terms_of_equal_weight_require_both(fresh_db):
    """Equal document frequency makes each term exactly half, and `>=` takes one.

    Two words in the same number of notes is ordinary on a young graph, and
    there the quorum silently became the bare OR it was chosen over: measured
    at precision 0.111 over the returned list against the 0.722 the rule is
    defended at. The two-term case is the one shape where "half the weight" is
    ambiguous, so it is the one the comparison has to be strict for.
    """
    both = _seed_equal_df_pair()
    counts = _document_frequencies(fresh_db, "kafka", "postgres")
    counts.pop("*rows*")
    assert counts["kafka"] == counts["postgres"], "the fixture must give the terms equal weight"
    assert _ids(search.search("kafka postgres", k=20, principal=owner())) == {both.id}


#: Decorations of one word that `porter unicode61` tokenizes to that word
#: alone, so every one of them is the *same term* as the bare word and must
#: never buy it a second share of the quorum's weight. The first three are the
#: only ones the pre-fix trim list held; the rest — markdown emphasis, a smart
#: quote, a hyphen, an em dash, a hash, an underscore, a slash, an asterisk —
#: are what a pasted question actually looks like, and every one of them
#: re-opened the bypass. Kept as a shared constant because the same set has to
#: hold for the dedup (here), for the stopword lookup
#: (`test_a_decorated_question_word_does_not_re_open_the_refusal`) and against
#: the real tokenizer (`test_the_fold_merges_only_what_the_tokenizer_merges`).
_SAME_WORD_DECORATIONS = (
    "{word},",
    "{word}?",
    "{word}.",
    "**{word}**",
    "“{word}”",
    "{word}-",
    "-{word}",
    "{word}—",
    "#{word}",
    "{word}_",
    "{word}/",
    "{word}*",
    "({word})",
    "'{word}'",
    "{word}…",
)


def test_a_decoration_does_not_buy_a_word_a_second_share_of_the_weight(fresh_db):
    """The dedup has to fold what FTS5 folds, or the quorum is bought off.

    `_query_terms` dedups so that typing a word twice cannot count its weight
    twice — but it dedupped the *raw* token while FTS5 (`porter unicode61`)
    tokenizes `kafka,` and `kafka` identically. So the same word wearing a
    comma arrived as two terms carrying one word's document frequency twice,
    which is enough to clear a bar half of itself: measured on this fixture,
    `kafka postgres` answered with the one node carrying both and
    `kafka, kafka postgres` with six — the bare disjunction the quorum was
    chosen over, restored by a comma.

    The first fix trimmed a hand-written list of fifteen ASCII characters,
    which closed the comma and left the hole: measured on this fixture,
    `kafka- kafka postgres`, `“kafka” kafka postgres`, `#kafka kafka postgres`
    and `**kafka** kafka postgres` each answered with **six** again. A test
    that probes only characters the trim list already contains cannot see
    that — it passes for any trim list containing them — so the cases below
    are driven off :data:`_SAME_WORD_DECORATIONS`, which is mostly characters
    no trim list ever held. The fold is now the tokenizer's own character rule
    (maximal alphanumeric runs, lowercased) rather than an enumeration, so
    there is no list left to be incomplete.

    Trailing punctuation is not a hypothetical here: a question mark rides
    along on the last word of every question, and `**What**` is what a pasted
    markdown question looks like. The two functions disagreeing about what
    "the same word" is was the whole of the original defect, so they share one
    helper (`_bare_word`).
    """
    both = _seed_equal_df_pair()
    plain = _ids(search.search("kafka postgres", k=20, principal=owner()))
    assert plain == {both.id}
    for decoration in _SAME_WORD_DECORATIONS:
        decorated = decoration.format(word="kafka")
        assert search._query_terms(f"{decorated} kafka postgres") == [
            f'"{decorated}"',
            '"postgres"',
        ], decorated
        query = f"{decorated} kafka postgres"
        assert _ids(search.search(query, k=20, principal=owner())) == plain, query
    # Case is part of the same fold, and the kept token is the first one seen
    # verbatim — FTS5 tokenizes the decoration away itself, so there is nothing
    # to gain by rewriting what the caller typed.
    assert search._query_terms("Kafka. kafka postgres") == ['"Kafka."', '"postgres"']
    assert search._query_terms("do do?") == ['"do"']
    # Inner punctuation is *not* a term boundary here, or `min.insync.replicas`
    # stops being one term — the property `_is_function_word`'s own docstring
    # names. Asserted on two tokens, because a one-token query returns
    # `['"<token>"']` for **any** fold whatsoever (the fold is the dict key and
    # the raw token is the value), so the single-token form could not fail: a
    # fold stripping inner dots left all 42 tests of these two files green.
    assert search._query_terms("min.insync.replicas mininsyncreplicas") == [
        '"min.insync.replicas"',
        '"mininsyncreplicas"',
    ]
    assert search._query_terms("min.insync.replicas") == ['"min.insync.replicas"']
    # The other side of the same property: FTS5 tokenizes both spellings to the
    # same three-token phrase, so they *are* one term — the fold reaches that
    # because it is the tokenizer's rule and not a list of edge characters.
    assert search._query_terms("min.insync.replicas min-insync-replicas") == [
        '"min.insync.replicas"'
    ]


def test_the_stemmer_is_the_one_bypass_left_and_it_is_measured(fresh_db):
    """The fold reaches the tokenizer's characters, not its English.

    `porter` stems, so `retain`, `retains`, `retained` and `retaining` are one
    FTS5 term and four `_bare_word` folds. That is the dedup bypass of the test
    above, re-opened without a single punctuation mark: measured here,
    `retain postgres` answers with the one node carrying both and
    `retain retains postgres` with six.

    Closing it needs a porter stemmer in Python that agrees with SQLite's own
    on every word, which is a much larger commitment than this fix — and a
    *disagreeing* stemmer is worse than none, because merging two tokens FTS5
    keeps apart makes the second one unreachable. So it stays open, and it
    stays measured: this test is what makes `nodum/search.py`'s "sound and
    incomplete" claim falsifiable rather than a hedge, and it fails the moment
    somebody closes the gap without rewriting the prose.
    """
    for index in range(29):
        service.create_node(
            type="note",
            title=f"Ordinary note {index}",
            content="This is a note about the way a thing is written down for the next reader.",
            principal=owner(),
        )
    for index in range(5):
        service.create_node(
            type="note",
            title=f"Broker note {index}",
            content="A note about the way a broker will retain the batch it is given.",
            principal=owner(),
        )
        service.create_node(
            type="note",
            title=f"Postgres note {index}",
            content="A note about postgres vacuum and the dead row it reclaims.",
            principal=owner(),
        )
    both = service.create_node(
        type="note",
        title="Together",
        content="What a postgres table will retain is decided by the vacuum horizon.",
        principal=owner(),
    )
    counts = _document_frequencies(fresh_db, "retain", "postgres")
    counts.pop("*rows*")
    assert counts["retain"] == counts["postgres"], "the fixture must give the terms equal weight"
    # `retains` reaches the same rows as `retain` — the tokenizer stems both.
    assert _document_frequencies(fresh_db, "retains")["retains"] == counts["retain"]
    # …and the fold does not, so the pair is still two terms.
    assert search._query_terms("retain retains") == ['"retain"', '"retains"']
    assert _ids(search.search("retain postgres", k=20, principal=owner())) == {both.id}
    assert len(_ids(search.search("retain retains postgres", k=20, principal=owner()))) == 6


#: Ordinary prose carrying the pronoun `us` — a `_QUERY_STOPWORDS` entry — and
#: never the verb `use`. `porter` stems `use`, `used`, `using`, `uses` and
#: `useful` all onto `us`, so these are the rows a question carrying any of
#: those five spellings actually reaches, while `_is_function_word` reads every
#: one of them as a content word.
_STOPWORD_STEM_PROSE = (
    "The point of writing it down is to give us the version we would say out loud.",
    "None of this tells us which of the two we should reach for on a Tuesday.",
    "What the trace gave us was a shape, not a cause, and that took another day.",
    "It saves us the argument we would otherwise have twice a quarter.",
    "The migration cost us an afternoon and bought us a decade.",
    "Nobody has shown us a graph where the cheaper option loses.",
)


def test_the_stemmer_re_opens_the_refusal_and_not_only_the_dedup(fresh_db):
    """The stemming residue's worse half, pinned open the way the dedup's is.

    `test_the_stemmer_is_the_one_bypass_left_and_it_is_measured` names the
    residue's cost as quorum weight. That is the cheaper half. `_bare_word` is
    a *character* fold and `_is_function_word` looks its output up in
    `_QUERY_STOPWORDS`, while FTS5 **stems** — so any word that stems onto a
    stopword arrives at `_compile_match` as a **content** word while matching
    the stopword's rows in the index. `known_content` is non-empty, `kept` is
    non-empty, and the early return never fires.

    That is, clause for clause, the failure
    `test_a_decorated_question_word_does_not_re_open_the_refusal` exists to
    prevent, with the decoration replaced by an inflection — and unlike a smart
    quote, an inflection is what English *is*. Measured over the 63 875
    lowercase words of `/usr/share/dict/american-english`, **167** of them
    collide this way; the worst is the commonest verb a technical question
    carries, since `use`, `used`, `using`, `uses` and `useful` all stem to
    `us`, which is in the list.

    Two shapes asserted here, both defeats:

    * the shipped claim fixture needs no help at all. `doe` is not a stopword
      and `does` is, `porter` stems both to `doe`, and *"What does zarquon
      doe?"* answers with all **8** `Loose notes N` — verbatim what
      `test_a_question_about_a_word_the_graph_has_never_seen_answers_with_nothing`
      calls the harm, against **0** for the undecorated question;
    * and on any corpus that says *us* in ordinary prose, all five spellings of
      `use` answer with exactly the rows carrying the pronoun and never the
      verb, for a subject the graph has never heard of.

    This asserts the bypass is **open**, deliberately, which is the same
    posture as `_FOLD_RESIDUE`: it is what makes `_bare_word`'s soundness
    paragraph falsifiable on the half that costs the most, and it fails the
    moment somebody closes it — at which point the prose is rewritten rather
    than left stale. Closing it is not obviously free. Stemming
    `_QUERY_STOPWORDS` at import would fix the lookup, but an over-merge there
    demotes a real content word to a function word and refuses a query that
    should have answered, which is the opposite error and not plainly cheaper.
    """
    _seed_claim_graph()
    for index, text in enumerate(_STOPWORD_STEM_PROSE):
        service.create_node(type="note", title=f"Prose {index}", content=text, principal=owner())
    inflections = ("doe", "use", "used", "using", "uses", "useful")
    counts = _document_frequencies(fresh_db, "does", "us", *inflections)
    rows = counts.pop("*rows*")
    ceiling = max(1, int(rows * search._COMMON_TERM_DF_FRACTION))
    # The lookup is by character fold, so none of these is a function word…
    assert not any(search._is_function_word(f'"{word}"') for word in inflections)
    # …while the tokenizer stems each onto one that is, and hands it that
    # word's real document frequency — under the ceiling, so nothing else drops
    # it either. That is the whole mechanism, and the assertions below are its
    # consequence.
    assert search._is_function_word('"does"') and search._is_function_word('"us"')
    assert counts["doe"] == counts["does"] == 8
    assert {counts[word] for word in inflections if word != "doe"} == {counts["us"]} == {6}
    assert max(counts.values()) <= ceiling, counts

    # The refusal, working: the graph knows no content word of this question.
    assert search.search("What does zarquon protect against?", k=20, principal=owner()).hits == []
    # …and defeated by one inflection of a word already in `_QUERY_STOPWORDS`.
    defeated = search.search("What does zarquon doe?", k=20, principal=owner()).hits
    assert {hit.title for hit in defeated} == {f"Loose notes {index}" for index in range(8)}
    pronoun_rows = {f"Prose {index}" for index in range(len(_STOPWORD_STEM_PROSE))}
    for spelling in ("use", "used", "using", "uses", "useful"):
        hits = search.search(f"How is zarquon {spelling}?", k=20, principal=owner()).hits
        assert {hit.title for hit in hits} == pronoun_rows, spelling


#: Tokens whose FTS5 row sets and `_bare_word` folds are compared pairwise
#: below. Grouped by the axis each one probes, because the point of the set is
#: coverage of the *ways* a fold can disagree with a tokenizer, not volume.
_FOLD_PROBE = (
    # One word wearing what a paste puts on it. Every one of these is `kafka`
    # to the tokenizer, and only the first four were to the pre-fix fold.
    "kafka",
    "kafka,",
    "kafka?",
    "kafka.",
    "Kafka",
    "KAFKA",
    "**kafka**",
    "“kafka”",
    "kafka-",
    "-kafka",
    "kafka—",
    "#kafka",
    "kafka_",
    "kafka/",
    "kafka*",
    "(kafka)",
    "'kafka'",
    "kafka…",
    # Inner punctuation: one term to `_query_terms`, a multi-token *phrase* to
    # FTS5 — which is why the first two are the same query and the third is not.
    "min.insync.replicas",
    "min-insync-replicas",
    "mininsyncreplicas",
    # …and the bare first word of that phrase, which is what makes the axis
    # measurable at all: `c++`/`c` cannot, because both tokenize to the same
    # single term, so there is nothing for the row-set comparison to disagree
    # about. A fold keeping only the *first* alnum run merges
    # `min.insync.replicas` into `min` while FTS5 keeps them apart, and left
    # every test of these two files green until this token was added.
    "min",
    "c++",
    "c",
    # The case axis. `casefold()` merged both of these pairs; the tokenizer
    # keeps both apart, so `casefold()` was unsound and `lower()` is not.
    "straße",
    "strasse",
    "ﬁle",
    "file",
    # The residue no character rule reaches: the tokenizer stems…
    "retain",
    "retains",
    "retained",
    # …and folds diacritics — but only the ones its `remove_diacritics=1`
    # default reaches, which is the whole reason NFD-stripping was rejected in
    # `_bare_word`'s docstring. These four are that reason, made falsifiable:
    # the tokenizer merges `ō` with `o` and keeps `ộ` and `ǭ` apart from both,
    # because a *precomposed* codepoint carrying two diacritics is left alone.
    # An NFD-stripping fold merges all four and is unsound on five of the six
    # pairs; without these tokens it reddens only the residue list below, so
    # the stated reason for the rejection went unpinned.
    "café",
    "cafe",
    "o",
    "ō",
    "ộ",
    "ǭ",
)

#: The complete list of probe pairs the tokenizer merges and `_bare_word` does
#: not — the "incomplete" half of its claim, written out so that closing any of
#: it fails this test rather than silently making the prose stale. Stemming
#: (three pairs) and diacritic folding (two). Nothing else.
_FOLD_RESIDUE = (
    ("retain", "retains"),
    ("retain", "retained"),
    ("retained", "retains"),
    ("cafe", "café"),
    ("o", "ō"),
)

#: Pairs `_bare_word` merges and `porter unicode61` does **not** — the exact
#: shape the soundness half says must not exist, and the reason `nodum/search.py`
#: qualifies that claim instead of asserting it. SQLite's fold table is frozen
#: at the Unicode version `fts5_unicode2.c` was written against, so every simple
#: case mapping added since is a counterexample: swept over all 133 808
#: alphanumeric codepoints below U+30000, 417 fold groups Python merges and
#: SQLite splits. One representative per living script here, plus a Latin one
#: for the tail, because the point is the *axis* and not the volume. Deseret
#: (U+10400/U+10428, cased since Unicode 3.1) folds correctly on both sides, so
#: this is a table-vintage limit and not a non-BMP one — it is the control
#: below. Every one of these is asserted **unsound**, so SQLite catching up
#: fails this test and the prose gets rewritten rather than left stale.
_FOLD_UNSOUND = (
    ("Ꭰ", "ꭰ"),  # Cherokee, cased in Unicode 8
    ("ᲓᲦᲔ", "დღე"),  # Georgian Mtavruli, cased in Unicode 11
    ("\U0001e900", "\U0001e922"),  # Adlam, Unicode 9
    ("\U000104b0", "\U000104d8"),  # Osage, Unicode 9
    ("\U000118a0", "\U000118c0"),  # Warang Citi, Unicode 7
    ("\U00016e40", "\U00016e60"),  # Medefaidrin, Unicode 11
    ("Ɡ", "ɡ"),  # LATIN CAPITAL LETTER SCRIPT G, cased in Unicode 8
)

#: Case pairs the fold and the tokenizer agree on, asserted beside
#: `_FOLD_UNSOUND` so that "SQLite's table is behind" cannot be read as "SQLite
#: does not case-fold outside ASCII". Deseret is the sharp one: non-BMP, and
#: still sound, so the axis is the table's vintage and nothing else.
_FOLD_SOUND_CONTROL = (
    ("KAFKA", "kafka"),
    ("\U00010400", "\U00010428"),  # Deseret, cased in Unicode 3.1
)


def test_the_fold_merges_only_what_the_tokenizer_merges(fresh_db):
    """`_bare_word`'s invariant, measured against the tokenizer it names.

    The fold decides two things — whether two query tokens are one term, and
    whether a token is in `_QUERY_STOPWORDS` — and `nodum/search.py` documents
    it as *incomplete, and sound on every script SQLite's case table has caught
    up with*:

    * **sound**: two tokens that fold alike select the same rows. An unsound
      merge is not a near miss, it makes a term the caller typed unreachable —
      `_query_terms` keeps the first token of a fold group and drops the rest.
    * **incomplete**: the tokenizer merges pairs the fold does not, because
      the tokenizer also stems and folds diacritics and no character rule
      reaches that.

    Both halves are asserted here rather than reasoned about, and asserted
    against a real FTS5 table carrying the *projector's own* tokenizer, read
    out of the live schema and passed to the probe verbatim — so if `node_fts`
    is ever rebuilt with a different one this fails instead of quietly
    measuring the wrong engine. A substring check was not that guard: it passed
    for `porter unicode61 remove_diacritics 2`, which merges `ộ` with `o` and
    `ǭ` with `ō` and so moves the residue this test pins.

    The residue is listed exactly, not as a floor: widening the fold has to
    come with a rewrite of the prose, and narrowing it back to a punctuation
    list explodes this list by two orders of magnitude (measured: 10 688 pairs
    against 231 over a 465-token probe).

    Soundness is asserted the same way, and that is the half whose *exceptions*
    are named: `_FOLD_UNSOUND` holds one pair per script SQLite's fold table
    predates, each asserted to be a real counterexample, with
    `_FOLD_SOUND_CONTROL` beside it so the axis reads as table vintage rather
    than as "non-ASCII" or "non-BMP". The blast radius on this corpus is nil
    and the fold is deliberately not changed; what these pin is that the
    docstring's qualification stays true of the SQLite it is running on.
    """
    conn = db.connect(fresh_db)
    try:
        db.init_db(conn)
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'node_fts'").fetchone()
        declared = re.search(r"tokenize\s*=\s*'([^']*)'", schema["sql"])
        assert declared is not None, schema["sql"]
        tokenizer = declared.group(1)
        assert tokenizer == "porter unicode61", tokenizer
        cased = (*_FOLD_UNSOUND, *_FOLD_SOUND_CONTROL)
        probed = (*_FOLD_PROBE, *(token for pair in cased for token in pair))
        conn.execute(f"CREATE VIRTUAL TABLE temp.probe USING fts5(x, tokenize='{tokenizer}')")
        for rowid, token in enumerate(probed):
            conn.execute("INSERT INTO temp.probe(rowid, x) VALUES (?, ?)", (rowid, token))
        rows = {
            token: frozenset(
                row[0]
                for row in conn.execute(
                    "SELECT rowid FROM temp.probe WHERE probe MATCH ?",
                    ('"' + token.replace('"', '""') + '"',),
                )
            )
            for token in probed
        }
    finally:
        conn.close()

    unsound, incomplete = [], []
    for index, left in enumerate(_FOLD_PROBE):
        for right in _FOLD_PROBE[index + 1 :]:
            pair = tuple(sorted((left, right)))
            same_fold = search._bare_word(left) == search._bare_word(right)
            same_rows = rows[left] == rows[right]
            if same_fold and not same_rows:
                unsound.append(pair)
            elif same_rows and not same_fold:
                incomplete.append(pair)
    assert unsound == [], f"the fold merged tokens FTS5 keeps apart: {unsound}"
    assert sorted(incomplete) == sorted(tuple(sorted(pair)) for pair in _FOLD_RESIDUE), (
        "the fold's incompleteness moved; `_bare_word`'s docstring names it as"
        f" stemming and diacritics only. measured: {sorted(incomplete)}"
    )
    # The counterexamples, asserted as counterexamples. Each folds alike and
    # matches different rows, which is precisely the unsoundness the assertion
    # above forbids inside `_FOLD_PROBE` — the fold is not wrong here, the
    # unqualified claim was.
    still_unsound = [
        (left, right)
        for left, right in _FOLD_UNSOUND
        if search._bare_word(left) == search._bare_word(right) and rows[left] != rows[right]
    ]
    assert still_unsound == list(_FOLD_UNSOUND), (
        "SQLite's fold table has caught up with Python's on some of these, so"
        " `_bare_word`'s soundness qualification has to be re-derived. still"
        f" unsound: {still_unsound}"
    )
    for left, right in _FOLD_SOUND_CONTROL:
        assert search._bare_word(left) == search._bare_word(right), (left, right)
        assert rows[left] == rows[right], (left, right)


def test_four_terms_of_equal_weight_still_only_need_half(fresh_db):
    """The strict comparison is gated on two terms, and this is why.

    A blanket `>` is not free: with four terms of equal weight it moves the bar
    from two-of-four to three-of-four, which is a quorum nobody chose. This
    test does not fail without the change — it fails if the gate is ever
    removed.
    """
    for index in range(24):
        service.create_node(
            type="note",
            title=f"Ordinary note {index}",
            content="This is a note about the way a thing is written down for the next reader.",
            principal=owner(),
        )
    # Four notes each for the two words the last node also carries, five for
    # the two it does not: all four terms end at df 5, which is the only
    # arrangement in which "half the weight" is ambiguous for four terms.
    for word, count in (("kafka", 4), ("postgres", 5), ("sqlite", 5), ("duckdb", 4)):
        for index in range(count):
            service.create_node(
                type="note",
                title=f"{word} note {index}",
                content=f"A note about {word} and the way it stores a row on disk.",
                principal=owner(),
            )
    half = service.create_node(
        type="note",
        title="Two of four",
        content="Comparing kafka with duckdb, and nothing else at all.",
        principal=owner(),
    )
    counts = _document_frequencies(fresh_db, "kafka", "postgres", "sqlite", "duckdb")
    counts.pop("*rows*")
    assert len(set(counts.values())) == 1, "the fixture must give all four terms equal weight"
    assert half.id in _ids(search.search("kafka postgres sqlite duckdb", k=20, principal=owner()))


def test_a_query_of_nothing_but_function_words_is_still_weighed(fresh_db):
    """Dropping every term needs a fallback, and the fallback is still a quorum.

    "what does" is two function words and nothing else, so the drop empties the
    query; the fallback searches them anyway, because a query the caller
    actually typed is the best evidence available. What it must **not** do is
    fall through to the bare disjunction — the node saying only *does* would
    then be a hit, which is precisely the rule the quorum was chosen over.
    Asserted on the decoy for that reason: without the fallback the target is
    found either way, and only the decoy tells the two paths apart.

    Does not fail without the change: it fails if the fallback is written as a
    fall-through to `plain` rather than as the quorum's own next step.
    """
    for index in range(24):
        service.create_node(
            type="note",
            title=f"Filler {index}",
            content="A short factual sentence with no question words in sight at all.",
            principal=owner(),
        )
    for index in range(11):
        service.create_node(
            type="note",
            title=f"Common {index}",
            content="This sentence does carry the commoner of the two words.",
            principal=owner(),
        )
    decoy = service.create_node(
        type="note",
        title="Decoy",
        content="This one also does, and stops there.",
        principal=owner(),
    )
    for index in range(3):
        service.create_node(
            type="note",
            title=f"Rare {index}",
            content="What a sentence like this one is for is anybody's guess.",
            principal=owner(),
        )
    target = service.create_node(
        type="note",
        title="Both",
        content="What this sentence does is carry both of the words at once.",
        principal=owner(),
    )
    assert search._is_function_word('"what"') and search._is_function_word('"does"')
    counts = _document_frequencies(fresh_db, "what", "does")
    rows = counts.pop("*rows*")
    assert counts["what"] < counts["does"] <= rows // 2, "the fixture must weigh the two apart"
    found = _ids(search.search("what does", k=20, principal=owner()))
    assert target.id in found
    assert decoy.id not in found


# ── The search endpoint ───────────────────────────────────────────────────────

_ENDPOINT_PASSWORD = "correct horse battery staple"


def _logged_in_app(db_path):
    """A Starlette app over the test database, plus a logged-in session cookie."""
    app = http_api.create_app(db_path=db_path)
    service.set_human_password("owner", _ENDPOINT_PASSWORD, principal=owner())

    async def login() -> str:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8600") as web:
            response = await web.post(
                "/api/login",
                json={"name": "owner", "password": _ENDPOINT_PASSWORD},
                headers={"Content-Type": "application/json", http_api.CLIENT_HEADER: "tests"},
            )
            assert response.status_code == 200, response.text
            return response.cookies[http_api.SESSION_COOKIE]

    return app, asyncio.run(login())


def _get(app, session: str, path: str, **params) -> httpx.Response:
    """One GET through the ASGI app, on its own event loop, as a browser would."""

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8600") as web:
            return await web.get(
                path,
                params=params,
                headers={"Cookie": f"{http_api.SESSION_COOKIE}={session}"},
            )

    return asyncio.run(run())


def test_the_search_endpoint_runs_off_the_event_loop(fresh_db, monkeypatch):
    """A GET must not stop the whole server for as long as it takes to answer.

    The `?nl=1` branch of this handler already went through the thread pool
    because a model call is seconds of work; the ordinary branch did not, and
    it is not cheap either — measured, one `GET /api/search` carrying 400 terms
    (a 4 KB query, nothing exotic) held the loop for 126 ms on a 120-row graph,
    with `/healthz`, the SPA and every other tab waiting behind it.

    Asserted on the thread rather than on a duration: a timing assertion in a
    suite is a flake, and "it ran somewhere other than the loop" is the
    property that matters.
    """
    service.create_node(type="note", title="T", content="mycelium networks", principal=owner())
    app, session = _logged_in_app(fresh_db)
    seen: dict[str, threading.Thread] = {}
    real = search.search

    def recording(*args, **kwargs):
        seen["thread"] = threading.current_thread()
        return real(*args, **kwargs)

    monkeypatch.setattr(http_api.search_module, "search", recording)
    response = _get(app, session, "/api/search", q="mycelium")
    assert response.status_code == 200, response.text
    assert response.json()["hits"], response.text
    assert seen["thread"] is not threading.main_thread()


def test_the_search_endpoint_refuses_an_oversized_query_as_a_caller_error(fresh_db):
    """The 503 the compound-SELECT limit produced is a 400, and says why.

    503 is this API's *retryable* status — lock contention — so a client that
    backs off and retries would have retried this one forever.
    """
    vocabulary = [f"lexeme{index:04d}" for index in range(700)]
    service.create_node(
        type="note", title="Vocabulary", content=" ".join(vocabulary), principal=owner()
    )
    app, session = _logged_in_app(fresh_db)
    response = _get(app, session, "/api/search", q=" ".join(vocabulary[:501]))
    assert response.status_code == 400, response.text
    assert "at most" in response.json()["error"]["message"]


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
