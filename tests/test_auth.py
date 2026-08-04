"""Credentials (Q13 R3): passwords, agent tokens, sessions, and grant admin."""

from __future__ import annotations

import hashlib
import json

import pytest
from helpers import OWNER_ACTOR, agent, owner

from nodum import auth, db, service
from nodum.migrations import GARDENER_AGENT_ID
from nodum.store import GrantNotPermitted

#: Cookie by session-row id, so a test can key the table and still present the
#: cookie the caller was handed (the row holds only the hash — review S9).
_cookies: dict[str, str] = {}


def _session_row_id(cookie: str) -> str:
    """The ``sessions.id`` for a cookie, remembering the cookie for later use."""
    row_id = hashlib.sha256(cookie.encode()).hexdigest()
    _cookies[row_id] = cookie
    return row_id


# ── Passwords (argon2id) ──────────────────────────────────────────────────────


def test_password_is_stored_as_an_argon2id_hash(fresh_db):
    """Never the password itself, and never a bare digest."""
    service.set_human_password("owner", "correct horse", principal=owner())
    stored = service.list_humans(principal=owner())
    assert [human.has_password for human in stored if human.id == "owner"] == [True]
    conn = db.connect()
    try:
        digest = conn.execute("SELECT credential_hash FROM humans WHERE id = 'owner'").fetchone()[0]
    finally:
        conn.close()
    assert digest.startswith("$argon2id$")
    assert "correct horse" not in digest


# ── HTTP login (name + password) ──────────────────────────────────────────────


def test_verify_login_resolves_the_name_to_the_account(fresh_db):
    """The login handle is the name; the principal carries the (random) id."""
    human = service.create_human("second", principal=owner())
    service.set_human_password(human.id, "second-pw", principal=owner())

    principal = auth.verify_login("second", "second-pw")

    assert principal.kind == "human"
    assert principal.id == human.id
    assert principal.actor_string == f"human:{human.id}"


@pytest.mark.parametrize(
    ("name", "password"),
    [
        ("owner", "wrong password"),  # right name, wrong password
        ("nobody", "owner-pw"),  # unknown name
    ],
)
def test_verify_login_failures_are_all_invalid_credentials(fresh_db, name, password):
    service.set_human_password("owner", "owner-pw", principal=owner())
    with pytest.raises(auth.InvalidCredentials):
        auth.verify_login(name, password)


def test_verify_login_refuses_a_passwordless_account(fresh_db):
    with pytest.raises(auth.InvalidCredentials):
        auth.verify_login("owner", "anything")


def test_verify_login_refuses_an_ambiguous_name(fresh_db):
    """Two accounts sharing a name can neither log in: which human would it be?"""
    human = service.create_human("owner", principal=owner())
    service.set_human_password("owner", "first-pw", principal=owner())
    service.set_human_password(human.id, "second-pw", principal=owner())

    for password in ("first-pw", "second-pw"):
        with pytest.raises(auth.InvalidCredentials):
            auth.verify_login("owner", password)


def _seed_login_failure(case: str) -> tuple[str, str]:
    """Set the database up for one ``verify_login`` failure path; return the attempt."""
    if case == "unknown name":
        return "nobody", "some-pw"
    if case == "passwordless":
        return "owner", "some-pw"
    if case == "ambiguous name":
        twin = service.create_human("owner", principal=owner())
        service.set_human_password("owner", "first-pw", principal=owner())
        service.set_human_password(twin.id, "second-pw", principal=owner())
        return "owner", "first-pw"
    if case == "wrong password":
        service.set_human_password("owner", "right-pw", principal=owner())
        return "owner", "wrong-pw"
    # A disabled account, with the right password: the verify still runs.
    service.create_human("second", principal=owner())  # the owner is not the last one
    service.set_human_password("owner", "owner-pw", principal=owner())
    service.disable_human("owner", principal=owner())
    return "owner", "owner-pw"


