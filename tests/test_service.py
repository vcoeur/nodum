"""Service-layer tests against the live database — kind-aware CRUD over the graph."""

from __future__ import annotations

import uuid

import pytest

from nodum import metamodel, service
from nodum.models import EdgeOut, NodeOut
from nodum.service import EdgeNotFound, NodeNotFound


def _person_and_reference() -> tuple[NodeOut, NodeOut]:
    """Create a Person and a Reference node and return the pair."""
    person = service.add_node("Person", "Ada Lovelace", data={"born": 1815})
    reference = service.add_node("Reference", "Lovelace 1843, Notes on the Engine")
    return person, reference


def test_add_typed_nodes() -> None:
    """Typed create stores the kind and validated payload (Person.born, Reference)."""
    person, reference = _person_and_reference()
    assert person.kind == "Person"
    assert person.data["text"] == "Ada Lovelace"
    assert person.data["born"] == 1815
    assert reference.kind == "Reference"


def test_add_edge_valid_signature() -> None:
    """``AuthorOf`` is accepted for a Person -> Reference pair."""
    person, reference = _person_and_reference()
    edge = service.add_edge("AuthorOf", person.uuid, reference.uuid)
    assert edge.kind == "AuthorOf"
    assert edge.from_uuid == person.uuid
    assert edge.to_uuid == reference.uuid


def test_add_edge_reversed_signature_raises_validation_error() -> None:
    """``AuthorOf`` rejects the reversed Reference -> Person direction."""
    person, reference = _person_and_reference()
    with pytest.raises(metamodel.ValidationError):
        service.add_edge("AuthorOf", reference.uuid, person.uuid)


def test_add_edge_missing_endpoint_raises_node_not_found() -> None:
    """An edge whose target does not exist raises ``NodeNotFound``."""
    person, _reference = _person_and_reference()
    with pytest.raises(NodeNotFound):
        service.add_edge("AuthorOf", person.uuid, uuid.uuid4())


def test_get_returns_node_with_incident_edges() -> None:
    """``get`` returns the node plus every edge incident on it."""
    person, reference = _person_and_reference()
    edge = service.add_edge("AuthorOf", person.uuid, reference.uuid)
    view = service.get(person.uuid)
    assert view.node.uuid == person.uuid
    assert edge.uuid in {incident.uuid for incident in view.edges}


def test_search_with_kind_filter() -> None:
    """Full-text search returns matches across kinds, and the kind filter narrows them."""
    person = service.add_node("Person", "Ada Lovelace pioneer of computing")
    note = service.add_node(
        "Note", "Ada Lovelace wrote the first algorithm", data={"role": "claim"}
    )

    everyone = service.search("Lovelace")
    assert {person.uuid, note.uuid} <= {hit.uuid for hit in everyone.hits}

    only_people = service.search("Lovelace", kind="Person")
    assert {hit.uuid for hit in only_people.hits} == {person.uuid}


def test_expand_with_edge_kind_filter() -> None:
    """``expand`` follows all edges by default, and ``edge_kinds`` restricts traversal."""
    note_one = service.add_node("Note", "first claim", data={"role": "claim"})
    note_two = service.add_node("Note", "second claim", data={"role": "claim"})
    reference = service.add_node("Reference", "a cited reference work")
    service.add_edge("supports", note_one.uuid, note_two.uuid)
    service.add_edge("cites", note_one.uuid, reference.uuid)

    full = service.expand(note_one.uuid, depth=1)
    assert {note_one.uuid, note_two.uuid, reference.uuid} <= {node.uuid for node in full.nodes}
    assert {edge.kind for edge in full.edges} == {"supports", "cites"}

    cites_only = service.expand(note_one.uuid, depth=1, edge_kinds=["cites"])
    assert {edge.kind for edge in cites_only.edges} == {"cites"}
    assert note_two.uuid not in {node.uuid for node in cites_only.nodes}


def test_update_node_merges_and_revalidates() -> None:
    """``update_node`` merges new payload over the old and re-validates the result."""
    person = service.add_node("Person", "Ada Lovelace", data={"born": 1815})

    merged = service.update_node(person.uuid, data={"aliases": ["Ada King"]})
    assert merged.data["born"] == 1815  # preserved from the original payload
    assert merged.data["aliases"] == ["Ada King"]

    # A merge that violates the kind's shape is rejected.
    with pytest.raises(metamodel.ValidationError):
        service.update_node(person.uuid, data={"born": "not-a-year"})


def test_update_edge_merges_payload() -> None:
    """``update_edge`` merges new payload onto an existing edge."""
    person, reference = _person_and_reference()
    edge = service.add_edge("AuthorOf", person.uuid, reference.uuid)
    updated = service.update_edge(edge.uuid, data={"note": "primary author"})
    assert isinstance(updated, EdgeOut)
    assert updated.data["note"] == "primary author"


def test_delete_node_cascades_to_edges() -> None:
    """Deleting a node also removes its incident edges; the count covers both."""
    person, reference = _person_and_reference()
    service.add_edge("AuthorOf", person.uuid, reference.uuid)
    result = service.delete_node(person.uuid)
    assert result.deleted == 2  # the node itself plus its one incident edge
    with pytest.raises(NodeNotFound):
        service.get(person.uuid)


def test_delete_edge_removes_only_the_edge() -> None:
    """Deleting an edge removes exactly one row and leaves the endpoints intact."""
    person, reference = _person_and_reference()
    edge = service.add_edge("AuthorOf", person.uuid, reference.uuid)
    result = service.delete_edge(edge.uuid)
    assert result.deleted == 1
    assert service.get(person.uuid).node.uuid == person.uuid


def test_unknown_uuids_raise_not_found() -> None:
    """Operations on absent UUIDs raise the matching not-found error."""
    missing = uuid.uuid4()
    with pytest.raises(NodeNotFound):
        service.get(missing)
    with pytest.raises(NodeNotFound):
        service.update_node(missing, text="nope")
    with pytest.raises(NodeNotFound):
        service.delete_node(missing)
    with pytest.raises(EdgeNotFound):
        service.update_edge(missing, data={"x": 1})
    with pytest.raises(EdgeNotFound):
        service.delete_edge(missing)
