"""Credentials (Q13 R3): passwords, agent tokens, sessions, and grant admin."""

from __future__ import annotations

import json

import pytest
from helpers import OWNER_ACTOR, agent, owner

from nodum import auth, service

# ── Passwords (argon2id) ──────────────────────────────────────────────────────


def test_password_set_and_verify(fresh_db):
    service.set_human_password("owner", "correct horse", principal=owner())
    assert auth.verify_password("owner", "correct horse").actor_string == OWNER_ACTOR


def test_wrong_password_is_invalid_credentials(fresh_db):
    service.set_human_password("owner", "right", principal=owner())
    with pytest.raises(auth.InvalidCredentials):
        auth.verify_password("owner", "wrong")


def test_passwordless_account_cannot_log_in(fresh_db):
    with pytest.raises(auth.InvalidCredentials):
        auth.verify_password("owner", "anything")


def test_disabled_human_cannot_log_in(fresh_db):
    service.set_human_password("owner", "secret", principal=owner())
    service.disable_human("owner", principal=owner())
    with pytest.raises(auth.InvalidCredentials):
        auth.verify_password("owner", "secret")


# ── Agent tokens (show-once, sha-256 at rest) ─────────────────────────────────


def test_create_agent_shows_the_token_once_and_stores_only_its_hash(fresh_db):
    created = service.create_agent("bot", owner_human_id="owner", principal=owner())
    assert created.token.startswith("ndm_")
    assert created.agent.has_token
    row = (
        service.db.connect()
        .execute("SELECT credential_hash FROM agents WHERE id = 'bot'")
        .fetchone()
    )
    assert row["credential_hash"] != created.token  # hash at rest, never the token


def test_token_verifies_to_the_agents_principal_with_its_grants(fresh_db):
    created = service.create_agent("bot", owner_human_id="owner", principal=owner())
    service.grant("bot", "main", "suggest", principal=owner())
    principal = auth.verify_agent_token(created.token)
    assert principal.actor_string == "agent:bot"
    assert principal.grants == {"meta": "read", "main": "suggest"}


def test_an_unknown_token_is_invalid(fresh_db):
    with pytest.raises(auth.InvalidCredentials):
        auth.verify_agent_token("ndm_not_a_real_token")


def test_rotation_kills_the_old_token(fresh_db):
    created = service.create_agent("bot", owner_human_id="owner", principal=owner())
    new_token = service.rotate_agent_token("bot", principal=owner())
    with pytest.raises(auth.InvalidCredentials):
        auth.verify_agent_token(created.token)
    assert auth.verify_agent_token(new_token).actor_string == "agent:bot"


def test_disabling_an_agent_kills_its_token_and_keeps_its_proposals(fresh_db):
    created = service.create_agent("bot", owner_human_id="owner", principal=owner())
    service.grant("bot", "main", "suggest", principal=owner())
    proposal = service.create_node(
        type="note", title="pending", principal=auth.verify_agent_token(created.token)
    )
    service.disable_agent("bot", principal=owner())
    with pytest.raises(auth.InvalidCredentials):
        auth.verify_agent_token(created.token)
    # In-flight proposals survive, still reviewable — attribution is immutable.
    queue = service.list_proposals(principal=owner())
    assert [p.id for p in queue] == [proposal.id]
    service.accept_proposals([proposal.id], principal=owner())


def test_disabling_the_owner_cascades_to_external_agents_tokens(fresh_db):
    created = service.create_agent("bot", owner_human_id="owner", principal=owner())
    service.disable_human("owner", principal=owner())
    with pytest.raises(auth.InvalidCredentials):
        auth.verify_agent_token(created.token)


# ── Sessions (server-side, 30-day sliding) ────────────────────────────────────


def test_session_resolves_and_slides_expiry(fresh_db):
    session_id = auth.create_session("owner")
    conn = service.db.connect()
    first = conn.execute("SELECT expires_at FROM sessions WHERE id = ?", (session_id,)).fetchone()[
        "expires_at"
    ]
    # Backdate the row so the slide is observable without sleeping.
    conn.execute(
        "UPDATE sessions SET expires_at = datetime('now', '+1 day') WHERE id = ?",
        (session_id,),
    )
    conn.commit()
    assert auth.principal_for_session(session_id).actor_string == OWNER_ACTOR
    slid = conn.execute("SELECT expires_at FROM sessions WHERE id = ?", (session_id,)).fetchone()[
        "expires_at"
    ]
    assert slid > first or slid != conn.execute("SELECT datetime('now', '+1 day')").fetchone()[0]


def test_an_expired_session_is_dead_and_deleted(fresh_db):
    session_id = auth.create_session("owner")
    conn = service.db.connect()
    conn.execute(
        "UPDATE sessions SET expires_at = datetime('now', '-1 day') WHERE id = ?",
        (session_id,),
    )
    conn.commit()
    with pytest.raises(auth.InvalidCredentials):
        auth.principal_for_session(session_id)
    assert (
        conn.execute("SELECT count(*) AS n FROM sessions WHERE id = ?", (session_id,)).fetchone()[
            "n"
        ]
        == 0
    )


def test_logout_deletes_the_session(fresh_db):
    session_id = auth.create_session("owner")
    auth.delete_session(session_id)
    with pytest.raises(auth.InvalidCredentials):
        auth.principal_for_session(session_id)


def test_disabling_a_human_deletes_its_sessions(fresh_db):
    session_id = auth.create_session("owner")
    service.disable_human("owner", principal=owner())
    with pytest.raises(auth.InvalidCredentials):
        auth.principal_for_session(session_id)


# ── Grant administration (human-only, event-logged) ───────────────────────────


def test_grant_and_revoke_are_event_logged(fresh_db):
    agent("bot")
    service.grant("bot", "main", "read", principal=owner())
    service.grant("bot", "main", "edit", principal=owner())  # re-level upserts
    service.revoke("bot", "main", principal=owner())
    ops = [e.op for e in service.list_events(owner(), limit=10) if e.op.startswith("grant.")]
    assert ops == ["grant.revoke", "grant.set", "grant.set"]
    # Credential hashes never enter a payload.
    for event in service.list_events(owner(), limit=50):
        assert "credential_hash" not in json.dumps(event.payload)


def test_grant_level_is_validated(fresh_db):
    agent("bot")
    with pytest.raises(ValueError, match="level"):
        service.grant("bot", "main", "admin", principal=owner())


def test_admin_is_human_only(fresh_db):
    suggest = agent("bot")
    for call in (
        lambda: service.grant("bot", "main", "read", principal=suggest),
        lambda: service.revoke("bot", "main", principal=suggest),
        lambda: service.create_human("mallory", principal=suggest),
        lambda: service.create_agent("bot2", owner_human_id="owner", principal=suggest),
        lambda: service.list_grants(principal=suggest),
        lambda: service.list_humans(principal=suggest),
        lambda: service.list_agents(principal=suggest),
    ):
        with pytest.raises(service.GrantNotPermitted):
            call()


def test_a_new_grant_takes_effect_immediately(fresh_db):
    created = service.create_agent("bot", owner_human_id="owner", principal=owner())
    principal = auth.verify_agent_token(created.token)
    with pytest.raises(service.GrantNotPermitted):
        service.create_node(type="note", title="no reach", principal=principal)
    service.grant("bot", "main", "suggest", principal=owner())
    node = service.create_node(
        type="note", title="reached", principal=auth.verify_agent_token(created.token)
    )
    assert node.state == "proposed"
