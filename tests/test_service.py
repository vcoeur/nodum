"""Service-layer tests against the live database — the core graph contract."""

from __future__ import annotations

import uuid

import pytest

from nodum import service
from nodum.db import connect
from nodum.service import NodeNotFound


def _count_type_nodes(type_name: str) -> int:
    """Count the type nodes (``kind == 'type'``) carrying ``type_name``."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM nodes "
            "WHERE data ->> 'text' = %s AND data ->> 'kind' = 'type'",
            (type_name,),
        )
        return cur.fetchone()["n"]


def test_type_creates_one_shared_node_and_two_is_edges() -> None:
    """Two nodes sharing a type → exactly one type node and two ``is`` edges."""
    first = service.add_node("Ada wrote a program.", type="claim")
    second = service.add_node("Babbage designed an engine.", type="claim")

    # Exactly one shared type node exists for "claim".
    assert _count_type_nodes("claim") == 1

    # Each content node has a single `is` edge to the type node.
    first_is = [e for e in service.get(first.uuid).edges if e.data.get("type") == "is"]
    second_is = [e for e in service.get(second.uuid).edges if e.data.get("type") == "is"]
    assert len(first_is) == 1
    assert len(second_is) == 1

    # Both `is` edges point at the same shared type node.
    type_uuid = first_is[0].to_uuid
    assert second_is[0].to_uuid == type_uuid

    # The type node is a plain node with the canonical type payload, and has two
    # incident `is` edges (one per content node).
    type_view = service.get(type_uuid)
    assert type_view.node.data == {"text": "claim", "kind": "type"}
    assert len([e for e in type_view.edges if e.data.get("type") == "is"]) == 2

    # The type string is also kept on each content node's payload.
    assert first.data["type"] == "claim"
    assert second.data["type"] == "claim"


def test_add_edge_sets_type() -> None:
    """``add_edge`` stores the type under ``edge.data.type`` between two nodes."""
    source = service.add_node("source node")
    target = service.add_node("target node")
    edge = service.add_edge(source.uuid, target.uuid, type="supports")
    assert edge.from_uuid == source.uuid
    assert edge.to_uuid == target.uuid
    assert edge.data["type"] == "supports"


def test_add_edge_missing_endpoint_raises_node_not_found() -> None:
    """An edge to a non-existent endpoint raises ``NodeNotFound``."""
    real = service.add_node("the only real node")
    with pytest.raises(NodeNotFound):
        service.add_edge(real.uuid, uuid.uuid4(), type="supports")


def test_add_edge_self_loop_raises_value_error() -> None:
    """An edge from a node to itself raises ``ValueError``."""
    node = service.add_node("lonely node")
    with pytest.raises(ValueError):
        service.add_edge(node.uuid, node.uuid, type="supports")


def test_add_node_blank_text_raises_value_error() -> None:
    """``add_node`` rejects empty/blank text with ``ValueError``."""
    with pytest.raises(ValueError):
        service.add_node("")


def test_get_unknown_uuid_raises_node_not_found() -> None:
    """``get`` on an unknown UUID raises ``NodeNotFound``."""
    with pytest.raises(NodeNotFound):
        service.get(uuid.uuid4())


def test_search_ranks_by_relevance() -> None:
    """Higher term frequency ranks first; non-matching nodes are excluded."""
    dense = service.add_node("graph graph graph database")
    sparse = service.add_node("graph database tooling")
    service.add_node("an unrelated note about painting")

    result = service.search("graph")
    hit_uuids = [hit.uuid for hit in result.hits]
    assert dense.uuid in hit_uuids
    assert sparse.uuid in hit_uuids
    assert result.total == 2  # the painting node does not match

    # Scores come back in descending order, densest match first.
    scores = [hit.score for hit in result.hits]
    assert scores == sorted(scores, reverse=True)
    assert result.hits[0].uuid == dense.uuid


def test_expand_depth_one_returns_neighbours() -> None:
    """``expand(depth=1)`` returns the seed plus its direct out-neighbours."""
    root = service.add_node("root claim")
    child_a = service.add_node("child a")
    child_b = service.add_node("child b")
    service.add_edge(root.uuid, child_a.uuid, type="supports")
    service.add_edge(root.uuid, child_b.uuid, type="supports")

    sub = service.expand(root.uuid, depth=1)
    node_uuids = {node.uuid for node in sub.nodes}
    assert {root.uuid, child_a.uuid, child_b.uuid} <= node_uuids

    edge_pairs = {(e.from_uuid, e.to_uuid) for e in sub.edges}
    assert (root.uuid, child_a.uuid) in edge_pairs
    assert (root.uuid, child_b.uuid) in edge_pairs


def test_expand_depth_zero_raises_value_error() -> None:
    """``expand`` with ``depth < 1`` raises ``ValueError``."""
    root = service.add_node("root for bad depth")
    with pytest.raises(ValueError):
        service.expand(root.uuid, depth=0)
