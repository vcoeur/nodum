"""Migrations from scratch: schema shape, seed catalog, atomicity, idempotency."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from helpers import owner

from nodum import assets, auth, db, service
from nodum.migrations import (
    GARDENER_AGENT_ID,
    MIGRATIONS,
    SEED_EDGE_TYPES,
    SEED_NODE_TYPES,
)

CORE_TABLES = {
    "nodes",
    "edges",
    "versions",
    "events",
    "merge_redirects",
    "schema_migrations",
}

#: The type catalogs became type-nodes in 0009 (Q13) — the tables must be gone.
DROPPED_TABLES = {"types", "edge_types"}


def test_no_test_can_reach_the_developers_own_database(monkeypatch):
    """The suite migrates whatever database it resolves, so an unset `NODUM_DB`
    is not a harmless fallback — it is an unreleased migration applied to a
    live graph. That happened during the phase-4 build: `monkeypatch.undo()`
    undoes the `NODUM_DB` patch `fresh_db` made along with everything else, and
    three asset tests then asserted against `~/.local/share/nodum/nodum.db`.
    `conftest._never_the_real_database` removes the reachable path entirely;
    this is the assertion that it stays removed.
    """
    monkeypatch.delenv(db.ENV_DB_VAR, raising=False)

    resolved = db.db_path()

    assert resolved != Path("~/.local/share/nodum/nodum.db").expanduser()
    assert not resolved.is_relative_to(Path.home() / ".local")


def test_init_drops_the_type_catalog_tables(fresh_db):
    conn = db.connect()
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        assert DROPPED_TABLES.isdisjoint({row["name"] for row in rows})
    finally:
        conn.close()


def test_init_applies_all_migrations(fresh_db):
    result = service.init()
    assert result.applied == []
    assert result.already_applied == [name for name, _ in MIGRATIONS]


def test_init_creates_core_tables(fresh_db):
    conn = db.connect()
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        assert {row["name"] for row in rows} >= CORE_TABLES
    finally:
        conn.close()


def test_init_seeds_builtin_node_types(fresh_db):
    catalog = service.list_types(principal=owner())
    names = {node_type.name for node_type in catalog.node_types}
    # 0009 adds the metaclass root and the space type to the seed vocabulary.
    assert names == set(SEED_NODE_TYPES) | {"type", "space"}
    assert all(node_type.is_builtin for node_type in catalog.node_types)
    # Built-in type ids equal their names.
    assert all(node_type.id == node_type.name for node_type in catalog.node_types)


def test_init_seeds_builtin_edge_types_with_inverses(fresh_db):
    catalog = service.list_types(principal=owner())
    by_name = {edge_type.name: edge_type for edge_type in catalog.edge_types}
    assert set(by_name) == {name for name, _ in SEED_EDGE_TYPES}
    for name, inverse in SEED_EDGE_TYPES:
        assert by_name[name].inverse_name == inverse
    # Inverse pairs are symmetric: the inverse of the inverse is the original.
    for name, edge_type in by_name.items():
        assert by_name[edge_type.inverse_name].inverse_name == name


def test_new_nodes_land_in_the_main_space(fresh_db):
    """graph_id became space_id on nodes only (0009); edges carry no space."""
    node = service.create_node(type="note", title="n1", principal=owner())
    assert node.space_id == "main"
    edge_target = service.create_node(type="note", title="n2", principal=owner())
    edge = service.create_edge(node.id, edge_target.id, "relates_to", principal=owner())
    assert not hasattr(edge, "space_id")


def test_wal_mode_enabled(fresh_db):
    conn = db.connect()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        conn.close()


# ── Asset storage is introduced once, already in the database ────────────────


def _prefix_through(name):
    """The migration list truncated after ``name`` (an interrupted upgrade)."""
    names = [entry[0] for entry in MIGRATIONS]
    return MIGRATIONS[: names.index(name) + 1]


def test_assets_are_storable_the_moment_the_asset_tables_exist(monkeypatch, tmp_path):
    """No migration may leave assets registerable but their bytes homeless.

    A schema where `assets` exists without in-database byte storage is what
    stranded 0007-era assets: bytes written elsewhere, then a later migration
    that never carried them over. Registering at that exact point must work.
    """
    monkeypatch.setenv("NODUM_DB", str(tmp_path / "partial.db"))
    monkeypatch.setattr(db, "MIGRATIONS", _prefix_through("0007_assets_and_renditions"))
    service.init()

    source = tmp_path / "payload.bin"
    source.write_bytes(b"asset bytes")
    asset = assets.register_asset(source)

    conn = db.connect()
    try:
        stored = conn.execute(
            "SELECT data FROM asset_blobs WHERE hash = ?", (asset.hash,)
        ).fetchone()["data"]
    finally:
        conn.close()
    assert stored == b"asset bytes"


def test_no_migration_moves_stored_bytes_between_tables(fresh_db):
    """Asset bytes have exactly one home, so nothing has to be copied later."""
    scripts = "\n".join(sql for _, sql in MIGRATIONS).upper()
    # The byte tables specifically are never rebuilt or dropped (the 0009
    # spaces migration legitimately rebuilds nodes/edges and drops the type
    # catalogs — assets are untouched by it).
    for table in ("ASSETS", "ASSET_BLOBS", "RENDITIONS"):
        assert f"DROP TABLE {table}" not in scripts

    conn = db.connect()
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(renditions)")}
        assert "data" in columns
        # A filesystem path column is what made a row and its bytes able to
        # disagree; renditions and originals are both bytes-in-the-row now.
        assert "path" not in columns
        assert "path" not in {row["name"] for row in conn.execute("PRAGMA table_info(assets)")}
    finally:
        conn.close()


#: The pre-consolidation, path-based ``0007`` schema: asset bytes lived on the
#: filesystem (``renditions.path``, no ``asset_blobs``). A dev DB built from an
#: intermediate branch commit carries this under the ``0007`` migration name.
STALE_PATH_BASED_0007 = """
CREATE TABLE assets (
    hash           TEXT PRIMARY KEY,
    mime           TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    original_name  TEXT,
    extracted_text TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE renditions (
    id          TEXT PRIMARY KEY,
    asset_hash  TEXT NOT NULL REFERENCES assets(hash),
    profile     TEXT NOT NULL,
    path        TEXT NOT NULL,
    width       INTEGER NOT NULL,
    height      INTEGER NOT NULL,
    size_bytes  INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def test_init_refuses_a_database_carrying_the_stale_path_based_0007(tmp_path, monkeypatch):
    """A dev DB that applied the since-consolidated 0007 must fail loudly at init.

    `init_db` keys on migration NAME, so a DB carrying the old path-based
    `0007_assets_and_renditions` (renditions.path, no asset_blobs) has the name
    recorded, skips 0007, applies only the tail migration, and would otherwise
    leave a schema that breaks `register_asset` at runtime with `no such table:
    asset_blobs`. init must detect and refuse it instead of auto-migrating.
    """
    path = tmp_path / "stale.db"
    monkeypatch.setenv("NODUM_DB", str(path))

    # Reconstruct the stale DB: core through vectors applied normally, then the
    # old path-based assets tables recorded by hand under the 0007 name.
    monkeypatch.setattr(db, "MIGRATIONS", _prefix_through("0006_vectors"))
    conn = db.connect(path)
    try:
        db.init_db(conn)
        conn.executescript(STALE_PATH_BASED_0007)
        conn.execute("INSERT INTO schema_migrations (name) VALUES ('0007_assets_and_renditions')")
        conn.commit()
    finally:
        conn.close()

    # Now init with the full migration list: 0007 is skipped (name recorded),
    # only the tail applies, and the consistency check must fire loudly.
    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect(path)
    try:
        with pytest.raises(db.SchemaConsistencyError, match="asset_blobs"):
            db.init_db(conn)
    finally:
        conn.close()


# ── 0012: the capability-token table ─────────────────────────────────────────


def test_the_migration_list_is_ordered_and_numbered_without_gaps():
    """Append-only means the tail is the only place a new entry may land."""
    names = [name for name, _ in MIGRATIONS]
    assert names == sorted(names)
    assert [name.split("_")[0] for name in names] == [
        f"{number:04d}" for number in range(1, len(names) + 1)
    ]
    assert "0012_url_tokens" in names


def test_url_tokens_exists_on_a_database_built_from_scratch(fresh_db):
    conn = db.connect()
    try:
        assert "0012_url_tokens" in db.applied_migrations(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(url_tokens)")}
        assert columns == {
            "id",
            "token_hash",
            "kind",
            "asset_hash",
            "original_name",
            "mime",
            "max_bytes",
            "space_id",
            "created_by",
            "expires_at",
            "used_at",
            "created_at",
        }
    finally:
        conn.close()


def test_0012_applies_to_a_populated_database_already_at_0011(tmp_path, monkeypatch):
    """The upgrade path, not just the fresh-file one: 0011 is where users are."""
    monkeypatch.setenv("NODUM_DB", str(tmp_path / "at0011.db"))
    monkeypatch.setattr(db, "MIGRATIONS", _prefix_through("0011_actor_strings"))
    service.init()
    node = service.create_node(type="note", title="before the upgrade", principal=owner())

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        assert db.init_db(conn) == [
            "0012_url_tokens",
            "0013_unique_space_titles",
            "0014_cycles_and_gardener",
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute(
            "INSERT INTO url_tokens (id, token_hash, kind, asset_hash, created_by, expires_at)"
            " VALUES ('t1', 'h1', 'download', 'deadbeef', 'human:owner',"
            " datetime('now', '+300 seconds'))"
        )
        conn.commit()
    finally:
        conn.close()
    # And the graph it was applied over is untouched.
    assert service.get_node(node.id, principal=owner()).title == "before the upgrade"


@pytest.mark.parametrize(
    ("columns", "values", "match"),
    [
        (
            "id, token_hash, kind, created_by, expires_at",
            "'t', 'h', 'sideways', 'human:owner', datetime('now')",
            "CHECK",
        ),
        (
            "id, token_hash, kind, created_by, expires_at",
            "'t', 'h', 'download', 'human:owner', datetime('now')",
            "CHECK",
        ),
        (
            "id, token_hash, kind, created_by, expires_at",
            "'t', 'h', 'upload', 'human:owner', datetime('now')",
            "CHECK",
        ),
    ],
    ids=["unknown kind", "download without a target", "upload without a ceiling"],
)
def test_a_token_row_that_grants_nothing_in_particular_is_refused(fresh_db, columns, values, match):
    """Discovering it at redemption means discovering it with a stranger waiting."""
    conn = db.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match=match):
            conn.execute(f"INSERT INTO url_tokens ({columns}) VALUES ({values})")
    finally:
        conn.close()


def test_one_token_hash_cannot_be_shared_by_two_rows(fresh_db):
    """Redemption keys on the hash; two rows behind one secret is ambiguity."""
    conn = db.connect()
    try:
        for token_id in ("t1", "t2"):
            insert = (
                "INSERT INTO url_tokens (id, token_hash, kind, asset_hash, created_by, expires_at)"
                f" VALUES ('{token_id}', 'same-hash', 'download', 'deadbeef', 'human:owner',"
                " datetime('now', '+300 seconds'))"
            )
            if token_id == "t1":
                conn.execute(insert)
            else:
                with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
                    conn.execute(insert)
        conn.commit()
    finally:
        conn.close()


# ── 0013: one space per title, in any state ──────────────────────────────────


def test_0013_applies_to_a_populated_database_holding_duplicate_space_titles(tmp_path, monkeypatch):
    """The failure mode 0009 was bitten by: an index that cannot be created.

    Nothing enforced this before 0013, so a real database can hold two spaces
    called `research` — and `CREATE UNIQUE INDEX` over them fails with a bare
    IntegrityError, rolling the whole upgrade back with no way forward but hand
    SQL. The migration dedupes first, and it **renames** rather than archives:
    archiving retires a space from the vocabulary permanently, which a
    duplicate title does not deserve.

    Archived duplicates are deduped too, because the index covers them: a title
    is reserved by every space that ever carried it. The tie-break is what that
    makes load-bearing — a live row must keep the name even when a **non-active**
    row is older, or the upgrade would silently change what `--space research`
    resolves to. Both non-active states are seeded older than the winner here,
    because a tie-break that only demoted `archived` let an older *proposed* row
    take the name off a live space and passed a fixture that seeded it last.
    """
    monkeypatch.setenv("NODUM_DB", str(tmp_path / "at0012.db"))
    monkeypatch.setattr(db, "MIGRATIONS", _prefix_through("0012_url_tokens"))
    service.init()
    conn = db.connect()
    try:
        conn.executescript(
            "INSERT INTO nodes (id, space_id, type_id, title, state, created_by, created_at)"
            " VALUES"
            # The tie-break cases: both non-active duplicates are *older* than
            # the live rows they share a title with, so a tie-break that ranked
            # either of them level with `active` would hand them the name.
            "  ('sp-third',  'meta', 'space', 'research', 'proposed', 'agent:x',     '2025-11-01'),"
            "  ('sp-gone',   'meta', 'space', 'research', 'archived', 'human:owner', '2025-11-02'),"
            "  ('sp-first',  'meta', 'space', 'research', 'active',   'human:owner', '2026-01-01'),"
            "  ('sp-second', 'meta', 'space', 'research', 'active',   'human:owner', '2026-01-02'),"
            "  ('sp-retired','meta', 'space', 'reading',  'archived', 'human:owner', '2025-12-01'),"
            "  ('sp-live',   'meta', 'space', 'reading',  'active',   'human:owner', '2026-01-05');"
            "INSERT INTO nodes (id, space_id, type_id, title, created_by)"
            " VALUES ('n-in-second', 'sp-second', 'note', 'lives in the loser', 'human:owner');"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        assert db.init_db(conn) == ["0013_unique_space_titles", "0014_cycles_and_gardener"]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        titles = dict(
            conn.execute("SELECT id, title FROM nodes WHERE id LIKE 'sp-%' ORDER BY id").fetchall()
        )
    finally:
        conn.close()

    # The earliest live space keeps the name; the later ones are renamed to
    # something unique and self-explaining, and stay perfectly usable.
    assert titles["sp-first"] == "research"
    assert titles["sp-second"] == "research (sp-second)"
    # Neither non-active row takes the name off a live space, though both are
    # older than it. A `proposed` duplicate is the one the tie-break used to
    # miss — it sorted level with `active` and won on `created_at`.
    assert titles["sp-third"] == "research (sp-third)"
    # An archived duplicate is renamed like any other: its title is inside the
    # index now, so leaving it would be leaving the index uncreatable.
    assert titles["sp-gone"] == "research (sp-gone)"
    # And when the archived row is the older one, it still loses: the name goes
    # on resolving to the space it resolved to before the upgrade.
    assert titles["sp-live"] == "reading"
    assert titles["sp-retired"] == "reading (sp-retired)"

    # The graph the index was created over still reads and still writes.
    assert service.get_node("n-in-second", principal=owner()).space_id == "sp-second"
    assert service.resolve_space_id("research", principal=owner()) == "sp-first"
    assert service.resolve_space_id("reading", principal=owner()) == "sp-live"
    assert (
        service.create_node(
            type="note", title="after", space="research (sp-second)", principal=owner()
        ).space_id
        == "sp-second"
    )
    # And accepting the proposal cannot move the name either — the harm the
    # tie-break used to do surfaced only once the proposed row went active.
    service.transition("sp-third", "accept", principal=owner())
    assert service.resolve_space_id("research", principal=owner()) == "sp-first"


def test_0013_finds_a_free_name_when_the_deduping_rename_would_itself_collide(
    tmp_path, monkeypatch
):
    """`<title> (<id>)` is unique among losers, but not against what is there.

    A database holding two spaces called `research` plus one literally titled
    `research (sp-b)` made the dedupe generate a name the index then refused —
    an `IntegrityError` that rolled the whole upgrade back, which is the exact
    0009 failure mode this migration exists to prevent. So the free name is
    searched, not assumed.
    """
    monkeypatch.setenv("NODUM_DB", str(tmp_path / "at0012.db"))
    monkeypatch.setattr(db, "MIGRATIONS", _prefix_through("0012_url_tokens"))
    service.init()
    conn = db.connect()
    try:
        conn.executescript(
            "INSERT INTO nodes (id, space_id, type_id, title, state, created_by, created_at)"
            " VALUES"
            "  ('sp-a','meta','space','research',        'active','human:owner','2026-01-01'),"
            "  ('sp-b','meta','space','research',        'active','human:owner','2026-01-02'),"
            "  ('sp-c','meta','space','research (sp-b)', 'active','human:owner','2026-01-03'),"
            "  ('sp-d','meta','space','research (sp-b) 1','active','human:owner','2026-01-04');"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        assert db.init_db(conn) == ["0013_unique_space_titles", "0014_cycles_and_gardener"]
        titles = dict(
            conn.execute("SELECT id, title FROM nodes WHERE id LIKE 'sp-%' ORDER BY id").fetchall()
        )
    finally:
        conn.close()

    # The base and the first suffix are both taken, so the search walks past
    # them rather than generating a name the index refuses.
    assert titles["sp-a"] == "research"
    assert titles["sp-b"] == "research (sp-b) 2"
    assert titles["sp-c"] == "research (sp-b)"
    assert titles["sp-d"] == "research (sp-b) 1"
    assert service.resolve_space_id("research", principal=owner()) == "sp-a"
    assert service.resolve_space_id("research (sp-b) 2", principal=owner()) == "sp-b"


def test_0013_deduplicates_a_title_that_is_another_spaces_id(tmp_path, monkeypatch):
    """The ambiguity no index can express, and the one the service refuses to create.

    `_resolve_space` matches `id = ? OR title = ?`, so a space *titled* `sp-x`
    while another is *identified* `sp-x` is exactly as ambiguous as two equal
    titles — `service._require_space_name_free` refuses to create it for that
    reason. The migration used to apply cleanly straight over it, leaving
    `--space sp-x` plan-dependent forever and invisibly. The row holding the
    title loses: an id is immutable and is the reference of last resort.
    """
    monkeypatch.setenv("NODUM_DB", str(tmp_path / "at0012.db"))
    monkeypatch.setattr(db, "MIGRATIONS", _prefix_through("0012_url_tokens"))
    service.init()
    conn = db.connect()
    try:
        conn.executescript(
            "INSERT INTO nodes (id, space_id, type_id, title, state, created_by, created_at)"
            " VALUES"
            "  ('sp-x', 'meta', 'space', 'research', 'active', 'human:owner', '2026-01-01'),"
            "  ('sp-y', 'meta', 'space', 'sp-x',     'active', 'human:owner', '2026-01-02');"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        assert db.init_db(conn) == ["0013_unique_space_titles", "0014_cycles_and_gardener"]
        titles = dict(
            conn.execute("SELECT id, title FROM nodes WHERE id LIKE 'sp-%' ORDER BY id").fetchall()
        )
        answering = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM nodes WHERE type_id = 'space' AND (id = 'sp-x' OR title = 'sp-x')"
            )
        ]
    finally:
        conn.close()

    assert titles["sp-x"] == "research"
    assert titles["sp-y"] == "sp-x (sp-y)"
    # Exactly one row answers to `sp-x` now, so resolution is not plan-dependent.
    assert answering == ["sp-x"]
    assert service.resolve_space_id("sp-x", principal=owner()) == "sp-x"


def test_0013_guards_the_titles_it_deduped(fresh_db):
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO nodes (id, space_id, type_id, title, created_by)"
            " VALUES ('sp-a', 'meta', 'space', 'research', 'human:owner')"
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                "INSERT INTO nodes (id, space_id, type_id, title, created_by)"
                " VALUES ('sp-b', 'meta', 'space', 'research', 'human:owner')"
            )
        # An archived one is inside the index too: a space title is reserved for
        # good, so that undoing an archive can never land on a taken name.
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                "INSERT INTO nodes (id, space_id, type_id, title, state, created_by)"
                " VALUES ('sp-c', 'meta', 'space', 'research', 'archived', 'human:owner')"
            )
        # And an untitled space is not one of a kind — NULLs are not in the index.
        for space_id in ("sp-d", "sp-e"):
            conn.execute(
                "INSERT INTO nodes (id, space_id, type_id, created_by)"
                f" VALUES ('{space_id}', 'meta', 'space', 'human:owner')"
            )
        conn.commit()
    finally:
        conn.close()


def test_0013_lets_a_space_change_state_without_touching_the_index(fresh_db):
    """No state predicate means no state change can collide.

    Archiving and restoring were the collision the old `state != 'archived'`
    index created: membership moved with the row's state, so a title could be
    freed and re-taken under a row that later came back. Now membership is
    fixed at insert, and the state column is free to move.
    """
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO nodes (id, space_id, type_id, title, created_by)"
            " VALUES ('sp-a', 'meta', 'space', 'research', 'human:owner')"
        )
        conn.execute("UPDATE nodes SET state = 'archived' WHERE id = 'sp-a'")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                "INSERT INTO nodes (id, space_id, type_id, title, created_by)"
                " VALUES ('sp-b', 'meta', 'space', 'research', 'human:owner')"
            )
        conn.execute("UPDATE nodes SET state = 'active' WHERE id = 'sp-a'")
        conn.commit()
    finally:
        conn.close()


