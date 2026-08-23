"""Principal construction and credential verification (Q13 R1/R3).

:class:`~nodum.principal.Principal` objects are minted **only** here:

- **Trusted-local** (:func:`owner_principal`): the CLI and scripts. Local
  access is the trust boundary, exactly as before accounts — the operator
  names their human with ``--as`` for attribution, and no password is asked
  on this path.
- **Password login** (:func:`verify_login` → :func:`create_session` →
  :func:`principal_for_session`): the HTTP surface's path, argon2id-hashed
  passwords and server-side session rows (30-day sliding expiry). Session
  cookies are stored as their sha-256, like agent tokens — the cookie is a
  live credential, so the table must not hold a usable copy of one.
- **Agent token** (:func:`verify_agent_token`): the MCP surface's path.
  Tokens are generated show-once; only their sha-256 is stored (sha-256 is
  enough for high-entropy tokens — argon2's work factor buys nothing against
  a random 256-bit secret, and a database read leak must not hand out live
  credentials either way).

Nothing else may construct a :class:`~nodum.principal.Principal`; identity
is never re-derived from a string deeper in the stack.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from nodum import db
from nodum.principal import Principal

#: The seeded first human account (migration ``0010``) — the bootstrap the
#: CLI defaults nothing to but most single-human files will name in ``--as``.
DEFAULT_HUMAN_ID = "owner"

#: The seeded owner's actor string, for tests and adapters that assert
#: attribution.
OWNER_ACTOR = f"human:{DEFAULT_HUMAN_ID}"

#: Agent token format: ``ndm_`` + 32 url-safe random bytes (~256 bits).
TOKEN_PREFIX = "ndm_"

#: Session lifetime, sliding — every verified use pushes expiry out again.
SESSION_DAYS = 30

_HASHER = PasswordHasher()  # argon2id, library-default parameters


class UnknownPrincipal(LookupError):
    """Raised when a named human or agent account does not exist."""


class PrincipalDisabled(PermissionError):
    """Raised when a named account exists but is disabled."""


class InvalidCredentials(PermissionError):
    """Raised when a password, token, or session does not verify."""


class LoginLocked(PermissionError):
    """Raised when a login name is refused for too many failed attempts.

    The HTTP surface raises this instead of running another argon2
    verification: a name under lockout is refused up front (429), which is
    both the honest answer and the point of the lockout — an attacker's
    attempts stop costing a password check, and the account stops being
    reachable by one. The refusal is about the *attempt* (too many of them),
    never about the account: a name that does not exist locks exactly like a
    real one, so the lockout is not an existence oracle.
    """


def _connect(path: str | Path | None) -> sqlite3.Connection:
    conn = db.connect(path)
    db.init_db(conn)
    return conn


def _grant_set(conn: sqlite3.Connection, agent_id: str) -> dict[str, str]:
    """One agent's live grants as ``{space_id: level}`` — archived spaces excluded.

    **A grant confers authority only while its space resolves.** Archiving a
    space is how a human cuts every agent off it — that is what the CLI, the
    service and the archive dialog all promise — and the grant rows are
    deliberately *kept* rather than deleted so the human can still see and
    revoke them, and so undoing the archive restores exactly the delegation
    that was there before. Inert, not destroyed.

    Filtering here rather than at each check is what makes it total: every
    downstream rule reads the principal's grant set, so ``level_on``,
    ``read_spaces``, both scope clauses, the landing states and the review gate
    all inherit it with no call-site sweep to get wrong. Before this, archiving
    left an agent full live authority over every node already in the space
    (reachable by id, since only the *reference* stopped resolving) while
    ``list_spaces`` stopped showing the space or its grants — authority that was
    hidden and, because ``revoke`` resolved active spaces only, unrevokable.

    Only a demonstrably archived space is dropped: a grant naming a space with
    no node row at all behaves exactly as it did, since nothing here claims to
    know what that means.

    Args:
        conn: Open connection.
        agent_id: The agent whose grants to load.

    Returns:
        Space id → level name, for every grant whose space is not archived.
    """
    return {
        row["space_id"]: row["level"]
        for row in conn.execute(
            "SELECT g.space_id, g.level FROM grants g WHERE g.agent_id = ?"
            " AND NOT EXISTS (SELECT 1 FROM nodes n WHERE n.id = g.space_id"
            " AND n.type_id = 'space' AND n.state = 'archived')",
            (agent_id,),
        ).fetchall()
    }


def principal_from_actor(actor: str, *, path: str | Path | None = None) -> Principal:
    """Re-mint the principal an actor string names — **from stored state only**.

    A capability URL carries no ambient credential: whoever holds the token
    presents nothing else. So when an upload is redeemed and the bytes have to
    be ingested as somebody, the only truthful answer is the principal who
    *authorised* the capability, recorded on the token row's ``created_by`` at
    mint time — by which point they had already authenticated.

    **The argument must come from a database column, never from a request.**
    An actor string taken off the wire and passed here would be an identity
    supplied by the caller, which is the one thing every surface in this system
    refuses. It stays in :mod:`nodum.auth` for that reason: principals are
    minted here and nowhere else, so this rule has exactly one place to live
    and one place to be audited. The HTTP adapter never calls it (an AST
    property in ``tests/test_http_api.py`` keeps it that way) — the domain does,
    on a row it just read.

    Args:
        actor: A stored actor string — ``human:<id>`` or ``agent:<id>``.
        path: Explicit database path.

    Returns:
        The principal, with an agent's grant set loaded.

    Raises:
        UnknownPrincipal: If the string is malformed or names no account.
        PrincipalDisabled: If the account is disabled — revocation applies
            here exactly as it does at the front door, so a capability minted
            before an agent was disabled cannot outlive it.
    """
    kind, separator, account_id = actor.partition(":")
    if not separator or not account_id:
        raise UnknownPrincipal(f"not an actor string: {actor!r}")
    if kind == "human":
        return owner_principal(account_id, path=path)
    if kind == "agent":
        return agent_principal(account_id, path=path)
    raise UnknownPrincipal(f"unknown actor kind: {kind!r}")


def owner_principal(
    human_id: str = DEFAULT_HUMAN_ID, *, path: str | Path | None = None
) -> Principal:
    """Mint a human principal — the trusted-local path (no credential check).

    The human must exist and not be disabled. Humans hold no grants; the
    principal is unfiltered by construction.

    Raises:
        UnknownPrincipal: If the account does not exist.
        PrincipalDisabled: If the account is disabled.
    """
    conn = _connect(path)
    try:
        row = conn.execute("SELECT disabled FROM humans WHERE id = ?", (human_id,)).fetchone()
        if row is None:
            raise UnknownPrincipal(f"unknown human account: {human_id}")
        if row["disabled"]:
            raise PrincipalDisabled(f"human account is disabled: {human_id}")
        return Principal(kind="human", id=human_id)
    finally:
        conn.close()


def agent_principal(agent_id: str, *, path: str | Path | None = None) -> Principal:
    """Load an agent principal with its grant set.

    This is the **loader**, not a verification: it answers "what may this
    agent do?" once identity is established. Callers that authenticate (MCP)
    must go through :func:`verify_agent_token` instead; only the
    trusted-local path and tests call this directly.

    Raises:
        UnknownPrincipal: If the account does not exist.
        PrincipalDisabled: If the account is disabled.
    """
    conn = _connect(path)
    try:
        row = conn.execute("SELECT kind, disabled FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            raise UnknownPrincipal(f"unknown agent account: {agent_id}")
        if row["disabled"]:
            raise PrincipalDisabled(f"agent account is disabled: {agent_id}")
        return Principal(kind=row["kind"], id=agent_id, grants=_grant_set(conn, agent_id))
    finally:
        conn.close()


def internal_principal(*, path: str | Path | None = None) -> Principal:
    """Load the internal agent's principal — the in-process trusted path (§8.4).

    The internal agent is the gardener, seeded by migration ``0014``. It is the
    one principal that authenticates **by being in-process**: it holds no
    credential at all (``agents.credential_hash`` is NULL, and
    :func:`nodum.service.rotate_agent_token` refuses to mint one for an internal
    agent), so there is nothing to present and nothing to steal. What makes it a
    principal and not a bypass is that everything downstream is unchanged: it
    carries an ordinary grant set loaded the same way
    :func:`agent_principal` loads one — :func:`_grant_set`, archived spaces and
    all — so archiving a space cuts the gardener off exactly as it cuts off any
    other agent, and ``nodum revoke`` reaches its grants like any other agent's.

    There is deliberately no ``agent_id`` argument: an internal identity that a
    caller could name is an identity a caller could choose, and the design has
    exactly one internal agent. A second one is a schema change, and it should
    have to be.

    Args:
        path: Explicit database path.

    Returns:
        The internal agent's principal, with its grant set.

    Raises:
        UnknownPrincipal: If no internal agent exists (a database that predates
            ``0014``, or one whose row was removed), or if more than one does.
        PrincipalDisabled: If the internal agent is disabled — the supported way
            to stop the gardener, and it must bite here rather than leave a
            disabled account writing because nobody checked a token for it.
    """
    conn = _connect(path)
    try:
        rows = conn.execute("SELECT id, disabled FROM agents WHERE kind = 'internal'").fetchall()
        if not rows:
            raise UnknownPrincipal(
                "no internal agent account exists: migration '0014_cycles_and_gardener' seeds it"
            )
        if len(rows) > 1:
            raise UnknownPrincipal(
                "more than one internal agent account exists "
                f"({', '.join(sorted(row['id'] for row in rows))}): "
                "the in-process path names none, so it cannot choose between them"
            )
        row = rows[0]
        if row["disabled"]:
            raise PrincipalDisabled(f"agent account is disabled: {row['id']}")
        return Principal(kind="internal", id=row["id"], grants=_grant_set(conn, row["id"]))
    finally:
        conn.close()


# ── Passwords (argon2id) ──────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """Hash a password for storage (argon2id, library-default parameters)."""
    return _HASHER.hash(password)


def set_password(human_id: str, password: str, *, path: str | Path | None = None) -> None:
    """Set or change a human's password.

    Raises:
        UnknownPrincipal: If the account does not exist.
    """
    conn = _connect(path)
    try:
        cursor = conn.execute(
            "UPDATE humans SET credential_hash = ? WHERE id = ?",
            (hash_password(password), human_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise UnknownPrincipal(f"unknown human account: {human_id}")
    finally:
        conn.close()


#: Throwaway hash for the constant-time login path — computed lazily on the
#: first failed lookup, so importing this module never pays the work factor.
_DUMMY_HASH: str | None = None


def _dummy_verify(password: str) -> None:
    """Spend the argon2 work factor against a throwaway hash, discarding the result."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = _HASHER.hash("nodum constant-time dummy")
    try:
        _HASHER.verify(_DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError):
        pass


