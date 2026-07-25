"""The bounded, server-side-filtered subgraph read (the graph view's query)."""

from __future__ import annotations

import json

import pytest
from helpers import OWNER_ACTOR, agent
from typer.testing import CliRunner

from nodum import service
from nodum.cli import app
from nodum.service import NodeNotFound, TypeNotFound

AGENT = "agent:researcher"
runner = CliRunner()


def _star(spokes: int = 5):
    """A hub with ``spokes`` `relates_to` neighbours, each an active note."""
    hub = service.create_node(type="concept", title="Hub")
    leaves = [service.create_node(type="note", title=f"Leaf {index}") for index in range(spokes)]
    for leaf in leaves:
        service.create_edge(hub.id, leaf.id, "relates_to", confidence=0.5)
    return hub, leaves


def _mixed():
    """A hub wired to one neighbour per filterable edge dimension."""
    hub = service.create_node(type="concept", title="Hub")
    live = service.create_node(type="note", title="Live")
    pending = service.create_node(type="note", title="Pending")
    weak = service.create_node(type="note", title="Weak")
    person = service.create_node(type="person", title="Author")
    service.create_edge(hub.id, live.id, "supports", confidence=0.9)
    service.create_edge(hub.id, pending.id, "relates_to", confidence=0.9, principal=agent(AGENT))
    service.create_edge(hub.id, weak.id, "relates_to", confidence=0.1)
    service.create_edge(hub.id, person.id, "authored_by")
    return hub, live, pending, weak, person


def _ids(result):
    return {node.id for node in result.nodes}


def _count_node_reads(monkeypatch):
    """Record every node row the service reads; returns the growing id list."""
    reads: list[str] = []
    original = service._get_node_row

    def counting(conn, node_id):
        reads.append(node_id)
        return original(conn, node_id)

    monkeypatch.setattr(service, "_get_node_row", counting)
    return reads


# ── The node cap ──────────────────────────────────────────────────────────────


def test_limit_caps_nodes_and_reports_truncation(fresh_db):
    hub, leaves = _star(spokes=5)
    result = service.subgraph(hub.id, depth=1, limit=3)
    assert len(result.nodes) == 3  # the hub plus two leaves
    assert result.truncated
    assert result.nodes[0].id == hub.id


def test_uncapped_walk_is_not_truncated(fresh_db):
    hub, leaves = _star(spokes=5)
    result = service.subgraph(hub.id, depth=1, limit=200)
    assert len(result.nodes) == 6
    assert not result.truncated


def test_limit_of_one_returns_the_root_alone(fresh_db):
    hub, _leaves = _star()
    result = service.subgraph(hub.id, depth=2, limit=1)
    assert [node.id for node in result.nodes] == [hub.id]
    assert result.edges == []
    assert result.truncated


def test_cap_never_yields_an_edge_without_its_node(fresh_db):
    """The invariant a renderer depends on: no edge points outside `nodes`."""
    hub, _leaves = _star(spokes=8)
    result = service.subgraph(hub.id, depth=2, limit=4)
    present = _ids(result)
    for edge in result.edges:
        assert edge.src_id in present
        assert edge.dst_id in present


def test_cap_bites_before_the_far_side_is_read(fresh_db, monkeypatch):
    """The cap bounds the *work*, not just the result: O(limit) node reads.

    A hub, not a chain: a chain has one neighbour per level, so reading the far
    side before testing the cap costs nothing there and the ordering bug hides.
    A 40-spoke hub read with `limit=1` must cost exactly one node read — the
    root — and 3 must cost 3, however many spokes wait behind the cap.
    """
    hub, _leaves = _star(spokes=40)
    reads = _count_node_reads(monkeypatch)

    result = service.subgraph(hub.id, depth=1, limit=1)
    assert [node.id for node in result.nodes] == [hub.id]
    assert result.truncated
    assert reads == [hub.id]  # the 40 spokes were never read

    reads.clear()
    result = service.subgraph(hub.id, depth=1, limit=3)
    assert len(result.nodes) == 3
    assert result.truncated
    assert len(reads) == 3  # root + the two admitted spokes, nothing else


def test_limit_is_clamped_to_the_server_ceiling(fresh_db, monkeypatch):
    """An absurd `limit` gets the ceiling, not the whole graph."""
    monkeypatch.setattr(service, "MAX_SUBGRAPH_LIMIT", 3)
    hub, _leaves = _star(spokes=8)
    result = service.subgraph(hub.id, depth=1, limit=10**9)
    assert len(result.nodes) == 3
    assert result.truncated


def test_limit_must_be_positive(fresh_db):
    hub, _leaves = _star()
    with pytest.raises(ValueError, match="limit"):
        service.subgraph(hub.id, limit=0)
    with pytest.raises(ValueError, match="limit"):
        service.subgraph(hub.id, limit=-1)  # LIMIT -1 would be unbounded in SQL


# ── The edge cap ──────────────────────────────────────────────────────────────


