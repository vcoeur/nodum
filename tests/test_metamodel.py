"""Metamodel tests — the typed contract over the generic graph (no database).

Exercises instance validation against a resolved kind, the spec (de)serialisation
that backs the runtime-evolvable catalog, and the serialised schema shape that
clients read to self-orient.
"""

from __future__ import annotations

import pytest

from nodum import metamodel
from nodum.metamodel import DEFAULT_EDGE_KINDS, DEFAULT_NODE_KINDS

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


# ── Instance validation ───────────────────────────────────────────────────────


def test_validate_node_accepts_typed_payload() -> None:
    """A well-formed Person (content + typed fields) validates cleanly."""
    metamodel.validate_node(DEFAULT_NODE_KINDS["Person"], "Ada Lovelace", {"born": 1815})


def test_validate_node_rejects_empty_content() -> None:
    """Every node needs non-empty ``content``."""
    with pytest.raises(metamodel.ValidationError):
        metamodel.validate_node(DEFAULT_NODE_KINDS["Person"], "  ", {})


def test_validate_node_rejects_bad_field_type() -> None:
    """A field whose value violates its declared type is rejected (``born`` is int)."""
    with pytest.raises(metamodel.ValidationError):
        metamodel.validate_node(DEFAULT_NODE_KINDS["Person"], "Ada", {"born": "1815"})


def test_validate_node_rejects_bad_enum() -> None:
    """An enum field rejects a value outside its declared choices (Note.role)."""
    with pytest.raises(metamodel.ValidationError):
        metamodel.validate_node(DEFAULT_NODE_KINDS["Note"], "a note", {"role": "rumination"})


def test_validate_edge_accepts_authorof_person_to_reference() -> None:
    """``AuthorOf`` validates for its signed endpoints Person -> Reference."""
    metamodel.validate_edge(DEFAULT_EDGE_KINDS["AuthorOf"], "Person", "Reference", {})


def test_validate_edge_rejects_reversed_endpoints() -> None:
    """``AuthorOf`` rejects the reversed Reference -> Person direction."""
    with pytest.raises(metamodel.ValidationError):
        metamodel.validate_edge(DEFAULT_EDGE_KINDS["AuthorOf"], "Reference", "Person", {})


# ── date / datetime fields ────────────────────────────────────────────────────


def _node_kind_with(field_type: str) -> metamodel.NodeKind:
    """A throwaway node kind carrying a single field of the given type."""
    return metamodel.NodeKind("X", "entity", "label", {"f": metamodel.FieldSpec(field_type)})


def test_date_field_canonicalises_to_iso_date() -> None:
    """A valid ISO date is accepted and re-emitted as YYYY-MM-DD."""
    data = {"f": "2026-06-14"}
    metamodel.validate_node(_node_kind_with("date"), "x", data)
    assert data["f"] == "2026-06-14"


def test_date_field_rejects_non_date() -> None:
    """A non-ISO date (or a datetime in a date field) is rejected."""
    with pytest.raises(metamodel.ValidationError):
        metamodel.validate_node(_node_kind_with("date"), "x", {"f": "14/06/2026"})
    with pytest.raises(metamodel.ValidationError):
        metamodel.validate_node(_node_kind_with("date"), "x", {"f": "2026-06-14T09:00:00Z"})


def test_datetime_field_converts_offset_to_utc() -> None:
    """An offset datetime is converted to UTC and stored with a Z suffix."""
    data = {"f": "2026-06-14T11:30:00+02:00"}
    metamodel.validate_node(_node_kind_with("datetime"), "x", data)
    assert data["f"] == "2026-06-14T09:30:00Z"


def test_datetime_field_assumes_utc_when_naive() -> None:
    """A naive datetime (no offset) is assumed to be UTC."""
    data = {"f": "2026-06-14T09:30:00"}
    metamodel.validate_node(_node_kind_with("datetime"), "x", data)
    assert data["f"] == "2026-06-14T09:30:00Z"