def verify_login(name: str, password: str, *, path: str | Path | None = None) -> Principal:
    """Verify a login name + password and mint the human's principal (HTTP login).

    The login handle is the account *name*: ids of CLI-created humans are
    random, and nobody types one at a login prompt. A name shared by two
    accounts resolves to no one — refusing keeps "which human is behind this
    session?" unambiguous. That used to be a live hazard rather than a
    formality: ``human create`` would store a second ``owner`` and take that
    name's login away for good. The lookup is single-valued at the source now —
    :func:`nodum.service.create_human` refuses a taken name, and migration
    ``0019_unique_human_names`` puts a unique index under the refusal — and what
    is below is what stands behind both.

    Timing discipline: an unknown, ambiguous, or passwordless name runs the
    same argon2 verification against a dummy hash, so the failure path costs
    what the success path costs and response time discloses nothing about
    which names exist.

    Raises:
        InvalidCredentials: If the name matches no single enabled account, or
            the password does not match.
    """
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT id, credential_hash, disabled FROM humans WHERE name = ?", (name,)
        ).fetchall()
        # `!= 1` rather than `== 0`: since 0019 a name matches at most one row —
        # every connection here runs the migrations — so in practice this is the
        # unknown-name refusal, and the ambiguous half is belt and braces. It is
        # kept as a *refusal* rather than relaxed to a `LIMIT 1` because the one
        # file that can still reach it is one whose unique index was dropped by
        # hand, and there the cheap reading picks a row: it would hand somebody
        # a session on an account they did not present a password for.
        if len(rows) != 1 or rows[0]["credential_hash"] is None:
            _dummy_verify(password)
            raise InvalidCredentials("invalid credentials")
        row = rows[0]
        try:
            _HASHER.verify(row["credential_hash"], password)
        except (VerifyMismatchError, VerificationError):
            raise InvalidCredentials("invalid credentials") from None
        if row["disabled"]:
            raise InvalidCredentials("invalid credentials")
        return Principal(kind="human", id=row["id"])
    finally:
        conn.close()


