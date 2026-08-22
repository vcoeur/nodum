"""Contract test: `web/src/api/types.ts` cannot drift from `nodum/models.py`.

The web mirror is a hand-maintained TypeScript copy of the pydantic I/O
schema — a 918-line file with no generator behind it, so a field added on
either side could sit out of lockstep with nothing failing (finding M31).
This file closes that: it parses ``types.ts`` with a small brace-matching
parser (there is no TypeScript toolchain in the test environment) and
compares it structurally against the pydantic models.

Three classes of drift fail here, each the one that actually bites a web
client:

* **Field-name drift** — a Python field without a TS mirror, or a TS field
  no Python model has. A renamed field silently breaks the UI; that is the
  load-bearing assertion in this file.
* **Nullability drift** — ``X | None`` in Python is ``X | null`` in the TS
  mirror (the mirror's own header rule: output fields are always present,
  never ``?:``). A field whose nullability diverges makes a client mis-read
  real rows.
* **Vocabulary drift** — every field typed with a ``nodum.vocab`` Literal
  (M30) must name a TS string union with exactly the same members. The
  Python Literal is the source of truth; a union that diverges from it
  fails the suite instead of silently excluding a value the server can
  emit.

What it deliberately does not do: no TypeScript type-checking (that is
``tsc --noEmit``, and ESLint lands with M33), no comparison of the *shape*
of nested JSON payloads (a ``dict`` field maps to ``JsonObject`` /
``Record<…>`` / an opaque alias, and a complex nested shape is asserted for
presence and nullability only), and no coverage of the TS-only request and
filter shapes beyond field names, optionality and the null semantics the
mirror itself documents.
"""

from __future__ import annotations

import re
import types as _types
import typing
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from nodum import consolidate, models, vocab
from nodum.migrations import MIGRATIONS

TYPES_TS = Path(__file__).resolve().parents[1] / "web" / "src" / "api" / "types.ts"

#: Python output model → TS interface name. Both sides are kept in the file's
#: own spelling (they happen to agree everywhere; the map is what lets the
#: names diverge later without breaking the test).
PY_TO_TS: dict[str, str] = {
    "NodeOut": "NodeOut",
    "EdgeOut": "EdgeOut",
    "VersionOut": "VersionOut",
    "EventOut": "EventOut",
    "TypeOut": "TypeOut",
    "EdgeTypeOut": "EdgeTypeOut",
    "TypesOut": "TypesOut",
    "UndoResult": "UndoResult",
    "InitResult": "InitResult",
    "ProjectorStatus": "ProjectorStatus",
    "ProjectorRun": "ProjectorRun",
    "SearchHit": "SearchHit",
    "SearchResult": "SearchResult",
    "ProposalOut": "ProposalOut",
    "TransitionFailure": "TransitionFailure",
    "BatchTransitionOut": "BatchTransitionOut",
    "SubgraphOut": "SubgraphOut",
    "PathOut": "PathOut",
    "DiffOut": "DiffOut",
    "ItemFailure": "ItemFailure",
    "ProposeEdgesOut": "ProposeEdgesOut",
    "AssetOut": "AssetOut",
    "RenditionOut": "RenditionOut",
    "PurgeResult": "PurgeResult",
    "ExtractionOut": "ExtractionOut",
    "IngestOut": "IngestOut",
    "UrlGrantOut": "UrlGrantOut",
    "UploadGrantOut": "UploadGrantOut",
    "HumanOut": "HumanOut",
    "AgentOut": "AgentOut",
    "AgentCreatedOut": "AgentCreatedOut",
    "GrantOut": "GrantOut",
    "CycleOut": "CycleOut",
    "CycleDetailOut": "CycleDetailOut",
    "RollbackConflictOut": "RollbackConflictOut",
    "RollbackBlockerOut": "RollbackBlockerOut",
    "RollbackOut": "RollbackOut",
    "SpaceOut": "SpaceOut",
    "TitleResolution": "TitleResolution",
    # Defined in nodum/consolidate.py, not models.py: the `POST /api/cycles`
    # envelope. Its `report` field is a complex nested model
    # (ConsolidationReport), so the mirror types it `JsonObject` and the
    # contract asserts presence + nullability only.
    "ConsolidationOut": "ConsolidationOut",
    # SP3: the settings report and its write/adopt shapes, served by
    # `GET/PUT/DELETE /api/settings` and `POST /api/settings/adopt-env` and
    # rendered by the Settings view. Mirrors nodum/models.py field for field;
    # no secret value is ever carried on any of them.
    "SettingOut": "SettingOut",
    "SettingsOut": "SettingsOut",
    "SettingChangeOut": "SettingChangeOut",
    "SettingAdoptSkippedOut": "SettingAdoptSkippedOut",
    "SettingAdoptOut": "SettingAdoptOut",
}

