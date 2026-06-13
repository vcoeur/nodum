"""HTTP API tests via the FastAPI TestClient — one check set per endpoint."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def _create_node(client: TestClient, text: str, node_type: str | None = None) -> dict:
    """POST a node and return the created NodeOut JSON (asserting a 200)."""
    body: dict = {"text": text}
    if node_type is not None:
        body["type"] = node_type
    response = client.post("/nodes", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_healthz(client: TestClient) -> None:
    """The liveness probe returns a static ok payload."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_node_and_get(client: TestClient) -> None:
    """A created node is fetchable, with its type payload and `is` edge."""
    node = _create_node(client, "an api node", node_type="claim")
    response = client.get(f"/nodes/{node['uuid']}")
    assert response.status_code == 200
    body = response.json()
    assert body["node"]["uuid"] == node["uuid"]
    assert body["node"]["data"]["type"] == "claim"
    assert any(edge["data"].get("type") == "is" for edge in body["edges"])


def test_get_missing_node_returns_404(client: TestClient) -> None:
    """Fetching an unknown node UUID returns 404."""
    response = client.get(f"/nodes/{uuid.uuid4()}")
    assert response.status_code == 404


def test_create_edge_and_expand(client: TestClient) -> None:
    """An edge is created and surfaces in the seed's expansion."""
    parent = _create_node(client, "parent claim", node_type="claim")
    child = _create_node(client, "child claim", node_type="claim")

    edge_response = client.post(
        "/edges",
        json={"from_uuid": parent["uuid"], "to_uuid": child["uuid"], "type": "supports"},
    )
    assert edge_response.status_code == 200
    assert edge_response.json()["data"]["type"] == "supports"

    response = client.get("/expand", params={"seed": parent["uuid"], "depth": 1})
    assert response.status_code == 200
    sub = response.json()
    node_uuids = {node["uuid"] for node in sub["nodes"]}
    assert {parent["uuid"], child["uuid"]} <= node_uuids
    edge_pairs = {(e["from_uuid"], e["to_uuid"]) for e in sub["edges"]}
    assert (parent["uuid"], child["uuid"]) in edge_pairs


def test_search(client: TestClient) -> None:
    """Full-text search returns the matching node and a consistent envelope."""
    match = _create_node(client, "analytical engine design notes")
    _create_node(client, "a note about gardening")

    response = client.get("/search", params={"q": "analytical engine"})
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "analytical engine"
    assert body["total"] == len(body["hits"])
    assert match["uuid"] in {hit["uuid"] for hit in body["hits"]}


def test_self_loop_edge_returns_422(client: TestClient) -> None:
    """A self-loop edge is rejected with 422 (service ValueError)."""
    node = _create_node(client, "self loop candidate")
    response = client.post(
        "/edges",
        json={"from_uuid": node["uuid"], "to_uuid": node["uuid"], "type": "supports"},
    )
    assert response.status_code == 422


def test_expand_depth_zero_returns_422(client: TestClient) -> None:
    """Expanding with depth 0 is rejected with 422 (service ValueError)."""
    node = _create_node(client, "seed for bad depth")
    response = client.get("/expand", params={"seed": node["uuid"], "depth": 0})
    assert response.status_code == 422
