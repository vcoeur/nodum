"""The upgrade path: a real pre-0009 database taken all the way to 0011.

Everything else in the suite starts from a *fresh* migrated file, which
exercises 0009–0011 only as DDL that ran against empty tables. The blockers
the adversarial review found (agent ids seeded from ``policies.agent`` with
their ``agent:`` prefix intact; duplicate asset_ref hashes wedging the new
unique index) live exactly where no fresh-database test can look: in the
data-carrying half of the migrations. This file builds the old shape by
hand, upgrades it, and checks what came out the other side.
"""

from __future__ import annotations

import pytest
from helpers import owner

from nodum import db, service
from nodum.migrations import MIGRATIONS

PRE_0009_THROUGH = "0008_version_proposed_fields"

#: Old-shape rows: a custom type, nodes/edges carrying `graph_id`, a bare
#: `human` actor beside prefixed agent actors, a policies-only agent, an
#: `agent:` actor with no name at all, and two asset_ref nodes sharing a hash.
OLD_SHAPE_DATA = """
INSERT INTO types (id, name, parent_type_id, schema_json, is_builtin, created_at)
VALUES ('recipe', 'recipe', 'note', '{"required":["servings"]}', 0, '2026-01-01 00:00:00');

INSERT INTO nodes (id, graph_id, type_id, title, content, props, state, created_by, created_at)
VALUES
    ('n-human', 'main', 'note',   'By the human', 'see [[n-agent]]', '{}', 'active',
     'human', '2026-01-02 00:00:00'),
    ('n-agent', 'main', 'recipe', 'By an agent',  '', '{"servings":2}', 'proposed',
     'agent:cook', '2026-01-03 00:00:00'),
    ('n-asset-a', 'main', 'asset_ref', 'Scan A', '', '{"asset_hash":"deadbeef"}', 'active',
     'human', '2026-01-04 00:00:00'),
    ('n-asset-b', 'main', 'asset_ref', 'Scan B', '', '{"asset_hash":"deadbeef"}', 'proposed',
     'agent:cook', '2026-01-05 00:00:00'),
    ('n-asset-c', 'main', 'asset_ref', 'Scan C', '', '{"asset_hash":"cafe"}', 'active',
     'human', '2026-01-06 00:00:00');

INSERT INTO edges (id, graph_id, src_id, dst_id, type_id, props, created_by, state, created_at)
VALUES ('e-1', 'main', 'n-human', 'n-agent', 'mentions', '{}', 'human', 'active',
        '2026-01-07 00:00:00');

INSERT INTO versions (node_id, title, content, props, actor, event_seq, created_at)
VALUES ('n-human', 'By the human', 'see [[n-agent]]', '{}', 'human', 1, '2026-01-02 00:00:00');

INSERT INTO events (actor, op, payload) VALUES
    ('human', 'node.create', '{}'),
    ('agent:cook', 'node.propose', '{}'),
    ('agent:', 'node.propose', '{}'),
    ('system', 'projector.run', '{}');

INSERT INTO policies (agent, rules, updated_by)
VALUES ('agent:polconly', '[]', 'human');
"""


@pytest.fixture()
def upgraded(tmp_path, monkeypatch):
    """A database built at 0008 with old-shape data, then upgraded to the tip."""
    path = tmp_path / "legacy.db"
    monkeypatch.setenv("NODUM_DB", str(path))
    names = [name for name, _ in MIGRATIONS]
    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS[: names.index(PRE_0009_THROUGH) + 1])
    conn = db.connect(path)
    try:
        db.init_db(conn)
        conn.executescript(OLD_SHAPE_DATA)
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect(path)
    try:
        applied = db.init_db(conn)
        assert applied == names[names.index(PRE_0009_THROUGH) + 1 :]
    finally:
        conn.close()

    conn = db.connect(path)
    yield conn
    conn.close()


def _one(conn, sql, *params):
    return conn.execute(sql, params).fetchone()


def _column(conn, sql, *params):
    return [row[0] for row in conn.execute(sql, params).fetchall()]


