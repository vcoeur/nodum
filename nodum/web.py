"""Serve the built React SPA (the full-app UI) from the filesystem.

This module holds no business logic: it mounts the compiled single-page app — the
React bundle built from ``frontend/`` — and the browser drives every operation by
calling the API's JSON endpoints. The SPA is **not** part of the Python wheel: it
is built into the Docker image, which sets ``NODUM_WEB_DIST`` to the bundle path.
When that setting is unset or missing (a bare ``pip install`` — the CLI/library
distribution), no UI is mounted and the API still serves normally.

Auth: the SPA shell (``index.html`` + ``/assets``) is served openly — it is just
code, the data stays gated by :func:`nodum.api.require_auth`. Because the session
cookie is HttpOnly (unreadable by JS), the SPA detects auth state via the open
``GET /auth/session`` endpoint in :mod:`nodum.api`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from nodum.settings import load_settings


def register(app) -> None:
    """Mount the built SPA when ``NODUM_WEB_DIST`` names a real bundle; else no-op.

    Args:
        app: The FastAPI application to extend in place. When a bundle is present,
            mounts ``/assets`` (the hashed JS/CSS) and serves ``index.html`` at
            ``GET /``. When absent (no UI shipped — the PyPI install), adds nothing
            and the API is served without a web view.
    """
    web_dist = load_settings().web_dist
    if not web_dist:
        return
    dist = Path(web_dist)
    index = dist / "index.html"
    if not index.is_file():
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/")
    def spa_index() -> FileResponse:
        """Serve the SPA shell; the app handles auth + routing client-side."""
        return FileResponse(index)
