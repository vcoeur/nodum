"""The consolidation runner: the deterministic jobs and the coherence metrics.

Phase 5a, the near side of the LLM line (design §8.4/§8.5). What is tested here
is the half of the gardener that is arithmetic: four deterministic jobs, the
abstraction job's deterministic selection (5b-ii's first LLM job — the model
writes the text and never decides *whether* to synthesize), five metrics, one
cycle around all of it, and the rails that keep the internal agent a *peer
client* — every write through the public service API, every event stamped with
the cycle, every one of them attributed to ``agent:builtin-gardener``.

The last of those is an AST property rather than a behaviour, deliberately: a
module that grew its own connection would pass every behavioural test in this
file while quietly leaving the grant model behind.
"""

from __future__ import annotations

import ast
import json
import math
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from helpers import agent, owner

from nodum import auth, consolidate, db, embeddings, service
from nodum.store import GrantNotPermitted

#: The labelled pairs the two cosine bars are measured *against* — not set from,
#: which is the distinction the reverted 0.72/0.38 pair was built on missing.
CALIBRATION_FIXTURE = Path(__file__).parent / "fixtures" / "embedding_calibration.json"

# ── Fixtures and helpers ──────────────────────────────────────────────────────


class _PlacedEmbedder:
    """A provider that puts each text at a chosen angle in the first two dims.

    The cosine between two texts is then exactly ``cos(angle difference)``, so a
    test pins a threshold instead of hoping a hashing embedder lands the right
    side of it. Texts naming no marker sit at angle 0.
    """

    model_id = "test-placed-embedder"
    dimensions = embeddings.EMBEDDING_DIMS

    def __init__(self, angles: dict[str, float]) -> None:
        self._angles = angles

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Place each text on the unit circle spanned by dimensions 0 and 1."""
        vectors = []
        for text in texts:
            angle = next((value for marker, value in self._angles.items() if marker in text), 0.0)
            vector = [0.0] * self.dimensions
            vector[0], vector[1] = math.cos(angle), math.sin(angle)
            vectors.append(vector)
        return vectors


def _place(**angles: float) -> _PlacedEmbedder:
    """Install a :class:`_PlacedEmbedder` for one test and return it."""
    provider = _PlacedEmbedder(angles)
    embeddings.set_provider(provider)
    return provider


class _RecordingEmbedder:
    """A provider that records every text it was handed, then answers constantly.

    What it exists to catch is a *truncation*, which no test double can show by
    its output: the real model has a 512-token window and silently drops
    everything past it, so a node handed over whole is compared on its opening
    pages with nothing in the vector to say so. The checkable form is therefore
    the call itself — what the consolidation cycle asks the model to embed.
    """

    model_id = "test-recording-embedder"
    dimensions = embeddings.EMBEDDING_DIMS

    def __init__(self) -> None:
        self.seen: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Record the batch and return one fixed unit vector per text."""
        self.seen.extend(texts)
        return [[1.0] + [0.0] * (self.dimensions - 1) for _ in texts]


def _at(cosine: float) -> float:
    """The angle whose cosine against a marker-less text is ``cosine``.

    Tests that mean "just above the link bar" say so in terms of the bar
    itself. The link bar is measured at 0.60 on real content and the duplicate
    bar stays at 0.93 — the invariant pinned below — so a test that writes a
    hard-coded angle would turn every re-tune into a rewrite and, worse, would
    keep passing while no longer testing the side of the bar it was written
    for.
    """
    return math.acos(cosine)


#: Squarely between the two bars: related, definitely not the same thing.
BETWEEN_THE_BARS = (consolidate.DUPLICATE_EMBEDDING_COSINE + consolidate.LINK_EMBEDDING_COSINE) / 2


def _run(**kwargs):
    """Run a consolidation cycle triggered by the seeded owner unless told otherwise."""
    kwargs.setdefault("triggered_by", auth.OWNER_ACTOR)
    return consolidate.consolidate(**kwargs)


def _node(title, *, type="claim", **kwargs):
    return service.create_node(type=type, title=title, principal=owner(), **kwargs)


def _edge(src, dst, type="supports"):
    return service.create_edge(src, dst, type, principal=owner())


def _outcome(report, name):
    """One job's outcome out of a report, by name."""
    (found,) = [job for job in report.jobs if job.name == name]
    return found


def _events(cycle_id=None):
    return list(reversed(service.list_events(owner(), limit=1000, cycle_id=cycle_id)))


def _max_seq():
    events = service.list_events(owner(), limit=1)
    return events[0].seq if events else 0


def _backdate(table, row_id, column, days):
    """Push one row's timestamp back, the way the URL and session tests do."""
    conn = db.connect()
    try:
        conn.execute(
            f"UPDATE {table} SET {column} = datetime('now', ?) WHERE id = ?",  # noqa: S608
            (f"-{days} days", row_id),
        )
        conn.commit()
    finally:
        conn.close()


def _set_position(node_id, position):
    conn = db.connect()
    try:
        conn.execute("UPDATE nodes SET position = ? WHERE id = ?", (position, node_id))
        conn.commit()
    finally:
        conn.close()


def _edge_state(edge_id):
    conn = db.connect()
    try:
        row = conn.execute("SELECT state FROM edges WHERE id = ?", (edge_id,)).fetchone()
        return None if row is None else row["state"]
    finally:
        conn.close()


def _duplicates():
    """Every ``duplicate_of`` edge in the file, in creation order."""
    return service.list_edges(type=consolidate.DUPLICATE_EDGE_TYPE, principal=owner())


def _related():
    return service.list_edges(type=consolidate.RELATED_EDGE_TYPE, principal=owner())


# ── A cycle that finds nothing still says so ──────────────────────────────────


def test_a_cycle_that_finds_nothing_still_writes_a_coherent_report(fresh_db):
    """An empty graph is the commonest first run, and it must still be readable."""
    result = _run()

    assert result.cycle.status == "completed"
    assert result.cycle.dry_run is False
    assert [job.name for job in result.report.jobs] == list(consolidate.JOBS)
    assert result.report.failed == []
    for job in result.report.jobs:
        assert job.proposed == []
        assert job.applied == []
        assert job.error is None
    assert set(result.report.metrics) == {"before", "after"}
    # The stored report is the same data, not a second one.
    assert result.cycle.report == result.report.model_dump(mode="json")


def test_every_metric_is_defined_on_an_empty_graph(fresh_db):
    """No metric may divide by zero, and an empty queue has no age."""
    metrics = _run().report.metrics["before"]

    assert metrics == {
        "orphan_rate": 0.0,
        "duplicate_candidates": 0.0,
        "queue_age_days": 0.0,
        "link_density": 0.0,
        "neglect_rate": 0.0,
    }


def test_the_report_carries_the_five_metrics_before_and_after(fresh_db):
    _node("Alpha")
    report = _run().report

    assert set(report.metrics["before"]) == set(report.metrics["after"])
    assert set(report.metrics["before"]) == {
        "orphan_rate",
        "duplicate_candidates",
        "queue_age_days",
        "link_density",
        "neglect_rate",
    }


# ── The metrics on a populated graph ──────────────────────────────────────────


def test_orphan_rate_and_link_density_count_the_live_graph(fresh_db):
    first, second, third = _node("One"), _node("Two"), _node("Three")
    _edge(first.id, second.id)
    del third  # deliberately unconnected

    metrics = _run(jobs=[]).report.metrics["before"]

    assert metrics["orphan_rate"] == pytest.approx(1 / 3)
    assert metrics["link_density"] == pytest.approx(1 / 3)


def test_the_metrics_ignore_the_meta_space(fresh_db):
    """Meta is vocabulary and territory, not knowledge.

    The seeded type nodes are excluded by their *type* already; the node below
    is the case only the space rule catches — an ordinary claim someone filed in
    meta, which would otherwise be counted as an orphan for good.
    """
    first, second = _node("One"), _node("Two")
    _edge(first.id, second.id)
    service.create_node(type="claim", title="Filed in meta", space="meta", principal=owner())

    assert _run(jobs=[]).report.metrics["before"]["orphan_rate"] == 0.0


def test_the_duplicate_metric_counts_unresolved_candidates(fresh_db):
    first, second = _node("Alpha"), _node("Beta")
    service.create_edge(second.id, first.id, consolidate.DUPLICATE_EDGE_TYPE, principal=owner())

    assert _run(jobs=[]).report.metrics["before"]["duplicate_candidates"] == 1.0


def test_a_resolved_duplicate_candidate_leaves_the_metric(fresh_db):
    """Archived is how a candidate stops being unresolved."""
    first, second = _node("Alpha"), _node("Beta")
    edge = service.create_edge(
        second.id, first.id, consolidate.DUPLICATE_EDGE_TYPE, principal=owner()
    )
    service.transition(edge.id, "archive", principal=owner())

    assert _run(jobs=[]).report.metrics["before"]["duplicate_candidates"] == 0.0


def test_queue_age_is_the_median_age_of_the_pending_proposals(fresh_db):
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest"})
    first = service.create_node(type="claim", title="One", principal=proposer)
    second = service.create_node(type="claim", title="Two", principal=proposer)
    third = service.create_node(type="claim", title="Three", principal=proposer)
    _backdate("nodes", first.id, "created_at", 2)
    _backdate("nodes", second.id, "created_at", 10)
    _backdate("nodes", third.id, "created_at", 30)

    assert _run(jobs=[]).report.metrics["before"]["queue_age_days"] == pytest.approx(10, abs=0.01)