# ── 0014: consolidation cycles and the gardener ──────────────────────────────


def test_cycles_exists_on_a_database_built_from_scratch(fresh_db):
    conn = db.connect()
    try:
        assert "0014_cycles_and_gardener" in db.applied_migrations(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(cycles)")}
        assert columns == {
            "id",
            "trigger",
            "triggered_by",
            "scope",
            "dry_run",
            "status",
            "report",
            "started_at",
            "finished_at",
            "rolled_back_by",
        }
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(cycles)")}
        assert "idx_cycles_started" in indexes
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("columns", "values"),
    [
        ("id, trigger, triggered_by, status", "'c', 'sideways', 'human:owner', 'running'"),
        ("id, trigger, triggered_by, status", "'c', 'manual', 'human:owner', 'sideways'"),
    ],
    ids=["unknown trigger", "unknown status"],
)
def test_a_cycle_row_outside_the_vocabulary_is_refused(fresh_db, columns, values):
    """The journal's two enums are the schema's, so no writer can invent a fifth."""
    conn = db.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(f"INSERT INTO cycles ({columns}) VALUES ({values})")
    finally:
        conn.close()


def test_0014_seeds_the_gardener_with_edit_on_meta_and_main(fresh_db):
    """D7 is auto-apply by default, and a gardener with no grant does nothing.

    The grants are ordinary rows on purpose: they list beside every other
    agent's on `/spaces` and `nodum revoke` takes them away. There is no
    gardener-shaped exception in the grant model.
    """
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM agents WHERE kind = 'internal'").fetchone()
        assert row["id"] == GARDENER_AGENT_ID
        assert row["name"] == GARDENER_AGENT_ID
        assert row["owner_human_id"] is None
        # An internal agent authenticates by being in-process; there is nothing
        # to present and nothing to steal.
        assert row["credential_hash"] is None
        assert row["disabled"] == 0
        levels = dict(
            conn.execute(
                "SELECT space_id, level FROM grants WHERE agent_id = ?", (GARDENER_AGENT_ID,)
            ).fetchall()
        )
        assert levels == {"meta": "edit", "main": "edit"}
    finally:
        conn.close()