def test_edge_list_is_capped_independently_of_the_node_cap(fresh_db):
    """Two nodes can carry any number of edges — the node cap bounds neither."""
    a = service.create_node(type="note", title="A")
    b = service.create_node(type="note", title="B")
    for _index in range(300):
        service.create_edge(a.id, b.id, "relates_to")
    result = service.subgraph(a.id, depth=1, limit=2)
    assert len(result.nodes) == 2  # the node cap did not bite
    assert len(result.edges) == 2 * service.SUBGRAPH_EDGE_FACTOR
    assert result.truncated  # …but the edge cap did, and it says so


def test_edge_cap_bounds_the_ring_closing_pass_too(fresh_db):
    """Closing the ring cannot smuggle an unbounded edge list past the cap."""
    a = service.create_node(type="note", title="A")
    b = service.create_node(type="note", title="B")
    c = service.create_node(type="note", title="C")
    service.create_edge(a.id, b.id, "relates_to")
    service.create_edge(a.id, c.id, "relates_to")
    for _index in range(100):
        service.create_edge(b.id, c.id, "relates_to")  # ring edges, far past the budget

    result = service.subgraph(a.id, depth=1, limit=3)
    assert len(result.edges) == 3 * service.SUBGRAPH_EDGE_FACTOR
    assert result.truncated
    assert len({edge.id for edge in result.edges}) == len(result.edges)
    assert [edge.id for edge in service.subgraph(a.id, depth=1, limit=3).edges] == [
        edge.id for edge in result.edges
    ]


def test_edge_count_stays_bounded_under_a_node_cap(fresh_db):
    """Nodes admitted, edges carried, and the endpoint invariant, together."""
    hub, _leaves = _star(spokes=8)
    result = service.subgraph(hub.id, depth=2, limit=4)
    present = _ids(result)
    assert len(present) == 4
    assert len(result.edges) == 3  # one per admitted spoke, none dangling
    assert all(edge.src_id in present and edge.dst_id in present for edge in result.edges)
    assert result.truncated


# ── Filters ───────────────────────────────────────────────────────────────────


def test_default_follows_active_edges_only(fresh_db):
    hub, live, pending, weak, person = _mixed()
    assert pending.id not in _ids(service.subgraph(hub.id, depth=1))


def test_edge_states_opens_the_walk_to_proposals(fresh_db):
    hub, live, pending, weak, person = _mixed()
    result = service.subgraph(hub.id, depth=1, edge_states=["active", "proposed"])
    assert pending.id in _ids(result)


def test_edge_type_and_confidence_filters(fresh_db):
    hub, live, pending, weak, person = _mixed()
    by_type = service.subgraph(hub.id, depth=1, edge_types=["supports"])
    assert _ids(by_type) == {hub.id, live.id}

    by_confidence = service.subgraph(hub.id, depth=1, min_confidence=0.5)
    assert weak.id not in _ids(by_confidence)
    # `authored_by` was written without a confidence: unstated never clears a floor.
    assert person.id not in _ids(by_confidence)


def test_created_by_filters_on_edge_attribution(fresh_db):
    hub, live, pending, weak, person = _mixed()
    result = service.subgraph(hub.id, depth=1, edge_states=["active", "proposed"], created_by=AGENT)
    assert _ids(result) == {hub.id, pending.id}


def test_node_types_filter_drops_the_node_and_its_edge(fresh_db):
    hub, live, pending, weak, person = _mixed()
    result = service.subgraph(hub.id, depth=1, node_types=["note"])
    assert person.id not in _ids(result)
    assert all(edge.dst_id != person.id for edge in result.edges)
    # The root keeps its place even though `concept` is not in the filter.
    assert result.nodes[0].id == hub.id


def test_filters_compose_as_a_conjunction(fresh_db):
    hub, live, pending, weak, person = _mixed()
    result = service.subgraph(
        hub.id,
        depth=1,
        edge_types=["supports", "relates_to"],
        edge_states=["active"],
        min_confidence=0.5,
        node_types=["note"],
    )
    assert _ids(result) == {hub.id, live.id}


def test_a_filter_is_not_truncation(fresh_db):
    """`truncated` means a cap bit, never that the caller's own filter matched.

    A view showing "some hidden" for a filter the user set would be noise; the
    flag exists to say the *server* stopped short.
    """
    hub, live, pending, weak, person = _mixed()
    filtered = service.subgraph(hub.id, depth=1, node_types=["note"])
    assert person.id not in _ids(filtered)
    assert not filtered.truncated

    for kwargs in (
        {"edge_types": ["supports"]},
        {"min_confidence": 0.5},
        {"created_by": OWNER_ACTOR},
    ):
        result = service.subgraph(hub.id, depth=1, **kwargs)
        assert len(result.nodes) < 5, kwargs  # something really was dropped
        assert not result.truncated, kwargs


def test_node_type_filter_blocks_the_path_beyond_it(fresh_db):
    hub = service.create_node(type="concept", title="Hub")
    middle = service.create_node(type="person", title="Middle")
    far = service.create_node(type="note", title="Far")
    service.create_edge(hub.id, middle.id, "relates_to")
    service.create_edge(middle.id, far.id, "relates_to")
    result = service.subgraph(hub.id, depth=3, node_types=["note", "concept"])
    assert _ids(result) == {hub.id}