def test_queue_age_is_narrowed_by_the_cycles_scope(fresh_db):
    """A scoped cycle's deltas would be unreadable against a whole-file queue."""
    space = service.create_space("research", principal=owner())
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest", space.id: "suggest"})
    service.grant("builtin-gardener", space.id, "edit", principal=owner())
    elsewhere = service.create_node(type="claim", title="One", principal=proposer)
    here = service.create_node(type="claim", title="Two", space=space.id, principal=proposer)
    _backdate("nodes", elsewhere.id, "created_at", 100)
    _backdate("nodes", here.id, "created_at", 10)

    scoped = _run(jobs=[], scope=space.id).report.metrics["before"]
    whole = _run(jobs=[]).report.metrics["before"]

    assert scoped["queue_age_days"] == pytest.approx(10, abs=0.01)
    assert whole["queue_age_days"] == pytest.approx(55, abs=0.01)


def test_neglect_rate_counts_the_nodes_nobody_has_touched(fresh_db):
    fresh, stale = _node("Fresh"), _node("Stale")
    _backdate("nodes", stale.id, "updated_at", consolidate.NEGLECT_DAYS + 5)
    del fresh

    assert _run(jobs=[]).report.metrics["before"]["neglect_rate"] == pytest.approx(0.5)


# ── Job 1: duplicate candidates ───────────────────────────────────────────────


def test_two_nodes_with_the_same_title_become_a_duplicate_candidate(fresh_db):
    older, newer = _node("Kafka Streams"), _node("kafka  streams!")

    outcome = _outcome(_run().report, consolidate.JOB_DUPLICATES)

    (edge,) = _duplicates()
    assert outcome.proposed == [edge.id]
    # The *newer* node is the duplicate of the older one, never the reverse.
    assert (edge.src_id, edge.dst_id) == (newer.id, older.id)
    assert edge.type == consolidate.DUPLICATE_EDGE_TYPE
    assert edge.props["signals"] == ["title-equal"]


def test_near_identical_titles_are_a_candidate_and_dated_siblings_are_not(fresh_db):
    """The threshold is set by the false positives immediately below it."""
    _node("Kafka Stream")
    _node("Kafka Streams")
    _node("Meeting 2026-07-01")
    _node("Meeting 2026-07-02")

    _run()

    (edge,) = _duplicates()
    titles = {
        service.get_node(edge.src_id, principal=owner()).title,
        service.get_node(edge.dst_id, principal=owner()).title,
    }
    assert titles == {"Kafka Stream", "Kafka Streams"}


def test_unrelated_titles_propose_nothing(fresh_db):
    _node("Graph theory")
    _node("Sourdough starter")

    outcome = _outcome(_run().report, consolidate.JOB_DUPLICATES)

    assert outcome.examined == 2
    assert outcome.proposed == []
    assert _duplicates() == []


def test_a_pair_that_already_carries_a_duplicate_edge_is_skipped(fresh_db):
    """In *any* state: a rejected candidate must not come back every night."""
    older, newer = _node("Alpha"), _node("Alpha")
    existing = service.create_edge(
        newer.id, older.id, consolidate.DUPLICATE_EDGE_TYPE, principal=owner()
    )
    service.transition(existing.id, "archive", principal=owner())

    outcome = _outcome(_run().report, consolidate.JOB_DUPLICATES)

    assert outcome.proposed == []
    assert [skip["reason"] for skip in outcome.skipped] == [
        "a duplicate_of edge already links this pair"
    ]
    assert len(_duplicates()) == 1


def test_only_one_direction_of_a_pair_is_ever_proposed(fresh_db):
    _node("Alpha")
    _node("Alpha")

    _run()

    assert len(_duplicates()) == 1


def test_a_second_cycle_over_the_same_graph_proposes_nothing_new(fresh_db):
    """The runner is idempotent: a nightly job must not grow the queue nightly."""
    _node("Alpha")
    _node("Alpha")
    _run()

    second = _run()

    assert _outcome(second.report, consolidate.JOB_DUPLICATES).proposed == []
    assert len(_duplicates()) == 1


def test_embedding_proximity_finds_a_duplicate_titles_cannot(fresh_db):
    _place(Alpha=0.0, Beta=0.2)  # cos(0.2) ≈ 0.980, above the duplicate bar
    _node("Alpha")
    _node("Beta")

    _run()

    (edge,) = _duplicates()
    assert edge.props["signals"] == ["embedding"]
    assert edge.confidence == pytest.approx(math.cos(0.2), abs=1e-4)


def test_the_cycle_embeds_a_long_node_in_chunks_never_whole(fresh_db):
    """A node is never handed to the model whole, because the model would truncate it.

    The cycle used to call `provider.embed` on each node's entire text, which
    the projector never did — so one node had two different vectors depending
    on which subsystem asked, and anything past the model's window contributed
    to neither the duplicate signal nor `relates_to`. The abstraction job's
    cohesion criterion would have clustered documents by their opening pages.
    """
    recorder = _RecordingEmbedder()
    embeddings.set_provider(recorder)
    words = [f"w{index}" for index in range(3 * embeddings.CHUNK_WORDS)]
    words[-1] = "tailmarker"
    _node("Long", content=" ".join(words))
    _node("Other", content="short")

    _run()

    assert recorder.seen, "the cycle never embedded anything"
    longest = max(len(text.split()) for text in recorder.seen)
    assert longest <= embeddings.CHUNK_WORDS
    # …and the far end of the node really did reach the model.
    assert any("tailmarker" in text for text in recorder.seen)


def test_the_cycles_chunks_are_exactly_the_projectors_chunks(fresh_db):
    """Agreement is structural: both consumers call the same chunking function.

    One cycle drives both of them — the pairwise jobs embed the node to compare
    it, and housekeeping's catch-up runs the `vec` projector over the same node
    — so the identical chunk list has to reach the model twice in one run. That
    it appears twice *and is the same list both times* is the whole property.
    """
    recorder = _RecordingEmbedder()
    embeddings.set_provider(recorder)
    content = " ".join(f"w{index}" for index in range(3 * embeddings.CHUNK_WORDS))
    node = _node("Long", content=content)

    _run()

    expected = embeddings.node_chunks({"title": node.title, "content": node.content})
    assert len(expected) > 1
    runs = [
        index
        for index in range(len(recorder.seen) - len(expected) + 1)
        if recorder.seen[index : index + len(expected)] == expected
    ]
    assert len(runs) >= 2, "the cycle and the projector did not chunk this node alike"


def test_a_merely_related_pair_is_not_a_duplicate_candidate(fresh_db):
    """The duplicate bar sits above the link bar, so one pair is one observation."""
    _place(Alpha=0.0, Beta=_at(BETWEEN_THE_BARS))
    _node("Alpha")
    _node("Beta")

    _run()

    assert _duplicates() == []
    assert len(_related()) == 1


def test_a_scan_that_reaches_its_cap_says_so(fresh_db, monkeypatch):
    """Pairwise comparison is quadratic, so it is bounded — never silently cut."""
    monkeypatch.setattr(consolidate, "MAX_PAIRWISE_NODES", 1)
    _node("Alpha")
    _node("Alpha")

    outcome = _outcome(_run().report, consolidate.JOB_DUPLICATES)

    assert outcome.truncated is True
    assert outcome.examined == 1
    assert outcome.proposed == []
    assert any("quadratic" in note for note in outcome.notes)


def test_an_edge_scan_past_the_cap_is_reported_not_silent(fresh_db, monkeypatch):
    """A read that returns more than the cap says so rather than pretending to be complete.

    ``list_edges`` orders oldest-first, so an over-cap graph drops the *newest*
    rows — exactly the freshly rejected edges a suppression read exists to see.
    The links job's outcome carries the flag and a note, and the cycle report
    carries one too, because a metric read may have missed edges the same way.
    """
    monkeypatch.setattr(consolidate, "MAX_SCAN_EDGES", 50)
    nodes = [_node(f"Node {index}") for index in range(61)]
    for first, second in zip(nodes, nodes[1:], strict=False):
        _edge(first.id, second.id)

    result = _run(jobs=[consolidate.JOB_LINKS])
    outcome = _outcome(result.report, consolidate.JOB_LINKS)

    assert outcome.truncated is True
    assert any("MAX_SCAN_EDGES" in note for note in outcome.notes)
    assert any("MAX_SCAN_EDGES" in note for note in result.report.notes)
    # The report says what the sticky flag actually knows — an edge read hit
    # the cap, so a metric read *may* have missed edges — never that a
    # specific under-count happened.
    assert any("may have missed edges" in note for note in result.report.notes)
    assert any("duplicate_candidates" in note for note in result.report.notes)


def test_a_graph_with_exactly_the_cap_edges_is_not_truncated(fresh_db, monkeypatch):
    """The flag means "rows were dropped", not "the cap was exactly reached".

    The reads fetch one row past :data:`MAX_SCAN_EDGES` to tell the two apart:
    a graph with exactly the cap's worth of edges drops nothing, so the run
    must not report a truncation it did not suffer.
    """
    monkeypatch.setattr(consolidate, "MAX_SCAN_EDGES", 50)
    nodes = [_node(f"Node {index}") for index in range(51)]
    for first, second in zip(nodes, nodes[1:], strict=False):
        _edge(first.id, second.id)

    result = _run(jobs=[consolidate.JOB_LINKS])
    outcome = _outcome(result.report, consolidate.JOB_LINKS)

    assert outcome.truncated is False
    assert not any("MAX_SCAN_EDGES" in note for note in outcome.notes)
    assert result.report.notes == []


