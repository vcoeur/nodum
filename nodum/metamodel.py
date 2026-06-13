"""The nodum metamodel — the typed layer over a generic graph.

A code-defined, curated registry that *programmatically* describes every node
kind's field shape and every edge kind's endpoint signature. It is the single
source of truth for kinds; :func:`schema` serialises it so any client (LLM,
CLI, API, web) self-orients. Instances live in the generic ``nodes``/``edges``
tables (one of each); this module is the typed contract over them.

Adding a kind is a registry edit here — no per-kind table or model class.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FIELD_TYPES = ("str", "int", "float", "bool", "list[str]", "enum")


class ValidationError(ValueError):
    """Raised when a node/edge violates its kind's shape or an edge signature."""


@dataclass(frozen=True)
class FieldSpec:
    """One field in a kind's payload schema."""

    type: str
    required: bool = False
    choices: tuple[str, ...] | None = None  # for type == "enum"
    description: str = ""


@dataclass(frozen=True)
class NodeKind:
    """A node kind: its group, what its universal ``text`` means, and its fields."""

    name: str
    group: str
    text_label: str
    fields: dict[str, FieldSpec] = field(default_factory=dict)


@dataclass(frozen=True)
class EdgeKind:
    """An edge kind: the allowed ``from``/``to`` node kinds, plus any fields."""

    name: str
    from_kinds: frozenset[str]
    to_kinds: frozenset[str]
    symmetric: bool = False
    fields: dict[str, FieldSpec] = field(default_factory=dict)


# ── Node-kind catalog ───────────────────────────────────────────────────────

NODE_KINDS: dict[str, NodeKind] = {
    "Person": NodeKind(
        "Person",
        "entity",
        "name",
        {"aliases": FieldSpec("list[str]"), "born": FieldSpec("int", description="birth year")},
    ),
    "Organization": NodeKind("Organization", "entity", "name", {"aliases": FieldSpec("list[str]")}),
    "Topic": NodeKind("Topic", "entity", "label", {"aliases": FieldSpec("list[str]")}),
    "Entity": NodeKind(
        "Entity",
        "entity",
        "label",
        {
            "entity_type": FieldSpec("str", description="place / concept / event / …"),
            "aliases": FieldSpec("list[str]"),
        },
    ),
    "Reference": NodeKind(
        "Reference",
        "literature",
        "citation",
        {
            "citekey": FieldSpec("str"),
            "authors": FieldSpec("list[str]"),
            "year": FieldSpec("int"),
            "venue": FieldSpec("str"),
            "doi": FieldSpec("str"),
            "url": FieldSpec("str"),
            "ref_type": FieldSpec("str", description="article / book / report / …"),
        },
    ),
    "Literature": NodeKind(
        "Literature", "literature", "summary", {"key_points": FieldSpec("list[str]")}
    ),
    "Note": NodeKind(
        "Note",
        "note",
        "text",
        {
            "role": FieldSpec(
                "enum",
                choices=(
                    "claim",
                    "question",
                    "hypothesis",
                    "observation",
                    "synthesis",
                    "definition",
                ),
                description="rhetorical role",
            ),
            "confidence": FieldSpec("float"),
        },
    ),
}

# Sentinel for an unconstrained edge endpoint: any node kind.
ANY: frozenset[str] = frozenset(NODE_KINDS)


# ── Edge-kind catalog ───────────────────────────────────────────────────────

EDGE_KINDS: dict[str, EdgeKind] = {
    "AuthorOf": EdgeKind("AuthorOf", frozenset({"Person"}), frozenset({"Reference"})),
    "AffiliatedWith": EdgeKind(
        "AffiliatedWith", frozenset({"Person"}), frozenset({"Organization"})
    ),
    "Publishes": EdgeKind("Publishes", frozenset({"Organization"}), frozenset({"Reference"})),
    "summarizes": EdgeKind("summarizes", frozenset({"Literature"}), frozenset({"Reference"})),
    "cites": EdgeKind("cites", frozenset({"Note"}), frozenset({"Literature", "Reference"})),
    "IsAbout": EdgeKind(
        "IsAbout", frozenset({"Note", "Literature", "Reference"}), frozenset({"Topic"})
    ),
    "BroaderThan": EdgeKind("BroaderThan", frozenset({"Topic"}), frozenset({"Topic"})),
    "mentions": EdgeKind("mentions", ANY, frozenset({"Person", "Organization", "Topic", "Entity"})),
    "supports": EdgeKind("supports", frozenset({"Note"}), frozenset({"Note"})),
    "contradicts": EdgeKind("contradicts", frozenset({"Note"}), frozenset({"Note"})),
    "refines": EdgeKind("refines", frozenset({"Note"}), frozenset({"Note"})),
    "answers": EdgeKind("answers", frozenset({"Note"}), frozenset({"Note"})),
}


