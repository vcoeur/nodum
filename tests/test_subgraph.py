"""The bounded, server-side-filtered subgraph read (the graph view's query)."""

from __future__ import annotations

import json

import pytest
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
    service.create_edge(hub.id, pending.id, "relates_to", confidence=0.9, actor=AGENT)
    service.create_edge(hub.id, weak.id, "relates_to", confidence=0.1)
    service.create_edge(hub.id, person.id, "authored_by")
    return hub, live, pending, weak, person


def _ids(result):
    return {node.id for node in result.nodes}


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


def test_cap_bites_before_the_far_side_is_read(fresh_db):
    """A deep chain past the cap is never walked, let alone sliced."""
    nodes = [service.create_node(type="note", title=f"N{index}") for index in range(10)]
    for left, right in zip(nodes, nodes[1:], strict=False):
        service.create_edge(left.id, right.id, "relates_to")
    result = service.subgraph(nodes[0].id, depth=9, limit=4)
    assert [node.id for node in result.nodes] == [node.id for node in nodes[:4]]
    assert result.truncated


def test_limit_must_be_positive(fresh_db):
    hub, _leaves = _star()
    with pytest.raises(ValueError, match="limit"):
        service.subgraph(hub.id, limit=0)
    with pytest.raises(ValueError, match="limit"):
        service.subgraph(hub.id, limit=-1)  # LIMIT -1 would be unbounded in SQL


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
