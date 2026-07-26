"""Curated graph reads (design §8.1 read tier): subgraphs, paths, schema, diffs."""

from __future__ import annotations

import pytest
from helpers import agent, owner

from nodum import search as search_module
from nodum import service
from nodum.service import NodeNotFound, TypeNotFound

AGENT = "agent:researcher"


def _chain():
    """a --supports--> b --mentions--> c, plus an isolated node d."""
    a = service.create_node(type="concept", title="Alpha", principal=owner())
    b = service.create_node(type="concept", title="Beta", principal=owner())
    c = service.create_node(type="note", title="Gamma", principal=owner())
    d = service.create_node(type="note", title="Delta", principal=owner())
    ab = service.create_edge(a.id, b.id, "supports", confidence=0.9, principal=owner())
    bc = service.create_edge(b.id, c.id, "mentions", principal=owner())
    return a, b, c, d, ab, bc


# ── get_neighborhood ──────────────────────────────────────────────────────────


def test_neighborhood_depth_zero_is_the_node_alone(fresh_db):
    a, *_ = _chain()
    subgraph = service.get_neighborhood(a.id, depth=0, principal=owner())
    assert [node.id for node in subgraph.nodes] == [a.id]
    assert subgraph.edges == []


def test_neighborhood_walks_both_directions_by_depth(fresh_db):
    a, b, c, d, *_ = _chain()
    one = service.get_neighborhood(b.id, depth=1, principal=owner())
    assert {node.id for node in one.nodes} == {a.id, b.id, c.id}
    assert d.id not in {node.id for node in one.nodes}

    two = service.get_neighborhood(a.id, depth=2, principal=owner())
    assert {node.id for node in two.nodes} == {a.id, b.id, c.id}


def test_neighborhood_ignores_proposed_edges(fresh_db):
    a, b, c, d, *_ = _chain()
    service.create_edge(a.id, d.id, "relates_to", principal=agent(AGENT))  # proposed
    subgraph = service.get_neighborhood(a.id, depth=1, principal=owner())
    assert d.id not in {node.id for node in subgraph.nodes}


def test_neighborhood_rejects_bad_input(fresh_db):
    a, *_ = _chain()
    with pytest.raises(ValueError, match="depth"):
        service.get_neighborhood(a.id, depth=-1, principal=owner())
    with pytest.raises(NodeNotFound):
        service.get_neighborhood("missing", depth=1, principal=owner())


# ── traverse ──────────────────────────────────────────────────────────────────


def test_traverse_filters_by_edge_type(fresh_db):
    a, b, c, d, *_ = _chain()
    supports_only = service.traverse(a.id, edge_types=["supports"], depth=3, principal=owner())
    assert {node.id for node in supports_only.nodes} == {a.id, b.id}

    mentions_only = service.traverse(a.id, edge_types=["mentions"], depth=3, principal=owner())
    assert [node.id for node in mentions_only.nodes] == [a.id]


def test_traverse_direction(fresh_db):
    a, b, c, d, *_ = _chain()
    outward = service.traverse(a.id, depth=3, direction="out", principal=owner())
    assert {node.id for node in outward.nodes} == {a.id, b.id, c.id}

    inward = service.traverse(c.id, depth=3, direction="in", principal=owner())
    assert {node.id for node in inward.nodes} == {a.id, b.id, c.id}

    stuck = service.traverse(c.id, depth=3, direction="out", principal=owner())
    assert [node.id for node in stuck.nodes] == [c.id]


def test_traverse_rejects_bad_input(fresh_db):
    a, *_ = _chain()
    with pytest.raises(ValueError, match="direction"):
        service.traverse(a.id, direction="sideways", principal=owner())
    with pytest.raises(TypeNotFound):
        service.traverse(a.id, edge_types=["bogus"], principal=owner())


# ── find_path ─────────────────────────────────────────────────────────────────


def test_find_path_shortest(fresh_db):
    a, b, c, d, ab, bc = _chain()
    result = service.find_path(a.id, c.id, principal=owner())
    assert result.found
    assert result.hops == 2
    assert [node.id for node in result.nodes] == [a.id, b.id, c.id]
    assert [edge.id for edge in result.edges] == [ab.id, bc.id]


def test_find_path_reverse_direction(fresh_db):
    a, b, c, d, *_ = _chain()
    result = service.find_path(c.id, a.id, principal=owner())
    assert result.found
    assert [node.id for node in result.nodes] == [c.id, b.id, a.id]


