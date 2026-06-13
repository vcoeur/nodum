"""Shared pytest fixtures for the nodum typed-graph test suite.

Every test runs against the live PostgreSQL named by ``NODUM_DATABASE_URL``
(default dev port 5436; CI uses 5432). The typed schema is created once per
session, and the graph is truncated before each test so the suite stays
order-independent and starts each test from an empty graph (the kind lookup
tables are left intact).
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from nodum import api, web
from nodum.cli import app as cli_app
from nodum.db import connect, init_schema


@pytest.fixture(scope="session", autouse=True)
def schema() -> None:
    """Create the typed nodes/edges schema and seed kind tables once per session."""
    with connect() as conn:
        init_schema(conn)


@pytest.fixture(autouse=True)
def clean_graph(schema: None) -> None:
    """Truncate the graph before every test; edges cascade, kind tables stay."""
    with connect() as conn:
        conn.cursor().execute("TRUNCATE nodes CASCADE")
        conn.commit()


def _ensure_web_view() -> None:
    """Mount the web view onto ``nodum.api.app`` once, unless already wired.

    The maintainer normally mounts the web view before running the suite; this
    keeps the acceptance test's ``GET /`` check robust either way.
    """
    mounted = any(getattr(route, "path", None) == "/" for route in api.app.routes)
    if not mounted:
        web.register(api.app)


@pytest.fixture(scope="session")
def client() -> TestClient:
    """A FastAPI ``TestClient`` bound to the real API app, web view mounted."""
    _ensure_web_view()
    return TestClient(api.app)


@pytest.fixture
def run_cli() -> Callable[..., dict]:
    """Return a helper that runs the CLI and returns its parsed JSON output.

    The CLI success path prints exactly one JSON object to stdout, so the
    captured stdout parses cleanly. The helper asserts a clean (exit 0) run.
    """
    runner = CliRunner()

    def _run(*args: object) -> dict:
        result = runner.invoke(cli_app, [str(arg) for arg in args])
        assert result.exit_code == 0, (
            f"CLI {args!r} exited {result.exit_code}\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}\n"
            f"exception: {result.exception!r}"
        )
        return json.loads(result.stdout)

    return _run
