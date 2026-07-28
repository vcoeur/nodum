"""The consolidation runner: the deterministic jobs and the coherence metrics.

Phase 5a, the near side of the LLM line (design §8.4/§8.5). What is tested here
is the half of the gardener that is arithmetic: four jobs, five metrics, one
cycle around all of it, and the rails that keep the internal agent a *peer
client* — every write through the public service API, every event stamped with
the cycle, every one of them attributed to ``agent:builtin-gardener``.

The last of those is an AST property rather than a behaviour, deliberately: a
module that grew its own connection would pass every behavioural test in this
file while quietly leaving the grant model behind.
"""

from __future__ import annotations

import ast
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from helpers import agent, owner

from nodum import auth, consolidate, db, embeddings, service

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


def test_a_merely_related_pair_is_not_a_duplicate_candidate(fresh_db):
    """The duplicate bar sits above the link bar, so one pair is one observation."""
    _place(Alpha=0.0, Beta=0.5)  # cos(0.5) ≈ 0.878: related, not the same thing
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


def test_the_duplicate_bar_sits_above_the_link_bar():
    """Pinned, because a pair between them would otherwise queue twice."""
    assert consolidate.DUPLICATE_EMBEDDING_COSINE > consolidate.LINK_EMBEDDING_COSINE


def test_the_job_degrades_to_titles_when_no_model_is_present(fresh_db):
    """An install without the extra is the commonest configuration there is."""
    _node("Alpha")
    _node("Alpha")

    outcome = _outcome(_run().report, consolidate.JOB_DUPLICATES)

    assert outcome.error is None
    assert len(outcome.proposed) == 1
    assert any("no embedding provider" in note for note in outcome.notes)


def test_with_a_provider_the_job_never_claims_it_degraded(fresh_db):
    _place(Alpha=0.0)
    _node("Alpha")

    outcome = _outcome(_run().report, consolidate.JOB_DUPLICATES)

    assert not [note for note in outcome.notes if "no embedding provider" in note]


def test_the_report_says_which_state_the_suggestions_landed_in(fresh_db):
    """The gardener holds `edit` on main, so they land active — and it is said."""
    _node("Alpha")
    _node("Alpha")

    outcome = _outcome(_run().report, consolidate.JOB_DUPLICATES)

    assert outcome.detail["landed"] == ["active"]
    assert any("landed 'active'" in note for note in outcome.notes)


def test_consolidation_never_proposes_a_merge(fresh_db):
    """D9: a merge is always human-approved, implemented as the cycle not doing one."""
    _node("Alpha")
    _node("Alpha")

    _run()

    assert [event.op for event in _events() if event.op == "node.merge"] == []


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
    _place(Alpha=0.0, Beta=0.5)
    first, second = _node("Alpha"), _node("Beta")

    _run()

    (edge,) = _related()
    assert {edge.src_id, edge.dst_id} == {first.id, second.id}
    assert edge.props["signals"] == ["embedding"]


def test_a_distant_pair_is_left_alone(fresh_db):
    _place(Alpha=0.0, Beta=0.9)  # cos(0.9) ≈ 0.62, below the link bar
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
    assert [event.op for event in stamped] == ["edge.create"]
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


# ── The structural rail: no service-layer bypass ─────────────────────────────


#: Everything :mod:`nodum.consolidate` is allowed to import out of the package.
#: A new entry here is a decision somebody has to make on purpose.
ALLOWED_NODUM_IMPORTS = {
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