@pytest.mark.parametrize(
    "case",
    ["unknown name", "passwordless", "ambiguous name", "wrong password", "disabled account"],
)
def test_every_failed_login_pays_the_argon2_work_factor_exactly_once(fresh_db, monkeypatch, case):
    """Constant-time discipline: every failure must time like a success.

    Otherwise the response time discloses which login names exist — an
    account enumeration primitive against exactly the surface that asks for
    passwords. Asserted as "one verification ran", not as a wall-clock
    comparison, so the test is never flaky. All five failure paths are
    covered: the spy used to see two of them (review N6).
    """
    name, password = _seed_login_failure(case)
    verifications = 0
    real_hasher = auth._HASHER

    class SpyHasher:
        def hash(self, password: str) -> str:
            return real_hasher.hash(password)

        def verify(self, hash: str, password: str) -> bool:
            nonlocal verifications
            verifications += 1
            return real_hasher.verify(hash, password)

    monkeypatch.setattr(auth, "_HASHER", SpyHasher())

    with pytest.raises(auth.InvalidCredentials):
        auth.verify_login(name, password)
    assert verifications == 1


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
    service.create_human("second", principal=owner())  # the owner is not the last one
    service.disable_human("owner", principal=owner())
    with pytest.raises(auth.InvalidCredentials):
        auth.verify_agent_token(created.token)


def test_a_human_cannot_create_an_agent_under_the_reserved_builtin_prefix(fresh_db):
    """Not naming hygiene — the event log's ability to name one writer.

    `agents.id` is the name verbatim and every agent, internal or external,
    writes to the log as `agent:<id>`. Without the reservation a human could
    mint an *external* `builtin-gardener`, hand its token to anything, and no
    reader of `events.actor` could tell its writes from the gardener's.
    """
    with pytest.raises(ValueError, match="reserved"):
        service.create_agent("builtin-gardener", owner_human_id="owner", principal=owner())
    with pytest.raises(ValueError, match="reserved"):
        service.create_agent("builtin-anything", kind="internal", grants={}, principal=owner())
    # And nothing was written on the way to the refusal.
    assert [row.id for row in service.list_agents(principal=owner())] == [GARDENER_AGENT_ID]


# ── The internal agent: in-process, credential-less (design §8.4) ─────────────


def test_internal_principal_loads_the_gardener_with_its_grants(fresh_db):
    principal = auth.internal_principal()
    assert principal.kind == "internal"
    assert principal.actor_string == f"agent:{GARDENER_AGENT_ID}"
    # `read` on meta, `edit` on main — the shape every curating agent in this
    # suite holds. Consolidation only ever *reads* the type vocabulary; what
    # `edit` on meta bought was authority no job reaches (see
    # `tests/test_migrations.py`). Migration `0016` adds `edit` on its own
    # conventions space, as ordinary a row as the first two.
    assert principal.grants == {"meta": "read", "main": "edit", "conventions": "edit"}


def test_internal_principal_reads_grants_through_the_same_archived_space_filter(fresh_db):
    """A gardener grant is an ordinary grant: archiving the space makes it inert.

    `agent_principal` gets this from `_grant_set`; loading the internal agent
    any other way would have quietly exempted the one agent that writes most.
    """
    space = service.create_space("research", principal=owner())
    service.grant(GARDENER_AGENT_ID, space.id, "edit", principal=owner())
    assert space.id in auth.internal_principal().grants

    service.archive_space(space.id, principal=owner())
    assert space.id not in auth.internal_principal().grants
    # The row survives so a human can still see and revoke it.
    assert space.id in {g.space_id for g in service.list_grants(principal=owner())}


def test_a_disabled_gardener_is_refused_rather_than_loaded(fresh_db):
    """Disabling the agent is the supported way to stop the gardener.

    Nothing checks a token on this path — it authenticates by being in-process —
    so if the check did not live here it would live nowhere, and a disabled
    account would go on writing.
    """
    service.disable_agent(GARDENER_AGENT_ID, principal=owner())
    with pytest.raises(auth.PrincipalDisabled, match=GARDENER_AGENT_ID):
        auth.internal_principal()
    service.enable_agent(GARDENER_AGENT_ID, principal=owner())
    assert auth.internal_principal().id == GARDENER_AGENT_ID


