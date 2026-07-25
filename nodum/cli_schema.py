"""Introspection of the CLI's own command surface — the self-describing contract.

``nodum schema-dump`` prints this structure so a caller can discover the whole
command tree without parsing ``--help`` text. The clean-install smoke test
(``scripts/smoke-install.sh``) asserts against it: if an installed wheel cannot
enumerate its own commands, the install is broken in a way a bare ``--help``
exit code would not catch.

Note the contrast with the sibling ``nodum schema <type>`` command, which
reports one *node/edge type's* catalog entry from the database. This module is
about the CLI adapter; that one is about graph data.
"""

from __future__ import annotations

from typing import Any

import typer

from nodum import __version__


def _first_line(text: str | None) -> str:
    """Reduce a command's help to its one-line summary."""
    return (text or "").strip().split("\n", 1)[0].strip()


def _param_info(param: Any) -> dict[str, Any] | None:
    """Describe one Click parameter, or None for kinds we don't surface.

    Duck-typed on ``param.param_type_name`` rather than ``isinstance`` against
    ``click``: typer vendors its own copy, so an introspected param is not an
    instance of a separately-imported ``click``'s classes — an isinstance check
    silently returns False and the entire surface drops out of the dump.
    """
    kind = getattr(param, "param_type_name", None)
    if kind == "argument":
        return {"name": param.name, "kind": "argument", "required": param.required}
    if kind == "option":
        return {
            "name": param.name,
            "kind": "option",
            "flags": list(param.opts),
            "required": param.required,
            "is_flag": getattr(param, "is_flag", False),
            "help": _first_line(getattr(param, "help", "")),
        }
    return None


def _command_info(name: str, command: Any) -> dict[str, Any]:
    """Describe a command, recursing one level into a group's subcommands.

    A group is detected by a populated ``.commands`` dict rather than an
    ``isinstance(command, click.Group)`` check — same vendored-click reason as
    :func:`_param_info`.
    """
    info: dict[str, Any] = {
        "name": name,
        "help": _first_line(command.help or command.short_help),
        "params": [p for p in (_param_info(pp) for pp in command.params) if p],
    }
    subcommands = getattr(command, "commands", None)
    if isinstance(subcommands, dict) and subcommands:
        info["subcommands"] = [_command_info(sub, subcommands[sub]) for sub in sorted(subcommands)]
    return info


def build_cli_schema() -> dict[str, Any]:
    """Build the machine-readable description of the whole CLI surface.

    Returns:
        A dict with the tool name, its version, and the sorted command tree.
    """
    from nodum.cli import app  # local import: cli imports this module

    cli = typer.main.get_command(app)
    commands = getattr(cli, "commands", {})
    return {
        "tool": "nodum",
        "version": __version__,
        "commands": [_command_info(name, commands[name]) for name in sorted(commands)],
    }
