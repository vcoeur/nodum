"""Single-user authentication — one main password, set from the local CLI.

The install starts **locked**: the HTTP API and web view refuse protected
requests until a main password is set with ``nodum auth set-password``. The secret
lives in a single-row ``auth_secret`` table — an argon2 ``password_hash`` plus a
random ``signing_key``. Login (and only login) verifies the password against the
argon2 hash and mints a session token signed with the ``signing_key``; the web
carries it in an HttpOnly cookie and API clients in an ``Authorization: Bearer``
header. Every subsequent request verifies the cheap HMAC signature on that token,
so argon2's deliberate slowness never touches the per-request hot path.

This module is transport-agnostic (no FastAPI imports): the API/web adapters call
these functions and translate the outcomes into HTTP.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, URLSafeTimedSerializer

from nodum.db import connect, migrate_auth

# Session tokens are valid for this many seconds (7 days).
TOKEN_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
# Name of the HttpOnly session cookie the web view sets and reads.
COOKIE_NAME = "nodum_session"
# itsdangerous namespace salt — a domain separator, not a secret.
_TOKEN_SALT = "nodum.session"

_hasher = PasswordHasher()


class AuthNotConfigured(Exception):
    """Raised when an auth operation runs before a main password has been set."""

    def __init__(self) -> None:
        super().__init__("auth not configured — run `nodum auth set-password`")


class BadPassword(Exception):
    """Raised when a login attempt presents the wrong main password.

    Deliberately *not* a ``ValueError`` so the API's ValueError→422 handler does
    not catch it; the login route maps it to a 401 instead.
    """


@dataclass(frozen=True)
class AuthStatus:
    """Whether a main password is configured, and when it was last set."""

    configured: bool
    updated_at: datetime | None


def _fetch_secret(conn: psycopg.Connection) -> dict | None:
    """Return the single auth row, or ``None`` when unset or the table is absent.

    A never-initialised database has no ``auth_secret`` table; that is treated as
    "not configured" rather than an error, so ``nodum auth status`` works anywhere.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash, signing_key, updated_at FROM auth_secret WHERE id")
            return cur.fetchone()
    except psycopg.errors.UndefinedTable:
        conn.rollback()
        return None


def _serializer(signing_key: str) -> URLSafeTimedSerializer:
    """Build the timed token serializer bound to a signing key."""
    return URLSafeTimedSerializer(signing_key, salt=_TOKEN_SALT)


def set_password(password: str) -> AuthStatus:
    """Set (or replace) the main password; create the signing key on first use.

    Args:
        password: The new main password (must be non-empty).

    Returns:
        The resulting :class:`AuthStatus` (always ``configured=True``).

    The argon2 hash is recomputed every call; the ``signing_key`` is generated
    once and preserved across password changes, so rotating the password does not
    silently invalidate existing sessions.
    """
    if not password:
        raise ValueError("password must not be empty")
    password_hash = _hasher.hash(password)
    with connect() as conn:
        migrate_auth(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT signing_key FROM auth_secret WHERE id")
            row = cur.fetchone()
            signing_key = row["signing_key"] if row else secrets.token_urlsafe(32)
            cur.execute(
                "INSERT INTO auth_secret (id, password_hash, signing_key, updated_at) "
                "VALUES (true, %s, %s, now()) "
                "ON CONFLICT (id) DO UPDATE SET "
                "password_hash = EXCLUDED.password_hash, updated_at = now()",
                (password_hash, signing_key),
            )
        conn.commit()
    return status()


def status() -> AuthStatus:
    """Report whether a main password is configured and when it was last set."""
    with connect() as conn:
        row = _fetch_secret(conn)
    if row is None:
        return AuthStatus(configured=False, updated_at=None)
    return AuthStatus(configured=True, updated_at=row["updated_at"])


def is_configured() -> bool:
    """Return whether a main password has been set."""
    with connect() as conn:
        return _fetch_secret(conn) is not None


def login(password: str) -> str:
    """Verify the password and return a fresh signed session token.

    Args:
        password: The candidate main password.

    Returns:
        A signed, time-limited session token (opaque string).

    Raises:
        AuthNotConfigured: No main password has been set.
        BadPassword: The password does not match.
    """
    with connect() as conn:
        row = _fetch_secret(conn)
    if row is None:
        raise AuthNotConfigured
    try:
        _hasher.verify(row["password_hash"], password)
    except VerifyMismatchError as exc:
        raise BadPassword from exc
    return _serializer(row["signing_key"]).dumps({"v": 1})


def verify_token(token: str) -> bool:
    """Return whether a session token is currently valid (signature + not expired).

    Raises:
        AuthNotConfigured: No main password (hence no signing key) is set.
    """
    with connect() as conn:
        row = _fetch_secret(conn)
    if row is None:
        raise AuthNotConfigured
    try:
        _serializer(row["signing_key"]).loads(token, max_age=TOKEN_MAX_AGE_SECONDS)
    except BadSignature:  # covers SignatureExpired (a BadSignature subclass)
        return False
    return True