def test_the_smallest_populated_database_upgrades_at_all(tmp_path, monkeypatch):
    """B5: 0009 rebuilds nodes, and a deferred FK counter made that fatal.

    Dropping a populated parent table counts every referencing row as an
    outstanding deferred violation; renaming the replacement in does not clear
    it, so COMMIT failed with a bare "FOREIGN KEY constraint failed" on any
    database holding a single node and its version row. Every test in the
    suite started from an empty file, where the counter is zero.
    """
    path = tmp_path / "tiny.db"
    monkeypatch.setenv("NODUM_DB", str(path))
    names = [name for name, _ in MIGRATIONS]
    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS[: names.index(PRE_0009_THROUGH) + 1])
    conn = db.connect(path)
    try:
        db.init_db(conn)
        conn.executescript(
            "INSERT INTO nodes (id, type_id, title, created_by)"
            " VALUES ('n1', 'note', 'x', 'human');"
            "INSERT INTO versions (node_id, title, content, props, actor, event_seq)"
            " VALUES ('n1', 'x', '', '{}', 'human', 1);"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect(path)
    try:
        assert db.init_db(conn)  # the tail applied, no IntegrityError
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_a_migration_leaving_dangling_references_is_refused(fresh_db):
    """The whole-database check replaces the deferred counter it removed."""
    conn = db.connect()
    try:
        with pytest.raises(Exception, match="dangling references in: edges"):
            db.apply_migration(
                conn,
                "0099_dangling",
                "INSERT INTO edges (id, src_id, dst_id, type_id, created_by)"
                " VALUES ('e-x', 'nope', 'nope', 'mentions', 'human:owner');",
            )
        assert "0099_dangling" not in db.applied_migrations(conn)
        assert conn.execute("SELECT count(*) AS n FROM edges").fetchone()["n"] == 0
    finally:
        conn.close()


# ── 0009: type conversion, spaces, the asset_ref dedupe ───────────────────────


def test_a_custom_type_becomes_a_type_node_keeping_its_id_and_schema(upgraded):
    row = _one(upgraded, "SELECT * FROM nodes WHERE id = 'recipe'")
    assert row["title"] == "recipe"
    assert row["type_id"] == "type"
    assert row["space_id"] == "meta"
    assert row["state"] == "active"
    props = _one(
        upgraded,
        "SELECT json_extract(props,'$.type_kind') AS kind,"
        " json_extract(props,'$.is_builtin') AS builtin,"
        " json_extract(props,'$.parent_type_id') AS parent,"
        " json_extract(props,'$.schema_json.required[0]') AS required"
        " FROM nodes WHERE id = 'recipe'",
    )
    assert (props["kind"], props["builtin"], props["parent"]) == ("node", 0, "note")
    assert props["required"] == "servings"
    # The node typed by it still resolves through the same id.
    assert _one(upgraded, "SELECT type_id FROM nodes WHERE id = 'n-agent'")["type_id"] == "recipe"


def test_edge_types_become_edge_kind_type_nodes(upgraded):
    row = _one(
        upgraded,
        "SELECT json_extract(props,'$.type_kind') AS kind,"
        " json_extract(props,'$.inverse_name') AS inverse, space_id"
        " FROM nodes WHERE id = 'supports'",
    )
    assert (row["kind"], row["inverse"], row["space_id"]) == ("edge", "supported_by", "meta")


def test_content_nodes_land_in_main_and_the_bootstrap_is_whole(upgraded):
    assert _one(upgraded, "SELECT space_id FROM nodes WHERE id = 'n-human'")["space_id"] == "main"
    bootstrap = set(
        _column(upgraded, "SELECT id FROM nodes WHERE id IN ('type','space','meta','main')")
    )
    assert bootstrap == {"type", "space", "meta", "main"}
    assert _one(upgraded, "SELECT space_id FROM nodes WHERE id = 'meta'")["space_id"] == "meta"


def test_duplicate_asset_ref_hashes_are_deduped_not_fatal(upgraded):
    """B4: the (hash, space) index cannot be created over pre-existing duplicates.

    The upgrade must survive them — it keeps the earliest describing node per
    hash and archives the rest, rather than failing with a bare IntegrityError
    on a database nothing but hand SQL could then move forward.
    """
    states = dict(
        upgraded.execute(
            "SELECT id, state FROM nodes WHERE id IN ('n-asset-a','n-asset-b','n-asset-c')"
        ).fetchall()
    )
    assert states == {"n-asset-a": "active", "n-asset-b": "archived", "n-asset-c": "active"}


def test_the_deduped_hash_is_still_guarded_afterwards(upgraded):
    with pytest.raises(Exception, match="UNIQUE"):
        upgraded.execute(
            "INSERT INTO nodes (id, space_id, type_id, title, props, created_by)"
            " VALUES ('n-asset-d', 'main', 'asset_ref', 'Scan D',"
            " '{\"asset_hash\":\"deadbeef\"}', 'human:owner')"
        )


# ── 0010: agent seeding, parity grants, the policy layer's death ──────────────


def test_every_agent_identity_is_seeded_once_under_its_bare_name(upgraded):
    """B3: `policies.agent` held the full actor string, every other source the name.

    A row seeded as `agent:polconly` is unusable — token verification looks up
    the bare id, and minting a principal for it would attribute writes to
    `agent:agent:polconly`.
    """
    # Scoped to the external agents, which is what 0010 seeds from the log's
    # actors; 0014's internal gardener is not one of them.
    assert sorted(
        _column(upgraded, "SELECT id FROM agents WHERE kind = 'external' ORDER BY id")
    ) == ["cook", "polconly"]
    assert _column(upgraded, "SELECT id FROM agents WHERE id LIKE 'agent:%'") == []
    # A bare `agent:` actor names nobody and must seed nothing.
    assert _column(upgraded, "SELECT id FROM agents WHERE length(id) = 0") == []


def test_seeded_agents_are_external_owned_and_unauthenticated(upgraded):
    row = _one(upgraded, "SELECT * FROM agents WHERE id = 'cook'")
    assert (row["kind"], row["name"], row["owner_human_id"]) == ("external", "cook", "owner")
    assert row["credential_hash"] is None
    assert row["disabled"] == 0


def test_parity_grants_preserve_every_agents_reach(upgraded):
    for agent_id in ("cook", "polconly"):
        grants = dict(
            upgraded.execute(
                "SELECT space_id, level FROM grants WHERE agent_id = ?", (agent_id,)
            ).fetchall()
        )
        assert grants == {"meta": "read", "main": "suggest"}


def test_an_actor_string_under_the_reserved_prefix_stops_the_upgrade(tmp_path, monkeypatch):
    """0010 invents accounts from the log, and 0014's guard has to see them.

    Nothing before 0014 reserved the `builtin-` prefix, so an old log naming
    `agent:builtin-librarian` back-fills a live external agent under it — one
    `nodum agent token-rotate` gives a working token. Checking only the single
    id `builtin-gardener` let that database upgrade clean; the prefix check is
    what closes the route, and this is the route it was found on.
    """
    path = tmp_path / "reserved-actor.db"
    monkeypatch.setenv("NODUM_DB", str(path))
    names = [name for name, _ in MIGRATIONS]
    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS[: names.index(PRE_0009_THROUGH) + 1])
    conn = db.connect(path)
    try:
        db.init_db(conn)
        conn.execute(
            "INSERT INTO events (actor, op, payload)"
            " VALUES ('agent:builtin-librarian', 'node.propose', '{}')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect(path)
    try:
        with pytest.raises(Exception, match="builtin-") as refusal:
            db.init_db(conn)
        assert "reserved" in str(refusal.value)
        applied = db.applied_migrations(conn)
        # 0010 still ran — it is what created the row — and 0014 is what stopped.
        assert "0010_principals" in applied
        assert "0014_cycles_and_gardener" not in applied
    finally:
        conn.close()


def test_the_first_human_is_seeded_passwordless_and_policies_are_gone(upgraded):
    human = _one(upgraded, "SELECT * FROM humans WHERE id = 'owner'")
    assert human["name"] == "owner"
    assert human["credential_hash"] is None
    tables = {row["name"] for row in upgraded.execute("SELECT name FROM sqlite_master")}
    assert "policies" not in tables


def test_an_external_agent_without_an_owner_is_refused_by_the_schema(upgraded):
    """N4: `owner_human_id` NOT-NULL-for-external was a comment, not a constraint."""
    with pytest.raises(Exception, match="CHECK"):
        upgraded.execute(
            "INSERT INTO agents (id, kind, name) VALUES ('ownerless', 'external', 'ownerless')"
        )


# ── 0011: actor strings ───────────────────────────────────────────────────────


def test_bare_human_actors_become_structured_references(upgraded):
    assert _column(upgraded, "SELECT actor FROM events WHERE actor LIKE 'human%'") == [
        "human:owner"
    ]
    assert _column(upgraded, "SELECT actor FROM versions") == ["human:owner"]
    assert _one(upgraded, "SELECT created_by FROM nodes WHERE id = 'n-human'")[0] == "human:owner"
    assert _one(upgraded, "SELECT created_by FROM edges WHERE id = 'e-1'")[0] == "human:owner"
    # Agent strings were already structured; system actors are left alone.
    assert _one(upgraded, "SELECT created_by FROM nodes WHERE id = 'n-agent'")[0] == "agent:cook"
    assert "system" in _column(upgraded, "SELECT actor FROM events")


# ── The whole upgraded database is sound ──────────────────────────────────────


def test_the_upgraded_database_has_no_dangling_references(upgraded):
    assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []


def test_the_upgraded_database_serves_reads_and_writes(upgraded):
    upgraded.close()
    assert service.get_node("n-human", principal=owner()).title == "By the human"
    created = service.create_node(type="recipe", title="After the upgrade", principal=owner())
    assert created.state == "active"