def human_login_name(human_id: str, *, path: str | Path | None = None) -> str:
    """The login name of a human id — the handle the lockout and argon2 key on.

    Sessions store the account's *id*; password verification keys on its
    *name*. The step-up check on an already-authenticated surface needs the
    bridge between the two, and this is it: a database read by id, never a
    name taken from a request.

    Args:
        human_id: The account's id.
        path: Explicit database path.

    Returns:
        The account's unique login name.

    Raises:
        UnknownPrincipal: If no human by that id exists.
    """
    conn = _connect(path)
    try:
        row = conn.execute("SELECT name FROM humans WHERE id = ?", (human_id,)).fetchone()
        if row is None or row["name"] is None:
            raise UnknownPrincipal(f"unknown human account: {human_id}")
        return row["name"]
    finally:
        conn.close()


# ── Agent tokens (show-once, sha-256 stored) ──────────────────────────────────


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> tuple[str, str]:
    """Generate ``(token, hash)``: shown once, only the hash is stored."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return token, _token_hash(token)


def store_token(agent_id: str, token_hash: str, *, path: str | Path | None = None) -> None:
    """Store a token hash as an agent's current credential (rotation: replace).

    Raises:
        UnknownPrincipal: If the account does not exist.
    """
    conn = _connect(path)
    try:
        cursor = conn.execute(
            "UPDATE agents SET credential_hash = ? WHERE id = ?", (token_hash, agent_id)
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise UnknownPrincipal(f"unknown agent account: {agent_id}")
    finally:
        conn.close()


def verify_agent_token(token: str, *, path: str | Path | None = None) -> Principal:
    """Verify an agent token and mint the agent's principal (the MCP path).

    Revocation is verification-time (R3): a disabled agent's token is dead,
    and a disabled owning human cascades — the external agents' tokens die
    with it. In-flight proposals are untouched rows in the queue.

    Raises:
        InvalidCredentials: If the token matches no enabled agent whose
            owner (for external agents) is also enabled.
    """
    conn = _connect(path)
    try:
        row = conn.execute(
            """
            SELECT a.id, a.kind, a.disabled AS agent_disabled,
                   h.disabled AS owner_disabled
            FROM agents a LEFT JOIN humans h ON h.id = a.owner_human_id
            WHERE a.credential_hash = ?
            """,
            (_token_hash(token),),
        ).fetchone()
        if row is None or row["agent_disabled"] or row["owner_disabled"]:
            raise InvalidCredentials("invalid credentials")
        return Principal(kind=row["kind"], id=row["id"], grants=_grant_set(conn, row["id"]))
    finally:
        conn.close()


# ── Sessions (server-side, 30-day sliding) ────────────────────────────────────


def _session_hash(cookie: str) -> str:
    """What the ``sessions`` table stores: the cookie's sha-256, never the cookie."""
    return hashlib.sha256(cookie.encode()).hexdigest()