def test_find_path_none_and_self(fresh_db):
    a, b, c, d, *_ = _chain()
    missing = service.find_path(a.id, d.id, principal=owner())
    assert not missing.found
    assert missing.nodes == []

    self_path = service.find_path(a.id, a.id, principal=owner())
    assert self_path.found
    assert self_path.hops == 0
    assert [node.id for node in self_path.nodes] == [a.id]


# ── get_schema ────────────────────────────────────────────────────────────────


def test_get_schema_node_and_edge_types(fresh_db):
    node_type = service.get_schema("concept", principal=owner())
    assert node_type.id == "concept"
    assert node_type.is_builtin

    edge_type = service.get_schema("supports", principal=owner())
    assert edge_type.inverse_name == "supported_by"

    with pytest.raises(TypeNotFound):
        service.get_schema("bogus", principal=owner())


# ── diff_versions ─────────────────────────────────────────────────────────────


def test_diff_versions(fresh_db):
    note = service.create_node(
        type="note",
        title="Draft",
        content="line one\nline two",
        principal=owner(),
    )
    service.update_node(
        note.id,
        content="line one\nline two changed",
        props={"k": 1},
        principal=owner(),
    )
    first, second = service.history(note.id, principal=owner())

    result = service.diff_versions(first.id, second.id, principal=owner())
    assert result.node_id == note.id
    assert set(result.changed_fields) == {"content", "props"}
    assert "-line two" in result.diff
    assert "+line two changed" in result.diff
    assert result.a.state == result.b.state == "applied"


def test_diff_versions_rejects_cross_node(fresh_db):
    x = service.create_node(type="note", title="x", principal=owner())
    y = service.create_node(type="note", title="y", principal=owner())
    with pytest.raises(service.VersionNotFound) as excinfo:
        service.diff_versions(
            service.history(x.id, principal=owner())[0].id,
            service.history(y.id, principal=owner())[0].id,
            principal=owner(),
        )
    # The refusal names neither node id: version ids are sequential, so the
    # message must not turn a diff into an enumeration oracle (review S1).
    assert x.id not in str(excinfo.value)
    assert y.id not in str(excinfo.value)


# ── propose_edges ─────────────────────────────────────────────────────────────


def test_propose_edges_batch(fresh_db):
    a, b, c, d, *_ = _chain()
    result = service.propose_edges(
        [
            {"src": a.id, "dst": d.id, "edge_type": "relates_to", "confidence": 0.7},
            {"src": a.id, "dst": "missing", "edge_type": "supports"},
            {"src": a.id, "dst": d.id},  # missing edge_type
            "not an object",
        ],
        principal=agent(AGENT),
    )
    assert len(result.created) == 1
    assert result.created[0].state == "proposed"
    assert result.created[0].created_by == AGENT
    assert [failure.index for failure in result.failed] == [1, 2, 3]
    assert "missing key" in result.failed[1].error


# ── search filters + expand ───────────────────────────────────────────────────


def _search_graph():
    alpha = service.create_node(
        type="concept",
        title="graph alpha",
        content="graph theory",
        principal=owner(),
    )
    beta = service.create_node(
        type="note",
        title="graph beta",
        content="graph theory",
        principal=owner(),
    )
    service.create_edge(alpha.id, beta.id, "supports", confidence=0.8, principal=owner())
    return alpha, beta


def test_search_created_by_filter(fresh_db):
    _search_graph()
    assert len(search_module.search("graph theory", principal=owner()).hits) == 2
    hits = search_module.search("graph theory", created_by="agent:nobody", principal=owner()).hits
    assert hits == []


def test_search_expand_adds_one_hop_neighbors(fresh_db):
    alpha, beta = _search_graph()
    hits = search_module.search("graph alpha", principal=owner()).hits
    assert [hit.node_id for hit in hits] == [alpha.id]

    expanded = search_module.search("graph alpha", expand=True, principal=owner()).hits
    assert [hit.node_id for hit in expanded] == [alpha.id, beta.id]
    assert expanded[1].signals == {"graph": 0.8}  # supports 1.0 × confidence 0.8


def test_search_expand_respects_state_filter(fresh_db):
    alpha, beta = _search_graph()
    service.transition(beta.id, "archive", principal=owner())
    hits = search_module.search("graph alpha", expand=True, principal=owner()).hits
    assert [hit.node_id for hit in hits] == [alpha.id]
    hits_any = search_module.search("graph alpha", state=None, expand=True, principal=owner()).hits
    assert beta.id in {hit.node_id for hit in hits_any}