def test_an_absent_internal_agent_is_a_refusal_not_an_empty_principal(tmp_path, monkeypatch):
    """An absent internal agent is a refusal, not an empty principal.

    The database is built stopped at 0015: removing the gardener's grants is
    part of the setup (the agent row cannot go while they reference it), and a
    file recording `0016` with the gardener's `conventions` grant gone is drift
    `_write_seam_problems` refuses at init — the consistency check would fire
    before this test's own refusal could.
    """
    path = tmp_path / "at0015.db"
    monkeypatch.setenv("NODUM_DB", str(path))
    monkeypatch.setattr(
        db,
        "MIGRATIONS",
        [entry for entry in db.MIGRATIONS if entry[0] != "0016_conventions_and_annotations"],
    )
    service.init()
    conn = db.connect()
    try:
        conn.execute("DELETE FROM grants WHERE agent_id = ?", (GARDENER_AGENT_ID,))
        conn.execute("DELETE FROM agents WHERE kind = 'internal'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(auth.UnknownPrincipal, match="0014"):
        auth.internal_principal()


def test_the_internal_agent_holds_no_credential_and_cannot_be_given_one(fresh_db):
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT credential_hash FROM agents WHERE id = ?", (GARDENER_AGENT_ID,)
        ).fetchone()
    finally:
        conn.close()
    assert row["credential_hash"] is None
    with pytest.raises(ValueError, match="internal"):
        service.rotate_agent_token(GARDENER_AGENT_ID, principal=owner())


# ── Sessions (server-side, 30-day sliding) ────────────────────────────────────


def test_the_session_table_never_holds_the_live_cookie(fresh_db):
    """S9: a database read leak must not hand out usable sessions."""
    cookie = auth.create_session("owner")
    conn = db.connect()
    try:
        stored = [row["id"] for row in conn.execute("SELECT id FROM sessions")]
    finally:
        conn.close()
    assert cookie not in stored
    assert stored == [hashlib.sha256(cookie.encode()).hexdigest()]


def test_login_sweeps_expired_sessions(fresh_db):
    """N7: expiry was only ever noticed when a dead cookie was presented."""
    stale = auth.create_session("owner")
    conn = db.connect()
    conn.execute("UPDATE sessions SET expires_at = datetime('now', '-1 day')")
    conn.commit()
    conn.close()

    auth.create_session("owner")

    conn = db.connect()
    try:
        remaining = conn.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"]
    finally:
        conn.close()
    assert remaining == 1
    with pytest.raises(auth.InvalidCredentials):
        auth.principal_for_session(stale)


def test_session_resolves_and_slides_expiry(fresh_db):
    session_id = _session_row_id(auth.create_session("owner"))
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
    assert auth.principal_for_session(_cookies[session_id]).actor_string == OWNER_ACTOR
    slid = conn.execute("SELECT expires_at FROM sessions WHERE id = ?", (session_id,)).fetchone()[
        "expires_at"
    ]
    assert slid > first or slid != conn.execute("SELECT datetime('now', '+1 day')").fetchone()[0]


def test_an_expired_session_is_dead_and_deleted(fresh_db):
    cookie = auth.create_session("owner")
    session_id = _session_row_id(cookie)
    conn = service.db.connect()
    conn.execute(
        "UPDATE sessions SET expires_at = datetime('now', '-1 day') WHERE id = ?",
        (session_id,),
    )
    conn.commit()
    with pytest.raises(auth.InvalidCredentials):
        auth.principal_for_session(cookie)
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
    second = service.create_human("second", principal=owner())
    cookie = auth.create_session(second.id)
    service.disable_human(second.id, principal=owner())
    with pytest.raises(auth.InvalidCredentials):
        auth.principal_for_session(cookie)


def test_changing_a_password_ends_that_humans_sessions(fresh_db):
    """S10: a password change is how a human answers a stolen cookie."""
    cookie = auth.create_session("owner")
    service.set_human_password("owner", "new password", principal=owner())
    with pytest.raises(auth.InvalidCredentials):
        auth.principal_for_session(cookie)


def test_a_password_under_the_floor_is_refused(fresh_db):
    """S11: the empty string was storable over both surfaces, and logged in."""
    for password in ("", "short"):
        with pytest.raises(ValueError, match="at least 6"):
            service.set_human_password("owner", password, principal=owner())


