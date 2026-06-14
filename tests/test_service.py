"""Service-layer tests against the live database — kind-aware CRUD over the graph."""

from __future__ import annotations

import uuid

import pytest

from nodum import metamodel, service
from nodum.models import EdgeOut, NodeOut
from nodum.service import EdgeNotFound, KindInUse, KindNotFound, NodeNotFound


def _person_and_reference() -> tuple[NodeOut, NodeOut]:
    """Create a Person and a Reference node and return the pair."""
    person = service.add_node("Person", "Ada Lovelace", data={"born": 1815})
    reference = service.add_node("Reference", "Lovelace 1843, Notes on the Engine")
    return person, reference


def test_add_typed_nodes() -> None:
    """Typed create stores the kind and validated payload (Person.born, Reference)."""
    person, reference = _person_and_reference()
    assert person.kind == "Person"
    assert person.content == "Ada Lovelace"
    assert "text" not in person.data  # the universal text lives in `content`, not `data`
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
        service.update_node(missing, content="nope")
    with pytest.raises(NodeNotFound):
        service.delete_node(missing)
    with pytest.raises(EdgeNotFound):
        service.update_edge(missing, data={"x": 1})
    with pytest.raises(EdgeNotFound):
        service.delete_edge(missing)


def test_update_node_changes_content() -> None:
    """``update_node`` replaces the node's content while preserving its payload."""
    note = service.add_node("Note", "first draft", data={"role": "claim"})
    updated = service.update_node(note.uuid, content="second draft")
    assert updated.content == "second draft"
    assert updated.data["role"] == "claim"


# ── Evolvable schema: kind CRUD ───────────────────────────────────────────────


def test_add_and_use_node_kind(restore_kinds: None) -> None:
    """A runtime-added node kind is usable for create and visible in the schema."""
    entry = service.add_node_kind(
        "Dataset", group="entity", content_label="name", fields={"rows": {"type": "int"}}
    )
    assert entry["name"] == "Dataset"
    assert entry["content_label"] == "name"

    node = service.add_node("Dataset", "MNIST", data={"rows": 70000})
    assert node.kind == "Dataset"
    assert node.data["rows"] == 70000
    assert "Dataset" in {nk["name"] for nk in service.schema()["node_kinds"]}


def test_add_node_kind_rejects_duplicate(restore_kinds: None) -> None:
    """Registering an existing node kind name is rejected."""
    with pytest.raises(metamodel.ValidationError):
        service.add_node_kind("Person")


def test_update_node_kind_edits_spec(restore_kinds: None) -> None:
    """Editing a node kind replaces only the given attributes."""
    updated = service.update_node_kind("Topic", content_label="subject")
    assert updated["content_label"] == "subject"
    # The fields were not passed, so they are preserved.
    assert "aliases" in updated["fields"]


def test_delete_node_kind_blocks_when_used_then_reassigns(restore_kinds: None) -> None:
    """Deleting an in-use node kind is refused; ``into`` reassigns then deletes."""
    service.add_node_kind("Draft", group="note", content_label="text")
    drafted = service.add_node("Draft", "a rough note")

    with pytest.raises(KindInUse):
        service.delete_node_kind("Draft")

    result = service.delete_node_kind("Draft", into="Note")
    assert result.reassigned == 1
    assert result.deleted is True
    assert "Draft" not in {nk["name"] for nk in service.schema()["node_kinds"]}
    # The node now carries the reassigned kind.
    assert service.get(drafted.uuid).node.kind == "Note"


def test_delete_node_kind_blocked_by_edge_signature(restore_kinds: None) -> None:
    """A node kind named in an edge signature blocks deletion even with no nodes."""
    # Topic has no nodes here, but `IsAbout` / `BroaderThan` / `mentions` name it.
    with pytest.raises(KindInUse):
        service.delete_node_kind("Topic")


def test_delete_node_kind_into_rewrites_signatures(restore_kinds: None) -> None:
    """Reassigning a node kind rewrites the edge signatures that referenced it."""
    service.delete_node_kind("Topic", into="Entity")
    is_about = next(ek for ek in service.schema()["edge_kinds"] if ek["name"] == "IsAbout")
    assert "Topic" not in is_about["to"]
    assert "Entity" in is_about["to"]


def test_add_edge_kind_and_delete_reassign(restore_kinds: None) -> None:
    """A runtime-added edge kind is usable, and ``into`` reassigns its edges on delete."""
    service.add_edge_kind("Rebuts", from_kinds=["Note"], to_kinds=["Note"])
    one = service.add_node("Note", "claim one", data={"role": "claim"})
    two = service.add_node("Note", "claim two", data={"role": "claim"})
    edge = service.add_edge("Rebuts", one.uuid, two.uuid)
    assert edge.kind == "Rebuts"

    with pytest.raises(KindInUse):
        service.delete_edge_kind("Rebuts")

    result = service.delete_edge_kind("Rebuts", into="contradicts")
    assert result.reassigned == 1
    assert service.get(one.uuid).edges[0].kind == "contradicts"


def test_schema_reports_usage_counts() -> None:
    """``schema()`` annotates each kind with how many nodes/edges currently use it."""
    one = service.add_node("Note", "claim one", data={"role": "claim"})
    two = service.add_node("Note", "claim two", data={"role": "claim"})
    service.add_node("Person", "Ada Lovelace")
    service.add_edge("supports", one.uuid, two.uuid)

    catalog = service.schema()
    usage = {nk["name"]: nk["usage"] for nk in catalog["node_kinds"]}
    edge_usage = {ek["name"]: ek["usage"] for ek in catalog["edge_kinds"]}
    assert usage["Note"] == 2
    assert usage["Person"] == 1
    assert usage["Topic"] == 0  # an unused kind reports zero, not absent
    assert edge_usage["supports"] == 1
    assert edge_usage["cites"] == 0


def test_delete_edge_kind_purge_removes_its_edges(restore_kinds: None) -> None:
    """``purge`` deletes an in-use edge kind's edges, then the kind itself."""
    service.add_edge_kind("Rebuts", from_kinds=["Note"], to_kinds=["Note"])
    one = service.add_node("Note", "claim one", data={"role": "claim"})
    two = service.add_node("Note", "claim two", data={"role": "claim"})
    service.add_edge("Rebuts", one.uuid, two.uuid)

    with pytest.raises(KindInUse):
        service.delete_edge_kind("Rebuts")

    result = service.delete_edge_kind("Rebuts", purge=True)
    assert result.removed == 1
    assert result.reassigned == 0
    assert "Rebuts" not in {ek["name"] for ek in service.schema()["edge_kinds"]}
    assert service.get(one.uuid).edges == []  # the edge was removed, not reassigned


def test_delete_edge_kind_into_and_purge_are_mutually_exclusive() -> None:
    """Passing both ``into`` and ``purge`` is a validation error."""
    with pytest.raises(metamodel.ValidationError):
        service.delete_edge_kind("contradicts", into="cites", purge=True)


def test_add_edge_kind_rejects_unknown_endpoint(restore_kinds: None) -> None:
    """An edge kind naming an unknown node kind is rejected."""
    with pytest.raises(metamodel.ValidationError):
        service.add_edge_kind("Uses", from_kinds=["Note"], to_kinds=["Ghost"])


def test_delete_missing_kind_raises_not_found(restore_kinds: None) -> None:
    """Deleting an absent kind raises ``KindNotFound``."""
    with pytest.raises(KindNotFound):
        service.delete_node_kind("Nonexistent")
    with pytest.raises(KindNotFound):
        service.delete_edge_kind("Nonexistent")
