"""Regenerate ``docs/http-api.md`` from the live HTTP route table.

The HTTP surface keeps its routes in one place — the ``api_routes`` list built
inside :func:`nodum.http_api.create_app` — and every claim about them (the
session-gate predicates, the adversarial sweep in ``tests/test_http_api.py``)
reads that table rather than restating it. This script is the documentation
half of the same rule: it imports the real app, walks ``app.routes``, and
renders every route — method, path, handler, auth class, and the handler
docstring's first line — as the reference page ``docs/http-api.md`` (M39).

The page is a **generated artefact**. It is committed so the docs site ships
it without a build step, and ``tests/test_docs.py`` runs this script and fails
if the committed page is not exactly what it produces — a route added,
renamed, re-verbed, or removed without regenerating the page is a test
failure, not a silent drift. Regenerate after any change to the route table::

    uv run python scripts/gen-http-api-docs.py

An output path may be given to write elsewhere; the lock test writes a scratch
file this way and compares it byte-for-byte with the committed page.
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route

from nodum.http_api import (
    LOGIN_PATH,
    _is_capability_path,
    _is_mcp_path,
    _needs_a_session,
    create_app,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_OUTPUT = REPO_ROOT / "docs" / "http-api.md"

#: Doc families for the reference page, keyed on the first path segment after
#: ``/api``. Routes are grouped by family in the rendered page; the grouping is
#: cosmetic — the coverage (every route, its methods, its handler, its auth) is
#: derived from the live table — so a new segment lands in "Other routes" until
#: the generator is given a family name for it.
FAMILIES: dict[str, str] = {
    "login": "Session",
    "logout": "Session",
    "types": "Catalog & schema",
    "schema": "Catalog & schema",
    "nodes": "Nodes",
    "edges": "Edges",
    "search": "Search & ask",
    "ask": "Search & ask",
    "summarize": "Search & ask",
    "links": "Search & ask",
    "graph": "Graph",
    "review": "Review",
    "diff": "Diff & events",
    "events": "Diff & events",
    "assets": "Assets",
    "ingest": "Ingestion",
    "uploads": "Ingestion",  # the mint half; the redeem half is a capability URL
    "undo": "History & undo",
    "export": "History & undo",
    "cycles": "Consolidation cycles",
    "me": "Accounts & sessions",
    "humans": "Accounts & sessions",
    "agents": "Accounts & sessions",
    "grants": "Grants & spaces",
    "spaces": "Grants & spaces",
    "settings": "Settings",
}

#: Route-table paths that are machinery rather than API surface: the JSON-404
#: catch-all under ``/api``, the icon, and the SPA fall-through.
_SKIP = {"/api/{path:path}", "/favicon.ico", "/{path:path}"}

#: The page's front matter, built one line per piece so the generated YAML
#: description stays a single line (mkdocs-material reads it as-is).
FRONT_MATTER = (
    "---\n"
    "title: HTTP API · nodum\n"
    "description: The full nodum HTTP surface — every route, its method, handler, auth "
    "class, and one line on what it does, generated from the live route table.\n"
    "---\n"
    "\n"
)

PREAMBLE = """\
# HTTP API

The route reference for `nodum serve` — the JSON API under `/api` plus the
`/healthz` liveness probe, exactly as `nodum.http_api` builds them.

This page is **generated** from the live route table — the `api_routes` list
inside `nodum.http_api.create_app` — and is committed so the docs site ships
it without a build step. Never edit it by hand; when the route table changes
(a route added, renamed, re-verbed, or removed), regenerate and commit:

```sh
uv run python scripts/gen-http-api-docs.py
```

`tests/test_docs.py` runs that exact command and fails if the committed page
is not what the generator produces, so the route table and this page cannot
drift apart silently.

## Auth model

The session gate is one rule: every `/api` route requires a valid session —
reads included — with exactly these exemptions:

* `POST /api/login` — **open**: the route that *makes* the session (name +
  password, argon2id; sets the `HttpOnly; SameSite=Strict` session cookie,
  and a failed-login lockout throttles brute force).
* `GET /api/download/{{token}}` and `PUT /api/uploads/{{token}}` — **token**:
  the single-use, minutes-long capability URL *is* the authorisation, minted
  by `nodum.urls` against a principal that already passed the session gate.
  No other route carries its own credential.

`/healthz` and the static UI at `/` are open, but neither is part of the
`/api` surface. Everything else is **session**-gated: the session middleware
verifies the cookie into the request scope, and every handler binds its
principal from there — no request field, header, or query parameter can set
an identity. The `Host` check, the same-origin proof for state-changing
requests, and the content-type rule apply to every route independently of
this gate.