def test_the_duplicates_job_reports_the_cap_too(fresh_db, monkeypatch):
    """``_job_duplicates``' suppression read is capped the same way and says so."""
    monkeypatch.setattr(consolidate, "MAX_SCAN_EDGES", 50)
    hub = _node("Hub")
    for index in range(51):
        _edge(hub.id, _node(f"Spoke {index}").id, consolidate.DUPLICATE_EDGE_TYPE)

    outcome = _outcome(_run(jobs=[consolidate.JOB_DUPLICATES]).report, consolidate.JOB_DUPLICATES)

    assert outcome.truncated is True
    assert any("MAX_SCAN_EDGES" in note for note in outcome.notes)
    assert any("rejected pair could be re-proposed" in note for note in outcome.notes)


def test_a_rejected_relates_to_pair_is_not_reproposed_under_the_cap(fresh_db, monkeypatch):
    """The suppression read must hold below the cap — the guarantee the flag protects.

    Rejecting archives the ``relates_to`` edge, and the next cycle reads every
    state of the type to keep the pair out of the queue. This is the guarantee
    that silently stops holding the moment the edge count passes
    :data:`MAX_SCAN_EDGES` — which is exactly why a read at the cap is reported
    rather than silent.
    """
    monkeypatch.setattr(consolidate, "MAX_SCAN_EDGES", 50)
    _place(Alpha=0.0, Beta=_at(BETWEEN_THE_BARS))
    _node("Alpha")
    _node("Beta")

    _run()
    (proposal,) = _related()
    service.reject_proposals([proposal.id], reason="not actually related", principal=owner())
    assert _edge_state(proposal.id) == "archived"

    _run()

    assert [edge.id for edge in _related()] == [proposal.id]


def test_the_duplicate_bar_sits_above_the_link_bar():
    """Pinned, because a pair between them would otherwise queue twice."""
    assert consolidate.DUPLICATE_EMBEDDING_COSINE > consolidate.LINK_EMBEDDING_COSINE


def test_a_pair_over_both_bars_is_queued_once_as_the_duplicate_it_is(fresh_db):
    """What actually stops one observation becoming two queue items.

    It is not the ordering of the bars — every duplicate clears the link bar
    too, by construction. It is that the duplicates job runs first and writes a
    `duplicate_of` edge, which makes the pair *connected*, and link inference
    skips connected pairs. Asserted here because the bars' comments used to
    claim the ordering did this work, and a re-tune could quietly rely on it.
    """
    _place(Alpha=0.0, Beta=_at(0.99))  # far above both bars
    _node("Alpha")
    _node("Beta")

    _run()

    assert len(_duplicates()) == 1
    assert _related() == []


def test_a_rejected_link_is_not_proposed_again_next_cycle(fresh_db):
    """A queue a human works has to get shorter.

    Rejecting archives the edge, and link inference used to read only live
    edges — so the pair fell back out of the `connected` set and the next cycle
    proposed it again, unchanged and forever. The duplicates job never had the
    hole because it reads every state of its own edge type; this is the same
    read on `relates_to`.
    """
    _place(Alpha=0.0, Beta=_at(BETWEEN_THE_BARS))
    _node("Alpha")
    _node("Beta")

    _run()
    (proposal,) = _related()
    assert proposal.state == "proposed"

    service.reject_proposals([proposal.id], reason="not actually related", principal=owner())
    assert _edge_state(proposal.id) == "archived"

    _run()

    assert [edge.id for edge in _related()] == [proposal.id]


# ── Threshold calibration (tests/fixtures/embedding_calibration.json) ─────────


def _calibration():
    """The labelled pairs and their recorded cosines, grouped by band."""
    document = json.loads(CALIBRATION_FIXTURE.read_text())
    bands: dict[str, list[float]] = {}
    for pair in document["pairs"]:
        bands.setdefault(pair["band"], []).append(pair["cosine"])
    return document, bands


def test_the_calibration_fixture_covers_every_band_bilingually():
    """A set that is only English, or only easy negatives, would prove nothing."""
    document, bands = _calibration()
    assert set(bands) == {"duplicate", "related", "same_area", "unrelated"}
    assert all(len(values) >= 4 for values in bands.values())
    languages = {pair["lang"] for pair in document["pairs"]}
    assert languages == {"en", "fr", "en-fr"}
    # Length is held constant across bands on purpose: a shorter negative would
    # read as "further away" for a reason that has nothing to do with meaning.
    widths = [pair["words"] for pair in document["pairs"]]
    assert min(widths) >= 55
    assert max(widths) <= 90


def test_the_link_bar_fires_on_the_duplicate_band_which_is_the_judgement_the_queue_cannot_make():
    """The honest shape of the mislabel: every fixture duplicate clears the link bar.

    With the link bar measured at 0.60 on real content, all 10 labelled
    duplicates (0.763-0.929) are proposed as ``relates_to`` — a near-duplicate
    worded differently is not missed, it arrives under the weaker label. The
    queue cannot tell "not merely related" from "not a duplicate": that
    distinction is a judgement about the *pairs*, which is the learned-curation
    cycle's job (§L1 annotations), not a bar any cosine signal can draw on this
    content — real duplicate candidates score 0.28-0.55, overlapping the
    related band completely.

    The test this replaces asserted the same shape as a defect with a
    hand-pinned count (``len(straddling) == 7``), which broke the moment the
    bars were measured; the shape itself is the point, so it is asserted from
    the constants.
    """
    _, bands = _calibration()

    assert all(cosine >= consolidate.LINK_EMBEDDING_COSINE for cosine in bands["duplicate"])
    assert max(bands["duplicate"]) < consolidate.DUPLICATE_EMBEDDING_COSINE


def test_the_fixture_separates_its_own_bands_and_still_cannot_set_a_bar():
    """Why the fixture-derived 0.72 / 0.38 were measured, tried and reverted.

    The bands really are cleanly ordered — that is what the set was built to
    show, and it is exactly why it cannot set a bar. Its pairs were written to
    demonstrate a separation, so a value dropped into the gap between two bands
    is fitted to 29 hand-written examples. Against a real 200-node graph the
    same 0.38 proposed 1 175 ``relates_to`` edges, 5.9 per node, where the
    shipped bar proposes 5; 35.2 % of all pairs in a homogeneous prose corpus
    clear it. Pinned so the next attempt does not repeat the method — a
    replacement bar has to be measured for volume and precision on real text,
    not for separation on this file.
    """
    _, bands = _calibration()

    assert max(bands["unrelated"]) < min(bands["related"])
    assert max(bands["related"]) < min(bands["duplicate"])


def test_the_bars_record_their_measurement_and_the_invariant():
    """The comment is load-bearing, so it is asserted rather than trusted.

    Without it these are two ordinary-looking constants whose values only a
    real-corpus measurement explains — a bar fitted to the fixture cannot
    measure a false-positive rate, and the next reader must be told that the
    numbers are the measured ones, not a second guess at the fixture.
    """
    source = Path(consolidate.__file__).read_text()
    marker = source.index("DUPLICATE_EMBEDDING_COSINE = ")
    preamble = source[:marker]

    assert "real corpus" in preamble
    assert "measure_kasten_calibration" in preamble
    assert "must stay above" in preamble
    # The measured values themselves, pinned hard. A regression to the reverted
    # flood bar (0.38 — 5.9-6.4 relates_to per node on the calibration corpus)
    # must fail loudly here, because nothing else in the suite catches that
    # direction: the invariant only catches the link bar crossing the
    # duplicate bar, and the fixture band only catches the high side. Both were
    # measured by scripts/measure_kasten_calibration.py on the 426-note
    # calibration corpus on 2026-08-02.
    assert pytest.approx(0.60) == consolidate.LINK_EMBEDDING_COSINE
    assert pytest.approx(0.93) == consolidate.DUPLICATE_EMBEDDING_COSINE


@pytest.mark.skipif(
    os.environ.get("NODUM_RUN_SLOW") != "1",
    reason="real-model smoke test: set NODUM_RUN_SLOW=1 (downloads the model once)",
)
def test_real_embeddings_fire_the_measured_link_bar():
    """The test that actually embeds text — the thing the fixture tests cannot do.

    The fixture tests read recorded cosines and never embed, which is exactly
    why they could not see the flood: a bar fitted to 29 hand-labelled pairs
    says nothing about a false-positive rate on real content. This test drives
    the pinned model through :func:`nodum.embeddings.node_vectors` — the same
    call the consolidation cycle makes — over real prose: pairs reused from the
    calibration fixture (whose recorded cosines it must reproduce within a
    margin, since the point is the bar's *behaviour* and not the fourth
    decimal) plus clearly same-area pairs of its own construction. The measured
    link bar at 0.60 fires on genuinely related content and rejects the junk
    the gate cited.
    """
    embeddings.reset_provider()
    provider = embeddings.get_provider()
    if provider is None:
        pytest.skip(f"no embedding provider: {embeddings.unavailable_reason()}")

    document = json.loads(CALIBRATION_FIXTURE.read_text())
    recorded = {pair["id"]: pair for pair in document["pairs"]}
    # Same-area pairs from the fixture's duplicate band (0.763-0.929, all far
    # above the 0.60 link bar), junk from its unrelated band (max 0.314), and
    # one pair from the related band (0.521) scoring *between the reverted
    # flood bar and the measured bar*: at 0.38 it fires, at 0.60 it must not,
    # so a regression to the flood value fails this test behaviourally and not
    # only on the pinned constant.
    reused = [
        "dup-en-backpressure",
        "dup-fr-retention",
        "dup-en-fractional-index",
        "unrel-en-sourdough",
        "unrel-fr-tomates",
        "rel-xl-projector",
    ]
    own_pairs = [
        (
            "Software architecture is mostly taught as a catalogue of patterns, but the "
            "working knowledge is the trade-offs: where a layered design buys "
            "changeability and where it buys latency, when an event bus is decoupling "
            "and when it is a second database. A developer who can argue those "
            "trade-offs on a real codebase understands architecture; one who can name "
            "twenty patterns has learned a vocabulary.",
            "Software architecture is usually presented as a catalogue of patterns, but "
            "the real knowledge is the trade-offs: when a layered design buys "
            "changeability and when it buys latency, when an event bus decouples and "
            "when it is just a second database. A developer who can argue those "
            "trade-offs on a real codebase understands architecture; one who can name "
            "twenty patterns has only learned a vocabulary.",
        ),
        (
            "A personal knowledge base only earns its keep when retrieval is faster than "
            "memory. The failure mode is the collector's archive: notes saved for later, "
            "filed under categories that seemed sensible at the time, and never linked to "
            "anything that would bring them back. Links and indexes are what make a note "
            "findable, and they are also what connect it to the question that needs it.",
            "A knowledge base pays off only when finding a note is faster than remembering "
            "it. The failure mode is the collector's archive: notes put aside for later, "
            "filed under categories that made sense at the time, and never linked to "
            "anything that would bring them back. Links and indexes are what make a note "
            "findable, and they are also what connect it to the question that needs it.",
        ),
    ]

    def cosine_of(left: str, right: str) -> float:
        first, second = embeddings.node_vectors(
            provider,
            [{"title": None, "content": left}, {"title": None, "content": right}],
        )
        return consolidate._cosine(first, second)

    for pair_id in reused:
        pair = recorded[pair_id]
        measured = cosine_of(pair["left"], pair["right"])
        assert measured == pytest.approx(pair["cosine"], abs=0.02), pair_id
        if pair["band"] == "duplicate":
            assert measured >= consolidate.LINK_EMBEDDING_COSINE, pair_id
        else:
            assert measured < consolidate.LINK_EMBEDDING_COSINE, pair_id

    for left, right in own_pairs:
        assert cosine_of(left, right) >= consolidate.LINK_EMBEDDING_COSINE


