"""The one JSON envelope every adapter emits — shared so surfaces cannot drift.

The CLI prints it on stdout and the HTTP API returns it as a response body,
both through the functions here: :func:`envelope` for a single result,
:func:`list_envelope` for the ``{"<plural>": [...], "count": n}`` list
convention, and :func:`render_json` for the text rendering itself. The result
is that ``nodum node get <id>`` and ``GET /api/nodes/{id}`` produce the *same
bytes*, and a change to the convention lands in both surfaces at once instead
of in one of them.

Serialisation is always ``model_dump(mode="json")`` on the shared
:mod:`nodum.models` schema, so the JSON shape is the pydantic model's, never an
adapter's idea of it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel


def envelope(result: BaseModel) -> dict[str, Any]:
    """Serialise one service result the way every adapter does.

    Args:
        result: A pydantic model from :mod:`nodum.models`.

    Returns:
        The JSON-mode dump of the model.
    """
    return result.model_dump(mode="json")


def list_envelope(key: str, results: Iterable[BaseModel]) -> dict[str, Any]:
    """Wrap a list result as ``{"<plural>": [...], "count": n}``.

    The named-key-plus-count shape is the CLI contract for every
    list-returning command, and the HTTP API repeats it verbatim, so a client
    parses one envelope whichever surface it talks to.

    Args:
        key: The plural key the rows are wrapped under (``nodes``, ``edges``…).
        results: The pydantic results to serialise, in order.

    Returns:
        The wrapped rows plus their count.
    """
    rows = [result.model_dump(mode="json") for result in results]
    return {key: rows, "count": len(rows)}


def render_json(payload: dict[str, Any]) -> str:
    """Render an envelope as the JSON text both surfaces emit.

    Indented for readability (these outputs are read by humans and piped into
    ``jq`` in equal measure) and never ASCII-escaped, since node content is
    multilingual Markdown.

    Args:
        payload: The envelope to render.

    Returns:
        The JSON text, without a trailing newline.
    """
    return json.dumps(payload, indent=2, ensure_ascii=False)