# ── Validation ──────────────────────────────────────────────────────────────


def validate_node(kind: str, data: dict) -> None:
    """Validate a node's kind and payload against the metamodel.

    Raises:
        ValidationError: Unknown kind, missing ``text``, missing required field,
            or a field whose value does not match its declared type.
    """
    nk = NODE_KINDS.get(kind)
    if nk is None:
        raise ValidationError(f"unknown node kind {kind!r} (known: {sorted(NODE_KINDS)})")
    if not str(data.get("text", "")).strip():
        raise ValidationError("node text must be a non-empty string")
    _validate_fields(nk.fields, data, context=f"node {kind}")


def validate_edge(kind: str, from_kind: str, to_kind: str, data: dict) -> None:
    """Validate an edge's kind, endpoint kinds, and payload against the metamodel.

    Raises:
        ValidationError: Unknown kind, an endpoint whose kind is outside the
            signature, or an invalid field value.
    """
    ek = EDGE_KINDS.get(kind)
    if ek is None:
        raise ValidationError(f"unknown edge kind {kind!r} (known: {sorted(EDGE_KINDS)})")
    if from_kind not in ek.from_kinds:
        raise ValidationError(
            f"{kind}: 'from' must be one of {sorted(ek.from_kinds)}, got {from_kind}"
        )
    if to_kind not in ek.to_kinds:
        raise ValidationError(f"{kind}: 'to' must be one of {sorted(ek.to_kinds)}, got {to_kind}")
    _validate_fields(ek.fields, data, context=f"edge {kind}")


def _validate_fields(specs: dict[str, FieldSpec], data: dict, *, context: str) -> None:
    """Check required fields are present and declared fields match their type.

    Undeclared keys are allowed (forward-compatible); ``text``/``role`` and any
    declared field are checked. Required fields must be present.
    """
    for name, spec in specs.items():
        if spec.required and name not in data:
            raise ValidationError(f"{context}: missing required field {name!r}")
    for name, value in data.items():
        if name == "text":
            continue
        spec = specs.get(name)
        if spec is not None:
            _check_type(spec, name, value, context)


def _check_type(spec: FieldSpec, name: str, value: object, context: str) -> None:
    """Validate one field value against its FieldSpec."""
    if value is None:
        return
    if spec.type == "str" and not isinstance(value, str):
        raise ValidationError(f"{context}: field {name!r} must be a string")
    if spec.type == "int" and (isinstance(value, bool) or not isinstance(value, int)):
        raise ValidationError(f"{context}: field {name!r} must be an integer")
    if spec.type == "float" and (isinstance(value, bool) or not isinstance(value, int | float)):
        raise ValidationError(f"{context}: field {name!r} must be a number")
    if spec.type == "bool" and not isinstance(value, bool):
        raise ValidationError(f"{context}: field {name!r} must be a boolean")
    if spec.type == "list[str]" and (
        not isinstance(value, list) or not all(isinstance(item, str) for item in value)
    ):
        raise ValidationError(f"{context}: field {name!r} must be a list of strings")
    if spec.type == "enum" and value not in (spec.choices or ()):
        raise ValidationError(
            f"{context}: field {name!r} must be one of {list(spec.choices or ())}, got {value!r}"
        )


# ── Introspection ───────────────────────────────────────────────────────────


def _fields_json(fields: dict[str, FieldSpec]) -> dict:
    return {
        name: {
            "type": spec.type,
            "required": spec.required,
            "choices": list(spec.choices) if spec.choices else None,
            "description": spec.description,
        }
        for name, spec in fields.items()
    }


def schema() -> dict:
    """Serialise the whole metamodel — the machine-readable contract."""
    return {
        "node_kinds": [
            {
                "name": nk.name,
                "group": nk.group,
                "text_label": nk.text_label,
                "fields": _fields_json(nk.fields),
            }
            for nk in NODE_KINDS.values()
        ],
        "edge_kinds": [
            {
                "name": ek.name,
                "from": sorted(ek.from_kinds),
                "to": sorted(ek.to_kinds),
                "symmetric": ek.symmetric,
                "fields": _fields_json(ek.fields),
            }
            for ek in EDGE_KINDS.values()
        ],
    }