def test_the_job_degrades_to_titles_when_no_model_is_present(fresh_db):
    """An install without the extra is the commonest configuration there is."""
    _node("Alpha")
    _node("Alpha")

    outcome = _outcome(_run().report, consolidate.JOB_DUPLICATES)

    assert outcome.error is None
    assert len(outcome.proposed) == 1
    assert any("no embedding provider" in note for note in outcome.notes)


def test_every_job_that_degrades_says_so_in_the_same_run(fresh_db):
    """The commonest install has no model, and a live pass proved it runs degraded.

    All three embedding-dependent halves — the duplicate cosine, `relates_to`
    from proximity, and the `vec` projector's catch-up — must report their own
    absence in the report a human reads, not just the first one to notice it.
    """
    _node("Alpha")
    _node("Alpha")

    report = _run().report

    degraded = {
        job.name for job in report.jobs if any("no embedding provider" in n for n in job.notes)
    }
    assert degraded == {
        consolidate.JOB_DUPLICATES,
        consolidate.JOB_LINKS,
        consolidate.JOB_HOUSEKEEPING,
    }
    assert report.failed == []


def test_with_a_provider_the_job_never_claims_it_degraded(fresh_db):
    _place(Alpha=0.0)
    _node("Alpha")

    outcome = _outcome(_run().report, consolidate.JOB_DUPLICATES)

    assert not [note for note in outcome.notes if "no embedding provider" in note]


def test_a_duplicate_candidate_lands_proposed_and_reaches_the_review_queue(fresh_db):
    """The point of the job: the gardener holds `edit` and files a proposal anyway.

    A grant is a ceiling, not a mandate (design §8.3), so the candidate arrives
    where a human reviews it rather than as asserted fact.
    """
    _node("Alpha")
    _node("Alpha")

    outcome = _outcome(_run().report, consolidate.JOB_DUPLICATES)

    (edge,) = _duplicates()
    assert edge.state == "proposed"
    assert outcome.detail["landed"] == ["proposed"]
    queued = [item for item in service.list_proposals(principal=owner()) if item.kind == "edge"]
    assert [item.id for item in queued] == [edge.id]
    assert queued[0].created_by == "agent:builtin-gardener"


def test_an_inferred_link_lands_proposed_too(fresh_db):
    """One door, one landing state: the link job suggests as much as job 1 does."""
    first, second = _node("First"), _node("Second")
    for hub in (_node("HubOne"), _node("HubTwo")):
        _edge(first.id, hub.id)
        _edge(second.id, hub.id)

    outcome = _outcome(_run().report, consolidate.JOB_LINKS)

    assert {edge.state for edge in _related()} == {"proposed"}
    assert outcome.detail["landed"] == ["proposed"]


def test_the_gardener_still_prunes_with_the_grant_it_files_proposals_under(fresh_db):
    """Filing below the grant is per write — it does not disarm the `edit` grant."""
    first, second = _node("First"), _node("Second")
    kept = _edge(first.id, second.id)
    duplicate = _edge(first.id, second.id)

    outcome = _outcome(_run().report, consolidate.JOB_LINKS)

    assert outcome.applied == [duplicate.id]
    assert _edge_state(duplicate.id) == "archived"
    assert _edge_state(kept.id) == "active"


def test_a_reviewed_candidate_does_not_come_back_next_cycle(fresh_db):
    """Accepting is what makes the candidate live, and the cycle leaves it alone."""
    _node("Alpha")
    _node("Alpha")
    _run()
    (candidate,) = _duplicates()
    service.transition(candidate.id, "accept", principal=owner())

    second = _run()

    assert _outcome(second.report, consolidate.JOB_DUPLICATES).proposed == []
    assert [edge.id for edge in _duplicates()] == [candidate.id]
    assert _edge_state(candidate.id) == "active"


def test_consolidation_never_proposes_a_merge(fresh_db):
    """D9: a merge is always human-approved, implemented as the cycle not doing one."""
    _node("Alpha")
    _node("Alpha")

    _run()

    assert [event.op for event in _events() if event.op == "node.merge"] == []


# ── What the gardener's grant has to be ──────────────────────────────────────


def test_a_full_cycle_completes_on_read_over_meta(fresh_db):
    """`0014` seeds `read` on meta because that is what consolidation needs.

    The migration's first cut said `edit`, justified as "consolidation reads and
    **writes** the type vocabulary". It never writes it: `_is_curatable` excludes
    the meta space and the structural types from every job, so the only thing
    meta is used for is resolving a type — which is a READ. What the extra level
    bought was latent authority no job reaches: creating spaces, renaming `main`,
    and archiving the `note` type, after which a *human* is blocked too.
    (Migration `0016`'s `conventions: edit` is the gardener's own workspace,
    not the type vocabulary.)
    """
    assert auth.internal_principal().grants == {
        "meta": "read",
        "main": "edit",
        "conventions": "edit",
    }
    _node("Alpha")
    _node("Alpha")
    first, second = _node("First"), _node("Second")
    _edge(first.id, second.id)
    _edge(first.id, second.id)

    result = _run()

    assert result.cycle.status == "completed"
    assert result.report.failed == []
    assert [job.error for job in result.report.jobs] == [None] * len(consolidate.JOBS)
    # Both halves still work: a proposal was filed and a duplicate edge pruned.
    assert _outcome(result.report, consolidate.JOB_DUPLICATES).proposed
    assert _outcome(result.report, consolidate.JOB_LINKS).applied


def test_no_meta_grant_at_all_is_what_actually_breaks_a_cycle(fresh_db):
    """The lower bound under the test above: `read` is not decoration.

    With the grant gone the edge type stops resolving before the first job even
    runs, which is the failure the level exists to prevent — so `read` is the
    smallest grant that works, not merely one that happens to.
    """
    service.revoke("builtin-gardener", "meta", principal=owner())
    _node("Alpha")
    _node("Alpha")

    with pytest.raises(service.TypeNotFound, match="unknown edge type: duplicate_of"):
        _run()

    (cycle,) = service.list_cycles(principal=owner())
    assert cycle.status == "failed"
    assert _duplicates() == []


def test_the_gardener_cannot_rewrite_the_type_vocabulary_it_reads(fresh_db):
    """The authority `edit` on meta bought, and no shipped job ever reached.

    Archiving the `note` type blocks every human write of that type too, so this
    is not a theoretical over-grant — and it is what a 5b job would inherit by
    default.
    """
    gardener = auth.internal_principal()

    with pytest.raises(GrantNotPermitted):
        service.transition("note", "archive", principal=gardener)
    with pytest.raises(GrantNotPermitted):
        service.rename_space("main", "renamed by the gardener", principal=gardener)
    with pytest.raises(GrantNotPermitted):
        service.create_space("invented", principal=gardener)


# ── Job 2: link inference and pruning ─────────────────────────────────────────


def test_two_nodes_sharing_two_neighbours_are_proposed_as_related(fresh_db):
    """Co-citation is symmetric, so a 2×2 bipartite graph yields both pairs.

    ``first`` and ``second`` share ``hub_one`` and ``hub_two``; the hubs share
    the other two just as much. Both proposals are correct — asserting only one
    would be asserting a bug.
    """
    first, second = _node("First"), _node("Second")
    hub_one, hub_two = _node("HubOne"), _node("HubTwo")
    for hub in (hub_one, hub_two):
        _edge(first.id, hub.id)
        _edge(second.id, hub.id)

    outcome = _outcome(_run().report, consolidate.JOB_LINKS)

    edges = _related()
    assert sorted(outcome.proposed) == sorted(edge.id for edge in edges)
    assert {frozenset((edge.src_id, edge.dst_id)) for edge in edges} == {
        frozenset((first.id, second.id)),
        frozenset((hub_one.id, hub_two.id)),
    }
    assert {tuple(edge.props["signals"]) for edge in edges} == {("co-citation",)}
    assert {edge.props["shared_neighbours"] for edge in edges} == {2}


