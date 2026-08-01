"""Hybrid search: RRF fusion of BM25 + vector, then graph expansion."""

from __future__ import annotations

from helpers import agent, owner

# The prose the keyword refusal exists to suppress, imported rather than copied
# so that editing it in one place cannot leave this file's fixture silently
# unable to express the failure it names.
from test_search import _QUESTION_PROSE

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


def test_the_keyword_refusal_is_the_keyword_arms_and_the_vector_arm_still_answers(
    fresh_db, fake_embedder
):
    """The honest empty is a property of the *keyword* arm, not of `search`.

    `_compile_match` refuses a query the graph knows no content word of, and
    `tests/test_search.py` pins that — but it pins it on the default install,
    where `conftest._no_embedding_provider` has already switched the other arm
    off. With a provider present the vector arm has no similarity threshold
    (`_search_vector`: *"the ANN list is always `k` deep"*), so it answers the
    query the keyword arm just refused, `k` rows deep, and the caller gets a
    ranked list whose every hit carries the `vector` signal alone.

    That is asserted here rather than fixed, because the fix is a number this
    repo has no way to measure yet. A cosine floor is a threshold, and an
    unmeasured threshold is what turns a visible failure into an invisible one
    — the rule `/ask`'s groundedness gate is held to. Measuring one needs a
    real embedding model (`fastembed` is not a test dependency and is not
    installed) over a real graph: `HashEmbedder` above is a signed hashing
    bag-of-words, so its similarity *is* token overlap, and every floor
    measured against it would look free while costing exactly the paraphrase
    recall the vector arm exists for. Suppressing the fused list on the
    keyword arm's refusal has the same problem from the other side, and would
    contradict `test_vector_only_hit_surfaces_with_vector_signal` directly.

    So the two arms disagree, deliberately and visibly, and this test is what
    makes the disagreement fail loudly the moment somebody changes it — at
    which point `nodum/search.py`, `AGENTS.md` and
    `web/src/views/search/noResults.ts` all need the qualifier taken back out.

    **The fixture is the whole test.** Seeded with the 20 claims alone it was
    green under the mutation it exists to catch: `what`, `does`, `zarquon`,
    `protect` and `against` were all at `df = 0` over those 20 rows, so the
    keyword arm contributed nothing for the trivial reason that nothing
    matched, and deleting the refusal outright (`return plain`) left every
    assertion here satisfied. `_QUESTION_PROSE` is what makes the two cases
    differ: it is the prose the refusal suppresses, it carries `what`, `does`
    and `against` at a real document frequency, and with it seeded the same
    mutation puts `bm25` on 8 of the 10 hits and reddens the first assertion.

    The one-term `zarquon` leg was dropped for the same reason: `_compile_match`
    returns `plain` at `len(terms) == 1` and never reaches the refusal branch,
    so that leg asserted the absence of a match, not the presence of the rule.
    """
    for index in range(20):
        service.create_node(
            type="claim",
            title=f"Claim {index}",
            content=f"Log compaction retains the newest value for key {index}.",
            principal=owner(),
        )
    for index, text in enumerate(_QUESTION_PROSE):
        service.create_node(
            type="note", title=f"Loose notes {index}", content=text, principal=owner()
        )
    query = "What does zarquon protect against?"
    result = search.search(query, k=10, principal=owner())
    # The keyword arm contributed nothing at all — no hit carries `bm25`. The
    # prose above is exactly what it would have contributed without the rule.
    assert not any("bm25" in hit.signals for hit in result.hits), result.hits
    # …and the vector arm answered anyway, the full `k` deep.
    assert len(result.hits) == 10
    assert all(set(hit.signals) == {"vector"} for hit in result.hits)


def test_degrades_to_bm25_when_no_provider(fresh_db):
    # No fake embedder: search must not crash and stays BM25-only.
    target = service.create_node(type="note", title="T", content="xylem vessels", principal=owner())
    result = search.search("xylem", principal=owner())
    assert [hit.node_id for hit in result.hits] == [target.id]
    assert set(result.hits[0].signals) == {"bm25"}
