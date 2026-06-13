"""Metamodel tests — the typed contract over the generic graph (no database).

Exercises :func:`validate_node` / :func:`validate_edge` happy paths and every
rejection branch, plus the serialised :func:`schema` shape that clients read to
self-orient.
"""

from __future__ import annotations

import pytest

from nodum import metamodel

EXPECTED_NODE_KINDS = {
    "Person",
    "Organization",
    "Topic",
    "Entity",
    "Reference",
    "Literature",
    "Note",
}


def test_validation_error_is_value_error() -> None:
    """``ValidationError`` is a ``ValueError`` subclass (so adapters catch both)."""
    assert issubclass(metamodel.ValidationError, ValueError)


def test_validate_node_accepts_typed_payload() -> None:
    """A well-formed Person payload (text + typed fields) validates cleanly."""
    metamodel.validate_node("Person", {"text": "Ada Lovelace", "born": 1815})


def test_validate_node_rejects_unknown_kind() -> None:
    """An unregistered node kind is rejected."""
    with pytest.raises(metamodel.ValidationError):
        metamodel.validate_node("Wizard", {"text": "not a kind"})


def test_validate_node_rejects_empty_text() -> None:
    """Every node needs a non-empty ``text``."""
    with pytest.raises(metamodel.ValidationError):
        metamodel.validate_node("Person", {"text": "  "})


def test_validate_node_rejects_bad_field_type() -> None:
    """A field whose value violates its declared type is rejected (``born`` is int)."""
    with pytest.raises(metamodel.ValidationError):
        metamodel.validate_node("Person", {"text": "Ada", "born": "1815"})


def test_validate_node_rejects_bad_enum() -> None:
    """An enum field rejects a value outside its declared choices (Note.role)."""
    with pytest.raises(metamodel.ValidationError):
        metamodel.validate_node("Note", {"text": "a note", "role": "rumination"})


def test_validate_edge_accepts_authorof_person_to_reference() -> None:
    """``AuthorOf`` validates for its signed endpoints Person -> Reference."""
    metamodel.validate_edge("AuthorOf", "Person", "Reference", {})


def test_validate_edge_rejects_reversed_endpoints() -> None:
    """``AuthorOf`` rejects the reversed Reference -> Person direction."""
    with pytest.raises(metamodel.ValidationError):
        metamodel.validate_edge("AuthorOf", "Reference", "Person", {})


def test_validate_edge_rejects_unknown_kind() -> None:
    """An unregistered edge kind is rejected."""
    with pytest.raises(metamodel.ValidationError):
        metamodel.validate_edge("WroteAbout", "Person", "Reference", {})


def test_schema_lists_all_node_kinds() -> None:
    """``schema`` serialises exactly the seven node kinds."""
    schema = metamodel.schema()
    names = {nk["name"] for nk in schema["node_kinds"]}
    assert names == EXPECTED_NODE_KINDS


def test_schema_lists_edge_kinds_with_signatures() -> None:
    """Every serialised edge kind carries its ``from``/``to`` endpoint signature."""
    schema = metamodel.schema()
    edge_kinds = {ek["name"]: ek for ek in schema["edge_kinds"]}
    assert set(edge_kinds) == set(metamodel.EDGE_KINDS)
    for ek in edge_kinds.values():
        assert "from" in ek and "to" in ek
    # Spot-check a signed edge: AuthorOf goes Person -> Reference.
    assert edge_kinds["AuthorOf"]["from"] == ["Person"]
    assert edge_kinds["AuthorOf"]["to"] == ["Reference"]