def test_0014_applies_to_a_populated_database_already_at_0013(tmp_path, monkeypatch):
    """The upgrade path, not just the fresh-file one: 0013 is where users are."""
    monkeypatch.setenv("NODUM_DB", str(tmp_path / "at0013.db"))
    monkeypatch.setattr(db, "MIGRATIONS", _prefix_through("0013_unique_space_titles"))
    service.init()
    node = service.create_node(type="note", title="before the upgrade", principal=owner())
    space = service.create_space("research", principal=owner())

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        assert db.init_db(conn) == ["0014_cycles_and_gardener"]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute(
            "INSERT INTO cycles (id, trigger, triggered_by, scope, status)"
            " VALUES ('c1', 'scheduled', 'scheduler', ?, 'running')",
            (space.id,),
        )
        conn.commit()
    finally:
        conn.close()
    # The graph it was applied over is untouched, and the gardener it seeded is
    # a usable principal on it.
    assert service.get_node(node.id, principal=owner()).title == "before the upgrade"
    gardener = auth.internal_principal()
    assert gardener.id == GARDENER_AGENT_ID


def test_0014_refuses_to_upgrade_a_database_whose_reserved_id_is_taken(tmp_path, monkeypatch):
    """Stealing the id, or renaming the impostor, would both corrupt an identity.

    `create_agent` sets `agent_id = name` verbatim and only reserves the prefix
    from this migration onward, so a database written before it can already hold
    a user's agent called `builtin-gardener` — with `agent:builtin-gardener` in
    `events.actor`, `versions.actor` and both `created_by` columns behind it.
    Taking the id attributes that history to the gardener; renaming the account
    detaches it from the history that names it, because actor strings are log
    entries and not references anything follows. So the migration stops.
    """
    monkeypatch.setenv("NODUM_DB", str(tmp_path / "impostor.db"))
    monkeypatch.setattr(db, "MIGRATIONS", _prefix_through("0013_unique_space_titles"))
    service.init()
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO agents (id, kind, name, owner_human_id)"
            " VALUES ('builtin-gardener', 'external', 'builtin-gardener', 'owner')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="is reserved"):
            db.init_db(conn)
        # Refused, not half-done: no cycles table, no migration row, and the
        # impostor's own account is exactly as it was.
        assert "0014_cycles_and_gardener" not in db.applied_migrations(conn)
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'cycles'"
            ).fetchone()
            is None
        )
        assert (
            conn.execute("SELECT kind FROM agents WHERE id = 'builtin-gardener'").fetchone()["kind"]
            == "external"
        )
    finally:
        conn.close()