#: TS response interfaces with no pydantic twin: hand-built dicts owned by the
#: API slice, documented in the "Shapes the HTTP surface adds on top of
#: models.py" section of types.ts. Every other `*Out`/`*Result` interface must
#: be in :data:`PY_TO_TS`.
TS_ONLY_OUT: frozenset[str] = frozenset(
    {"HealthOut", "LoginOut", "RotatedTokenOut", "AgentStateOut"}
)

#: Pydantic models deliberately not mirrored — CLI-only command outputs with no
#: web surface. They are listed so an *intentional* future addition to the map
#: starts from a reviewed decision, and so a reviewer can see at a glance that
#: their absence is not an oversight.
CLI_ONLY: frozenset[str] = frozenset(
    {
        "AnnotationOut",
        "HandlerStatus",
        "MergeRedirectOut",
        "RetiredEdgeOut",
        "MergeOut",
        "RetypeOut",
        "SupersedeOut",
        "RelinkDiff",
        "BulkRelinkOut",
    }
)

#: TS-only fields on a mirrored interface — the "phantom field" allowlist.
#: Currently empty: every TS field on a mirrored interface exists on the
#: Python model, which is the state a hand mirror should stay in. If a
#: client-side-only field is ever justified, it must be named here with the
#: reason, or the test fails.
TS_ONLY_FIELDS: dict[str, frozenset[str]] = {}

#: Python fields a mirrored interface may omit. Currently empty; every Python
#: output field is a real part of the wire shape, so a missing TS field is a
#: divergence unless named here with the reason.
TS_MISSING_FIELDS: dict[str, frozenset[str]] = {}

#: Python input model → TS request-body interface (M1 shapes).
INPUT_TO_TS: dict[str, str] = {
    "NodeCreateIn": "CreateNodeBody",
    "NodeUpdateIn": "UpdateNodeBody",
    "EdgeCreateIn": "CreateEdgeBody",
}

#: Input fields where the TS type deliberately narrows the pydantic annotation.
#: The rule checked elsewhere — TS carries `| null` iff the Python annotation
#: does — has these exceptions, each the documented contract rather than drift:
#:
#: * ``UpdateNodeBody.content`` / ``.props`` — ``str | None`` / ``dict | None``
#:   in the model, but the handler *refuses* null for both ("content and props
#:   are non-nullable and a null value for either is refused server-side"), so
#:   the mirror types them non-null: a client must omit rather than null.
#: * ``CreateNodeBody.space`` — a null resolves to ``main`` exactly as omission
#:   does, and the mirror documents "Omitted lands it in ``main``".
#: * ``CreateEdgeBody.props`` — a null is read as "no props", exactly as
#:   omission is.
INPUT_NULL_EXCEPTIONS: dict[str, frozenset[str]] = {
    "UpdateNodeBody": frozenset({"content", "props"}),
    "CreateNodeBody": frozenset({"space"}),
    "CreateEdgeBody": frozenset({"props"}),
}

#: The vocab Literals that have a same-named TS string-union alias (the M30
#: naming contract). TransitionKind, LandingState, PrincipalKind and Direction
#: deliberately have no alias — no mirrored field is typed with them, and an
#: alias nothing references would be dead weight.
VOCAB_ALIASES: tuple[str, ...] = (
    "NodeState",
    "VersionState",
    "ProposalKind",
    "GrantLevel",
    "CycleTrigger",
    "CycleStatus",
    "UrlGrantKind",
    "AgentKind",
    "TransitionAction",
    "RollbackKind",
)

#: The simple Python → TS primitive mapping, checked leniently (see
#: :func:`_assert_simple_type`). The load-bearing checks are names,
#: nullability and vocab members; this catches a whole-field type slip without
#: pinning the exact spelling of a complex type.
TS_SIMPLE = {"str": "string", "int": "number", "float": "number", "bool": "boolean"}


