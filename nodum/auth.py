"""Principal construction — the only place :class:`Principal` is minted (Q13 R1).

Three paths in, one per surface:

- **Trusted-local** (:func:`owner_principal`): the CLI and scripts. Local
  access is the trust boundary here, exactly as it was before accounts —
  the operator names their human with ``--as`` for attribution, and no
  password is asked on this path.
- **HTTP session** (surfaces task): a verified password login mints through
  the same loader, keyed by the session row.
- **MCP token** (surfaces task): a verified bearer token mints through
  :func:`agent_principal`.

Nothing else may construct a :class:`~nodum.principal.Principal`; identity
is never re-derived from a string deeper in the stack.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from nodum import db
from nodum.principal import Principal

#: The seeded first human account (migration ``0010``) — the bootstrap the
#: CLI defaults nothing to but most single-human files will name in ``--as``.
DEFAULT_HUMAN_ID = "owner"

#: The seeded owner's actor string, for tests and adapters that assert
#: attribution.
OWNER_ACTOR = f"human:{DEFAULT_HUMAN_ID}"


class UnknownPrincipal(LookupError):
    """Raised when a named human or agent account does not exist."""


class PrincipalDisabled(PermissionError):
    """Raised when a named account exists but is disabled."""


def _connect(path: str | Path | None) -> sqlite3.Connection:
    conn = db.connect(path)
    db.init_db(conn)
    return conn


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

    .. warning::

        INTERIM (Q13 surfaces task): this loads identity and grants **without
        verifying a credential** — the MCP surface currently authenticates
        nothing. Token verification lands in the surfaces task; until then
        this must not back any network surface's authentication decision.

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
        grants = {
            grant["space_id"]: grant["level"]
            for grant in conn.execute(
                "SELECT space_id, level FROM grants WHERE agent_id = ?", (agent_id,)
            ).fetchall()
        }
        return Principal(kind=row["kind"], id=agent_id, grants=grants)
    finally:
        conn.close()
