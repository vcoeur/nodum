"""Service-level tests for the single-password auth module.

These mutate the ``auth_secret`` row directly, so each requests the
``restore_auth`` fixture to re-seed the canonical suite password afterwards.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from nodum import auth
from nodum.cli import app as cli_app
from nodum.db import connect


def _clear_auth() -> None:
    """Remove the single auth row, returning the install to the unconfigured state."""
    with connect() as conn:
        conn.cursor().execute("DELETE FROM auth_secret")
        conn.commit()


def test_status_unconfigured_then_configured(restore_auth: None) -> None:
    """status/is_configured flip from False to True once a password is set."""
    _clear_auth()
    assert auth.is_configured() is False
    assert auth.status().configured is False

    auth.set_password("hunter2")
    assert auth.is_configured() is True
    result = auth.status()
    assert result.configured is True
    assert result.updated_at is not None


def test_login_wrong_password_raises(restore_auth: None) -> None:
    """A wrong password raises BadPassword (not a ValueError, so it maps to 401)."""
    _clear_auth()
    auth.set_password("right-pass")
    with pytest.raises(auth.BadPassword):
        auth.login("wrong-pass")
    assert not isinstance(auth.BadPassword(), ValueError)


def test_login_returns_verifiable_token(restore_auth: None) -> None:
    """A token from a correct login verifies; a tampered token does not."""
    _clear_auth()
    auth.set_password("right-pass")
    token = auth.login("right-pass")
    assert auth.verify_token(token) is True
    assert auth.verify_token(token + "x") is False


def test_verify_token_unconfigured_raises(restore_auth: None) -> None:
    """Verifying a token before any password is set raises AuthNotConfigured."""
    _clear_auth()
    with pytest.raises(auth.AuthNotConfigured):
        auth.verify_token("anything")
    with pytest.raises(auth.AuthNotConfigured):
        auth.login("anything")


def test_signing_key_preserved_across_password_change(restore_auth: None) -> None:
    """Rotating the password keeps the signing key, so existing sessions stay valid."""
    _clear_auth()
    auth.set_password("first")
    token = auth.login("first")

    auth.set_password("second")  # password changes; signing key must persist
    assert auth.verify_token(token) is True  # the pre-rotation token still verifies
    with pytest.raises(auth.BadPassword):
        auth.login("first")
    assert auth.verify_token(auth.login("second")) is True


def test_empty_password_rejected(restore_auth: None) -> None:
    """set_password refuses an empty password."""
    _clear_auth()
    with pytest.raises(ValueError):
        auth.set_password("")


def test_cli_auth_set_password_and_status(restore_auth: None) -> None:
    """The CLI sets the password from piped stdin and reports status as JSON."""
    _clear_auth()
    runner = CliRunner()

    result = runner.invoke(cli_app, ["auth", "set-password"], input="cli-pass\n")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["configured"] is True

    status = runner.invoke(cli_app, ["auth", "status"])
    assert status.exit_code == 0
    assert json.loads(status.stdout)["configured"] is True

    assert auth.verify_token(auth.login("cli-pass")) is True