def test_the_last_enabled_human_cannot_disable_itself(fresh_db):
    """S13: no enabled human means no principal at all — not even from the CLI."""
    with pytest.raises(GrantNotPermitted, match="last enabled human"):
        service.disable_human("owner", principal=owner())

    second = service.create_human("second", principal=owner())
    service.disable_human("owner", principal=owner())  # a second one exists now
    with pytest.raises(GrantNotPermitted, match="last enabled human"):
        service.disable_human(second.id, principal=auth.owner_principal(second.id))


# ── Auth events and the login lockout (finding M5) ────────────────────────────


def test_record_auth_event_writes_the_three_auth_ops_with_derived_actors(fresh_db):
    """The actor comes from the op: the verified human, or nobody.

    A failure has no verified principal, so it records
    ``UNAUTHENTICATED_ACTOR`` and keeps the attempted name in the payload. It
    used to record the name itself, which made ``events.actor`` a field an
    unauthenticated caller wrote (finding M2).
    """
    service.record_auth_event("human.login", {"human_id": "owner"})
    service.record_auth_event(
        "human.login_failed", {"name": "owner", "reason": "invalid credentials"}
    )
    service.record_auth_event("human.logout", {"human_id": "owner"})

    recorded = [e for e in service.list_events(owner(), limit=10) if e.op.startswith("human.")]
    assert [(e.op, e.actor) for e in recorded] == [
        ("human.logout", "human:owner"),
        ("human.login_failed", service.UNAUTHENTICATED_ACTOR),
        ("human.login", "human:owner"),
    ]
    # The claimed name is evidence, not identity: it stays in the payload.
    (failed,) = [e for e in recorded if e.op == "human.login_failed"]
    assert failed.payload["name"] == "owner"


def test_record_auth_event_refuses_ops_and_payloads_outside_its_allowlist(fresh_db):
    """The allowlist is the point: no other op, no missing identity, no event."""
    with pytest.raises(ValueError, match="op must be one of"):
        service.record_auth_event("node.create", {"before": None, "after": {}})
    with pytest.raises(ValueError, match="human_id"):
        service.record_auth_event("human.login", {"name": "owner"})
    with pytest.raises(ValueError, match="name"):
        service.record_auth_event("human.login_failed", {"human_id": "owner"})
    assert service.list_events(owner(), limit=10) == []


def test_the_login_failure_count_is_per_name_and_window_bounded(fresh_db):
    """The audit trail is the state: the count reads the failure events."""
    service.record_auth_event(
        "human.login_failed", {"name": "owner", "reason": "invalid credentials"}
    )
    service.record_auth_event(
        "human.login_failed", {"name": "nobody", "reason": "invalid credentials"}
    )

    assert service.login_failure_count("owner") == 1
    assert service.login_failure_count("nobody") == 1
    assert service.login_failure_count("second") == 0  # a name no attempt claimed
    assert service.login_is_locked("owner") is False

    conn = db.connect()
    try:
        conn.execute(
            "UPDATE events SET created_at = datetime('now', '-30 minutes')"
            " WHERE op = 'human.login_failed'"
        )
        conn.commit()
    finally:
        conn.close()
    assert service.login_failure_count("owner") == 0  # the window slid past them


def test_a_successful_login_is_the_reset_boundary_for_the_failure_count(fresh_db):
    """Failures before the last success do not count: the lockout clears."""
    service.record_auth_event(
        "human.login_failed", {"name": "owner", "reason": "invalid credentials"}
    )
    service.record_auth_event(
        "human.login_failed", {"name": "owner", "reason": "invalid credentials"}
    )
    service.record_auth_event("human.login", {"human_id": "owner"})
    service.record_auth_event(
        "human.login_failed", {"name": "owner", "reason": "invalid credentials"}
    )

    assert service.login_failure_count("owner") == 1  # only the post-success miss
    assert service.login_is_locked("owner") is False


def test_login_is_locked_honours_the_module_constants(fresh_db, monkeypatch):
    """The rule is injectable: lower the threshold and two misses lock."""
    monkeypatch.setattr(service, "LOGIN_MAX_FAILED_ATTEMPTS", 2)
    service.record_auth_event(
        "human.login_failed", {"name": "owner", "reason": "invalid credentials"}
    )
    assert service.login_is_locked("owner") is False
    service.record_auth_event(
        "human.login_failed", {"name": "owner", "reason": "invalid credentials"}
    )
    assert service.login_is_locked("owner") is True


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
