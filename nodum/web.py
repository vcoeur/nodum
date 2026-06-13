"""The schema-driven web view — a full-CRUD browser client of the HTTP API.

This module holds no business logic: it serves a single HTML page plus its
JS/CSS, which drive every operation (search, create, read, update, delete,
subgraph expansion) by calling the API's JSON endpoints from the browser via
``fetch()``. The page is metamodel-driven — it fetches ``/schema`` on load and
builds its forms from the returned kinds. Mount it onto the API's FastAPI app
with :func:`register`.

Authentication: ``GET /`` redirects to ``/login`` unless the request carries a
valid session cookie (the browser side of :mod:`nodum.auth`); ``GET /login``
serves the sign-in page (and an "initialise the password" hint when the install
is still locked). The API's JSON routes are gated separately in
:mod:`nodum.api`; the static assets stay open.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from nodum import auth

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def _request_authed(request: Request) -> bool:
    """Return whether the request carries a valid session cookie."""
    token = request.cookies.get(auth.COOKIE_NAME)
    if not token:
        return False
    try:
        return auth.verify_token(token)
    except auth.AuthNotConfigured:
        return False


def register(app) -> None:
    """Mount the web view on a FastAPI app: ``/`` (gated), ``/login``, ``/static``.

    Args:
        app: The FastAPI application to extend in place. Adds a ``/static`` mount
            for the JS/CSS, a ``GET /login`` sign-in page, and a ``GET /`` route
            that serves the single page or redirects unauthenticated visitors to
            ``/login``.
    """
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/login")
    def login_page(request: Request):
        """Serve the sign-in page; shows a setup hint when no password is set yet."""
        return templates.TemplateResponse(
            request, "login.html", {"configured": auth.is_configured()}
        )

    @app.get("/")
    def index(request: Request):
        """Serve the single-page CRUD view, or redirect to /login when unauthenticated."""
        if not _request_authed(request):
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "index.html")
