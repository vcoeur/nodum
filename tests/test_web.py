"""SPA serving (nodum.web): mounts the built React bundle from NODUM_WEB_DIST.

The bundle is not part of the wheel, so these tests fabricate a minimal one in a
temp dir and mount it on a fresh app. Serving the shell + assets is open (no
auth) — only the JSON data routes are gated (see test_auth_api).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nodum import web


def _make_bundle(root: Path) -> None:
    """Write a minimal SPA bundle (index.html + one hashed asset) under root."""
    (root / "assets").mkdir()
    (root / "index.html").write_text(
        "<!doctype html><title>nodum</title><div id=root></div>", encoding="utf-8"
    )
    (root / "assets" / "app.js").write_text("console.log('nodum');", encoding="utf-8")


def test_serves_spa_when_bundle_present(tmp_path: Path, monkeypatch) -> None:
    """With a real bundle, / serves index.html and /assets serves assets — openly."""
    _make_bundle(tmp_path)
    monkeypatch.setenv("NODUM_WEB_DIST", str(tmp_path))
    app = FastAPI()
    web.register(app)
    client = TestClient(app)

    index = client.get("/")
    assert index.status_code == 200
    assert "id=root" in index.text

    asset = client.get("/assets/app.js")
    assert asset.status_code == 200
    assert "nodum" in asset.text


def test_no_ui_when_bundle_absent(monkeypatch) -> None:
    """Without NODUM_WEB_DIST, no SPA routes are mounted (the pip-install case)."""
    monkeypatch.delenv("NODUM_WEB_DIST", raising=False)
    app = FastAPI()
    web.register(app)
    assert TestClient(app).get("/").status_code == 404


def test_missing_dist_is_noop(tmp_path: Path, monkeypatch) -> None:
    """A NODUM_WEB_DIST that lacks index.html mounts nothing."""
    monkeypatch.setenv("NODUM_WEB_DIST", str(tmp_path / "does-not-exist"))
    app = FastAPI()
    web.register(app)
    assert TestClient(app).get("/").status_code == 404