def test_one_shared_neighbour_is_structure_and_not_evidence(fresh_db):
    """Every pair of siblings shares a parent; that must not become a link."""
    first, second, hub = _node("First"), _node("Second"), _node("Hub")
    _edge(first.id, hub.id)
    _edge(second.id, hub.id)

    _run()

    assert _related() == []


def test_a_pair_the_graph_already_connects_is_not_proposed_again(fresh_db):
    first, second = _node("First"), _node("Second")
    hub_one, hub_two = _node("HubOne"), _node("HubTwo")
    for hub in (hub_one, hub_two):
        _edge(first.id, hub.id)
        _edge(second.id, hub.id)
    _edge(first.id, second.id, "contradicts")

    _run()

    # The hub pair is still co-cited and still proposed; the connected pair is not.
    proposed = {frozenset((edge.src_id, edge.dst_id)) for edge in _related()}
    assert proposed == {frozenset((hub_one.id, hub_two.id))}


def test_embedding_proximity_proposes_a_link_on_its_own(fresh_db):
    _place(Alpha=0.0, Beta=_at(BETWEEN_THE_BARS))
    first, second = _node("Alpha"), _node("Beta")

    _run()

    (edge,) = _related()
    assert {edge.src_id, edge.dst_id} == {first.id, second.id}
    assert edge.props["signals"] == ["embedding"]


def test_a_distant_pair_is_left_alone(fresh_db):
    _place(Alpha=0.0, Beta=_at(consolidate.LINK_EMBEDDING_COSINE - 0.1))
    _node("Alpha")
    _node("Beta")

    _run()

    assert _related() == []


def test_an_exact_duplicate_edge_is_pruned_and_the_oldest_kept(fresh_db):
    first, second = _node("First"), _node("Second")
    kept = _edge(first.id, second.id)
    duplicate = _edge(first.id, second.id)

    outcome = _outcome(_run().report, consolidate.JOB_LINKS)

    assert outcome.applied == [duplicate.id]
    assert _edge_state(duplicate.id) == "archived"
    assert _edge_state(kept.id) == "active"


def test_the_oldest_of_a_duplicate_group_is_the_one_kept(fresh_db):
    """Which one survives is not arbitrary: the oldest edge is the record.

    ``datetime('now')`` has one-second resolution, so a group written in one
    second must not be ordered by uuid — the row written first is the one kept,
    and this fixes the timestamps so the rule is actually asserted.
    """
    first, second = _node("First"), _node("Second")
    written_first = _edge(first.id, second.id)
    written_second = _edge(first.id, second.id)
    _backdate("edges", written_second.id, "created_at", 1)  # now the older row

    _run()

    assert _edge_state(written_second.id) == "active"
    assert _edge_state(written_first.id) == "archived"


def test_edges_differing_in_type_or_direction_are_not_duplicates(fresh_db):
    first, second = _node("First"), _node("Second")
    same_way = _edge(first.id, second.id, "supports")
    other_type = _edge(first.id, second.id, "contradicts")
    other_way = _edge(second.id, first.id, "supports")

    _run()

    assert {_edge_state(edge.id) for edge in (same_way, other_type, other_way)} == {"active"}


def test_an_edge_incident_to_an_archived_node_is_pruned(fresh_db):
    first, second = _node("First"), _node("Second")
    edge = _edge(first.id, second.id)
    service.transition(second.id, "archive", principal=owner())

    outcome = _outcome(_run().report, consolidate.JOB_LINKS)

    assert outcome.applied == [edge.id]
    assert _edge_state(edge.id) == "archived"


def test_the_pruner_leaves_a_proposed_edge_for_the_human(fresh_db):
    """Retiring a proposal is a review decision, and the queue is the human's."""
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest"})
    first, second = _node("First"), _node("Second")
    proposal = service.create_edge(first.id, second.id, "supports", principal=proposer)
    service.transition(second.id, "archive", principal=owner())

    outcome = _outcome(_run().report, consolidate.JOB_LINKS)

    assert _edge_state(proposal.id) == "proposed"
    assert proposal.id in [skip["id"] for skip in outcome.skipped]


def test_a_healthy_graph_is_pruned_of_nothing(fresh_db):
    first, second, third = _node("First"), _node("Second"), _node("Third")
    _edge(first.id, second.id)
    _edge(second.id, third.id)

    outcome = _outcome(_run().report, consolidate.JOB_LINKS)

    assert outcome.applied == []
    assert outcome.examined == 2


# ── Job 3: housekeeping ───────────────────────────────────────────────────────


def test_the_position_rebalance_is_a_no_op_that_says_why(fresh_db):
    parent = _node("Parent", type="page")
    for index in range(3):
        _node(f"Child {index}", type="block", parent_id=parent.id)

    outcome = _outcome(_run().report, consolidate.JOB_HOUSEKEEPING)

    assert outcome.applied == []
    assert outcome.detail["tight_pair_count"] == 0
    assert consolidate.POSITION_NOOP_NOTE in outcome.notes


def test_the_gap_check_is_live_rather_than_decorative(fresh_db):
    """When a reorder operation lands, this is the job that notices."""
    parent = _node("Parent", type="page")
    first = _node("Child A", type="block", parent_id=parent.id)
    second = _node("Child B", type="block", parent_id=parent.id)
    _set_position(first.id, 1.0)
    _set_position(second.id, 1.0 + consolidate.MIN_POSITION_GAP / 10)

    outcome = _outcome(_run().report, consolidate.JOB_HOUSEKEEPING)

    assert outcome.detail["tight_pairs"] == [[first.id, second.id]]
    assert any("fractional positions" in note for note in outcome.notes)
    # Still a report, never a rewrite: nothing was applied.
    assert outcome.applied == []


def test_embedding_catch_up_drives_the_existing_vec_projector(fresh_db):
    _place(Alpha=0.0)
    _node("Alpha")

    outcome = _outcome(_run().report, consolidate.JOB_HOUSEKEEPING)

    assert outcome.detail["vec_projector"]["name"] == "vec"
    assert outcome.detail["vec_projector"]["applied"] > 0
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"] > 0
    finally:
        conn.close()


def test_embedding_catch_up_reports_an_unavailable_projector_and_survives(fresh_db):
    _node("Alpha")

    outcome = _outcome(_run().report, consolidate.JOB_HOUSEKEEPING)

    assert outcome.error is None
    assert outcome.detail["vec_projector"]["applied"] == 0
    assert any("vec projector unavailable" in note for note in outcome.notes)


# ── Job 4: the neglect report ─────────────────────────────────────────────────


def test_the_neglect_report_names_the_untouched_and_writes_nothing(fresh_db):
    fresh, stale = _node("Fresh"), _node("Stale")
    _backdate("nodes", stale.id, "updated_at", consolidate.NEGLECT_DAYS + 1)

    outcome = _outcome(_run(jobs=[consolidate.JOB_NEGLECT]).report, consolidate.JOB_NEGLECT)

    assert outcome.detail["neglected"] == [stale.id]
    assert fresh.id not in outcome.detail["neglected"]
    assert outcome.proposed == []
    assert outcome.applied == []


def test_a_node_inside_the_threshold_is_not_neglected(fresh_db):
    node = _node("Recent")
    _backdate("nodes", node.id, "updated_at", consolidate.NEGLECT_DAYS - 1)

    outcome = _outcome(_run(jobs=[consolidate.JOB_NEGLECT]).report, consolidate.JOB_NEGLECT)

    assert outcome.detail["neglected"] == []


def test_the_neglect_job_writes_no_events_at_all(fresh_db):
    """Two nodes, so a job that decided to link or merge them would have somewhere

    to write — and it still writes nothing.
    """
    stale, fresh = _node("Stale"), _node("Also stale")
    _backdate("nodes", stale.id, "updated_at", consolidate.NEGLECT_DAYS + 1)
    _backdate("nodes", fresh.id, "updated_at", consolidate.NEGLECT_DAYS + 1)

    result = _run(jobs=[consolidate.JOB_NEGLECT])

    assert service.list_events(owner(), cycle_id=result.cycle.id) == []


def test_the_run_measures_every_age_against_one_pinned_clock(fresh_db, monkeypatch):
    """One clock per run, patchable in one place — the determinism rail.

    A node written a moment ago is neglected iff the clock says so, which is what
    makes an age assertion a test rather than a wait.
    """
    node = _node("Not actually old")
    pinned = datetime.now(UTC) + timedelta(days=consolidate.NEGLECT_DAYS + 1)
    monkeypatch.setattr(consolidate, "_utcnow", lambda: pinned)

    outcome = _outcome(_run(jobs=[consolidate.JOB_NEGLECT]).report, consolidate.JOB_NEGLECT)

    assert outcome.detail["neglected"] == [node.id]


# ── Dry run ───────────────────────────────────────────────────────────────────


def test_a_dry_run_writes_a_journal_entry_and_nothing_else(fresh_db):
    older, newer = _node("Alpha"), _node("Alpha")
    first, second = _node("First"), _node("Second")
    duplicate_edge = _edge(first.id, second.id)
    _edge(first.id, second.id)
    before_seq = _max_seq()

    result = _run(dry_run=True)

    assert result.cycle.dry_run is True
    assert result.cycle.status == "completed"
    # The cycle produced no events at all — the checkable form of "changed nothing".
    assert service.list_events(owner(), cycle_id=result.cycle.id) == []
    assert _max_seq() == before_seq
    assert _duplicates() == []
    assert _edge_state(duplicate_edge.id) == "active"
    assert service.get_node(older.id, principal=owner()).state == "active"
    assert service.get_node(newer.id, principal=owner()).state == "active"


