"""Migrations from scratch: schema shape, seed catalog, atomicity, idempotency."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from helpers import owner

from nodum import assets, db, service
from nodum.migrations import MIGRATIONS, SEED_EDGE_TYPES, SEED_NODE_TYPES

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
        assert db.init_db(conn) == ["0012_url_tokens"]
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