def _strip_comments(src: str) -> str:
    """Drop /* … */ and // … from TypeScript source (strings are not parsed)."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", src)


def _split_top_level(text: str, sep: str) -> list[str]:
    """Split on `sep` at brace/paren/bracket depth 0 (nested types stay whole)."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch in "{([":
            depth += 1
        elif ch in "})]":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _string_union_members(rhs: str) -> set[str] | None:
    """The quoted members if `rhs` is a union of string literals, else None."""
    members: set[str] = set()
    for part in _split_top_level(rhs, "|"):
        match = re.fullmatch(r'"([^"]*)"', part.strip())
        if match is None:
            return None
        members.add(match.group(1))
    return members


TypeInfo = tuple[dict[str, tuple[str, bool]], str | None]


def _parse_types_ts() -> tuple[dict[str, TypeInfo], dict[str, set[str]]]:
    """Parse types.ts into (interfaces, string-unions).

    ``interfaces`` maps interface name → (fields, base), where fields maps
    field name → (type expression, optional) and base is the ``extends``
    name or None. ``string-unions`` maps alias name → member set, for the
    ``export type X = "a" | "b"`` aliases only.
    """
    code = _strip_comments(TYPES_TS.read_text())
    interfaces: dict[str, tuple[dict[str, tuple[str, bool]], str | None]] = {}
    unions: dict[str, set[str]] = {}

    for match in re.finditer(r"export\s+interface\s+(\w+)(?:\s+extends\s+(\w+))?\s*\{", code):
        name, base = match.group(1), match.group(2)
        body_start = match.end() - 1  # index of the opening brace
        depth = 1
        i = body_start + 1
        while i < len(code) and depth:
            if code[i] == "{":
                depth += 1
            elif code[i] == "}":
                depth -= 1
            i += 1
        body = code[body_start + 1 : i - 1]
        fields: dict[str, tuple[str, bool]] = {}
        for chunk in _split_top_level(body, ";"):
            chunk = chunk.strip()
            if not chunk or chunk.startswith("["):
                continue  # index signature (HealthOut's `[key: string]: unknown`)
            field_match = re.match(r"^(?:readonly\s+)?([A-Za-z_]\w*)(\?)?\s*:\s*([\s\S]*)$", chunk)
            if field_match is None:
                continue
            fields[field_match.group(1)] = (
                field_match.group(3).strip(),
                bool(field_match.group(2)),
            )
        interfaces[name] = (fields, base)

    for match in re.finditer(r"export\s+type\s+(\w+)\s*=\s*(.+?);", code):
        name, rhs = match.group(1), match.group(2)
        members = _string_union_members(rhs)
        if members is not None:
            unions[name] = members

    return interfaces, unions


@dataclass(frozen=True)
class _PyField:
    """The parts of a pydantic field annotation the contract compares."""

    kind: str  # "literal" | "str" | "int" | "float" | "bool" | "dict" | "list" | "model" | "other"
    nullable: bool
    detail: object = None  # frozenset[str] for a literal; element kind for a list


def _summarize_annotation(annotation: object) -> _PyField:
    """Reduce a resolved pydantic annotation to what the mirror must preserve."""
    if annotation is type(None):
        return _PyField(kind="other", nullable=True)
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, _types.UnionType):
        args = typing.get_args(annotation)
        others = [a for a in args if a is not type(None)]
        if len(others) == 1:
            inner = _summarize_annotation(others[0])
            return _PyField(kind=inner.kind, nullable=True, detail=inner.detail)
        return _PyField(kind="other", nullable=len(others) != len(args))
    if origin is typing.Literal:
        return _PyField(
            kind="literal", nullable=False, detail=frozenset(typing.get_args(annotation))
        )
    if origin is list:
        element = typing.get_args(annotation)[0]
        return _PyField(kind="list", nullable=False, detail=_summarize_annotation(element).kind)
    if origin is dict:
        return _PyField(kind="dict", nullable=False)
    if annotation in (str, int, float, bool):
        return _PyField(kind=annotation.__name__, nullable=False)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _PyField(kind="model", nullable=False)
    return _PyField(kind="other", nullable=False)


def _py_model(name: str) -> type[BaseModel]:
    """The pydantic class behind a map entry (models.py, or consolidate.py)."""
    source = models if hasattr(models, name) else consolidate
    model = getattr(source, name, None)
    assert model is not None, f"no pydantic model {name!r} in nodum.models or nodum.consolidate"
    return model


def _resolved_ts_fields(
    interfaces: dict[str, tuple[dict[str, tuple[str, bool]], str | None]], name: str
) -> dict[str, tuple[str, bool]]:
    """The interface's own fields with its `extends` base merged in."""
    fields, base = interfaces[name]
    merged = dict(_resolved_ts_fields(interfaces, base)) if base is not None else {}
    merged.update(fields)
    return merged


def _ts_union_parts(type_str: str) -> set[str]:
    """The top-level `|` members of a TS type expression (nullability probe)."""
    return {part.strip() for part in _split_top_level(type_str, "|") if part.strip()}


def _ts_string_members(type_str: str, unions: dict[str, set[str]]) -> set[str] | None:
    """The string members a TS type names, or None if it is not a string union."""
    members: set[str] = set()
    for part in _ts_union_parts(type_str) - {"null", "undefined"}:
        if part in unions:
            members |= unions[part]
            continue
        match = re.fullmatch(r'"([^"]*)"', part)
        if match is not None:
            members.add(match.group(1))
            continue
        return None
    return members


def _assert_output_model(py_name: str, ts_name: str) -> None:
    """One mirrored output model: names, nullability, vocab, simple types."""
    model = _py_model(py_name)
    hints = typing.get_type_hints(model)
    interfaces, unions = _parse_types_ts()
    ts_fields = _resolved_ts_fields(interfaces, ts_name)
    ts_names = set(ts_fields)
    py_names = set(hints)

    missing = py_names - ts_names - set(TS_MISSING_FIELDS.get(py_name, ()))
    assert not missing, (
        f"{ts_name} is missing field(s) the pydantic model emits: {sorted(missing)} — "
        "add them to the TS interface, or name them in TS_MISSING_FIELDS with a reason"
    )
    phantom = ts_names - py_names - set(TS_ONLY_FIELDS.get(py_name, ()))
    assert not phantom, (
        f"{ts_name} declares field(s) no pydantic model has: {sorted(phantom)} — "
        "remove them, or name them in TS_ONLY_FIELDS with a reason"
    )

    for name in sorted(py_names):
        py = _summarize_annotation(hints[name])
        ts_type, ts_optional = ts_fields[name]
        assert not ts_optional, (
            f"{ts_name}.{name}: output fields are always present in the dumped JSON — "
            "no `?` (types.ts header rule)"
        )

        ts_parts = _ts_union_parts(ts_type)
        ts_has_null = "null" in ts_parts
        assert py.nullable == ts_has_null, (
            f"{ts_name}.{name}: Python `{hints[name]}` maps to `{ts_type}` — "
            "`X | None` must become `X | null` here, never `?:` and never a dropped null"
        )

        if py.kind == "literal":
            assert py.detail is not None
            ts_members = _ts_string_members(ts_type, unions)
            assert ts_members is not None, (
                f"{ts_name}.{name}: Python types it `{sorted(py.detail)}` but the TS type "
                f"`{ts_type}` is not a string union"
            )
            assert ts_members == py.detail, (
                f"{ts_name}.{name}: vocab union drifted — Python `{sorted(py.detail)}`, "
                f"TS `{sorted(ts_members)}` (nodum.vocab is the source of truth)"
            )
        else:
            _assert_simple_type(ts_name, name, py, ts_parts - {"null", "undefined"})


def _assert_simple_type(ts_name: str, name: str, py: _PyField, ts_parts: set[str]) -> None:
    """Lenient Python → TS primitive mapping check for the simple cases."""
    if py.kind in TS_SIMPLE:
        mapped = TS_SIMPLE[py.kind]
        assert any(mapped in part for part in ts_parts), (
            f"{ts_name}.{name}: Python `{py.kind}` should read as `{mapped}` somewhere in "
            f"`{sorted(ts_parts)}`"
        )
    elif py.kind == "list" and py.detail in ("str", "int"):
        token = "string[]" if py.detail == "str" else "number[]"
        assert any(token in part for part in ts_parts), (
            f"{ts_name}.{name}: Python list[{py.detail}] should read as `{token}` somewhere "
            f"in `{sorted(ts_parts)}`"
        )
    elif py.kind == "dict":
        assert any(
            "JsonObject" in part
            or part.startswith("Record<")
            or re.fullmatch(r"[A-Za-z_]\w*", part)
            for part in ts_parts
        ), (
            f"{ts_name}.{name}: Python dict should map to `JsonObject` / `Record<…>` / a named "
            f"alias, not `{sorted(ts_parts)}`"
        )


def test_types_ts_parses_and_every_response_shape_is_contract_covered() -> None:
    """The parser sees every interface, and every `*Out`/`*Result` shape is
    either mirrored from a pydantic model or documented as API-slice-owned.

    The second half is the drift direction a per-field comparison cannot see:
    a whole new response interface added to types.ts with no pydantic model
    behind it. New wire shapes belong in models.py (or, if hand-built, in
    the documented TS-only set).
    """
    interfaces, _ = _parse_types_ts()
    named = set(interfaces)

    missing_ts = set(PY_TO_TS.values()) - named
    assert not missing_ts, f"types.ts no longer defines interface(s): {sorted(missing_ts)}"

    for py_name in PY_TO_TS:
        _py_model(py_name)  # every map entry must resolve to a real pydantic model
    for py_name in CLI_ONLY:
        _py_model(py_name)  # every listed CLI-only name must still exist to be absent

    response_shaped = {n for n in named if n.endswith("Out") or n.endswith("Result")}
    uncovered = response_shaped - set(PY_TO_TS.values()) - set(TS_ONLY_OUT)
    assert not uncovered, (
        f"types.ts defines response interface(s) with no pydantic twin: {sorted(uncovered)} — "
        "add a model to models.py (and the map above), or name them in TS_ONLY_OUT with a reason"
    )


def test_header_stamp_names_the_current_migration_range() -> None:
    """The header's migration stamp tracks ``nodum.migrations.MIGRATIONS``.

    The number derives from the migration list itself — the first and last
    entry names — never a hardcoded count, so appending a migration moves the
    stamp and fails this test until the header is brought along.
    """
    first = MIGRATIONS[0][0].split("_", 1)[0]
    last = MIGRATIONS[-1][0].split("_", 1)[0]
    stamp = f"migrations {first}–{last}"
    assert stamp in TYPES_TS.read_text(), (
        f"types.ts header must claim `{stamp}` — MIGRATIONS now runs {first}–{last} "
        f"({len(MIGRATIONS)} entries), and the mirror's stamp is the reader's record of that"
    )


def test_vocab_aliases_match_the_python_literals() -> None:
    """The same-named TS string unions equal the nodum.vocab Literals (M30).

    The per-field check in :func:`test_output_models` is the enforcement;
    this pins the aliases themselves, so a vocab member changed in Python
    fails even before any model field names it.
    """
    _, unions = _parse_types_ts()
    for alias in VOCAB_ALIASES:
        py_members = frozenset(typing.get_args(getattr(vocab, alias)))
        assert alias in unions, (
            f"types.ts has no `{alias}` string union — the Python Literal "
            f"`{sorted(py_members)}` needs its mirror alias"
        )
        assert unions[alias] == py_members, (
            f"`{alias}` drifted: Python `{sorted(py_members)}`, TS `{sorted(unions[alias])}`"
        )


@pytest.mark.parametrize("py_name,ts_name", sorted(PY_TO_TS.items()))
def test_output_model_matches_ts_interface(py_name: str, ts_name: str) -> None:
    """`{py_name}` (pydantic) and `{ts_name}` (types.ts) are one wire shape."""
    _assert_output_model(py_name, ts_name)


@pytest.mark.parametrize("py_name,ts_name", sorted(INPUT_TO_TS.items()))
def test_input_shapes_mirror_the_pydantic_input_models(py_name: str, ts_name: str) -> None:
    """The request bodies still mirror the M1 input models.

    Field names must match exactly; a field is `?:` exactly when the pydantic
    field has a default (absent = default, the input convention); and null
    typing follows the documented semantics — `title: null` clears, while
    `content` and `props` are non-null (see INPUT_NULL_EXCEPTIONS for the
    deliberate narrowings and their reasons).
    """
    model = _py_model(py_name)
    hints = typing.get_type_hints(model)
    required = {n for n, f in model.model_fields.items() if f.is_required()}
    interfaces, _ = _parse_types_ts()
    ts_fields, _ = interfaces[ts_name]

    assert set(hints) == set(ts_fields), (
        f"{ts_name} and {py_name} disagree on fields: "
        f"Python-only {sorted(set(hints) - set(ts_fields))}, "
        f"TS-only {sorted(set(ts_fields) - set(hints))}"
    )

    exceptions = INPUT_NULL_EXCEPTIONS[ts_name]
    for name, (ts_type, ts_optional) in ts_fields.items():
        assert ts_optional != (name in required), (
            f"{ts_name}.{name}: `?` must mark exactly the fields the pydantic model defaults "
            f"(required here: {sorted(required)})"
        )
        ts_has_null = "null" in _ts_union_parts(ts_type)
        py_nullable = _summarize_annotation(hints[name]).nullable
        if name in exceptions:
            assert not ts_has_null, (
                f"{ts_name}.{name}: the documented contract is non-null here (see "
                f"INPUT_NULL_EXCEPTIONS), but the TS type is `{ts_type}`"
            )
        else:
            assert ts_has_null == py_nullable, (
                f"{ts_name}.{name}: Python `{hints[name]}` vs TS `{ts_type}` — a null value "
                "the API accepts must be expressible, and one it refuses must not be"
            )
