"""The nodum metamodel — the typed contract over a generic graph.

The metamodel describes every node kind's field shape and every edge kind's
endpoint signature. It used to be a *frozen, code-defined* registry; it is now a
**runtime-evolvable schema stored in the database**. This module keeps the value
types (:class:`FieldSpec`, :class:`NodeKind`, :class:`EdgeKind`), the validation
logic, and the (de)serialisation between a kind and its stored ``spec`` JSON. The
*catalog* lives in the ``node_kinds`` / ``edge_kinds`` tables (loaded by
:mod:`nodum.db`); the service resolves a kind from the DB and hands the resolved
object here to validate an instance.

The dicts below are only the **seed** written on ``init-db`` (and backfilled by
migration). They are fully editable/deletable thereafter via the kind-CRUD
surfaces, so they are no longer the source of truth at runtime — only the
starting point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC
from datetime import date as _date
from datetime import datetime as _datetime

# ``date`` is a plain calendar date (YYYY-MM-DD, no timezone); ``datetime`` is an
# instant stored canonically as UTC ISO-8601 with a 'Z' suffix (the SPA shows and
# enters it in the browser's local time, converting on the edges).
FIELD_TYPES = ("str", "int", "float", "bool", "list[str]", "enum", "date", "datetime")


class ValidationError(ValueError):
    """Raised when a node/edge violates its kind, or a kind spec is malformed."""


@dataclass(frozen=True)
class FieldSpec:
    """One field in a kind's payload schema."""

    type: str
    required: bool = False
    choices: tuple[str, ...] | None = None  # for type == "enum"
    description: str = ""


@dataclass(frozen=True)
class NodeKind:
    """A node kind: its group, what its universal ``content`` means, and its fields."""

    name: str
    group: str
    content_label: str
    fields: dict[str, FieldSpec] = field(default_factory=dict)


@dataclass(frozen=True)
class EdgeKind:
    """An edge kind: the allowed ``from``/``to`` node kinds, plus any fields."""

    name: str
    from_kinds: frozenset[str]
    to_kinds: frozenset[str]
    symmetric: bool = False
    fields: dict[str, FieldSpec] = field(default_factory=dict)


# ── Default seed catalog ──────────────────────────────────────────────────────
# Written once on init-db; editable at runtime thereafter (no longer canonical).