def test_a_dry_run_still_reports_what_it_would_have_done(fresh_db):
    _node("Alpha")
    _node("Alpha")
    first, second = _node("First"), _node("Second")
    for hub in (_node("HubOne"), _node("HubTwo")):
        _edge(first.id, hub.id)
        _edge(second.id, hub.id)

    report = _run(dry_run=True).report

    duplicates = _outcome(report, consolidate.JOB_DUPLICATES)
    assert duplicates.detail["matched"] == 1
    assert any("would propose 1 duplicate_of" in note for note in duplicates.notes)
    links = _outcome(report, consolidate.JOB_LINKS)
    assert links.detail["inferred"] == 2
    assert any("would propose 2 relates_to" in note for note in links.notes)
    assert _related() == []
    # And the preview changed nothing, so the two snapshots agree.
    assert report.metrics["before"] == report.metrics["after"]


def test_a_dry_run_does_not_run_the_vec_projector(fresh_db):
    _place(Alpha=0.0)
    _node("Alpha")

    outcome = _outcome(_run(dry_run=True).report, consolidate.JOB_HOUSEKEEPING)

    assert "vec_projector" not in outcome.detail
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"] == 0
    finally:
        conn.close()


# ── Failure ───────────────────────────────────────────────────────────────────


def test_a_failing_job_closes_the_cycle_failed_without_losing_the_others(fresh_db, monkeypatch):
    def _explode(context):
        raise RuntimeError("the job fell over")

    monkeypatch.setitem(consolidate.JOBS, consolidate.JOB_LINKS, _explode)
    _node("Alpha")
    _node("Alpha")

    result = _run()

    assert result.cycle.status == "failed"
    assert [failure.job for failure in result.report.failed] == [consolidate.JOB_LINKS]
    assert "the job fell over" in result.report.failed[0].error
    # The other jobs still ran and still reported.
    assert _outcome(result.report, consolidate.JOB_DUPLICATES).proposed
    assert _outcome(result.report, consolidate.JOB_NEGLECT).error is None
    # And the metrics were still taken on both sides.
    assert set(result.report.metrics) == {"before", "after"}


def test_the_events_a_failed_cycle_wrote_stay_and_stay_stamped(fresh_db, monkeypatch):
    """They are real writes; a rollback is what takes them back, not a rollback of

    the report.
    """

    def _explode(context):
        raise RuntimeError("boom")

    monkeypatch.setitem(consolidate.JOBS, consolidate.JOB_HOUSEKEEPING, _explode)
    _node("Alpha")
    _node("Alpha")

    result = _run()

    stamped = service.list_events(owner(), cycle_id=result.cycle.id)
    assert [event.op for event in stamped] == ["edge.propose"]
    assert result.cycle.status == "failed"


def test_a_failure_outside_a_job_closes_the_cycle_and_re_raises(fresh_db, monkeypatch):
    """A scope the gardener cannot read is a caller error, not a job result."""
    monkeypatch.setattr(
        consolidate, "_metrics", lambda context: (_ for _ in ()).throw(RuntimeError("no scope"))
    )

    with pytest.raises(RuntimeError, match="no scope"):
        _run()

    (cycle,) = service.list_cycles(principal=owner())
    assert cycle.status == "failed"
    assert cycle.report["failed"][0]["error"] == "RuntimeError: no scope"


def test_ctrl_c_closes_the_cycle_instead_of_leaving_it_running(fresh_db, monkeypatch):
    """`KeyboardInterrupt` is a `BaseException`, and it used to escape the guard.

    A cycle left `running` cannot be rolled back and `undo` refuses every event
    it stamped, so the writes it managed to make before the interrupt were
    irreversible on every surface. Ctrl-C during `nodum consolidate` is the
    ordinary way to meet it.
    """

    def _interrupt(context):
        raise KeyboardInterrupt

    monkeypatch.setitem(consolidate.JOBS, consolidate.JOB_LINKS, _interrupt)
    _node("Alpha")
    _node("Alpha")

    with pytest.raises(KeyboardInterrupt):
        _run()

    (cycle,) = service.list_cycles(principal=owner())
    assert cycle.status == "failed"
    assert "KeyboardInterrupt" in cycle.report["failed"][0]["error"]
    # The writes it did make are inside a closed cycle, so rollback reaches them —
    # which a `running` cycle refuses outright.
    rollback = service.rollback_cycle(cycle.id, principal=owner())
    assert rollback.rollback_cycle_id is not None
    assert service.get_cycle(cycle.id, principal=owner()).status == "rolled_back"


# ── One cycle at a time ───────────────────────────────────────────────────────


def _run_in_thread(results, **kwargs):
    def _target():
        try:
            results.append(_run(**kwargs))
        except BaseException as failure:
            # Reported, not swallowed: the caller asserts on what came out.
            results.append(failure)

    thread = threading.Thread(target=_target)
    thread.start()
    return thread


def test_a_second_cycle_is_refused_while_one_is_running(fresh_db, monkeypatch):
    """The scheduler's no-overlap property guarded it against *itself* only.

    Nothing serialised the nightly task against `POST /api/cycles` or `nodum
    consolidate`, and the duplicate job's "a pair already carrying a
    `duplicate_of` edge is left alone" is a read-then-write with no transaction
    spanning it — so two concurrent runs proposed every pair twice and the human
    got a review queue of the same size as the graph.
    """
    inside = threading.Event()
    release = threading.Event()

    def _hold(context):
        inside.set()
        assert release.wait(10), "the second caller never returned"
        return consolidate.JobOutcome(name=consolidate.JOB_NEGLECT)

    monkeypatch.setitem(consolidate.JOBS, consolidate.JOB_NEGLECT, _hold)
    _node("Alpha")
    _node("Alpha")
    results: list = []
    first = _run_in_thread(results, jobs=[consolidate.JOB_NEGLECT])
    try:
        assert inside.wait(10), "the first cycle never started"
        with pytest.raises(consolidate.CycleInProgress, match="already running"):
            _run(jobs=[consolidate.JOB_NEGLECT])
    finally:
        release.set()
        first.join(10)

    assert [type(result) for result in results] == [consolidate.ConsolidationOut]
    # The refused caller opened no cycle, so the journal records one run.
    assert len(service.list_cycles(principal=owner())) == 1


def test_a_second_process_is_refused_too(fresh_db, tmp_path):
    """The half a process lock never covered, driven through a real second process.

    A `nodum consolidate` fired while the server ran one is two processes, and a
    module-level lock is in neither of the other's. Both completed: 1580
    `duplicate_of` edges over 790 pairs, every pair proposed twice, and two
    journal rows for one human intention. The review queue is the human's, and
    doubling it is precisely the defect the in-process lock was raised against —
    so the guard has to live where both processes can see it, which is the
    `cycles` row.

    This test is deliberately a **subprocess** rather than a second thread: a
    thread shares the lock that was there before, so it could not tell a guard
    in Python apart from a guard in the database, which is the whole question.
    """
    open_cycle = service.open_cycle(trigger="scheduled", principal=owner())
    assert open_cycle.status == "running"

    program = (
        "from nodum import consolidate\n"
        "from nodum import auth\n"
        "consolidate.consolidate(triggered_by=auth.OWNER_ACTOR, jobs=['neglect_report'])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env={**os.environ, db.ENV_DB_VAR: str(fresh_db), "NODUM_CONSOLIDATE_AT": ""},
        cwd=tmp_path,
        timeout=120,
    )

    assert result.returncode != 0, result.stdout
    assert "CycleInProgress" in result.stderr
    assert "already running" in result.stderr
    # The second process opened no cycle, so the journal still records one run.
    assert [cycle.id for cycle in service.list_cycles(principal=owner())] == [open_cycle.id]


def test_the_lock_is_released_when_a_cycle_fails(fresh_db, monkeypatch):
    """A refusal that outlived one crash would be a nightly job nobody can restart."""
    original = consolidate.JOBS[consolidate.JOB_NEGLECT]

    def _explode(context):
        raise RuntimeError("boom")

    monkeypatch.setitem(consolidate.JOBS, consolidate.JOB_NEGLECT, _explode)
    assert _run(jobs=[consolidate.JOB_NEGLECT]).cycle.status == "failed"

    monkeypatch.setitem(consolidate.JOBS, consolidate.JOB_NEGLECT, original)
    assert _run(jobs=[consolidate.JOB_NEGLECT]).cycle.status == "completed"


def test_the_lock_is_released_when_a_cycle_is_interrupted(fresh_db, monkeypatch):
    original = consolidate.JOBS[consolidate.JOB_NEGLECT]

    def _interrupt(context):
        raise KeyboardInterrupt

    monkeypatch.setitem(consolidate.JOBS, consolidate.JOB_NEGLECT, _interrupt)
    with pytest.raises(KeyboardInterrupt):
        _run(jobs=[consolidate.JOB_NEGLECT])

    monkeypatch.setitem(consolidate.JOBS, consolidate.JOB_NEGLECT, original)
    assert _run(jobs=[consolidate.JOB_NEGLECT]).cycle.status == "completed"


def test_a_refused_second_cycle_is_a_clean_message_and_not_a_traceback(fresh_db):
    """It reaches a human through the CLI and the journal's run button alike."""
    assert issubclass(consolidate.CycleInProgress, ValueError)


# ── Attribution and the cycle stamp ───────────────────────────────────────────


