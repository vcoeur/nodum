"""Shared fixtures: every test runs against a fresh, migrated SQLite file."""

from __future__ import annotations

import pytest

from nodum import service


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Point NODUM_DB at a fresh temp file and run migrations; returns the path."""
    path = tmp_path / "graph.db"
    monkeypatch.setenv("NODUM_DB", str(path))
    service.init()
    return path
