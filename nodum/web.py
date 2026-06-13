"""The minimal, read-first web view — a thin browser client of the HTTP API.

This module holds no business logic: it only serves a single static HTML page
and its JS/CSS, which call the API's JSON endpoints from the browser via
``fetch()``. Mount it onto the API's FastAPI app with :func:`register`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def register(app) -> None:
    """Mount the minimal web view onto an existing FastAPI app: GET / serves the
    page, and /static serves the JS/CSS."""
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/")
    def index(request: Request):
        """Serve the single-page read-first view."""
        return templates.TemplateResponse(request, "index.html")
