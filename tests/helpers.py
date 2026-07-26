"""Shared test helpers for the Q13 principal/grant model.

Write functions in the service layer take a :class:`Principal`, not an actor
string. These builders mint them the way tests need: ``owner()`` for the
seeded first human, ``agent()`` to idempotently seed an agent account plus
grants and load its principal. Attribution assertions keep working unchanged
because agent ids are the old actor names (``agent:x`` means ``agents.id``
``x``).
"""

from __future__ import annotations

from nodum import auth, db
from nodum.principal import Principal

#: The seeded owner's actor string (assertions on created_by / events.actor).
OWNER_ACTOR = auth.OWNER_ACTOR


def owner() -> Principal:
    """The owner human principal (requires a migrated database)."""
    return auth.owner_principal()


def agent(
    name: str = "test-agent",
    *,
    grants: dict[str, str] | None = None,
    kind: str = "external",
    token: str | None = None,
) -> Principal:
    """Seed (idempotently) an agent account plus grants; return its Principal.

    Accepts the id with or without the ``agent:`` prefix. Default grants are
    the migration's parity set: read meta, suggest main. With ``token``, the
    account's credential hash is set to that token's sha-256, so the MCP
    path can verify it.
    """
    import hashlib

    agent_id = name.removeprefix("agent:")
    grants = {"meta": "read", "main": "suggest"} if grants is None else grants
    conn = db.connect()
    db.init_db(conn)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, kind, name, owner_human_id)"
            " VALUES (?, ?, ?, 'owner')",
            (agent_id, kind, agent_id),
        )
        if token is not None:
            conn.execute(
                "UPDATE agents SET credential_hash = ? WHERE id = ?",
                (hashlib.sha256(token.encode()).hexdigest(), agent_id),
            )
        for space_id, level in grants.items():
            conn.execute(
                "INSERT OR REPLACE INTO grants (agent_id, space_id, level) VALUES (?, ?, ?)",
                (agent_id, space_id, level),
            )
        conn.commit()
    finally:
        conn.close()
    return auth.agent_principal(agent_id)


def seed_space(space_id: str, *, title: str | None = None) -> str:
    """Seed (idempotently) a space node in meta; return its id."""

    conn = db.connect()
    db.init_db(conn)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO nodes (id, space_id, type_id, title, props, state, created_by)"
            " VALUES (?, 'meta', 'space', ?, '{}', 'active', 'human:owner')",
            (space_id, title or space_id),
        )
        conn.commit()
    finally:
        conn.close()
    return space_id