# ── Atomicity: a migration and its schema_migrations row commit together ─────


HALF_APPLIED = (
    "0099_half_applied",
    """
    CREATE TABLE first_half (id TEXT PRIMARY KEY);
    CREATE TABLE first_half (id TEXT PRIMARY KEY);
    """,
)


def test_a_migration_that_fails_midway_leaves_no_trace(fresh_db, monkeypatch):
    """An interrupted migration rolls back whole — otherwise it can never retry.

    Applying the script outside a transaction is what made an interruption
    fatal: the statements that already ran stayed, the `schema_migrations`
    row did not, and every later run re-ran the script and died on "table …
    already exists".
    """
    monkeypatch.setattr(db, "MIGRATIONS", [*MIGRATIONS, HALF_APPLIED])
    with pytest.raises(sqlite3.OperationalError):
        service.init()

    conn = db.connect()
    try:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert "first_half" not in tables
        assert HALF_APPLIED[0] not in db.applied_migrations(conn)
    finally:
        conn.close()


def test_a_failed_migration_can_be_retried_after_a_fix(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "MIGRATIONS", [*MIGRATIONS, HALF_APPLIED])
    with pytest.raises(sqlite3.OperationalError):
        service.init()

    fixed = (HALF_APPLIED[0], "CREATE TABLE first_half (id TEXT PRIMARY KEY);")
    monkeypatch.setattr(db, "MIGRATIONS", [*MIGRATIONS, fixed])
    assert service.init().applied == [fixed[0]]
    # And the graph is usable again, not wedged on a half-applied schema.
    assert service.create_node(
        type="note",
        title="after the fix",
        principal=owner(),
    )


def test_migration_names_are_checked_before_being_inlined(fresh_db):
    conn = db.connect()
    try:
        before = conn.execute("SELECT count(*) AS n FROM nodes").fetchone()["n"]
        with pytest.raises(ValueError, match="invalid migration name"):
            db.apply_migration(conn, "0099'); DROP TABLE nodes; --", "SELECT 1;")
        assert conn.execute("SELECT count(*) AS n FROM nodes").fetchone()["n"] == before
    finally:
        conn.close()


def test_migration_name_with_a_trailing_newline_is_refused(fresh_db):
    """The name check is fully anchored: a trailing newline is not a valid name.

    `re.match` with a `$`-anchored pattern accepts `"0007_x\\n"` (the `$` sits
    before the newline), which would inline a name carrying a newline into the
    migration script. `fullmatch` refuses it.
    """
    conn = db.connect()
    try:
        with pytest.raises(ValueError, match="invalid migration name"):
            db.apply_migration(conn, "0099_trailing_newline\n", "SELECT 1;")
    finally:
        conn.close()
