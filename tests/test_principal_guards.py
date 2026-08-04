"""Structural guards over principal construction, across every module.

:mod:`nodum.principal` states that a ``Principal`` is minted only by
:mod:`nodum.auth` and that "a test (the AST properties over the adapters)
keeps it that way". Until now no such test existed — the HTTP module had its
own AST properties and nothing looked at the CLI or the MCP adapter at all
(Q13 review N9). This file is that test, and it reads every module in the
package rather than one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from helpers import nodum_imports

import nodum
from nodum import auth

#: Where minting is the module's whole job.
MINTING_MODULES = {"auth.py", "principal.py"}

#: Every ``nodum.auth`` function that hands back a ``Principal``. A new one that
#: is not listed here would be invisible to the property below, so this list is
#: the one thing to extend when ``auth`` grows a loader — and it had already
#: fallen behind: ``principal_from_actor`` returns a ``Principal`` and is called
#: by :mod:`nodum.consolidate` and :mod:`nodum.ingest`, so the very check that
#: exists to notice a new loader could not see the newest one.
MINTING_FUNCTIONS = {
    "owner_principal",
    "agent_principal",
    "internal_principal",
    "principal_from_actor",
    "verify_agent_token",
    "verify_login",
    "principal_for_session",
}

#: Module → the identity sources it may use. **Every** module in the package is
#: checked against this map and a module that is not in it may mint nothing at
#: all, so an absent entry is a denial rather than a hole. It used to be the
#: other way round — the property iterated the map — which meant a module could
#: mint whatever it liked simply by never being listed, and two already did.
#: Entries name only functions that actually mint a principal: ``delete_session``
#: is a session *sink*, not a source (it returns no principal), so it is not a
#: minting function and is out of this guard's scope — the map is the record of
#: where identity enters, not of every ``auth`` call a module makes.
IDENTITY_SOURCES = {
    "cli.py": {"owner_principal"},  # trusted-local: --as names the human
    "mcp_server.py": {"verify_agent_token"},  # the agent's token
    "http_api.py": {"verify_login", "principal_for_session"},  # the session pair
    # The consolidation runner: the internal agent authenticates by being
    # in-process and holds no credential to present, and `principal_from_actor`
    # re-mints *who asked* from the stored string the journal row carries.
    "consolidate.py": {"internal_principal", "principal_from_actor"},
    # The ingestion pipeline re-mints the principal that authorised a capability
    # upload from the token row's own `created_by` — stored state, read after
    # the fact. It lives here rather than in the HTTP adapter precisely because
    # that adapter is structurally forbidden from minting an identity.
    "ingest.py": {"principal_from_actor"},
    # The nightly scheduler mints **nothing**, and the empty set is the
    # assertion: nobody asked for a scheduled cycle, the clock did, so it hands
    # the runner a plain `SCHEDULER_ACTOR` string and never a principal. A
    # background writer that could mint one would be an unattended process
    # choosing whose authority to write under.
    "scheduler.py": set(),
}


def _modules() -> list[Path]:
    """Every source file in the package (the guard's input is the tree, not a list)."""
    return sorted(
        path for path in Path(nodum.__file__).parent.glob("*.py") if path.name != "__init__.py"
    )


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _dotted(node: ast.AST | None) -> str | None:
    """The dotted name an Attribute/Name chain spells, or None for anything else.

    ``auth.owner_principal`` → ``"auth.owner_principal"``; a call target that
    is not a name or attribute chain (a ``**`` unpack, a subscript) spells
    nothing and resolves to nothing.
    """
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _import_from_base(node: ast.ImportFrom) -> str | None:
    """The absolute module an ImportFrom names, or None for one outside the package."""
    if node.level:
        if node.level > 1:
            return None  # a relative level beyond the flat package escapes it
        return "nodum" if node.module is None else f"nodum.{node.module}"
    if node.module == "nodum" or (node.module or "").startswith("nodum."):
        return node.module or ""
    return None


def _auth_bindings(tree: ast.Module) -> dict[str, str]:
    """Every name a module binds to :mod:`nodum.auth` or one of its functions.

    The import spellings plus the dynamic ones that bind by assignment
    (``importlib.import_module`` / ``__import__`` with a string) — the same
    reach :func:`helpers.nodum_imports` claims, seen from the side of the
    names the calls are spelled under. ``auth`` from ``from nodum import
    auth``, the alias in ``import nodum.auth as identity``, the ``nodum``
    chain from ``import nodum``, and the function itself from ``from
    nodum.auth import owner_principal as resolve`` all land here.
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not (alias.name == "nodum" or alias.name.startswith("nodum.")):
                    continue
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    # `import nodum.auth` binds `nodum`, and the chain
                    # `nodum.auth.<fn>` resolves through it.
                    bindings["nodum"] = "nodum"
        elif isinstance(node, ast.ImportFrom):
            base = _import_from_base(node)
            if base is None:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                target = alias.asname or alias.name
                bindings[target] = f"{base}.{alias.name}"
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            value = node.value
            if not isinstance(value, ast.Call):
                continue
            function = (
                value.func.attr
                if isinstance(value.func, ast.Attribute)
                else value.func.id
                if isinstance(value.func, ast.Name)
                else None
            )
            if function not in {"import_module", "__import__"}:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            for argument in [*value.args, *(keyword.value for keyword in value.keywords)]:
                if (
                    isinstance(argument, ast.Constant)
                    and isinstance(argument.value, str)
                    and (argument.value == "nodum" or argument.value.startswith("nodum."))
                ):
                    bindings[target.id] = argument.value
    return bindings


def _resolve_chain(dotted: str | None, bindings: dict[str, str]) -> str | None:
    """What a dotted call target resolves to through the module's bindings.

    ``auth.owner_principal`` with ``auth`` bound to ``nodum.auth`` resolves to
    ``nodum.auth.owner_principal``; a chain no binding is a prefix of
    (``service.create_node`` in a module that imports auth) resolves to
    nothing, so no minting call is counted in it.
    """
    if dotted is None:
        return None
    for prefix, path in bindings.items():
        if dotted == prefix:
            return path
        if dotted.startswith(prefix + "."):
            return path + dotted[len(prefix) :]
    return None


def _minting_calls(source: str) -> set[str]:
    """Every minting function a module uses — called, or passed by reference.

    The older matcher read only ``auth.<fn>`` **call** nodes on the bare name
    ``auth`` — a refactor that aliased the import (``import nodum.auth as
    identity``), reached it through the package chain (``nodum.auth.<fn>``),
    imported the function directly (``from nodum.auth import
    owner_principal``), or passed the loader as a value (``_run(auth.
    owner_principal, human_id)`` — the CLI's ``--as``) moved the same minting
    use out from under it, and a module that stopped importing ``auth`` at all
    made the guard vacuous. Any reference counts, called or handed on, because
    both are "using" the loader; the identity-rail spelling test
    (:func:`test_the_identity_rail_sees_every_spelling_of_the_import`) pins
    that every spelling is seen. This is the extractor it checks.
    """
    if "nodum.auth" not in nodum_imports(source):
        return set()
    tree = ast.parse(source)
    bindings = _auth_bindings(tree)
    used: set[str] = set()
    for node in ast.walk(tree):
        resolved = _resolve_chain(_dotted(node), bindings)
        if resolved is None:
            continue
        for function in MINTING_FUNCTIONS:
            if resolved == f"nodum.auth.{function}":
                used.add(function)
    return used


def test_the_package_has_modules_to_check():
    """A glob that matched nothing would make every property below vacuous."""
    names = {path.name for path in _modules()}
    assert {"cli.py", "http_api.py", "mcp_server.py", "service.py", "store.py"} <= names


@pytest.mark.parametrize("path", _modules(), ids=lambda path: path.name)
def test_only_auth_constructs_a_principal(path: Path):
    """``Principal(...)`` outside ``auth`` is an identity nobody verified.

    Every other module receives one. An adapter that built its own would be
    exactly the bypass the type exists to prevent — including the subtle
    version, where a service function reconstructs a principal from a string
    it was handed.
    """
    if path.name in MINTING_MODULES:
        return
    constructions = [
        node.lineno
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Principal"
    ]
    assert constructions == [], (
        f"{path.name} constructs a Principal at line(s) {constructions}: "
        "only nodum.auth may mint one"
    )


@pytest.mark.parametrize("path", _modules(), ids=lambda path: path.name)
def test_no_module_reconstructs_an_identity_from_a_dataclass_replace(path: Path):
    """``dataclasses.replace(principal, …)`` would re-grant around the loader.

    ``Principal`` is frozen, which stops attribute assignment but not a
    ``replace`` that swaps the grant set for a wider one.
    """
    if path.name in MINTING_MODULES:
        return
    replaces = [
        node.lineno
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "replace")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "replace")
        )
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in ("principal", "self")
        and any(keyword.arg in ("grants", "kind", "id") for keyword in node.keywords)
    ]
    assert replaces == [], (
        f"{path.name} rebuilds a principal at line(s) {replaces}: grants come from the loader"
    )


@pytest.mark.parametrize("path", _modules(), ids=lambda path: path.name)
def test_every_module_binds_only_the_identity_it_is_allowed(path: Path):
    """Each module calls exactly the identity sources the map gives it, and no other.

    The HTTP module's own guards pin it to the session; this states the weaker
    property every other module must also satisfy — an identity enters through
    ``nodum.auth`` and nowhere else, and only where a human wrote down that it
    may. **Exactly**, not at most: a module whose map entry names a loader it
    never calls is a permission waiting to be used, so the assertion is
    equality with the allowed set, matching the first sentence of this
    docstring (it used to be a subset check that let an allowed function go
    uncalled).

    The minting calls are matched through **any** binding of ``nodum.auth`` —
    aliased imports, the ``nodum.auth.`` chain, direct function imports —
    via :func:`_minting_calls`, whose reach
    :func:`test_the_identity_rail_sees_every_spelling_of_the_import` pins.

    **The input is the tree, not the map.** This used to iterate
    :data:`IDENTITY_SOURCES`, so a module could mint anything it liked by simply
    never being listed — and two of them were doing exactly that
    (``ingest.py``, which re-mints a capability's authoriser, and
    ``scheduler.py``, which must never mint at all). A module absent from the
    map now gets the empty set, which is a denial.

    ``auth.py`` and ``principal.py`` are exempt because minting is their whole
    job; every other module receives a principal.
    """
    if path.name in MINTING_MODULES:
        return
    allowed = IDENTITY_SOURCES.get(path.name, set())
    minting = _minting_calls(path.read_text(encoding="utf-8"))
    assert minting == allowed, (
        f"{path.name} mints through {sorted(minting - allowed)} and never calls "
        f"{sorted(allowed - minting)}: "
        "add the module to IDENTITY_SOURCES with a reason, or stop minting there"
    )


def test_the_identity_map_names_exactly_the_modules_that_are_on_disk():
    """Every entry names a real module, and every real minter is an entry.

    Two ways the map could rot. A **stale** entry is a permission waiting for a
    file of that name to reappear, now that an absent entry is a denial. And the
    three adapters are named literally, because a map that had lost all of them
    would let the property above pass by permitting nothing anybody does.
    """
    on_disk = {path.name for path in _modules()}
    assert set(IDENTITY_SOURCES) <= on_disk, sorted(set(IDENTITY_SOURCES) - on_disk)
    assert {"cli.py", "mcp_server.py", "http_api.py"} <= set(IDENTITY_SOURCES)


def test_the_minting_list_is_everything_auth_actually_hands_back():
    """The list called itself "the one thing to extend", and nothing enforced that.

    ``principal_from_actor`` returns a ``Principal`` and had been called by
    :mod:`nodum.consolidate` and :mod:`nodum.ingest` for a whole phase while
    sitting in no list — so the guard that exists to notice a new loader was
    blind to the newest one, and every property keyed on
    :data:`MINTING_FUNCTIONS` was quietly narrower than it read.

    The cure is to stop trusting the list and derive the answer from ``auth``
    itself: any public function whose **return annotation** is ``Principal`` is
    a loader, and a loader that is not named here fails right here rather than
    in six months.
    """
    tree = _tree(Path(auth.__file__))
    returns_principal = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and not node.name.startswith("_")
        and isinstance(node.returns, ast.Name)
        and node.returns.id == "Principal"
    }

    assert returns_principal, "nothing in nodum.auth returns a Principal; this test is broken"
    assert returns_principal == MINTING_FUNCTIONS, (
        "MINTING_FUNCTIONS must be exactly the auth functions that return a Principal; "
        f"missing {sorted(returns_principal - MINTING_FUNCTIONS)}, "
        f"stale {sorted(MINTING_FUNCTIONS - returns_principal)}"
    )


def test_every_module_that_mints_at_all_is_in_the_map():
    """The map is the record of *where* identity enters, so it must be complete.

    ``ingest.py`` re-mints a capability's authoriser and ``consolidate.py``
    re-mints who asked; neither was in the map, and the property that reads it
    therefore said nothing about either. This is the same question asked from
    the other side — walk the tree, find every caller of a minting function, and
    require that the map already knows about it. Minting calls are matched
    through any binding of auth, exactly as the property above does.
    """
    minters = set()
    for path in _modules():
        if path.name in MINTING_MODULES:
            continue
        if _minting_calls(path.read_text(encoding="utf-8")):
            minters.add(path.name)

    assert minters, "no module mints a principal; this property has gone vacuous"
    assert minters <= set(IDENTITY_SOURCES), (
        f"these modules mint a principal and are in no identity map: {sorted(minters)}"
    )


def test_the_identity_rail_sees_every_spelling_of_the_import():
    """The identity guard's own coverage — one case per way to bind auth.

    A rail is only as strong as what it can see, and a rail that read
    ``auth.`` attribute calls alone would have a documented way around it: a
    module that aliased the import, reached the loader through the package
    chain, imported the function outright, or loaded the module dynamically
    mints through the same loader while the old matcher went on passing. Each
    line below is a spelling a refactor might reach for, with the minting call
    that spelling makes; the assertion is that the extractor sees the call in
    all of them. The spellings are the LLM rail's twelve
    (:func:`test_llm.test_the_rail_sees_every_spelling_of_the_import`),
    pointed at :mod:`nodum.auth` and each carrying its call.
    """
    spellings = {
        "plain": "import nodum.auth\nnodum.auth.owner_principal()",
        "aliased": "import nodum.auth as identity\nidentity.owner_principal()",
        "from-package": "from nodum import auth\nauth.owner_principal()",
        "from-module": "from nodum.auth import owner_principal\nowner_principal()",
        "from-module-aliased": ("from nodum.auth import owner_principal as resolve\nresolve()"),
        "relative-package": "from . import auth\nauth.owner_principal()",
        "relative-module": "from .auth import owner_principal\nowner_principal()",
        "importlib": (
            "import importlib\n"
            "identity = importlib.import_module('nodum.auth')\n"
            "identity.owner_principal()"
        ),
        "importlib-keyword": (
            "import importlib\n"
            "identity = importlib.import_module(name='nodum.auth')\n"
            "identity.owner_principal()"
        ),
        "dunder-import": "identity = __import__('nodum.auth')\nidentity.owner_principal()",
        "dunder-keyword": "identity = __import__(name='nodum.auth')\nidentity.owner_principal()",
        "attribute-chain": "import nodum\nnodum.auth.owner_principal()",
    }
    blind = [
        name for name, source in spellings.items() if _minting_calls(source) != {"owner_principal"}
    ]
    assert blind == [], f"the rail cannot see these spellings: {blind}"