def create_session(human_id: str, *, path: str | Path | None = None) -> str:
    """Create a session row for a (password-verified) human; return the cookie.

    Only the cookie's sha-256 is stored, for the same reason agent tokens are
    stored hashed: a database read leak must not hand out live credentials
    (Q13 review S9). The value returned here is the only copy — it goes to
    the browser and is never recoverable from the file.

    Expired rows are swept on the way in: expiry is otherwise only noticed
    when a dead cookie is presented, so a session nobody comes back for was
    never deleted at all (review N7).
    """
    cookie = secrets.token_urlsafe(32)
    conn = _connect(path)
    try:
        conn.execute("DELETE FROM sessions WHERE expires_at <= datetime('now')")
        conn.execute(
            "INSERT INTO sessions (id, human_id, expires_at) VALUES (?, ?, datetime('now', ?))",
            (_session_hash(cookie), human_id, f"+{SESSION_DAYS} days"),
        )
        conn.commit()
        return cookie
    finally:
        conn.close()


def principal_for_session(cookie: str, *, path: str | Path | None = None) -> Principal:
    """Resolve a session cookie to a principal, sliding the expiry forward.

    Raises:
        InvalidCredentials: If the session is unknown, expired, or its human
            is disabled.
    """
    session_id = _session_hash(cookie)
    conn = _connect(path)
    try:
        row = conn.execute(
            """
            SELECT s.id, s.human_id, s.expires_at, h.disabled
            FROM sessions s JOIN humans h ON h.id = s.human_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None or row["disabled"]:
            raise InvalidCredentials("invalid session")
        expired = conn.execute(
            "SELECT ? <= datetime('now') AS expired", (row["expires_at"],)
        ).fetchone()["expired"]
        if expired:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
            raise InvalidCredentials("invalid session")
        conn.execute(
            "UPDATE sessions SET expires_at = datetime('now', ?) WHERE id = ?",
            (f"+{SESSION_DAYS} days", session_id),
        )
        conn.commit()
        return Principal(kind="human", id=row["human_id"])
    finally:
        conn.close()


def delete_session(cookie: str, *, path: str | Path | None = None) -> None:
    """Log out: drop the session row (idempotent)."""
    conn = _connect(path)
    try:
        conn.execute("DELETE FROM sessions WHERE id = ?", (_session_hash(cookie),))
        conn.commit()
    finally:
        conn.close()
