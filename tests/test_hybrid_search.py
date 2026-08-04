"""Hybrid search: RRF fusion of BM25 + vector, then graph expansion."""

from __future__ import annotations

from helpers import (
    REPLAY_DECOY_MIGRATION,
    REPLAY_DECOY_ROUTES,
    REPLAY_FARTHEST,
    REPLAY_MIDDLE,
    REPLAY_NEAREST,
    REPLAY_QUERY,
    agent,
    owner,
)

# The prose the keyword refusal exists to suppress, imported rather than copied
# so that editing it in one place cannot leave this file's fixture silently
# unable to express the failure it names.
from test_search import _QUESTION_PROSE

from nodum import search, service


def test_the_vector_floor_keeps_a_genuine_match_and_cuts_a_disjoint_one(fresh_db, fake_embedder):
    strong = service.create_node(
        type="note", title="Target", content="xylem vessels", principal=owner()
    )
    disjoint = service.create_node(
        type="note", title="Unrelated", content="quantum entanglement qubits", principal=owner()
    )

    result = search.search("xylem", k=5, principal=owner())
    by_id = {hit.node_id: hit for hit in result.hits}
    assert result.hits[0].node_id == strong.id
    # "Target xylem vessels" sits at cosine 1/√3 ≈ 0.577 — above the floor, so
    # the vector signal fires on a genuine match.
    assert "vector" in by_id[strong.id].signals
    # The disjoint node shares no token with the query: cosine 0.0, distance
    # 1.0, below the floor — it used to fuse with a tiny vector-only
    # contribution from the k-deep ANN list, and this is the test that pinned
    # that behaviour. The floor is what cuts it (finding M20).
    assert disjoint.id not in by_id


def test_fused_beats_single_signal_where_lists_agree(fresh_db, fake_embedder):
    # "both" shares the query terms and little else → top of both lists.
    # "weak" matches BM25 too (terms are ANDed) but is diluted with disjoint
    # vocabulary → second in both lists. "disjoint" shares no token with the
    # query → cosine 0.0, below the similarity floor, so it is not a hit at
    # all rather than a threshold-free vector-only one.
    both = service.create_node(
        type="note", title="Both", content="xylem vessels transport water upward", principal=owner()
    )
    weak = service.create_node(
        type="note",
        title="Weak",
        content="xylem vessels quantum entanglement photon flux",
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
    assert disjoint.id not in by_id
    # Agreement across signals beats any single-signal hit on the fused score…
    assert result.hits[0].score > by_id[weak.id].score
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


def test_the_absent_term_answers_with_nothing_even_with_a_provider(fresh_db, fake_embedder):
    """The honest empty is now a property of `search` whole (finding M20).

    `_compile_match` refuses a query the graph knows no content word of, and
    `tests/test_search.py` pins that — but on the default install, where
    `conftest._no_embedding_provider` has already switched the other arm
    off. With a provider present the vector arm used to have no similarity
    floor (`_search_vector`: *"the ANN list is always `k` deep"*), so it
    answered the query the keyword arm just refused, `k` rows deep, every
    hit carrying the `vector` signal alone — which `/ask` then cited as
    confident fact. This is the test that pinned that disagreement.

    The floor (`search._VECTOR_MIN_SIMILARITY`, cosine 0.5) closes it: the
    query shares no content word with the claims and only function words
    with the prose, so the nearest chunk sits at distance 0.63 (cosine
    0.37) — below the bar, and the vector arm contributes nothing. Both
    arms now return the same empty result, and this test makes the pair
    fail loudly the moment either half changes: deleting the floor puts the
    prose back on the vector list, and deleting the keyword refusal puts it
    on the BM25 list. At that point `nodum/search.py`, `docs/decisions.md` and
    `web/src/views/search/noResults.ts` all need the qualifier taken back
    out.

    **The fixture is the whole test.** Seeded with the 20 claims alone it
    was green under the mutation it exists to catch: `what`, `does`,
    `zarquon`, `protect` and `against` were all at `df = 0` over those 20
    rows, so the keyword arm contributed nothing for the trivial reason that
    nothing matched, and deleting the refusal outright (`return plain`) left
    every assertion here satisfied. `_QUESTION_PROSE` is what makes the two
    cases differ: it is the prose the refusal suppresses, it carries `what`,
    `does` and `against` at a real document frequency, and with it seeded
    the same mutation puts `bm25` on 8 of the 10 hits and reddens the first
    assertion — and, under the floor, the prose is also what the vector arm
    would return without it (its chunks are the nearest to the query, at
    distance 0.63).

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
    # Neither arm answers: the keyword arm refuses (no known content word),
    # and the vector arm's nearest chunk is below the similarity floor.
    assert result.hits == [], result.hits


def test_degrades_to_bm25_when_no_provider(fresh_db):
    # No fake embedder: search must not crash and stays BM25-only.
    target = service.create_node(type="note", title="T", content="xylem vessels", principal=owner())
    result = search.search("xylem", principal=owner())
    assert [hit.node_id for hit in result.hits] == [target.id]
    assert set(result.hits[0].signals) == {"bm25"}


def test_the_vector_arm_recalls_a_paraphrase_a_lexical_decoy_cannot(fresh_db, replay_embedder):
    """The vector signal's differentiator, pinned: recall without token overlap.

    ``HashEmbedder`` is a bag-of-words embedder whose cosine **is** token
    overlap — every vector test built on it ranks by the same signal BM25
    uses, so nothing asserts the recall the real arm exists for: a chunk that
    shares no word with the query still wins because its embedding sits near
    the query's. ``ReplayEmbedder`` answers from a frozen table whose geometry
    the test controls, so this can be stated directly:

    * the query's two content words appear only in the two decoy rows — each
      in exactly one, which is what the keyword arm's quorum needs to exclude
      them both (a row carrying both would be a genuine BM25 hit);
    * the three paraphrases share **no word** with the query, so the keyword
      arm cannot name them at all;
    * the nearest paraphrase's frozen cosine clears the similarity floor and
      the decoys' cosines do not, so the vector arm names the paraphrases and
      nothing else.

    The nearest paraphrase therefore wins on the ``vector`` signal alone,
    ahead of rows that carry the query's own words — the ranking is semantic,
    not lexical, and a test built on the hash embedder could never have
    expressed that.
    """
    near = service.create_node(type="note", content=REPLAY_NEAREST, principal=owner())
    middle = service.create_node(type="note", content=REPLAY_MIDDLE, principal=owner())
    farthest = service.create_node(type="note", content=REPLAY_FARTHEST, principal=owner())
    decoy_migration = service.create_node(
        type="note", content=REPLAY_DECOY_MIGRATION, principal=owner()
    )
    decoy_routes = service.create_node(type="note", content=REPLAY_DECOY_ROUTES, principal=owner())
    # The fixture is not a tautology: each decoy really does carry one of the
    # query's words (a naive keyword search would match them), and no
    # paraphrase carries either.
    assert "migration" in decoy_migration.content
    assert "routes" in decoy_routes.content
    assert not any(word in near.content for word in ("migration", "routes"))

    result = search.search(REPLAY_QUERY, k=5, principal=owner())

    assert result.hits[0].node_id == near.id, [hit.node_id for hit in result.hits]
    assert set(result.hits[0].signals) == {"vector"}
    # The paraphrases rank by frozen cosine (the nearest first); the lexical
    # decoys are not hits at all — below the similarity floor and under the
    # keyword quorum alike.
    assert [hit.node_id for hit in result.hits] == [near.id, middle.id, farthest.id]
    assert all(set(hit.signals) == {"vector"} for hit in result.hits)