def test_datetime_field_keeps_utc_z() -> None:
    """A UTC datetime already carrying Z round-trips unchanged."""
    data = {"f": "2026-06-14T09:30:00Z"}
    metamodel.validate_node(_node_kind_with("datetime"), "x", data)
    assert data["f"] == "2026-06-14T09:30:00Z"


def test_datetime_field_rejects_non_datetime() -> None:
    """A value that is not an ISO 8601 datetime is rejected."""
    with pytest.raises(metamodel.ValidationError):
        metamodel.validate_node(_node_kind_with("datetime"), "x", {"f": "not-a-time"})


def test_field_spec_accepts_date_and_datetime() -> None:
    """The new value types are accepted by the spec deserialiser."""
    assert metamodel.field_spec_from_json("on", {"type": "date"}).type == "date"
    assert metamodel.field_spec_from_json("at", {"type": "datetime"}).type == "datetime"


# ── Spec (de)serialisation ────────────────────────────────────────────────────


def test_node_kind_spec_round_trips() -> None:
    """A node kind serialises to a spec and back without loss."""
    person = DEFAULT_NODE_KINDS["Person"]
    rebuilt = metamodel.node_kind_from_spec("Person", metamodel.node_kind_to_spec(person))
    assert rebuilt == person


def test_edge_kind_spec_round_trips() -> None:
    """An edge kind serialises to a spec and back without loss."""
    cites = DEFAULT_EDGE_KINDS["cites"]
    rebuilt = metamodel.edge_kind_from_spec("cites", metamodel.edge_kind_to_spec(cites))
    assert rebuilt == cites


def test_field_spec_rejects_unknown_type() -> None:
    """A field type outside the supported set is rejected."""
    with pytest.raises(metamodel.ValidationError):
        metamodel.field_spec_from_json("weight", {"type": "decimal"})


def test_field_spec_enum_requires_choices() -> None:
    """An enum field without a non-empty ``choices`` list is rejected."""
    with pytest.raises(metamodel.ValidationError):
        metamodel.field_spec_from_json("role", {"type": "enum"})


def test_node_kind_from_spec_requires_content_label() -> None:
    """A node kind spec needs a non-empty ``content_label``."""
    with pytest.raises(metamodel.ValidationError):
        metamodel.node_kind_from_spec("Blank", {"group": "x", "content_label": "", "fields": {}})


def test_edge_kind_from_spec_requires_non_empty_endpoints() -> None:
    """An edge kind spec needs non-empty ``from`` and ``to`` lists."""
    with pytest.raises(metamodel.ValidationError):
        metamodel.edge_kind_from_spec("Bad", {"from": [], "to": ["Topic"]})


def test_validate_edge_endpoints_known_rejects_unknown_node_kind() -> None:
    """An edge signature naming an unknown node kind is rejected."""
    edge_kind = metamodel.edge_kind_from_spec("Uses", {"from": ["Note"], "to": ["Ghost"]})
    with pytest.raises(metamodel.ValidationError):
        metamodel.validate_edge_endpoints_known(edge_kind, set(DEFAULT_NODE_KINDS))


# ── Schema serialisation ──────────────────────────────────────────────────────


def test_default_schema_lists_all_node_kinds() -> None:
    """``default_schema`` serialises exactly the seven seed node kinds."""
    schema = metamodel.default_schema()
    names = {nk["name"] for nk in schema["node_kinds"]}
    assert names == EXPECTED_NODE_KINDS


def test_default_schema_lists_edge_kinds_with_signatures() -> None:
    """Every serialised edge kind carries its ``from``/``to`` endpoint signature."""
    schema = metamodel.default_schema()
    edge_kinds = {ek["name"]: ek for ek in schema["edge_kinds"]}
    assert set(edge_kinds) == set(DEFAULT_EDGE_KINDS)
    for ek in edge_kinds.values():
        assert "from" in ek and "to" in ek
    # Spot-check a signed edge: AuthorOf goes Person -> Reference.
    assert edge_kinds["AuthorOf"]["from"] == ["Person"]
    assert edge_kinds["AuthorOf"]["to"] == ["Reference"]
