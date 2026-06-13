"""HTTP API tests via the FastAPI TestClient — one check set per endpoint."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def _create_node(client: TestClient, kind: str, text: str, data: dict | None = None) -> dict:
    """POST a typed node and return its NodeOut JSON (asserting a 200)."""
    response = client.post("/nodes", json={"kind": kind, "text": text, "data": data or {}})
    assert response.status_code == 200, response.text
    return response.json()


def test_healthz(client: TestClient) -> None:
    """The liveness probe returns a static ok payload."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_node_and_get(client: TestClient) -> None:
    """A created node is fetchable with its typed payload; a random UUID is 404."""
    node = _create_node(client, "Person", "API Ada", {"born": 1815})
    fetched = client.get(f"/nodes/{node['uuid']}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["node"]["uuid"] == node["uuid"]
    assert body["node"]["data"]["born"] == 1815

    assert client.get(f"/nodes/{uuid.uuid4()}").status_code == 404


def test_patch_node(client: TestClient) -> None:
    """PATCH merges new payload, re-validates, and preserves untouched fields."""
    node = _create_node(client, "Person", "Patch Ada", {"born": 1815})
    response = client.patch(f"/nodes/{node['uuid']}", json={"data": {"aliases": ["Ada King"]}})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["born"] == 1815
    assert data["aliases"] == ["Ada King"]


def test_delete_node_cascades(client: TestClient) -> None:
    """DELETE removes the node and its incident edge; the count covers both."""
    person = _create_node(client, "Person", "Doomed author")
    reference = _create_node(client, "Reference", "Some cited work")
    edge = client.post(
        "/edges",
        json={"kind": "AuthorOf", "from_uuid": person["uuid"], "to_uuid": reference["uuid"]},
    )
    assert edge.status_code == 200, edge.text

    deleted = client.delete(f"/nodes/{person['uuid']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == 2


def test_create_edge_and_expand(client: TestClient) -> None:
    """A created edge surfaces in the seed's expansion."""
    parent = _create_node(client, "Note", "parent claim", {"role": "claim"})
    child = _create_node(client, "Note", "child claim", {"role": "claim"})
    edge = client.post(
        "/edges",
        json={"kind": "supports", "from_uuid": parent["uuid"], "to_uuid": child["uuid"]},
    )
    assert edge.status_code == 200, edge.text

    response = client.get("/expand", params={"seed": parent["uuid"], "depth": 1})
    assert response.status_code == 200
    sub = response.json()
    assert {parent["uuid"], child["uuid"]} <= {node["uuid"] for node in sub["nodes"]}
    assert (parent["uuid"], child["uuid"]) in {(e["from_uuid"], e["to_uuid"]) for e in sub["edges"]}


def test_search(client: TestClient) -> None:
    """Full-text search returns the matching node and a consistent envelope."""
    match = _create_node(client, "Reference", "analytical engine design notes")
    _create_node(client, "Note", "a note about gardening", {"role": "observation"})

    response = client.get("/search", params={"q": "analytical engine"})
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "analytical engine"
    assert body["total"] == len(body["hits"])
    assert match["uuid"] in {hit["uuid"] for hit in body["hits"]}


def test_schema_lists_seven_node_kinds(client: TestClient) -> None:
    """The schema endpoint exposes the metamodel's seven node kinds."""
    response = client.get("/schema")
    assert response.status_code == 200
    assert len(response.json()["node_kinds"]) == 7


def test_invalid_typed_edge_returns_422(client: TestClient) -> None:
    """A signature violation (AuthorOf Reference -> Person) is rejected with 422."""
    person = _create_node(client, "Person", "reversed author")
    reference = _create_node(client, "Reference", "reversed reference")
    response = client.post(
        "/edges",
        json={"kind": "AuthorOf", "from_uuid": reference["uuid"], "to_uuid": person["uuid"]},
    )
    assert response.status_code == 422


def test_bad_field_returns_422(client: TestClient) -> None:
    """A payload field of the wrong type (Person.born) is rejected with 422."""
    response = client.post(
        "/nodes",
        json={"kind": "Person", "text": "bad field", "data": {"born": "not-an-int"}},
    )
    assert response.status_code == 422