DEFAULT_NODE_KINDS: dict[str, NodeKind] = {
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

# Sentinel for an unconstrained edge endpoint: any default node kind. Materialised
# to the concrete kind list at seed time, so it does not auto-track later edits.
ANY: frozenset[str] = frozenset(DEFAULT_NODE_KINDS)


DEFAULT_EDGE_KINDS: dict[str, EdgeKind] = {
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


# ── Instance validation (against a resolved kind) ─────────────────────────────


def validate_node(node_kind: NodeKind, content: str, data: dict) -> None:
    """Validate a node's ``content`` and payload against its (resolved) kind.

    Args:
        node_kind: The kind object, resolved from the DB by the caller.
        content: The node's universal text. Required and non-empty.
        data: The kind-specific metadata payload.

    Raises:
        ValidationError: Empty content, a missing required field, or a field
            whose value does not match its declared type.
    """
    if not str(content or "").strip():
        raise ValidationError("node content must be a non-empty string")
    _validate_fields(node_kind.fields, data, context=f"node {node_kind.name}")


def validate_edge(edge_kind: EdgeKind, from_kind: str, to_kind: str, data: dict) -> None:
    """Validate an edge's endpoint kinds and payload against its (resolved) kind.

    Raises:
        ValidationError: An endpoint whose kind is outside the signature, or an
            invalid field value.
    """
    if from_kind not in edge_kind.from_kinds:
        raise ValidationError(
            f"{edge_kind.name}: 'from' must be one of "
            f"{sorted(edge_kind.from_kinds)}, got {from_kind}"
        )
    if to_kind not in edge_kind.to_kinds:
        raise ValidationError(
            f"{edge_kind.name}: 'to' must be one of {sorted(edge_kind.to_kinds)}, got {to_kind}"
        )
    _validate_fields(edge_kind.fields, data, context=f"edge {edge_kind.name}")


def _validate_fields(specs: dict[str, FieldSpec], data: dict, *, context: str) -> None:
    """Check required fields are present and declared fields match their type.

    Undeclared keys are allowed (forward-compatible); only declared fields are
    type-checked. Required fields must be present.
    """
    for name, spec in specs.items():
        if spec.required and name not in data:
            raise ValidationError(f"{context}: missing required field {name!r}")
    # ``list(...)`` because date/datetime values are normalised back into ``data``
    # in place (the stored payload becomes canonical) while we iterate.
    for name, value in list(data.items()):
        spec = specs.get(name)
        if spec is not None:
            data[name] = _check_type(spec, name, value, context)


def _check_type(spec: FieldSpec, name: str, value: object, context: str) -> object:
    """Validate one field value against its FieldSpec; return the value to store.

    For every type but date/datetime the value is returned unchanged. A ``date`` is
    re-emitted as ``YYYY-MM-DD`` and a ``datetime`` is normalised to UTC ISO-8601
    (``…Z``), so the stored payload is always canonical.
    """
    if value is None:
        return None
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
    if spec.type == "date":
        return _normalise_date(name, value, context)
    if spec.type == "datetime":
        return _normalise_datetime(name, value, context)
    return value


def _normalise_date(name: str, value: object, context: str) -> str:
    """Validate an ISO calendar date and return it canonicalised as ``YYYY-MM-DD``."""
    if not isinstance(value, str):
        raise ValidationError(f"{context}: field {name!r} must be an ISO date string (YYYY-MM-DD)")
    try:
        return _date.fromisoformat(value.strip()).isoformat()
    except ValueError as exc:
        raise ValidationError(
            f"{context}: field {name!r} is not a valid ISO date (YYYY-MM-DD): {value!r}"
        ) from exc


def _normalise_datetime(name: str, value: object, context: str) -> str:
    """Validate an ISO-8601 datetime and return it as UTC ISO-8601 with a ``Z`` suffix.

    A value carrying an explicit offset is converted to UTC; a naive value (no
    offset, no ``Z``) is assumed to already be UTC.
    """
    if not isinstance(value, str):
        raise ValidationError(f"{context}: field {name!r} must be an ISO 8601 datetime string")
    try:
        parsed = _datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValidationError(
            f"{context}: field {name!r} is not a valid ISO 8601 datetime: {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ── Spec (de)serialisation — the on-disk / on-the-wire JSON form ──────────────


def field_spec_to_json(spec: FieldSpec) -> dict:
    """Serialise one FieldSpec to its JSON form."""
    return {
        "type": spec.type,
        "required": spec.required,
        "choices": list(spec.choices) if spec.choices else None,
        "description": spec.description,
    }


def _fields_to_json(fields: dict[str, FieldSpec]) -> dict:
    return {name: field_spec_to_json(spec) for name, spec in fields.items()}


def field_spec_from_json(name: str, raw: object) -> FieldSpec:
    """Build (and validate) a FieldSpec from its JSON form.

    Raises:
        ValidationError: Unknown ``type``, or an ``enum`` without a non-empty
            list of string ``choices``.
    """
    if not isinstance(raw, dict):
        raise ValidationError(f"field {name!r}: spec must be an object")
    field_type = raw.get("type")
    if field_type not in FIELD_TYPES:
        raise ValidationError(
            f"field {name!r}: type must be one of {list(FIELD_TYPES)}, got {field_type!r}"
        )
    choices_raw = raw.get("choices")
    choices: tuple[str, ...] | None = None
    if field_type == "enum":
        if not isinstance(choices_raw, list) or not choices_raw:
            raise ValidationError(f"field {name!r}: enum requires a non-empty 'choices' list")
        if not all(isinstance(choice, str) for choice in choices_raw):
            raise ValidationError(f"field {name!r}: enum 'choices' must be strings")
        choices = tuple(choices_raw)
    return FieldSpec(
        type=field_type,
        required=bool(raw.get("required", False)),
        choices=choices,
        description=str(raw.get("description", "")),
    )


def _fields_from_json(raw: object) -> dict[str, FieldSpec]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError("'fields' must be an object mapping name → spec")
    return {name: field_spec_from_json(name, spec) for name, spec in raw.items()}


def node_kind_to_spec(node_kind: NodeKind) -> dict:
    """Serialise a NodeKind to its stored ``spec`` JSON (without the name)."""
    return {
        "group": node_kind.group,
        "content_label": node_kind.content_label,
        "fields": _fields_to_json(node_kind.fields),
    }


def node_kind_from_spec(name: str, spec: dict) -> NodeKind:
    """Build (and validate) a NodeKind from its name + stored ``spec`` JSON."""
    content_label = str(spec.get("content_label") or "").strip()
    if not content_label:
        raise ValidationError(f"node kind {name!r}: 'content_label' must be non-empty")
    return NodeKind(
        name=name,
        group=str(spec.get("group", "")),
        content_label=content_label,
        fields=_fields_from_json(spec.get("fields")),
    )


def edge_kind_to_spec(edge_kind: EdgeKind) -> dict:
    """Serialise an EdgeKind to its stored ``spec`` JSON (without the name)."""
    return {
        "from": sorted(edge_kind.from_kinds),
        "to": sorted(edge_kind.to_kinds),
        "symmetric": edge_kind.symmetric,
        "fields": _fields_to_json(edge_kind.fields),
    }


def edge_kind_from_spec(name: str, spec: dict) -> EdgeKind:
    """Build (and validate) an EdgeKind from its name + stored ``spec`` JSON."""
    from_kinds = spec.get("from")
    to_kinds = spec.get("to")
    if not isinstance(from_kinds, list) or not from_kinds:
        raise ValidationError(f"edge kind {name!r}: 'from' must be a non-empty list of node kinds")
    if not isinstance(to_kinds, list) or not to_kinds:
        raise ValidationError(f"edge kind {name!r}: 'to' must be a non-empty list of node kinds")
    return EdgeKind(
        name=name,
        from_kinds=frozenset(str(item) for item in from_kinds),
        to_kinds=frozenset(str(item) for item in to_kinds),
        symmetric=bool(spec.get("symmetric", False)),
        fields=_fields_from_json(spec.get("fields")),
    )


def validate_edge_endpoints_known(edge_kind: EdgeKind, known_node_kinds: set[str]) -> None:
    """Ensure an edge kind's endpoints all reference existing node kinds.

    Raises:
        ValidationError: A ``from``/``to`` entry names an unknown node kind.
    """
    unknown = (edge_kind.from_kinds | edge_kind.to_kinds) - known_node_kinds
    if unknown:
        raise ValidationError(
            f"edge kind {edge_kind.name!r}: unknown node kind(s) in signature: {sorted(unknown)}"
        )


# ── Schema serialisation ──────────────────────────────────────────────────────


def schema_from(node_kinds: dict[str, NodeKind], edge_kinds: dict[str, EdgeKind]) -> dict:
    """Serialise a kind catalog — the machine-readable, evolvable contract.

    The output explains the relations: every node kind with its fields, and every
    edge kind with its ``from``→``to`` signature. Kinds are sorted by name.
    """
    return {
        "node_kinds": [
            {"name": nk.name, **node_kind_to_spec(nk)}
            for nk in sorted(node_kinds.values(), key=lambda nk: nk.name)
        ],
        "edge_kinds": [
            {"name": ek.name, **edge_kind_to_spec(ek)}
            for ek in sorted(edge_kinds.values(), key=lambda ek: ek.name)
        ],
    }


def default_schema() -> dict:
    """Serialise the default seed catalog (no DB) — used in unit tests + docs."""
    return schema_from(DEFAULT_NODE_KINDS, DEFAULT_EDGE_KINDS)
