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
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer
from typer.testing import CliRunner

from nodum import api, auth
from nodum.cli import app as cli_app
from nodum.db import connect, init_schema, seed_kinds

# A fixed main password + signing key so the whole suite runs authenticated and
# the minted tokens stay valid across tests. set_password preserves the signing
# key, and the restore_auth fixture re-seeds this exact row after any test that
# mutates the auth state, so the shared client's session never goes stale.
TEST_PASSWORD = "correct horse battery staple"
TEST_SIGNING_KEY = "test-fixed-signing-key-not-a-secret"


@pytest.fixture(scope="session", autouse=True)
def schema() -> None:
    """Create the typed nodes/edges schema and seed kind tables once per session."""
    with connect() as conn:
        init_schema(conn)


def _seed_auth() -> None:
    """Write the canonical test main password + fixed signing key (idempotent)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO auth_secret (id, password_hash, signing_key) "
                "VALUES (true, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "password_hash = EXCLUDED.password_hash, signing_key = EXCLUDED.signing_key",
                (PasswordHasher().hash(TEST_PASSWORD), TEST_SIGNING_KEY),
            )
        conn.commit()


def _session_token() -> str:
    """Mint a session token signed with the fixed test signing key."""
    return URLSafeTimedSerializer(TEST_SIGNING_KEY, salt=auth._TOKEN_SALT).dumps({"v": 1})


@pytest.fixture(scope="session", autouse=True)
def auth_configured(schema: None) -> None:
    """Seed the canonical main password once per session so the suite is authenticated."""
    _seed_auth()


@pytest.fixture
def restore_auth() -> None:
    """Restore the canonical auth row after a test that mutates auth state."""
    yield
    _seed_auth()


@pytest.fixture
def test_password() -> str:
    """The main password seeded for the suite."""
    return TEST_PASSWORD


@pytest.fixture
def session_token() -> str:
    """A valid Bearer/session token for the seeded signing key."""
    return _session_token()


@pytest.fixture(autouse=True)
def clean_graph(schema: None) -> None:
    """Truncate the graph before every test; edges cascade, kind/auth tables stay."""
    with connect() as conn:
        conn.cursor().execute("TRUNCATE nodes CASCADE")
        conn.commit()


@pytest.fixture
def restore_kinds() -> None:
    """Reset the kind catalog to the seeded defaults after a kind-mutating test.

    The kind tables persist across tests (only the graph is truncated), so any
    test that adds/edits/deletes a kind must take this fixture to keep the suite
    order-independent. Truncating nodes first lets the kind rows be deleted (the
    FK no longer has referencing rows), then the defaults are re-seeded.
    """
    yield
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE nodes CASCADE")
            cur.execute("DELETE FROM edge_kinds")
            cur.execute("DELETE FROM node_kinds")
        conn.commit()
        seed_kinds(conn)


@pytest.fixture(scope="session")
def client(auth_configured: None) -> TestClient:
    """An authenticated ``TestClient`` bound to the real API app.

    Carries the session cookie and a Bearer header — both signed with the seeded
    test key — so every endpoint test runs as an authenticated caller. The SPA is
    not mounted in tests (no ``NODUM_WEB_DIST``); SPA serving is covered in
    ``test_web.py`` against a fresh app with a temporary bundle.
    """
    test_client = TestClient(api.app)
    token = _session_token()
    test_client.cookies.set(auth.COOKIE_NAME, token)
    test_client.headers["Authorization"] = f"Bearer {token}"
    return test_client


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