def test_every_write_the_cycle_makes_is_stamped_and_the_gardeners(fresh_db):
    older, newer = _node("Alpha"), _node("Alpha")
    first, second = _node("First"), _node("Second")
    _edge(first.id, second.id)
    _edge(first.id, second.id)
    del older, newer
    before_seq = _max_seq()

    result = _run()

    written = [event for event in _events() if event.seq > before_seq]
    assert written, "the cycle wrote something"
    assert {event.cycle_id for event in written} == {result.cycle.id}
    assert {event.actor for event in written} == {"agent:builtin-gardener"}


def test_the_journal_records_who_asked_and_the_events_record_who_acted(fresh_db):
    _node("Alpha")
    _node("Alpha")

    result = _run(triggered_by=auth.OWNER_ACTOR)

    assert result.cycle.trigger == "manual"
    assert result.cycle.triggered_by == auth.OWNER_ACTOR
    assert {event.actor for event in service.list_events(owner(), cycle_id=result.cycle.id)} == {
        "agent:builtin-gardener"
    }


def test_a_scheduled_run_names_the_clock_and_not_an_account(fresh_db):
    result = _run(triggered_by=service.SCHEDULER_ACTOR)

    assert result.cycle.trigger == "scheduled"
    assert result.cycle.triggered_by == service.SCHEDULER_ACTOR


def test_an_unknown_trigger_account_cannot_ask_for_a_cycle(fresh_db):
    with pytest.raises(auth.UnknownPrincipal):
        _run(triggered_by="human:nobody")


def test_a_disabled_gardener_stops_the_runner(fresh_db):
    """The supported way to switch the gardener off is to disable its account."""
    service.disable_agent("builtin-gardener", principal=owner())

    with pytest.raises(auth.PrincipalDisabled):
        _run()


def test_the_runner_names_no_internal_agent_and_so_refuses_a_second(fresh_db):
    """An internal identity a caller could name is one a caller could choose.

    The runner asks :func:`nodum.auth.internal_principal` for *the* in-process
    identity rather than loading one by id, so a file holding two internal
    accounts stops instead of picking whichever the id string happened to spell.
    """
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO agents (id, kind, name, owner_human_id, credential_hash)"
            " VALUES ('builtin-impostor', 'internal', 'builtin-impostor', NULL, NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(auth.UnknownPrincipal, match="more than one internal agent"):
        _run()


# ── Scope and job selection ───────────────────────────────────────────────────


def test_a_scope_confines_the_run_to_one_space(fresh_db):
    space = service.create_space("research", principal=owner())
    service.grant("builtin-gardener", "research", "edit", principal=owner())
    _node("Alpha")
    _node("Alpha")
    service.create_node(type="claim", title="Beta", space="research", principal=owner())
    service.create_node(type="claim", title="Beta", space="research", principal=owner())

    result = _run(scope="research")

    assert result.cycle.scope == space.id
    (edge,) = _duplicates()
    assert service.get_node(edge.src_id, principal=owner()).space_id == space.id


def test_a_scope_the_gardener_holds_no_grant_on_names_the_grant_command(fresh_db):
    """The one message a human must never be shown is "that space does not exist".

    Every space created after `0014` is invisible to the gardener until somebody
    grants it, and the scope picker offers those spaces on the first click. The
    read that used to fail was `list_nodes(space=…, principal=gardener)`, deep
    inside the metrics, and it failed with the Q13 non-oracle refusal — which is
    the right sentence for a caller who lacks the grant and the wrong one here,
    where the caller can see the space and it is the *gardener* that cannot. It
    also landed in a permanent journal row the dream journal splices into an
    entry's headline, complete with a bare 32-hex id.
    """
    space = service.create_space("research", principal=owner())

    with pytest.raises(GrantNotPermitted) as refusal:
        _run(scope="research")

    message = str(refusal.value)
    assert "builtin-gardener" in message
    assert "nodum grant builtin-gardener research edit" in message
    # The caller supplied a name, so the refusal quotes a name.
    assert space.id not in message
    assert "unknown space" not in message
    # And the journal entry a human actually reads says the same thing.
    (cycle,) = service.list_cycles(principal=owner())
    assert cycle.status == "failed"
    journal = cycle.report["failed"][0]["error"]
    assert "nodum grant builtin-gardener research edit" in journal
    assert "unknown space" not in journal
    assert space.id not in journal


def test_a_scope_the_gardener_can_only_read_still_runs(fresh_db):
    """`read` resolves the space, which is all the metrics and the reads need.

    The refusal above is about a scope the gardener cannot see at all. A grant
    below `edit` is a narrower posture, not a broken one: the reads work, the
    writes are refused one suggestion at a time and reported, and the cycle
    still closes.
    """
    service.create_space("research", principal=owner())
    service.grant("builtin-gardener", "research", "read", principal=owner())
    service.create_node(type="claim", title="Beta", space="research", principal=owner())
    service.create_node(type="claim", title="Beta", space="research", principal=owner())

    result = _run(scope="research")

    assert result.cycle.status == "completed"
    outcome = _outcome(result.report, consolidate.JOB_DUPLICATES)
    assert outcome.error is None
    # The candidate was found and the write refused — reported, not raised.
    assert outcome.detail["matched"] == 1
    assert outcome.proposed == []
    assert len(outcome.skipped) == 1
    assert _duplicates() == []


def test_an_unscoped_run_needs_no_new_grant(fresh_db):
    """The nightly default must not start asking for one."""
    service.create_space("research", principal=owner())
    _node("Alpha")
    _node("Alpha")

    assert _run().cycle.status == "completed"


def test_a_subset_of_jobs_runs_and_the_rest_do_not(fresh_db):
    _node("Alpha")
    _node("Alpha")

    report = _run(jobs=[consolidate.JOB_NEGLECT]).report

    assert [job.name for job in report.jobs] == [consolidate.JOB_NEGLECT]
    assert _duplicates() == []


def test_an_unknown_job_is_refused_by_name(fresh_db):
    with pytest.raises(ValueError, match="unknown consolidation job"):
        _run(jobs=["polish"])


def test_no_job_at_all_still_produces_a_metrics_snapshot(fresh_db):
    report = _run(jobs=[]).report

    assert report.jobs == []
    assert set(report.metrics) == {"before", "after"}


# ── The abstraction job's deterministic half (5b-ii: gates only, no model) ────


def _relates(first, second):
    """An active ``relates_to`` edge between two nodes, by id."""
    return service.create_edge(first, second, consolidate.RELATED_EDGE_TYPE, principal=owner())


def _abstraction_outcome(report):
    return _outcome(report, consolidate.JOB_ABSTRACTION)


def test_abstraction_needs_a_provider_and_says_so(fresh_db):
    """Cohesion is the job's one vector signal, so there is no degraded mode.

    The other jobs fall back to a deterministic half when the embedding provider
    is absent; this job has none — a dense, sized, fresh cluster exists and the
    cohesion gate simply cannot be computed, so the job no-ops and says so,
    exactly like their degraded postures report their own absence.
    """
    first, second, third = _node("Alpha"), _node("Beta"), _node("Gamma")
    _relates(first.id, second.id)
    _relates(second.id, third.id)
    _relates(third.id, first.id)

    outcome = _abstraction_outcome(_run(jobs=[consolidate.JOB_ABSTRACTION]).report)

    assert outcome.error is None
    assert outcome.proposed == []
    assert any("no embedding provider" in note for note in outcome.notes)
    assert any("did not run" in note for note in outcome.notes)


def test_a_dense_sized_fresh_cluster_is_eligible(fresh_db):
    """One cycle's worth of edges and one shared area make a synthesis candidate."""
    _place(Alpha=0.0, Beta=1.0, Gamma=2.0)
    first, second, third = (
        _node("Alpha one"),
        _node("Alpha two"),
        _node("Alpha three"),
    )
    _relates(first.id, second.id)
    _relates(second.id, third.id)
    _relates(third.id, first.id)

    outcome = _abstraction_outcome(_run(jobs=[consolidate.JOB_ABSTRACTION]).report)

    assert outcome.detail["clusters_eligible"] == 1
    assert outcome.detail["clusters_considered"] == 1
    assert outcome.detail["eligible_clusters"] == [sorted([first.id, second.id, third.id])]
    assert outcome.skipped == []


def test_a_chain_is_not_dense(fresh_db):
    """Four members in a line have three edges — average degree below 2.

    The vectors are cohesive and the size gate passes, so it is the graph half
    of the density gate and only that half that refuses the cluster.
    """
    _place(Alpha=0.0)
    first, second, third, fourth = (
        _node("Alpha one"),
        _node("Alpha two"),
        _node("Alpha three"),
        _node("Alpha four"),
    )
    _relates(first.id, second.id)
    _relates(second.id, third.id)
    _relates(third.id, fourth.id)

    outcome = _abstraction_outcome(_run(jobs=[consolidate.JOB_ABSTRACTION]).report)

    assert outcome.detail["clusters_eligible"] == 0
    (skip,) = outcome.skipped
    assert skip["id"] == ",".join(sorted([first.id, second.id, third.id, fourth.id]))
    assert "not dense" in skip["reason"]


def test_a_synthesized_member_is_not_resynthesized(fresh_db):
    """Both halves of the freshness gate: the member's own flag, or a
    ``derived_from`` edge from a synthesized node."""
    _place(Alpha=0.0)
    flagged = _node("Alpha one", props={"synthesized": True})
    second, third = _node("Alpha two"), _node("Alpha three")
    _relates(flagged.id, second.id)
    _relates(second.id, third.id)
    _relates(third.id, flagged.id)

    ancestor = _node("Beta one", props={"synthesized": True})
    member, other = _node("Beta two"), _node("Beta three")
    _relates(member.id, other.id)
    _relates(other.id, ancestor.id)
    _relates(ancestor.id, member.id)
    service.create_edge(
        ancestor.id, member.id, consolidate.DERIVED_FROM_EDGE_TYPE, principal=owner()
    )

    outcome = _abstraction_outcome(_run(jobs=[consolidate.JOB_ABSTRACTION]).report)

    assert outcome.detail["clusters_eligible"] == 0
    assert sorted(skip["reason"] for skip in outcome.skipped) == [
        "member is already part of a synthesis",
        "member is already part of a synthesis",
    ]


