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

#: Where minting is the module's whole job.
MINTING_MODULES = {"auth.py", "principal.py"}


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


def test_every_adapter_binds_a_principal_it_was_given():
    """Each adapter has exactly one identity source, and it is in ``auth``.

    The HTTP module's own guards pin it to the session; this states the
    weaker property the other two adapters must also satisfy — the identity
    enters through ``nodum.auth`` and nowhere else.
    """
    sources = {
        "cli.py": {"owner_principal"},  # trusted-local: --as names the human
        "mcp_server.py": {"verify_agent_token"},  # the agent's token
        "http_api.py": {"verify_login", "principal_for_session", "delete_session"},
    }
    for name, allowed in sources.items():
        path = Path(nodum.__file__).parent / name
        calls = {
            node.func.attr
            for node in ast.walk(_tree(path))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "auth"
        }
        minting = calls & {
            "owner_principal",
            "agent_principal",
            "verify_agent_token",
            "verify_login",
            "principal_for_session",
        }
        assert minting <= allowed, f"{name} mints principals through {sorted(minting - allowed)}"