The method column lists the methods the route table configured; Starlette
answers `HEAD` for any route configured `GET`.

The tables below list **{count} routes**, grouped by family.
"""


def _methods(route: Route) -> str:
    """The configured HTTP methods, comma-joined.

    Starlette silently adds ``HEAD`` to any route whose methods include
    ``GET`` (and ``_NoHeadRoute`` exists precisely because that default is
    wrong for the download-token route, whose token a HEAD probe would
    spend); the reference lists what the table configured, not the implied
    HEAD.

    The MCP route declares no methods at all — the SDK's ASGI app dispatches on
    the verb itself — so an empty cell would read as "none" for the one route
    that in fact answers three. Name them.
    """
    if _is_mcp_path(getattr(route, "path", "")):
        return "POST, GET, DELETE"
    methods = sorted(route.methods or ())
    return ", ".join(method for method in methods if method != "HEAD")


def _auth(path: str) -> str:
    """The auth class the session gate gives a route pattern.

    Applies the gate's own predicates to the route-table pattern — the same
    predicates the middleware applies to real request paths — so the doc and
    the gate cannot disagree about what is open.
    """
    if _is_mcp_path(path):
        # Not "open", which is what `_needs_a_session` alone would imply: this
        # route takes no session because it takes a *different* credential, and
        # a reference that called it open would be describing the opposite of
        # what `BearerGuard` does.
        return "bearer — an agent token, per request"
    if not _needs_a_session(path):
        if _is_capability_path(path):
            return "token — the URL is the credential"
        if path == LOGIN_PATH:
            return "open — makes the session"
        return "open"
    return "session"


def _notes(endpoint: Callable[..., object], path: str = "") -> str:
    """The handler docstring's first line — the one-line summary.

    The MCP transport is written for us rather than by us, so its own docstring
    describes the SDK's ASGI app and not what this route is. Say what it is.
    """
    if _is_mcp_path(path):
        return "The MCP surface for external agents: read and additive tiers only, streamable HTTP."
    doc = inspect.getdoc(endpoint) or ""
    return doc.splitlines()[0].strip() if doc else ""


def _family(path: str) -> str:
    """The doc family a route belongs to, derived from its path."""
    if _is_mcp_path(path):
        return "Agent surface (MCP)"
    if _is_capability_path(path):
        return "Capability URLs"
    parts = path.split("/")
    if len(parts) >= 3 and parts[1] == "api":
        return FAMILIES.get(parts[2], "Other routes")
    return "Health"


def collect(app: Starlette) -> list[dict[str, str]]:
    """Walk the app's route table and return the rows the reference documents."""
    rows: list[dict[str, str]] = []
    for route in app.routes:
        path = getattr(route, "path", "?")
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or path in _SKIP:
            continue
        rows.append(
            {
                "method": _methods(route),
                "path": path,
                # The MCP route's endpoint is an SDK object, not a function.
                "handler": getattr(endpoint, "__name__", type(endpoint).__name__),
                "auth": _auth(path),
                "notes": _notes(endpoint, path),
            }
        )
    return rows


def render(rows: list[dict[str, str]]) -> str:
    """Group the collected routes by family and render the reference page."""
    groups: dict[str, list[dict[str, str]]] = {}
    order: list[str] = []
    for row in rows:
        family = _family(row["path"])
        if family not in groups:
            groups[family] = []
            order.append(family)
        groups[family].append(row)
    # Health last: it is the one non-/api route, and reading order puts the
    # API surface first.
    order = [family for family in order if family != "Health"]
    order.append("Health")

    sections = [FRONT_MATTER + PREAMBLE.format(count=len(rows))]
    for family in order:
        sections.append(f"\n### {family}\n")
        sections.append("| Method | Path | Handler | Auth | Notes |")
        sections.append("|---|---|---|---|---|")
        for row in groups[family]:
            sections.append(
                "| {method} | `{path}` | `{handler}` | {auth} | {notes} |".format(
                    method=row["method"],
                    path=row["path"],
                    handler=row["handler"],
                    auth=row["auth"],
                    notes=row["notes"].replace("|", "\\|"),
                )
            )
    return "\n".join(sections) + "\n"


def main() -> None:
    """Regenerate the reference page into the given path (default: committed)."""
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    output.write_text(render(collect(create_app())), encoding="utf-8")


if __name__ == "__main__":
    main()
