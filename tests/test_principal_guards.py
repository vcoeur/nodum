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
IDENTITY_SOURCES = {
    "cli.py": {"owner_principal"},  # trusted-local: --as names the human
    "mcp_server.py": {"verify_agent_token"},  # the agent's token
    "http_api.py": {"verify_login", "principal_for_session", "delete_session"},
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
    """Each module has exactly the identity sources the map gives it, and no other.

    The HTTP module's own guards pin it to the session; this states the weaker
    property every other module must also satisfy — an identity enters through
    ``nodum.auth`` and nowhere else, and only where a human wrote down that it
    may.

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
    calls = {
        node.func.attr
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "auth"
    }
    minting = calls & MINTING_FUNCTIONS
    assert minting <= allowed, (
        f"{path.name} mints principals through {sorted(minting - allowed)}: "
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
    require that the map already knows about it.
    """
    minters = set()
    for path in _modules():
        if path.name in MINTING_MODULES:
            continue
        calls = {
            node.func.attr
            for node in ast.walk(_tree(path))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "auth"
        }
        if calls & MINTING_FUNCTIONS:
            minters.add(path.name)

    assert minters, "no module mints a principal; this property has gone vacuous"
    assert minters <= set(IDENTITY_SOURCES), (
        f"these modules mint a principal and are in no identity map: {sorted(minters)}"
    )