def test_an_over_cap_graph_reports_the_overflow(fresh_db, monkeypatch):
    """Eligible clusters beyond the per-cycle cap are reported, never dropped."""
    monkeypatch.setattr(consolidate, "MAX_CLUSTERS_PER_CYCLE", 2)
    _place(Alpha=0.0, Beta=1.0, Gamma=2.0)
    for marker in ("Alpha", "Beta", "Gamma"):
        first, second, third = (
            _node(f"{marker} one"),
            _node(f"{marker} two"),
            _node(f"{marker} three"),
        )
        _relates(first.id, second.id)
        _relates(second.id, third.id)
        _relates(third.id, first.id)

    outcome = _abstraction_outcome(_run(jobs=[consolidate.JOB_ABSTRACTION]).report)

    assert outcome.detail["clusters_eligible"] == 3
    assert outcome.detail["clusters_considered"] == 2
    assert any("cap" in note for note in outcome.notes)


def test_a_sized_cluster_below_the_minimum_is_skipped(fresh_db):
    """A pair is a link, not a synthesis — the size gate fires before any model call."""
    first, second = _node("Alpha one"), _node("Alpha two")
    _relates(first.id, second.id)

    outcome = _abstraction_outcome(_run(jobs=[consolidate.JOB_ABSTRACTION]).report)

    assert outcome.detail["clusters_eligible"] == 0
    (skip,) = outcome.skipped
    assert "below the" in skip["reason"]


# ── The structural rail: no service-layer bypass ─────────────────────────────


#: Everything :mod:`nodum.consolidate` is allowed to import out of the package.
#: A new entry here is a decision somebody has to make on purpose. ``nodum.agent``
#: joined with the abstraction job — the model call goes through its one door.
ALLOWED_NODUM_IMPORTS = {
    "nodum.agent",
    "nodum.auth",
    "nodum.embeddings",
    "nodum.migrations",
    "nodum.models",
    "nodum.principal",
    "nodum.projectors",
    "nodum.service",
    "nodum.store",
}

#: Call names that mean somebody is talking to SQLite directly.
CONNECTION_CALLS = {
    "connect",
    "cursor",
    "execute",
    "executemany",
    "executescript",
    "commit",
    "init_db",
    "blobopen",
}


def _module_ast() -> ast.Module:
    return ast.parse(Path(consolidate.__file__).read_text(encoding="utf-8"))


def _imported_modules() -> set[str]:
    """Every *module* the file imports, however it spells the import.

    ``from nodum import service`` and ``from nodum.service import X`` both name
    ``nodum.service``, so one set answers "what does this file depend on?"
    whichever spelling a future edit reaches for.
    """
    modules: set[str] = set()
    for node in ast.walk(_module_ast()):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "nodum":
                modules |= {f"nodum.{alias.name}" for alias in node.names}
            else:
                modules.add(module)
    return modules


def test_the_runner_opens_no_connection_of_its_own():
    """§8.4 rule 1: the internal agent is a peer client, not a second writer.

    Behaviour cannot catch this — a module with its own connection would pass
    every test above while writing rows no grant covered and no event recorded.
    """
    modules = _imported_modules()
    assert "sqlite3" not in modules
    assert "nodum.db" not in modules

    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(_module_ast())
        if isinstance(node, ast.Call)
    }
    offenders = calls & CONNECTION_CALLS
    assert offenders == set(), f"talks to SQLite directly: {sorted(offenders)}"


def test_the_runner_imports_nothing_private_and_reaches_no_private():
    private_imports = [
        f"{node.module}.{alias.name}"
        for node in ast.walk(_module_ast())
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("nodum")
        for alias in node.names
        if alias.name.startswith("_")
    ]
    assert private_imports == [], f"private imports: {private_imports}"

    modules = {"service", "auth", "projectors", "embeddings", "assets", "store"}
    reached = [
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(_module_ast())
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in modules
        and node.attr.startswith("_")
    ]
    assert reached == [], f"reaches into a private: {reached}"


def test_the_runner_imports_only_the_modules_it_is_allowed_to():
    from_nodum = {name for name in _imported_modules() if name.startswith("nodum.")}

    assert from_nodum <= ALLOWED_NODUM_IMPORTS, (
        f"unreviewed import: {sorted(from_nodum - ALLOWED_NODUM_IMPORTS)}"
    )


def test_the_deterministic_runner_consults_no_stop_switch_and_the_copy_says_so(
    fresh_db, monkeypatch
):
    """The kill switch's surfaces promise a latency, and this is what bounds it.

    ``nodum cycle-stop`` and ``POST /api/cycles/{id}/stop`` record an instruction
    the *run* is supposed to notice, and the only thing that notices one today is
    :meth:`nodum.agent.AgentRun.chat`, before a provider call. The four
    **deterministic** jobs make no provider call and no stop check — so a stop
    asked for during one of their runs is recorded and that run finishes. The
    abstraction job (5b-ii's first) is the deliberate exception, and it is the
    thing the copy now names: it reaches the model through ``AgentRun.chat``,
    which is the stop check. Every surface says exactly that, and this is what
    keeps the sentence honest: a stop-consulting call that lands in a
    deterministic job fails this test, and the copy it names has to be rewritten
    rather than quietly becoming an understatement.

    Both halves are asserted, because neither alone is the claim. The AST half
    confines every stop-consulting call site to the abstraction job's own
    function; the behavioural half proves the run really does complete, since a
    check could be added through a helper this file does not name.
    """
    module = _module_ast()
    abstraction = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_job_abstraction"
    )

    def inside_abstraction(node: ast.AST) -> bool:
        return abstraction.lineno <= node.lineno <= (abstraction.end_lineno or abstraction.lineno)

    # Every call that consults the switch — the service read itself, the wiring
    # helper, the runtime's own check, the runtime's construction, and the one
    # door that checks first — must live inside the abstraction job. The
    # `agent.prompt_version` module-level constant is excluded from the `agent`
    # use check below: it is computed once at import time from the template
    # string and consults nothing.
    consulting = {"stop_requested", "cycle_stop_check", "check_stop", "for_cycle", "chat"}
    offside = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        )
        in consulting
        and not inside_abstraction(node)
    ]
    assert offside == [], (
        "a stop-consulting call sits outside the abstraction job: "
        f"{[ast.unparse(call) for call in offside]}"
    )

    agent_uses = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "agent"
        and node.attr != "prompt_version"
    ]
    offside_uses = [use for use in agent_uses if not inside_abstraction(use)]
    assert offside_uses == [], (
        f"`agent` is used outside the abstraction job: {[ast.unparse(use) for use in offside_uses]}"
    )

    # And in the run itself. The switch is hit from *inside* the cycle — the
    # only moment anybody hits one — by a job standing where the real first job
    # stands, and every job after it still runs to completion. The abstraction
    # job is in that list: with no provider and no budget it no-ops, so a run
    # containing it completes exactly as a deterministic one does.
    human = owner()

    def stopper(context):
        (running,) = [
            entry for entry in service.list_cycles(principal=human) if entry.status == "running"
        ]
        service.request_stop(running.id, principal=human)
        return consolidate.JobOutcome(name="stopper")

    monkeypatch.setitem(consolidate.JOBS, "stopper", stopper)
    ran = ["stopper", *[name for name in consolidate.JOBS if name != "stopper"]]

    outcome = consolidate.consolidate(jobs=ran, triggered_by=human.actor_string)

    assert outcome.cycle.stop_requested is True, "the stop was recorded mid-run"
    assert outcome.cycle.status == "completed", "and the run finished anyway"
    assert [job.name for job in outcome.report.jobs] == ran
    assert outcome.report.failed == []


def test_every_write_the_module_makes_names_the_gardener():
    """The only ``principal=`` a job may bind is the run context's.

    A job that reached for ``owner_principal()`` or took a principal off its
    caller would put a human's name on an edit the gardener chose.
    """
    bindings = [
        keyword.value
        for node in ast.walk(_module_ast())
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "principal"
    ]
    assert bindings, "the module binds at least one principal"
    rendered = {ast.unparse(value) for value in bindings}
    assert rendered <= {"context.principal", "self.principal", "opener", "gardener"}, (
        f"unreviewed principal binding: {sorted(rendered)}"
    )


def test_the_module_documents_when_a_revoked_grant_bites(fresh_db):
    """The grant set is minted once per run, so a revocation lands next cycle.

    `disable_agent` already carries this note for the MCP server's
    process-lifetime principal, and the archive dialog's copy promises an agent
    "can read, write, propose and review nothing" from the moment a space is
    archived. The window is one cycle, and an undocumented window is the part
    that is wrong.
    """
    documentation = consolidate.__doc__ or ""

    assert "revocation" in documentation.lower()
    assert "next cycle" in documentation

    # And the behaviour the sentence describes: the run holds the grants it
    # started with.
    space = service.create_space("research", principal=owner())
    service.grant("builtin-gardener", space.id, "edit", principal=owner())
    minted = auth.internal_principal()
    service.revoke("builtin-gardener", space.id, principal=owner())

    assert space.id in minted.grants
    assert space.id not in auth.internal_principal().grants
