"""HTTP API tests via the FastAPI TestClient — one check set per endpoint."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi.testclient import TestClient


def _create_node(client: TestClient, kind: str, content: str, data: dict | None = None) -> dict:
    """POST a typed node and return its NodeOut JSON (asserting a 200)."""
    response = client.post("/nodes", json={"kind": kind, "content": content, "data": data or {}})
    assert response.status_code == 200, response.text
    return response.json()


def test_healthz(client: TestClient) -> None:
    """The liveness probe returns a static ok payload."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_node_and_get(client: TestClient) -> None:
    """A created node is fetchable with its content + typed payload; random UUID is 404."""
    node = _create_node(client, "Person", "API Ada", {"born": 1815})
    assert node["content"] == "API Ada"
    fetched = client.get(f"/nodes/{node['uuid']}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["node"]["uuid"] == node["uuid"]
    assert body["node"]["content"] == "API Ada"
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
        json={"kind": "Person", "content": "bad field", "data": {"born": "not-an-int"}},
    )
    assert response.status_code == 422


# ── Kind administration (the evolvable schema) ────────────────────────────────


def test_node_kind_crud(client: TestClient, restore_kinds: None) -> None:
    """POST/PATCH/DELETE a node kind round-trips through the API and the schema."""
    created = client.post(
        "/node-kinds",
        json={"name": "Dataset", "group": "entity", "content_label": "name"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["name"] == "Dataset"

    patched = client.patch("/node-kinds/Dataset", json={"content_label": "title"})
    assert patched.status_code == 200
    assert patched.json()["content_label"] == "title"

    deleted = client.delete("/node-kinds/Dataset")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert "Dataset" not in {nk["name"] for nk in client.get("/schema").json()["node_kinds"]}


def test_delete_node_kind_in_use_returns_409(client: TestClient, restore_kinds: None) -> None:
    """Deleting an in-use node kind without ``into`` is a 409; with it, a 200."""
    client.post("/node-kinds", json={"name": "Draft", "group": "note", "content_label": "text"})
    _create_node(client, "Draft", "a rough note")

    refused = client.delete("/node-kinds/Draft")
    assert refused.status_code == 409

    reassigned = client.delete("/node-kinds/Draft", params={"into": "Note"})
    assert reassigned.status_code == 200
    assert reassigned.json()["reassigned"] == 1


def test_edge_kind_crud_with_alias_signature(client: TestClient, restore_kinds: None) -> None:
    """An edge kind is created via the ``from``/``to`` wire names and is deletable."""
    created = client.post(
        "/edge-kinds",
        json={"name": "Rebuts", "from": ["Note"], "to": ["Note"]},
    )
    assert created.status_code == 200, created.text
    assert created.json()["from"] == ["Note"] and created.json()["to"] == ["Note"]

    deleted = client.delete("/edge-kinds/Rebuts")
    assert deleted.status_code == 200
    assert "Rebuts" not in {ek["name"] for ek in client.get("/schema").json()["edge_kinds"]}


def test_schema_entries_carry_usage_counts(client: TestClient) -> None:
    """Every node/edge kind entry reports how many rows currently use the kind."""
    one = _create_node(client, "Note", "claim one", {"role": "claim"})
    two = _create_node(client, "Note", "claim two", {"role": "claim"})
    edge = client.post(
        "/edges",
        json={"kind": "supports", "from_uuid": one["uuid"], "to_uuid": two["uuid"]},
    )
    assert edge.status_code == 200, edge.text

    catalog = client.get("/schema").json()
    note = next(nk for nk in catalog["node_kinds"] if nk["name"] == "Note")
    supports = next(ek for ek in catalog["edge_kinds"] if ek["name"] == "supports")
    assert note["usage"] == 2
    assert supports["usage"] == 1


def test_delete_edge_kind_purge_via_api(client: TestClient, restore_kinds: None) -> None:
    """DELETE /edge-kinds/{name}?purge=true removes its edges, then the kind (200)."""
    client.post("/edge-kinds", json={"name": "Rebuts", "from": ["Note"], "to": ["Note"]})
    one = _create_node(client, "Note", "claim one", {"role": "claim"})
    two = _create_node(client, "Note", "claim two", {"role": "claim"})
    edge = client.post(
        "/edges",
        json={"kind": "Rebuts", "from_uuid": one["uuid"], "to_uuid": two["uuid"]},
    )
    assert edge.status_code == 200, edge.text

    refused = client.delete("/edge-kinds/Rebuts")
    assert refused.status_code == 409

    purged = client.delete("/edge-kinds/Rebuts", params={"purge": "true"})
    assert purged.status_code == 200, purged.text
    assert purged.json()["removed"] == 1
    assert "Rebuts" not in {ek["name"] for ek in client.get("/schema").json()["edge_kinds"]}


def test_edge_kind_rm_purge_via_cli(run_cli: Callable[..., dict], restore_kinds: None) -> None:
    """`edge-kind rm --purge` removes the kind's edges, then the kind (CLI surface)."""
    run_cli("edge-kind", "add", "Rebuts", "--from", "Note", "--to", "Note")
    one = run_cli("add", "Note", "claim one", "--set", "role=claim")["uuid"]
    two = run_cli("add", "Note", "claim two", "--set", "role=claim")["uuid"]
    run_cli("link", one, two, "Rebuts")

    result = run_cli("edge-kind", "rm", "Rebuts", "--purge")
    assert result["removed"] == 1
    assert result["deleted"] is True


def test_patch_missing_kind_returns_404(client: TestClient, restore_kinds: None) -> None:
    """Editing an absent kind is a 404."""
    response = client.patch("/node-kinds/Nonexistent", json={"group": "x"})
    assert response.status_code == 404


def test_date_datetime_fields_via_cli(run_cli: Callable[..., dict], restore_kinds: None) -> None:
    """date/datetime fields round-trip through the CLI; datetime canonicalises to UTC."""
    run_cli(
        "node-kind",
        "add",
        "Event",
        "--group",
        "entity",
        "--content-label",
        "label",
        "--fields",
        '{"on": {"type": "date"}, "at": {"type": "datetime"}}',
    )
    node = run_cli(
        "add",
        "Event",
        "Launch",
        "--set",
        "on=2026-06-14",
        "--set",
        "at=2026-06-14T11:30:00+02:00",
    )
    assert node["data"]["on"] == "2026-06-14"
    assert node["data"]["at"] == "2026-06-14T09:30:00Z"


def test_date_datetime_in_schema_field_types(client: TestClient, restore_kinds: None) -> None:
    """A kind created with date/datetime fields reports those types back in the schema."""
    client.post(
        "/node-kinds",
        json={
            "name": "Event",
            "group": "entity",
            "content_label": "label",
            "fields": {"on": {"type": "date"}, "at": {"type": "datetime"}},
        },
    )
    event = next(nk for nk in client.get("/schema").json()["node_kinds"] if nk["name"] == "Event")
    assert event["fields"]["on"]["type"] == "date"
    assert event["fields"]["at"]["type"] == "datetime"