# ── Shape, ordering, and validation ───────────────────────────────────────────


def test_depth_zero_is_the_root_alone(fresh_db):
    hub, _leaves = _star()
    result = service.subgraph(hub.id, depth=0)
    assert [node.id for node in result.nodes] == [hub.id]
    assert result.edges == []
    assert not result.truncated


def test_walk_is_undirected_and_breadth_first(fresh_db):
    a = service.create_node(type="note", title="A")
    b = service.create_node(type="note", title="B")
    c = service.create_node(type="note", title="C")
    service.create_edge(a.id, b.id, "relates_to")
    service.create_edge(c.id, b.id, "relates_to")  # points *at* the middle
    result = service.subgraph(b.id, depth=1)
    assert [node.id for node in result.nodes] == [b.id, a.id, c.id]


def test_outermost_ring_is_closed(fresh_db, monkeypatch):
    """Two returned nodes are never drawn unconnected when an edge joins them.

    A triangle read at depth 1: the walk only ever sees the two edges incident
    to the root, so B–C has to be picked up afterwards — and by one bounded
    query, not a node read per admitted node.
    """
    a = service.create_node(type="note", title="A")
    b = service.create_node(type="note", title="B")
    c = service.create_node(type="note", title="C")
    service.create_edge(a.id, b.id, "relates_to")
    service.create_edge(a.id, c.id, "relates_to")
    bc = service.create_edge(b.id, c.id, "relates_to")

    reads = _count_node_reads(monkeypatch)
    result = service.subgraph(a.id, depth=1)
    assert _ids(result) == {a.id, b.id, c.id}
    assert bc.id in {edge.id for edge in result.edges}
    assert len(result.edges) == 3
    assert not result.truncated
    assert len(reads) == 3  # closing the ring costs no extra node reads


def test_closing_edges_obey_the_filters_and_the_node_set(fresh_db):
    """A closed ring is still a filtered, endpoint-safe ring."""
    a = service.create_node(type="note", title="A")
    b = service.create_node(type="note", title="B")
    c = service.create_node(type="note", title="C")
    service.create_edge(a.id, b.id, "relates_to")
    service.create_edge(a.id, c.id, "relates_to")
    service.create_edge(b.id, c.id, "relates_to", principal=agent(AGENT))  # proposed

    result = service.subgraph(a.id, depth=1)
    assert len(result.edges) == 2  # the proposed cross edge is not live graph
    opened = service.subgraph(a.id, depth=1, edge_states=["active", "proposed"])
    assert len(opened.edges) == 3

    present = _ids(result)
    assert all(edge.src_id in present and edge.dst_id in present for edge in result.edges)


def test_repeated_calls_return_the_same_subgraph(fresh_db):
    hub, _leaves = _star(spokes=6)
    first = service.subgraph(hub.id, depth=2, limit=4)
    second = service.subgraph(hub.id, depth=2, limit=4)
    assert [node.id for node in first.nodes] == [node.id for node in second.nodes]
    assert [edge.id for edge in first.edges] == [edge.id for edge in second.edges]


def test_uncapped_reads_report_truncated_false(fresh_db):
    """`traverse`/`get_neighborhood` gained the field without gaining behaviour."""
    hub, _leaves = _star()
    assert not service.traverse(hub.id).truncated
    assert not service.get_neighborhood(hub.id, depth=1).truncated


def test_rejects_bad_input(fresh_db):
    hub, _leaves = _star()
    with pytest.raises(NodeNotFound):
        service.subgraph("missing")
    with pytest.raises(ValueError, match="depth"):
        service.subgraph(hub.id, depth=-1)
    with pytest.raises(ValueError, match="state"):
        service.subgraph(hub.id, edge_states=["sideways"])
    with pytest.raises(ValueError, match="min_confidence"):
        service.subgraph(hub.id, min_confidence=1.5)
    with pytest.raises(TypeNotFound):
        service.subgraph(hub.id, edge_types=["bogus"])
    with pytest.raises(TypeNotFound):
        service.subgraph(hub.id, node_types=["bogus"])


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_cli_subgraph(fresh_db):
    hub, live, pending, weak, person = _mixed()
    result = runner.invoke(
        app,
        [
            "subgraph",
            hub.id,
            "--depth",
            "1",
            "--edge-type",
            "supports",
            "--edge-state",
            "active",
            "--min-confidence",
            "0.5",
            "--node-type",
            "note",
            "--limit",
            "10",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["root"] == hub.id
    assert {node["id"] for node in payload["nodes"]} == {hub.id, live.id}
    assert payload["truncated"] is False


def test_cli_subgraph_reports_truncation(fresh_db):
    hub, _leaves = _star(spokes=5)
    result = runner.invoke(app, ["subgraph", hub.id, "--limit", "2"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["truncated"] is True
    assert len(payload["nodes"]) == 2


def test_cli_rejects_bad_limit(fresh_db):
    hub, _leaves = _star()
    result = runner.invoke(app, ["subgraph", hub.id, "--limit", "0"])
    assert result.exit_code == 1
    assert "limit" in result.stderr
