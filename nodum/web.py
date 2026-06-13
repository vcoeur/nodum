"""The schema-driven web view — a full-CRUD browser client of the HTTP API.

This module holds no business logic: it serves a single HTML page plus its
JS/CSS, which drive every operation (search, create, read, update, delete,
subgraph expansion) by calling the API's JSON endpoints from the browser via
``fetch()``. The page is metamodel-driven — it fetches ``/schema`` on load and
builds its forms from the returned kinds. Mount it onto the API's FastAPI app
with :func:`register`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def register(app) -> None:
    """Mount the web view on a FastAPI app: GET / serves index.html, /static serves assets.

    Args:
        app: The FastAPI application to extend in place. Adds a ``/static`` mount
            for the JS/CSS and a ``GET /`` route that renders the single page.
    """
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/")
    def index(request: Request):
        """Serve the single-page, schema-driven CRUD view."""
        return templates.TemplateResponse(request, "index.html")
