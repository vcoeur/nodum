"""Migrations from scratch: schema shape, seed catalog, atomicity, idempotency."""

from __future__ import annotations

import io
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
from nodum.principal import EDIT

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
            "0015_cycle_stop_switch",
            "0016_conventions_and_annotations",
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
        assert db.init_db(conn) == [
            "0013_unique_space_titles",
            "0014_cycles_and_gardener",
            "0015_cycle_stop_switch",
            "0016_conventions_and_annotations",
        ]
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
        assert db.init_db(conn) == [
            "0013_unique_space_titles",
            "0014_cycles_and_gardener",
            "0015_cycle_stop_switch",
            "0016_conventions_and_annotations",
        ]
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
        assert db.init_db(conn) == [
            "0013_unique_space_titles",
            "0014_cycles_and_gardener",
            "0015_cycle_stop_switch",
            "0016_conventions_and_annotations",
        ]
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
        # What `0014` puts on the table, as a subset: later migrations add
        # columns to it (`0015` adds the stop switch's two), and the exact-set
        # assertion belongs to whichever migration is at the tail — otherwise
        # every append rewrites an assertion about a migration it did not touch.
        assert columns >= {
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


def _insert_cycle(conn, cycle_id, trigger, status="running"):
    conn.execute(
        "INSERT INTO cycles (id, trigger, triggered_by, status) VALUES (?, ?, 'human:owner', ?)",
        (cycle_id, trigger, status),
    )


def test_only_one_consolidation_cycle_may_be_running_in_the_whole_file(fresh_db):
    """The cross-process lock, and it is the row rather than anything in Python.

    A module-level lock serialises the runner inside **one** process, which
    covers the HTTP route, the nightly task and an in-process caller — and
    covers a `nodum consolidate` in a second process not at all. Both runs then
    completed and every duplicate pair was proposed twice, so the human's review
    queue doubled from one click and one command.

    The `cycles` table already *is* the cross-process state: a `running`
    consolidation row means one is running, whoever opened it. A partial unique
    index makes the second opener lose atomically, rather than after a read that
    was true when it was made.
    """
    conn = db.connect()
    try:
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(cycles)")}
        assert "idx_cycles_one_running_consolidation" in indexes

        _insert_cycle(conn, "first", "scheduled")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            _insert_cycle(conn, "second", "manual")

        # Closing the first frees it: the index is over `running` rows only.
        conn.execute("UPDATE cycles SET status = 'completed' WHERE id = 'first'")
        _insert_cycle(conn, "second", "manual")
    finally:
        conn.close()


@pytest.mark.parametrize("trigger", ["curative", "rollback"])
def test_a_curative_or_rollback_cycle_still_runs_beside_a_consolidation(fresh_db, trigger):
    """The guard is scoped to consolidation triggers, and that scoping is the point.

    A curative cycle is one human-driven operation and a rollback is the human's
    undo; both are short, and blocking either for the length of a nightly sweep
    would take the curative tier offline every night. Only the two triggers the
    runner opens — `manual` and `scheduled` — are serialised against each other.
    """
    conn = db.connect()
    try:
        _insert_cycle(conn, "sweep", "scheduled")
        _insert_cycle(conn, "beside-it", trigger)
        _insert_cycle(conn, "and-another", trigger)
    finally:
        conn.close()


def test_a_database_recorded_at_0014_without_the_lock_index_is_refused(tmp_path, monkeypatch):
    """0014 was amended in place while unreleased, so a dev file can be stale.

    `init_db` skips a migration whose name is already recorded, so a database
    built from the first cut of `0014` keeps the name and loses the index — and
    the cross-process guard is then absent with nothing saying so. That is the
    exact drift `_verify_schema_consistency` exists for, and 0014 has a
    checkable guarantee like the four migrations already listed there.
    """
    path = tmp_path / "stale.db"
    monkeypatch.setenv(db.ENV_DB_VAR, str(path))
    service.init()
    conn = db.connect()
    try:
        conn.execute("DROP INDEX idx_cycles_one_running_consolidation")
        conn.commit()
        with pytest.raises(db.SchemaConsistencyError, match="0014_cycles_and_gardener"):
            db.init_db(conn)
    finally:
        conn.close()


def test_the_missing_index_is_refused_with_the_statement_that_repairs_it(tmp_path, monkeypatch):
    """The remedy is per check, and this one is a `CREATE INDEX`, not a deletion.

    The wrapper's sentence is shared with four checks whose only cure genuinely
    is recreating the file — a missing table cannot be derived from rows. A
    missing index can: it constrains rows the database already holds. Telling a
    human to delete their graph over it is the wrong instruction by every node
    they own.

    So the refusal is followed here rather than pattern-matched. The statement it
    prints is executed verbatim, and the file it prints it about goes back to
    passing init *and* enforcing the guard the index exists for.
    """
    path = tmp_path / "stale.db"
    monkeypatch.setenv(db.ENV_DB_VAR, str(path))
    service.init()
    conn = db.connect()
    try:
        conn.execute("DROP INDEX idx_cycles_one_running_consolidation")
        conn.commit()
        with pytest.raises(db.SchemaConsistencyError) as refused:
            db.init_db(conn)
        message = str(refused.value)
        assert "delete the database file" not in message, "it told a human to bin their graph"
        assert db.CYCLES_RUNNING_INDEX_SQL in message

        # Follow it: the printed statement, run as printed, is the whole cure.
        conn.executescript(db.CYCLES_RUNNING_INDEX_SQL)
        conn.commit()
        assert db.init_db(conn) == []
    finally:
        conn.close()

    # And the repaired file enforces what the index is for — both halves of the
    # predicate, so a statement that merely carried the right *name* would not
    # pass: consolidations are serialised against each other, and a curative
    # cycle is deliberately outside the rule.
    first = service.open_cycle(trigger="scheduled", principal=owner())
    with pytest.raises(service.CycleInProgress):
        service.open_cycle(trigger="manual", principal=owner())
    beside_it = service.open_cycle(trigger="curative", principal=owner())
    service.close_cycle(beside_it.id, status="completed", report={}, principal=owner())
    service.close_cycle(first.id, status="completed", report={}, principal=owner())


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


def test_0014_seeds_the_gardener_with_read_on_meta_and_edit_on_main(fresh_db):
    """D7 is auto-apply by default, and a gardener with no grant does nothing.

    The grants are ordinary rows on purpose: they list beside every other
    agent's on `/spaces` and `nodum revoke` takes them away. There is no
    gardener-shaped exception in the grant model.

    **`read` on meta, not `edit`.** The first cut said `edit`, justified as
    "consolidation reads and *writes* the type vocabulary"; it never writes it —
    `consolidate._is_curatable` excludes the meta space and the structural types
    from every job, so meta is only ever read, to resolve a type. `read` is
    also exactly what every other curating agent in this suite holds. What the
    extra level bought was authority no shipped job reaches: creating spaces,
    renaming `main`, and archiving the `note` type, after which a human is
    blocked from writing a note too. `tests/test_consolidate.py` pins the
    behavioural half — a full cycle completes on `read`.
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
        # `0016` adds the third row: `edit` on the conventions space, the
        # gardener's own workspace (a revocable grant like the other two).
        assert levels == {"meta": "read", "main": "edit", "conventions": "edit"}
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
        assert db.init_db(conn) == [
            "0014_cycles_and_gardener",
            "0015_cycle_stop_switch",
            "0016_conventions_and_annotations",
        ]
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
        with pytest.raises(sqlite3.IntegrityError, match="reserved"):
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


def test_0014_refuses_any_pre_existing_id_under_the_reserved_prefix(tmp_path, monkeypatch):
    """The reservation is the **prefix**, and the upgrade guard has to say so.

    Checking the one id `builtin-gardener` let a database holding, say,
    `builtin-librarian` upgrade cleanly — leaving a live, token-bearing
    *external* agent under the prefix whose whole purpose is that
    `agent:<id>` in the event log names exactly one principal. Nothing is
    impersonated the day it upgrades; the day 5b seeds a second `builtin-*`
    agent, the collision this guard exists to refuse is already installed.
    """
    monkeypatch.setenv("NODUM_DB", str(tmp_path / "librarian.db"))
    monkeypatch.setattr(db, "MIGRATIONS", _prefix_through("0013_unique_space_titles"))
    service.init()
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO agents (id, kind, name, owner_human_id)"
            " VALUES ('builtin-librarian', 'external', 'builtin-librarian', 'owner')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="builtin-") as refusal:
            db.init_db(conn)
        assert "reserved" in str(refusal.value)
        assert "0014_cycles_and_gardener" not in db.applied_migrations(conn)
        assert (
            conn.execute("SELECT kind FROM agents WHERE id = 'builtin-librarian'").fetchone()[
                "kind"
            ]
            == "external"
        )
    finally:
        conn.close()


def test_0014_lets_an_ordinary_agent_id_through(tmp_path, monkeypatch):
    """The widened guard must still be a prefix and not a substring match."""
    monkeypatch.setenv("NODUM_DB", str(tmp_path / "ordinary.db"))
    monkeypatch.setattr(db, "MIGRATIONS", _prefix_through("0013_unique_space_titles"))
    service.init()
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO agents (id, kind, name, owner_human_id) VALUES"
            " ('librarian', 'external', 'librarian', 'owner'),"
            " ('my-builtin-helper', 'external', 'my-builtin-helper', 'owner')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        assert db.init_db(conn) == [
            "0014_cycles_and_gardener",
            "0015_cycle_stop_switch",
            "0016_conventions_and_annotations",
        ]
    finally:
        conn.close()


# ── 0015: the kill switch's row ──────────────────────────────────────────────


#: A database recorded at ``0015`` whose columns are not ``0015``'s. The route
#: in is ``0014``'s, walked twice already: ``init_db`` skips a migration whose
#: name it holds, so a file built from an earlier cut — the three-column shape
#: ``agent.cycle_stop_check``'s docstring proposes, say — keeps the name and
#: loses the columns, and nothing else would ever notice.
STALE_0015_NAME = "0015_cycle_stop_switch"


def _at_0014(tmp_path, monkeypatch, filename):
    """Build a populated database stopped at ``0014``, and return its path.

    Raw INSERTs for the cycle rows on purpose: ``service.open_cycle`` returns a
    ``CycleOut``, which reads the columns ``0015`` has not added yet.
    """
    path = tmp_path / filename
    monkeypatch.setenv("NODUM_DB", str(path))
    monkeypatch.setattr(db, "MIGRATIONS", _prefix_through("0014_cycles_and_gardener"))
    service.init()
    return path


def test_the_stop_switch_columns_exist_on_a_database_built_from_scratch(fresh_db):
    """Two columns and no boolean flag, which is this migration's one decision.

    `agent.cycle_stop_check`'s docstring proposes `stop_requested INTEGER NOT
    NULL DEFAULT 0` beside the two stamps. Checked against the table it
    describes, that is one column too many: `cycles` writes every fact that
    arrives *after* the INSERT as a nullable column whose presence is the flag —
    `finished_at`, `report`, `rolled_back_by` — and carries a boolean only for
    `dry_run`, which is fixed when the row is created and never transitions. A
    third representation of one event is a flag that can contradict the record
    beside it, and `ALTER TABLE` cannot add the table-level CHECK that would
    forbid it. `CycleOut.stop_requested` is `stop_requested_at IS NOT NULL`,
    computed on every read and stored nowhere.
    """
    conn = db.connect()
    try:
        assert "0015_cycle_stop_switch" in db.applied_migrations(conn)
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
            "stop_requested_at",
            "stop_requested_by",
        }
        assert "stop_requested" not in columns, "the boolean is derived, never stored"
    finally:
        conn.close()


def test_a_stop_records_who_asked_and_when_or_neither(fresh_db):
    """Two columns can still disagree, and the CHECK is what stops them.

    A `stop_requested_at` with no `stop_requested_by` is a stop nobody asked
    for; the reverse is an asker with no moment. `ALTER TABLE ... ADD COLUMN`
    does accept a cross-column CHECK, and its **name** is the message SQLite
    prints — the device `0014` uses for its reserved-prefix abort, because
    `RAISE()` is trigger-only here.
    """
    conn = db.connect()
    try:
        _insert_cycle(conn, "c", "scheduled")
        for column, value in (
            ("stop_requested_at", "datetime('now')"),
            ("stop_requested_by", "'human:owner'"),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="a stop records who asked and when"):
                conn.execute(f"UPDATE cycles SET {column} = {value} WHERE id = 'c'")
            # The refused half really was refused, so the next case starts from
            # both-NULL rather than from a row the previous one half-stamped.
            assert conn.execute("SELECT * FROM cycles WHERE id = 'c'").fetchone()[column] is None

        conn.execute(
            "UPDATE cycles SET stop_requested_at = datetime('now'),"
            " stop_requested_by = 'human:owner' WHERE id = 'c'"
        )
        row = conn.execute("SELECT * FROM cycles WHERE id = 'c'").fetchone()
        assert row["stop_requested_by"] == "human:owner"
        assert row["stop_requested_at"] is not None
    finally:
        conn.close()


def test_0015_applies_to_a_populated_database_already_at_0014(tmp_path, monkeypatch):
    """The upgrade path on a populated file: `0014` is where v0.7.0 users are.

    A migration only ever run against an empty database is a migration whose
    upgrade path nothing has walked — Phase 5a's Q13 review found one that could
    not upgrade any populated database at all, and the suite could not see it.
    So this file holds a graph, a finished cycle carrying a report, and a cycle
    still `running`; and the switch has to work on that last row, which predates
    the columns it is written in.
    """
    _at_0014(tmp_path, monkeypatch, "at0014.db")
    node = service.create_node(type="note", title="before the upgrade", principal=owner())
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO cycles (id, trigger, triggered_by, status, report, finished_at)"
            " VALUES ('finished', 'manual', 'human:owner', 'completed', '{\"merged\": 2}',"
            " datetime('now'))"
        )
        conn.execute(
            "INSERT INTO cycles (id, trigger, triggered_by, status)"
            " VALUES ('mid-run', 'scheduled', 'scheduler', 'running')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        assert db.init_db(conn) == ["0015_cycle_stop_switch", "0016_conventions_and_annotations"]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

    # Both rows survived the upgrade saying what they said, plus "nobody asked
    # this to stop" — which is what two NULLs mean and needs no back-fill.
    journal = {entry.id: entry for entry in service.list_cycles(principal=owner())}
    assert journal["finished"].report == {"merged": 2}
    assert [journal[cycle].stop_requested for cycle in ("finished", "mid-run")] == [False, False]
    assert service.get_node(node.id, principal=owner()).title == "before the upgrade"

    # And the switch works on the row that predates it, read by the principal
    # that would be running it.
    gardener = auth.internal_principal()
    assert service.stop_requested("mid-run", principal=gardener) is False
    stopped = service.request_stop("mid-run", principal=owner())
    assert (stopped.stop_requested, stopped.stop_requested_by) == (True, "human:owner")
    assert service.stop_requested("mid-run", principal=gardener) is True


def test_a_database_recorded_at_0015_without_the_columns_is_refused(tmp_path, monkeypatch):
    """Every recorded migration with a checkable guarantee has a check (Q13 S6).

    `0015`'s is as checkable as they come — two columns, or the switch has
    nowhere to write. Without the entry the drift would surface where the four
    earlier ones used to: deep inside a write, as `no such column` raised by the
    very call a human made to stop a run. Nothing in the runtime catches it
    first: `LLMReport.stop_switch` reports the posture a *run* had, so a cycle
    over such a database reads `armed` right up to the failed write.
    """
    _at_0014(tmp_path, monkeypatch, "stale.db")
    conn = db.connect()
    try:
        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (STALE_0015_NAME,))
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        with pytest.raises(db.SchemaConsistencyError, match=STALE_0015_NAME) as refused:
            db.init_db(conn)
        assert "stop_requested_at" in str(refused.value)
        assert "stop_requested_by" in str(refused.value)
    finally:
        conn.close()


def test_the_missing_stop_columns_are_refused_with_the_statements_that_add_them(
    tmp_path, monkeypatch
):
    """The remedy is per check, and this one adds columns rather than binning a graph.

    Both columns are pure additions with no back-fill — every row that predates
    them is a cycle nobody asked to stop, which is what two NULLs say — so the
    file is repairable in place, exactly as `0014`'s missing index is. The
    refusal is therefore *followed* here rather than pattern-matched: every
    statement it prints is executed verbatim, in the order printed, and the file
    goes back to passing init **and** enforcing what the columns are for.
    """
    _at_0014(tmp_path, monkeypatch, "stale.db")
    conn = db.connect()
    try:
        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (STALE_0015_NAME,))
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        with pytest.raises(db.SchemaConsistencyError) as refused:
            db.init_db(conn)
        message = str(refused.value)
        assert "delete the database file" not in message, "it told a human to bin their graph"

        # Follow it: the printed statements, run as printed and in the printed
        # order — the second column's CHECK names the first.
        for _, statement in db.CYCLE_STOP_COLUMN_SQL:
            assert statement in message
            conn.executescript(statement)
        conn.commit()
        # The file was stopped at 0014, so the repair clears the last obstacle
        # and 0016 applies like any later migration.
        assert db.init_db(conn) == ["0016_conventions_and_annotations"]

        # And the repaired file enforces the coherence the CHECK exists for, so
        # a statement carrying only the right column *names* would not pass.
        _insert_cycle(conn, "c", "scheduled")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="a stop records who asked and when"):
            conn.execute("UPDATE cycles SET stop_requested_at = datetime('now') WHERE id = 'c'")
        conn.rollback()
    finally:
        conn.close()

    # The whole verb works over the repair, not just the schema read.
    assert service.request_stop("c", principal=owner()).stop_requested is True


def test_a_half_applied_0015_is_repaired_without_re_adding_the_column_it_has(tmp_path, monkeypatch):
    """The refusal names the columns it *found* missing, and that is load-bearing.

    `ADD COLUMN` has no `IF NOT EXISTS`, so a remedy that always printed both
    statements would hand a human one that dies on `duplicate column name` —
    advice nobody can carry out, which is the failure shape this repo has
    already fixed once in `CycleInProgress`. A file carrying the first column
    and not the second is the shape an interrupted upgrade leaves, since the two
    ALTERs are separate statements.
    """
    _at_0014(tmp_path, monkeypatch, "half.db")
    conn = db.connect()
    try:
        conn.executescript(db.CYCLE_STOP_COLUMN_SQL[0][1])
        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (STALE_0015_NAME,))
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        with pytest.raises(db.SchemaConsistencyError) as refused:
            db.init_db(conn)
        message = str(refused.value)
        assert "stop_requested_by" in message
        assert db.CYCLE_STOP_COLUMN_SQL[0][1] not in message, (
            "it printed a statement that would die on 'duplicate column name'"
        )

        conn.executescript(db.CYCLE_STOP_COLUMN_SQL[1][1])
        conn.commit()
        # The file was stopped at 0014, so the repair clears the last obstacle
        # and 0016 applies like any later migration.
        assert db.init_db(conn) == ["0016_conventions_and_annotations"]
    finally:
        conn.close()


def _stop_columns_without_the_check(conn):
    """Leave `cycles` carrying both stamps and no constraint over them.

    **Not a shape this repo has ever produced.** `0015` landed with both columns
    and the CHECK attached and `nodum/migrations.py` has not changed since, so
    unlike `0014` — genuinely amended in place while unreleased — no database
    built from this history can record `0015` and lack the constraint. What the
    check guards, and what this reproduces, is a file drifted by something
    outside the migration runner: a hand-edited schema, an externally applied
    migration, or a human following half of the repair. Dropping the constrained
    column and adding it back bare is the shortest route to that state and is
    also, precisely, what the last of those does.
    """
    conn.executescript(
        "ALTER TABLE cycles DROP COLUMN stop_requested_by;"
        "ALTER TABLE cycles ADD COLUMN stop_requested_by TEXT;"
    )
    conn.commit()


def _rebuilt_with(conn, replacement):
    """Rebuild `cycles` with `replacement` in place of `0015`'s `<CONSTRAINT …>`.

    The repair statement itself, with its one constraint swapped out — so the
    table under test differs from a sound one in exactly that clause and in
    nothing else.

    The separator goes with it — the last column declaration's trailing comma —
    since a table whose columns are followed by nothing needs no separator.

    The parked copy the repair leaves behind goes with it too: this is a
    *fixture* building a drifted `cycles`, and a file also carrying
    `cycles_before_repair` is a different drift, which `_cycle_stop_problems`
    reports first and would hide everything the callers are asking about.
    """
    conn.executescript(
        db.CYCLE_STOP_CHECK_REBUILD_SQL.replace(f",\n{db.CYCLE_STOP_CHECK_SQL}", replacement)
    )
    conn.execute(f"DROP TABLE {db.CYCLES_PARKED_TABLE}")
    conn.commit()


def _script_from_message(message, first_line, last_line):
    """The statement a refusal printed, cut out of the message a human reads.

    A test that runs the repair out of the module tests the module. What a human
    has is the message, and between the two sits every renderer it ships
    through — so the ones below take the SQL back out of the text, by lines
    written here rather than by the constants they are checking.
    """
    lines = message.splitlines()
    start = lines.index(first_line)
    end = start + lines[start:].index(last_line)
    return "\n".join(lines[start : end + 1])


def _drop_the_parked_copy(conn):
    """Take the second step the refusal prints, and check it is the one printed.

    The rebuild parks a copy of `cycles` and leaves it, so the file goes on
    being refused until a human has been told the copy is there — that refusal
    is the only thing standing between "the copy did not finish" and an empty
    journal nobody hears about. Here the copy is whole, so the statement is a
    `DROP` and this asserts the message says exactly that.
    """
    with pytest.raises(db.SchemaConsistencyError) as refused:
        db.init_db(conn)
    message = str(refused.value)
    assert "cycles_before_repair" in message
    assert "not in 'cycles'" not in message, "it said rows were stranded when none are"
    statement = _script_from_message(
        message, "DROP TABLE cycles_before_repair;", "DROP TABLE cycles_before_repair;"
    )
    conn.executescript(statement)
    conn.commit()


def _statements(script):
    """`script` split the way a SQL console reads it, statement by statement.

    `sqlite3.complete_statement` is the predicate the `sqlite3` shell itself
    uses to decide a statement has ended, so this is that shell's reading of the
    text rather than a guess at one.
    """
    statements, current = [], ""
    for character in script:
        current += character
        if character == ";" and sqlite3.complete_statement(current):
            statements.append(current.strip())
            current = ""
    return statements


def _run_as_a_console_does(path, script, *, stop_at_the_first_error):
    """Run `script` a statement at a time against `path`, as a console does.

    `executescript` is **one** execution model, and it is the forgiving one: it
    abandons the whole script at the first error. A human does not necessarily
    have that. An interactive shell, a database GUI and a notebook cell report
    the error and read the next statement — `sqlite3` ships a `-bail` flag
    precisely because stopping is not what it does by default — and every
    statement after a failed one then runs against a half-built schema.

    The connection is in autocommit, so the script's own `BEGIN`/`COMMIT` are
    the only transaction control, exactly as they are for the human pasting it.

    Returns the errors, in order.
    """
    connection = sqlite3.connect(path, isolation_level=None)
    errors = []
    try:
        for statement in _statements(script):
            try:
                connection.execute(statement)
            except sqlite3.Error as error:
                errors.append(error)
                if stop_at_the_first_error:
                    break
    finally:
        connection.close()
    return errors


def _cycle_ids_anywhere_in(path):
    """Every cycle id the file still holds, in `cycles` or in the parked copy."""
    connection = sqlite3.connect(path)
    try:
        listed = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        tables = {row[0] for row in listed}
        found = set()
        for table in ("cycles", db.CYCLES_PARKED_TABLE):
            if table in tables:
                found |= {row[0] for row in connection.execute(f"SELECT id FROM {table}")}
        return found
    finally:
        connection.close()


def _half_stop_is_storable(conn, cycle_id):
    """Can this file hold a time with no requester — the state the CHECK forbids?

    Leaves the row behind either way, at whichever of the two states it reached,
    so the caller can go on asking about the same file.
    """
    _insert_cycle(conn, cycle_id, "curative")
    try:
        conn.execute(
            "UPDATE cycles SET stop_requested_at = datetime('now') WHERE id = ?", (cycle_id,)
        )
    except sqlite3.IntegrityError:
        conn.commit()
        return False
    conn.commit()
    return True


def test_the_constraint_name_in_a_comment_does_not_satisfy_the_check(fresh_db):
    """A check over stored DDL must not fail *open*, and by default it does.

    SQLite keeps a `CREATE TABLE` verbatim, comments included, and the `cycles`
    DDL this repo ships is heavily commented — so a substring search for the
    constraint name is satisfied by a file that merely *mentions* it. That is
    the one failure direction that costs something: the check reports a sound
    schema, `init_db` returns `[]`, and the half-stop the constraint exists to
    forbid goes straight in. `_table_sql` strips comments before anything
    searches, and this is what says so.
    """
    conn = db.connect()
    try:
        _rebuilt_with(conn, f"\n  /* {db.CYCLE_STOP_CHECK_SQL} */")
        # The name really is in the schema SQLite stored, which is what the
        # search used to be satisfied by...
        stored = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cycles'"
        ).fetchone()["sql"]
        assert db.CYCLE_STOP_CHECK_NAME in stored
        # ...and nothing constrains the columns, so the drift is real.
        assert _half_stop_is_storable(conn, "half") is True

        assert db.CYCLE_STOP_CHECK_NAME not in db._table_sql(conn, "cycles")
        problems = db._cycle_stop_problems(conn)
        assert len(problems) == 1
        assert db.CYCLE_STOP_CHECK_NAME in problems[0]
        with pytest.raises(db.SchemaConsistencyError):
            db.init_db(conn)
    finally:
        conn.close()


def test_the_constraint_name_in_a_dash_comment_does_not_satisfy_the_check(fresh_db):
    """The other half of the comment stripper, and it is the half this repo writes.

    `_SQL_COMMENT_RE` has two alternatives and only `/* … */` was exercised.
    Deleting the `--` arm outright passed all 155 tests — while `migrations.py`
    contains no `/*` at all: every comment in the `cycles` DDL the docstring
    cites as the motivation is a `--` comment, and they reach `sqlite_master`
    verbatim. The motivating case was the untested one.
    """
    conn = db.connect()
    try:
        _rebuilt_with(conn, f'\n  -- the CHECK "{db.CYCLE_STOP_CHECK_NAME}" belongs here\n')
        stored = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cycles'"
        ).fetchone()["sql"]
        # The name is in the stored schema, in a `--` comment...
        assert db.CYCLE_STOP_CHECK_NAME in stored
        assert "--" in stored
        # ...and nothing constrains the columns, so the drift is real.
        assert _half_stop_is_storable(conn, "half") is True

        assert db.CYCLE_STOP_CHECK_NAME not in db._table_sql(conn, "cycles")
        problems = db._cycle_stop_problems(conn)
        assert len(problems) == 1
        assert db.CYCLE_STOP_CHECK_NAME in problems[0]
        with pytest.raises(db.SchemaConsistencyError):
            db.init_db(conn)
    finally:
        conn.close()


def test_a_cycles_carrying_a_column_the_rebuild_never_heard_of_is_not_handed_it(fresh_db):
    """The rebuild's column list is this version's, and the drifted file is not.

    `CYCLES_COLUMNS` is pinned against `PRAGMA table_info(cycles)` on a freshly
    migrated database — which is the one schema the repair never runs against,
    because a file in this repo's own shape does not need repairing. The whole
    population the statement exists for is files this repo did not produce, and
    against one of those the hand-written list is a guess: a column added by a
    hand-edited schema or an externally applied migration was rebuilt away, with
    its data, silently, and `init_db` returned `[]` afterwards.

    So the check compares the live columns to the list before printing anything,
    and where they differ it prints the difference instead of the rebuild. A
    repair whose column list is not the table's is not a repair.
    """
    conn = db.connect()
    try:
        _stop_columns_without_the_check(conn)
        conn.execute("ALTER TABLE cycles ADD COLUMN operator_notes TEXT")
        _insert_cycle(conn, "kept", "curative", status="completed")
        conn.execute("UPDATE cycles SET operator_notes = 'why this ran' WHERE id = 'kept'")
        conn.commit()

        with pytest.raises(db.SchemaConsistencyError) as refused:
            db.init_db(conn)
        message = str(refused.value)
        assert "operator_notes" in message, "it did not name the column it would have dropped"
        assert db.CYCLE_STOP_CHECK_REBUILD_SQL not in message, (
            "it printed a rebuild that drops a column of this file, with its data"
        )
        # The drift is still reported — refusing to print the rebuild is not
        # refusing to notice — and the column and its value are still there.
        assert db.CYCLE_STOP_CHECK_NAME in message
        kept = conn.execute("SELECT operator_notes FROM cycles WHERE id = 'kept'").fetchone()
        assert kept[0] == "why this ran"

        # Reconciled, the rebuild comes back.
        conn.execute("ALTER TABLE cycles DROP COLUMN operator_notes")
        conn.commit()
        with pytest.raises(db.SchemaConsistencyError) as second:
            db.init_db(conn)
        assert db.CYCLE_STOP_CHECK_REBUILD_SQL in str(second.value)
    finally:
        conn.close()


def test_a_cycles_missing_a_column_the_rebuild_copies_is_not_handed_it_either(fresh_db):
    """The same guard from the other side: the copy would die on a missing column.

    `SELECT report FROM cycles` is not a repair on a file that has no `report`,
    and the failure — `no such column` in the middle of the copy — is one the
    message never anticipated. It only ever named the half-stop.
    """
    conn = db.connect()
    try:
        _stop_columns_without_the_check(conn)
        conn.execute("ALTER TABLE cycles DROP COLUMN report")
        conn.commit()

        with pytest.raises(db.SchemaConsistencyError) as refused:
            db.init_db(conn)
        message = str(refused.value)
        assert "report" in message
        assert db.CYCLE_STOP_CHECK_REBUILD_SQL not in message
    finally:
        conn.close()


def test_an_unnamed_check_that_enforces_the_rule_is_still_reported(fresh_db):
    """Reported on purpose: the name is half of what `0015` guarantees.

    SQLite prints a named constraint verbatim as `CHECK constraint failed:
    <name>`, so the name is the sentence a human meets when the stop stamps
    disagree — `0014`'s device, since `RAISE()` is trigger-only — and it is what
    the rebuild puts back. A file enforcing the same rule anonymously enforces
    the rule and does not carry the migration, so saying so is the right answer
    and not a false alarm. The cost is bounded and in the safe direction: a human
    reads a repair that turns out to change only the message.

    Asserted rather than left implicit because the alternative reading — "this
    check over-fires" — invites loosening it, and loosening a search over DDL is
    how it starts failing open instead.
    """
    conn = db.connect()
    try:
        _rebuilt_with(conn, ",\nCHECK ((stop_requested_by IS NULL) = (stop_requested_at IS NULL))")
        # It bites, so the file is not broken in the way the message describes.
        assert _half_stop_is_storable(conn, "anonymous") is False
        # And it is reported anyway, because the constraint `0015` records is a
        # named one and this file has not got it.
        problems = db._cycle_stop_problems(conn)
        assert len(problems) == 1
        assert db.CYCLE_STOP_CHECK_NAME in problems[0]

        # The repair is a no-op for the rule and puts the name on, which is the
        # whole of what it was missing — and then names its own parked copy,
        # which is the second half of the two-step it always is now.
        conn.executescript(db.CYCLE_STOP_CHECK_REBUILD_SQL)
        conn.commit()
        _drop_the_parked_copy(conn)
        assert db.init_db(conn) == []
        assert _half_stop_is_storable(conn, "named") is False
        with pytest.raises(sqlite3.IntegrityError, match=db.CYCLE_STOP_CHECK_NAME):
            conn.execute(
                "UPDATE cycles SET stop_requested_at = datetime('now') WHERE id = 'anonymous'"
            )
        conn.rollback()
    finally:
        conn.close()


def test_the_stop_columns_without_their_check_are_drift_the_check_has_to_see(fresh_db):
    """`0015` guarantees two stamps *and one CHECK*, and only one was checked.

    The constraint is the whole reason two nullable columns are honest: without
    it the file can hold a time with no requester or a requester with no time,
    which is precisely the state the migration chose two columns over a boolean
    to make unstorable. `PRAGMA table_info` cannot see it — it answers what the
    columns are and says nothing about what constrains them — so the check reads
    the stored schema, and until it did, a file wearing the migration's name
    with no constraint under its stamps passed init and stayed that way.
    """
    conn = db.connect()
    try:
        assert db._cycle_stop_problems(conn) == [], "a file straight from 0015 is not drift"
        _stop_columns_without_the_check(conn)
        # The columns are both there, so the earlier check has nothing to say.
        assert {"stop_requested_at", "stop_requested_by"} <= db._columns(conn, "cycles")
        problems = db._cycle_stop_problems(conn)
        assert len(problems) == 1
        assert db.CYCLE_STOP_CHECK_NAME in problems[0]
        # And the file really is broken in the way the sentence claims: the
        # half-stop the constraint exists to forbid goes straight in.
        _insert_cycle(conn, "half", "scheduled")
        conn.execute("UPDATE cycles SET stop_requested_at = datetime('now') WHERE id = 'half'")
        conn.commit()
        row = conn.execute("SELECT * FROM cycles WHERE id = 'half'").fetchone()
        assert (row["stop_requested_at"], row["stop_requested_by"]) != (None, None)
        assert row["stop_requested_by"] is None, "a stop nobody asked for, stored"
    finally:
        conn.close()


#: One `cycles` row with a distinct, non-NULL value in **every** column the
#: table has, for the rebuild's "keeps every row" claim — which is a claim about
#: all twelve and was asserted over five. `_insert_cycle` only ever fills four,
#: so a rebuild that dropped `report` — every cycle's journal body — passed the
#: whole suite. Keyed by column so the test can check it still covers the table.
_EVERY_COLUMN_FILLED = {
    "id": "stopped",
    "trigger": "curative",
    "triggered_by": "human:owner",
    "scope": "research",
    "dry_run": 1,
    "status": "failed",
    "report": '{"jobs": ["duplicates"], "detail": "the body of the journal entry"}',
    "started_at": "2026-07-29 22:00:00",
    "finished_at": "2026-07-30 01:30:00",
    "rolled_back_by": "the-rollback",
    "stop_requested_at": "2026-07-30 01:00:00",
    "stop_requested_by": "human:owner",
}


def test_the_rebuilds_column_list_is_every_column_a_migrated_cycles_has(fresh_db):
    """A column list written by hand is a data-loss bug the day a migration adds one.

    `CYCLE_STOP_CHECK_REBUILD_SQL` builds its replacement table from
    `db.CYCLES_COLUMNS` and copies the same list across, so the two halves of
    the repair cannot disagree — but nothing inside the repair can know whether
    that list is still the table. A column `0016` adds to `cycles` would be
    dropped by it, **with its data**, in a statement a human is told to run
    against their own graph, silently, with `init_db` returning `[]` afterwards
    because no check looks for it.

    This is the pin, and it fails on the commit that adds the column rather than
    on the install that runs the repair.
    """
    conn = db.connect()
    try:
        live = [row["name"] for row in conn.execute("PRAGMA table_info(cycles)")]
    finally:
        conn.close()
    assert live == [name for name, _ in db.CYCLES_COLUMNS], (
        "a migration changed `cycles` and `db.CYCLES_COLUMNS` did not follow: the rebuild "
        "printed by `_cycle_stop_problems` would drop the difference and its data"
    )


def _create_table_from_the_rebuild():
    """The rebuild's replacement table, built in a scratch database of its own.

    The statement out of `CYCLE_STOP_CHECK_REBUILD_SQL`, run nowhere near a real
    graph, so the *declarations* can be interrogated: what a rebuilt `cycles`
    would be, as opposed to what it would be called.
    """
    statement = _script_from_message(
        db.CYCLE_STOP_CHECK_REBUILD_SQL, "CREATE TABLE cycles_rebuilt (", ");"
    )
    scratch = sqlite3.connect(":memory:")
    scratch.row_factory = sqlite3.Row
    scratch.executescript(statement)
    return scratch


#: Values probed against `trigger` and `status`, live table against rebuilt one.
#: The four each column allows, plus one a widened CHECK would let through and
#: two nothing should — a CHECK is a set of accepted values, so comparing the
#: sets is comparing the constraint.
_TRIGGER_PROBES = ("manual", "scheduled", "curative", "rollback", "repair", "", "MANUAL")
_STATUS_PROBES = ("running", "completed", "failed", "rolled_back", "cancelled", "", "COMPLETED")


def _accepts(conn, table, column, value, row_id):
    """Does `table` accept `value` in `column`? Leaves nothing behind either way."""
    other = "status" if column == "trigger" else "trigger"
    fixed = "completed" if column == "trigger" else "curative"
    try:
        conn.execute(
            f"INSERT INTO {table} (id, {column}, triggered_by, {other}) VALUES (?, ?, 'x', ?)",
            (row_id, value, fixed),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    conn.rollback()
    return True


def test_the_rebuilds_columns_are_the_migrated_ones_down_to_type_default_and_check(fresh_db):
    """The names matching is not the table matching, and the rebuild declares the rest.

    `test_the_rebuilds_column_list_is_every_column_a_migrated_cycles_has`
    compares `row["name"]` and nothing else, so it is blind to everything the
    rebuild's declarations actually assert. Three edits to the shipped `cycles`
    DDL — retyping `report` to `BLOB`, dropping `dry_run`'s `NOT NULL`, widening
    the `trigger` CHECK — passed the whole suite while leaving
    `db.CYCLES_COLUMNS` untouched, and each of them is a repair that silently
    rewrites the column back: a human runs the rebuild to put a CHECK on and
    gets a differently-typed column, a re-imposed `NOT NULL` their rows may
    violate, or a narrower CHECK than the file they started with.

    So the comparison is the whole of `PRAGMA table_info` — type, `notnull`,
    `dflt_value`, `pk` — plus, for the two columns whose declaration is a CHECK,
    the set of values each table accepts. `table_info` cannot see a CHECK at
    all, and a constraint is exactly the values it lets through.
    """
    conn = db.connect()
    scratch = _create_table_from_the_rebuild()
    try:
        live = [tuple(row) for row in conn.execute("PRAGMA table_info(cycles)")]
        rebuilt = [tuple(row) for row in scratch.execute("PRAGMA table_info(cycles_rebuilt)")]
        assert live == rebuilt, (
            "the table the rebuild would build is not the table the migrations leave: a "
            "human running it to add a CHECK would get a different column back"
        )

        for column, probes in (("trigger", _TRIGGER_PROBES), ("status", _STATUS_PROBES)):
            for index, value in enumerate(probes):
                row_id = f"probe-{column}-{index}"
                assert _accepts(conn, "cycles", column, value, row_id) == _accepts(
                    scratch, "cycles_rebuilt", column, value, row_id
                ), f"the two tables disagree about {column} = {value!r}"
    finally:
        scratch.close()
        conn.close()


def test_the_missing_stop_check_is_refused_with_a_rebuild_that_keeps_every_row(fresh_db):
    """The remedy is this check's own, and putting a CHECK on is not adding a column.

    `ALTER TABLE` adds a constraint only *with* a column, so the repair for a
    column that already exists is the documented create-copy-drop-rename
    rebuild — still a repair in place, with every row carried across and both
    indexes recreated. What it must not be is the first four checks' sentence:
    "delete the database file and re-run `nodum init`" reads as *your graph is
    unrecoverable* over a constraint the file has all the rows for, which is the
    mistake `0014`'s missing index already had to be rescued from.

    The refusal is **followed** rather than pattern-matched: the statement it
    prints is executed verbatim, and afterwards the file passes init, still
    holds its rows, refuses the half-stop, and still serialises its cycles.

    "Every row" is asserted over **every column**, which it was not: the row went
    in through `_insert_cycle`, which fills four of the twelve, and the test then
    looked at the two stop stamps. `scope`, `dry_run`, `report`, `started_at`,
    `finished_at` and `rolled_back_by` were neither written nor read, so NULLing
    `report` in the copy — destroying every cycle's journal body — passed here.
    A row-preservation test is worth exactly the columns it populates.
    """
    conn = db.connect()
    try:
        assert set(_EVERY_COLUMN_FILLED) == {name for name, _ in db.CYCLES_COLUMNS}
        # Stopped *after* the drift, which is the only order a real file can
        # have it in: a constraint-less schema is what the row was written on.
        _stop_columns_without_the_check(conn)
        # `rolled_back_by` is a self-reference, so the row it names has to exist.
        _insert_cycle(conn, "the-rollback", "rollback", status="completed")
        columns = ", ".join(_EVERY_COLUMN_FILLED)
        conn.execute(
            f"INSERT INTO cycles ({columns}) VALUES ({', '.join('?' * len(_EVERY_COLUMN_FILLED))})",
            tuple(_EVERY_COLUMN_FILLED.values()),
        )
        conn.commit()

        with pytest.raises(db.SchemaConsistencyError) as refused:
            db.init_db(conn)
        message = str(refused.value)
        assert "delete the database file" not in message, "it told a human to bin their graph"
        assert db.CYCLE_STOP_CHECK_REBUILD_SQL in message

        # Run what the message printed, not what the module holds — and run it
        # as a script, which is one of the three ways it has to survive.
        conn.executescript(
            _script_from_message(message, "PRAGMA foreign_keys=off;", "PRAGMA foreign_keys=on;")
        )
        conn.commit()
        _drop_the_parked_copy(conn)
        assert db.init_db(conn) == []

        # Every row came across, and every *column* of it — the recorded stop,
        # the scope, the rehearsal flag, both timestamps, the rollback link, and
        # the report, which is the whole readable body of a journal entry.
        row = conn.execute("SELECT * FROM cycles WHERE id = 'stopped'").fetchone()
        assert dict(row) == _EVERY_COLUMN_FILLED
        assert {r["id"] for r in conn.execute("SELECT id FROM cycles")} == {
            "stopped",
            "the-rollback",
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        # The constraint bites, by the name SQLite prints...
        _insert_cycle(conn, "fresh", "scheduled")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match=db.CYCLE_STOP_CHECK_NAME):
            conn.execute("UPDATE cycles SET stop_requested_at = datetime('now') WHERE id='fresh'")
        conn.rollback()
        # ...and the two indexes the rebuild dropped with the table are back, so
        # the repair did not trade one guarantee for another.
        with pytest.raises(sqlite3.IntegrityError, match="cycles.status"):
            _insert_cycle(conn, "second", "manual")
        conn.rollback()
    finally:
        conn.close()

    # And the whole verb works over the repair, not just the schema read.
    assert service.request_stop("fresh", principal=owner()).stop_requested is True


def test_a_half_stop_is_named_and_cleared_before_the_rebuild_is_printed_at_all(fresh_db):
    """A repair that is *going* to fail is not handed to a human to run.

    The rebuild copies the rows through the constraint, so a file that ran
    without one and stored a half-stop meets `CHECK constraint failed` there —
    the constraint doing its job, and, at the end of the one instruction the
    refusal gave, a dead end. It used to be printed anyway, with a sentence
    about what to do *if* it failed.

    That row is visible from the check, before anything is printed
    (`CYCLE_STOP_HALF_STOP_SQL` is a query, and the check can run it), so the
    order is the other way round now: name the rows, print the statement that
    clears them, and print the rebuild on the next run, when it will work.
    Advice that fails halfway is advice that gets abandoned halfway, and a
    rebuild abandoned halfway is the state the test below this one is about.
    """
    conn = db.connect()
    try:
        _stop_columns_without_the_check(conn)
        _insert_cycle(conn, "half", "scheduled")
        conn.execute("UPDATE cycles SET stop_requested_at = datetime('now') WHERE id = 'half'")
        conn.commit()

        with pytest.raises(db.SchemaConsistencyError) as refused:
            db.init_db(conn)
        message = str(refused.value)
        # The row is named, the query that finds it is given...
        assert "half" in message
        assert db.CYCLE_STOP_HALF_STOP_SQL in message
        # ...and the rebuild is *withheld*, because running it here fails.
        assert db.CYCLE_STOP_CHECK_REBUILD_SQL not in message
        with pytest.raises(sqlite3.IntegrityError, match=db.CYCLE_STOP_CHECK_NAME):
            conn.executescript(db.CYCLE_STOP_CHECK_REBUILD_SQL)
        conn.rollback()

        # The statement it prints instead is the one that unblocks it, and the
        # query it names finds the row it is about.
        assert [row["id"] for row in conn.execute(db.CYCLE_STOP_HALF_STOP_SQL)] == ["half"]
        conn.executescript(
            _script_from_message(
                message,
                "UPDATE cycles SET stop_requested_at = NULL,",
                "     != (stop_requested_at IS NULL);",
            )
        )
        conn.commit()
        assert conn.execute(db.CYCLE_STOP_HALF_STOP_SQL).fetchall() == []

        # And now — and only now — the rebuild is printed, and it works.
        with pytest.raises(db.SchemaConsistencyError) as second:
            db.init_db(conn)
        assert db.CYCLE_STOP_CHECK_REBUILD_SQL in str(second.value)
        conn.executescript(db.CYCLE_STOP_CHECK_REBUILD_SQL)
        conn.commit()
        _drop_the_parked_copy(conn)
        assert db.init_db(conn) == []
        # The cycle whose stop was recorded half-way is still a cycle.
        assert [row["id"] for row in conn.execute("SELECT id FROM cycles")] == ["half"]
    finally:
        conn.close()


def _four_cycles_and_a_half_stop(path):
    """A drifted `cycles` with four journal entries, one of them a half-stop.

    The half-stop is what makes the rebuild's copy fail, and the other three are
    there to be counted afterwards: this fixture is about what survives.
    """
    conn = db.connect()
    try:
        _stop_columns_without_the_check(conn)
        for index in range(4):
            _insert_cycle(conn, f"cycle-{index}", "curative", status="completed")
        conn.execute("UPDATE cycles SET stop_requested_at = datetime('now') WHERE id = 'cycle-0'")
        conn.commit()
    finally:
        conn.close()
    assert _cycle_ids_anywhere_in(path) == {f"cycle-{index}" for index in range(4)}


def test_no_way_of_running_the_rebuild_can_lose_a_cycle(fresh_db):
    """A console that runs on past an error emptied the journal, and nothing noticed.

    The rebuild's copy can fail — that is the constraint doing its job on a file
    that stored a half-stop while it had no constraint — and the four statements
    after it were `DROP TABLE cycles`, the `RENAME`, the indexes and `COMMIT`.
    Under `executescript` that never happens, because the first error abandons
    the script; under a console that reports the error and reads the next
    statement, all four ran, and a file with four cycles in it had none. The
    `events` rows kept pointing at cycles that no longer existed, `init_db`
    returned `[]` afterwards, and the only trace of any of it was in a terminal
    scrollback.

    A transaction is a guarantee about one execution model. What holds across
    all of them is structural: **the repair carries no statement that can lose a
    row.** The copy into `cycles_before_repair` is the first thing it does, and
    the `DROP` is against a table whose every row is already in that copy. There
    is nothing weaker that works — SQLite has no conditional DDL and no `RAISE`
    outside a trigger, so no statement in the script can stop the next one from
    running, and *not destroying anything* is the only guarantee left that does
    not depend on the tool obeying an error.

    Run here in the model that broke it, and the file is followed all the way
    back: refused with the rows named, restored by the statements the refusal
    prints, and passing `init` with all four cycles in it.
    """
    _four_cycles_and_a_half_stop(fresh_db)

    errors = _run_as_a_console_does(
        fresh_db, db.CYCLE_STOP_CHECK_REBUILD_SQL, stop_at_the_first_error=False
    )

    # The copy failed, as it must — and everything after it still ran.
    assert [type(error).__name__ for error in errors] == ["IntegrityError"]
    assert db.CYCLE_STOP_CHECK_NAME in str(errors[0])
    assert _cycle_ids_anywhere_in(fresh_db) == {f"cycle-{index}" for index in range(4)}, (
        "a run that stopped in the middle lost cycles"
    )

    conn = db.connect()
    try:
        # Nothing is allowed to quietly pass over a file in this state, which is
        # the other half of what went wrong: the loss was silent.
        with pytest.raises(db.SchemaConsistencyError) as refused:
            db.init_db(conn)
        message = str(refused.value)
        assert "cycles_before_repair" in message
        assert "Do not drop it" in message
        for index in range(4):
            assert f"cycle-{index}" in message, "it did not name the rows it is holding"

        # Follow it. The copy carries the half-stop, so the insert refuses
        # first — which the message says, with the statement for that too.
        restore = _script_from_message(
            message, "INSERT INTO cycles SELECT", "  WHERE id NOT IN (SELECT id FROM cycles);"
        )
        with pytest.raises(sqlite3.IntegrityError, match=db.CYCLE_STOP_CHECK_NAME):
            conn.executescript(restore)
        conn.rollback()
        conn.executescript(
            _script_from_message(
                message,
                "UPDATE cycles_before_repair SET stop_requested_at = NULL,",
                "     != (stop_requested_at IS NULL);",
            )
        )
        conn.executescript(restore)
        conn.commit()

        _drop_the_parked_copy(conn)
        assert db.init_db(conn) == []
        assert {row["id"] for row in conn.execute("SELECT id FROM cycles")} == {
            f"cycle-{index}" for index in range(4)
        }
    finally:
        conn.close()


def test_a_console_that_stops_at_the_first_error_changes_nothing(fresh_db):
    """The other statement-at-a-time model, and it must leave the file alone.

    `-bail`, a script with `set -e`, a driver that raises: the copy fails, the
    transaction is never committed, and closing the connection rolls it back.
    Asserted beside its opposite because the two together are the claim — the
    repair is safe *whatever* the tool does with an error, not safe in one
    reading and merely survivable in the other.
    """
    _four_cycles_and_a_half_stop(fresh_db)

    errors = _run_as_a_console_does(
        fresh_db, db.CYCLE_STOP_CHECK_REBUILD_SQL, stop_at_the_first_error=True
    )

    assert [type(error).__name__ for error in errors] == ["IntegrityError"]
    conn = db.connect()
    try:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert db.CYCLES_PARKED_TABLE not in tables, "the rolled-back copy was left behind"
        assert {row["id"] for row in conn.execute("SELECT id FROM cycles")} == {
            f"cycle-{index}" for index in range(4)
        }
    finally:
        conn.close()


def test_a_repair_attempt_a_human_committed_can_be_run_again(fresh_db):
    """Following the advice used to wedge the repair permanently, on one connection.

    The failed copy leaves `CREATE TABLE cycles_rebuilt` pending in an open
    transaction. The next thing the message asks for is clearing the half-stops
    — and the `COMMIT` that persists *that* persists the orphaned scratch table
    with it, so every later attempt died on "table cycles_rebuilt already
    exists" and the message named no way out. Data was safe; the repair was not
    runnable, forever.

    `DROP TABLE IF EXISTS cycles_rebuilt` leads the script for this, and the
    parked copy the attempt also left behind is reported rather than silently
    reused. Both steps are taken here on one connection, in the order the
    messages give them.
    """
    _four_cycles_and_a_half_stop(fresh_db)
    console = sqlite3.connect(fresh_db, isolation_level=None)
    try:
        for statement in _statements(db.CYCLE_STOP_CHECK_REBUILD_SQL):
            try:
                console.execute(statement)
            except sqlite3.IntegrityError:
                break
        # The human now does what the refusal asked and commits it, which is
        # what used to commit the wreckage of the attempt along with it.
        console.execute(db.CYCLE_STOP_CLEAR_HALF_STOP_SQL)
        console.execute("COMMIT")
        assert {row[0] for row in console.execute("SELECT name FROM sqlite_master")} >= {
            "cycles_rebuilt",
            db.CYCLES_PARKED_TABLE,
        }, "this test is not reproducing the wedge it is about"
    finally:
        console.close()

    conn = db.connect()
    try:
        _drop_the_parked_copy(conn)
        with pytest.raises(db.SchemaConsistencyError) as refused:
            db.init_db(conn)
        script = _script_from_message(
            str(refused.value), "PRAGMA foreign_keys=off;", "PRAGMA foreign_keys=on;"
        )
        conn.executescript(script)
        conn.commit()
        _drop_the_parked_copy(conn)
        assert db.init_db(conn) == []
        assert {row["id"] for row in conn.execute("SELECT id FROM cycles")} == {
            f"cycle-{index}" for index in range(4)
        }
    finally:
        conn.close()


def _every_statement_a_refusal_can_print():
    """Every SQL statement in `db`, by the naming convention they all share.

    Collected from the module rather than listed here on purpose: a statement
    added to a refusal next year is one nobody will remember to add to a list,
    and the property below is about all of them.
    """
    statements = {}
    for name, value in vars(db).items():
        if not name.endswith("_SQL"):
            continue
        if isinstance(value, str):
            statements[name] = value
        elif isinstance(value, tuple):
            statements.update({f"{name}[{key}]": sql for key, sql in value})
    return statements


def test_every_statement_a_refusal_prints_is_narrower_than_a_terminal():
    """SQL a human must paste verbatim must not be something a renderer re-wraps.

    The refusal reaches a human through rich, which re-wraps any line wider than
    the window it is drawn in. That is harmless for almost all SQL — SQL does
    not care where its whitespace falls — and it is not harmless for this SQL,
    because the CHECK these repairs put back is a **named** constraint and the
    name has spaces in it. At a terminal 90 columns wide, `CONSTRAINT "a stop
    records who asked and when, or neither"` was broken across two lines; the
    paste ran without error, kept every row, installed a working constraint, and
    left `nodum init` refusing with the identical message, because the stored
    name now had a newline in it. 60 of the 141 widths between 60 and 200 did
    that. Width 80 did not, and a non-tty pipe gets 80 — which is why nothing
    ever saw it.

    A renderer only re-wraps a line that does not fit, so every statement is
    written pre-wrapped narrower than any terminal anyone runs. This is the
    property, checked on the statements themselves; the test below checks it
    through the renderer.
    """
    statements = _every_statement_a_refusal_can_print()
    assert "CYCLE_STOP_CHECK_REBUILD_SQL" in statements, "the collector stopped collecting"
    too_wide = {
        name: max(len(line) for line in sql.splitlines())
        for name, sql in statements.items()
        if any(len(line) > db._SQL_WIDTH for line in sql.splitlines())
    }
    assert too_wide == {}, (
        f"a terminal narrower than these re-wraps them: {too_wide} — and a line break inside "
        f"the quoted constraint name is a repair that runs and does not satisfy the check"
    )
    # And the width is one no terminal is under: 58 columns is narrower than
    # the 80 a pipe reports and narrower than any window a person works in.
    assert db._SQL_WIDTH <= 58


def _rendered_at(width, error, *, with_frames=False):
    """What a terminal `width` columns wide shows for `error`.

    rich is what draws the traceback typer prints for an unhandled exception —
    it is a hard dependency of typer, so this is the channel every install
    delivers the refusal over, not one of several.

    The frames are off by default because they are two-thirds of a second per
    render to syntax-highlight and none of them is what wraps; the sweep below
    turns them on once, at a width in the range that used to break, to show the
    two renderings agree about the part being asserted on.
    """
    from rich.console import Console
    from rich.traceback import Traceback

    console = Console(file=io.StringIO(), width=width, force_terminal=False, no_color=True)
    frames = error.__traceback__ if with_frames else None
    console.print(Traceback.from_exception(type(error), error, frames))
    return console.file.getvalue()


def _repair_out_of(text):
    """The rebuild as it appears in rendered `text`, or None if it is not there."""
    lines = [line.rstrip() for line in text.splitlines()]
    if "PRAGMA foreign_keys=off;" not in lines or "PRAGMA foreign_keys=on;" not in lines:
        return None
    start = lines.index("PRAGMA foreign_keys=off;")
    end = len(lines) - 1 - lines[::-1].index("PRAGMA foreign_keys=on;")
    return "\n".join(lines[start : end + 1])


def test_the_repair_survives_the_renderer_it_ships_through_at_every_width(fresh_db):
    """Pin the mangle where it happened: in the terminal, not in the constant.

    Both repair tests here run `db.CYCLE_STOP_CHECK_REBUILD_SQL` directly, so
    they pass whatever the human actually sees. This one takes the SQL back out
    of the rendered refusal, at every width from 58 to 200, and requires it to
    be byte-identical to the statement the module meant to give — and then runs
    one of those pastes, a statement at a time, against a real drifted file.
    """
    conn = db.connect()
    try:
        _stop_columns_without_the_check(conn)
        _insert_cycle(conn, "kept", "curative", status="completed")
        conn.commit()
        with pytest.raises(db.SchemaConsistencyError) as refused:
            db.init_db(conn)
    finally:
        conn.close()

    mangled = [
        width
        for width in range(58, 201)
        if _repair_out_of(_rendered_at(width, refused.value)) != db.CYCLE_STOP_CHECK_REBUILD_SQL
    ]
    assert mangled == [], f"the terminal changed the repair at {len(mangled)} widths: {mangled[:5]}"
    # The full rendering, frames and all, says the same thing at a width that
    # used to break — so the sweep above is measuring the real channel.
    paste = _repair_out_of(_rendered_at(61, refused.value, with_frames=True))
    assert paste == db.CYCLE_STOP_CHECK_REBUILD_SQL

    # And that paste is a repair, run the way a console runs it.
    errors = _run_as_a_console_does(fresh_db, paste, stop_at_the_first_error=False)
    assert errors == []
    conn = db.connect()
    try:
        _drop_the_parked_copy(conn)
        assert db.init_db(conn) == []
        assert [row["id"] for row in conn.execute("SELECT id FROM cycles")] == ["kept"]
    finally:
        conn.close()


def test_a_constraint_name_a_renderer_already_broke_still_answers_to_the_name(fresh_db):
    """The files that were mangled before the emitter was fixed must not be stuck.

    A repair pasted out of a terminal too narrow for it installs the constraint
    under a name with a newline inside it. It enforces the rule — SQLite is
    indifferent to whitespace inside a quoted identifier — and an exact
    substring search does not find it, so `nodum init` refused, with the
    identical message, *after* its owner had done everything the message asked.
    That is the worst refusal this check can produce, and pre-wrapping the
    statement does nothing for the databases where it already happened.

    So the search is by words, not by whitespace, and this is the file it is for.
    """
    conn = db.connect()
    try:
        _stop_columns_without_the_check(conn)
        _insert_cycle(conn, "kept", "curative", status="completed")
        conn.commit()
        # Exactly what a 50-column terminal hands back: a break between two
        # words of the name, inside the quotes.
        conn.executescript(
            db.CYCLE_STOP_CHECK_REBUILD_SQL.replace(
                "who asked and when, or neither", "who asked and when, or\nneither"
            )
        )
        conn.execute(f"DROP TABLE {db.CYCLES_PARKED_TABLE}")
        conn.commit()

        stored = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cycles'"
        ).fetchone()["sql"]
        assert db.CYCLE_STOP_CHECK_NAME not in stored, "the fixture is not the mangle it claims"
        # The rule is enforced, under a name spelled with a newline...
        assert _half_stop_is_storable(conn, "half") is False
        # ...and the file passes, instead of being refused forever.
        assert db._cycle_stop_problems(conn) == []
        assert db.init_db(conn) == []
        assert {row["id"] for row in conn.execute("SELECT id FROM cycles")} == {"kept", "half"}
    finally:
        conn.close()


# ── 0016: the conventions space and the annotations table ─────────────────────


def _at_0015(tmp_path, monkeypatch, filename):
    """Build a populated database stopped at ``0015``, and return its path."""
    path = tmp_path / filename
    monkeypatch.setenv("NODUM_DB", str(path))
    monkeypatch.setattr(db, "MIGRATIONS", _prefix_through("0015_cycle_stop_switch"))
    service.init()
    return path


def test_0016_applies_to_a_populated_database_already_at_0015(tmp_path, monkeypatch):
    """The upgrade path, not just the fresh-file one: 0015 is where v0.8 users are."""
    _at_0015(tmp_path, monkeypatch, "at0015.db")
    node = service.create_node(type="note", title="before the upgrade", principal=owner())

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        assert db.init_db(conn) == ["0016_conventions_and_annotations"]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

    # The conventions space resolves, and the gardener holds `edit` on it — the
    # grant that makes the workspace actually writeable by the cycle.
    assert service.resolve_space_id("conventions", principal=owner()) == "conventions"
    assert auth.internal_principal().level_on("conventions") == EDIT
    # And the graph the upgrade ran over is untouched.
    assert service.get_node(node.id, principal=owner()).title == "before the upgrade"


def test_0016_refuses_to_upgrade_when_a_space_named_conventions_exists(tmp_path, monkeypatch):
    """The reserved-name guard refuses the collision readably rather than as a bare UNIQUE error."""
    _at_0015(tmp_path, monkeypatch, "taken-space.db")
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO nodes (id, space_id, type_id, title, created_by)"
            " VALUES ('user-space', 'meta', 'space', 'conventions', 'human:owner')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="conventions") as refusal:
            db.init_db(conn)
        assert "rename or remove it" in str(refusal.value)
        # Refused, not half-done: the migration's row is not recorded.
        assert "0016_conventions_and_annotations" not in db.applied_migrations(conn)
    finally:
        conn.close()


def test_0016_refuses_to_upgrade_when_a_node_id_conventions_exists(tmp_path, monkeypatch):
    """A raw node whose *id* is `conventions` collides on the primary key instead."""
    _at_0015(tmp_path, monkeypatch, "taken-id.db")
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO nodes (id, space_id, type_id, title, created_by)"
            " VALUES ('conventions', 'main', 'note', 'my notes', 'human:owner')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="conventions"):
            db.init_db(conn)
        assert "0016_conventions_and_annotations" not in db.applied_migrations(conn)
    finally:
        conn.close()


def test_annotations_are_an_exclusive_arc(fresh_db):
    """One annotation per queue item, targeting exactly one of the three tables.

    The exclusive arc is this schema's own idiom (`url_tokens` before it):
    three typed nullable target columns, a CHECK that exactly one is non-null,
    and a partial unique index per column so re-annotating on a later cycle
    replaces rather than accumulates.
    """
    node = service.create_node(type="note", title="target", principal=owner())
    version_id = 41  # an explicit id, as the lookup keys on INTEGER versions.id
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO versions (id, node_id, title, content, props, actor, event_seq)"
            " VALUES (?, ?, 'v', 'c', '{}', 'human:owner', 999)",
            (version_id, node.id),
        )
        base = (
            "INSERT INTO annotations (id, target_node_id, target_edge_id,"
            " target_version_id, body, actor)"
        )
        conn.execute(
            f"{base} VALUES ('a1', ?, NULL, NULL, '{{\"rate\": 0.9}}', 'agent:builtin-gardener')",
            (node.id,),
        )
        # (a) a row with no target is refused by the CHECK...
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                f"{base} VALUES ('a2', NULL, NULL, NULL, '{{}}', 'agent:builtin-gardener')"
            )
        # (b) ...and so is one naming two targets.
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                f"{base} VALUES ('a3', ?, NULL, ?, '{{}}', 'agent:builtin-gardener')",
                (node.id, version_id),
            )
        # (c) the version column is INTEGER: the row is found by the number and
        # by its string spelling alike.
        conn.execute(
            f"{base} VALUES ('a4', NULL, NULL, ?, '{{\"rate\": 0.5}}', 'agent:builtin-gardener')",
            (version_id,),
        )
        for probe in (version_id, str(version_id)):
            row = conn.execute(
                "SELECT body FROM annotations WHERE target_version_id = ?", (probe,)
            ).fetchone()
            assert row is not None
        # (d) the partial unique index refuses a second annotation on the same target.
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                f"{base} VALUES ('a5', ?, NULL, NULL, '{{}}', 'agent:builtin-gardener')",
                (node.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                f"{base} VALUES ('a6', NULL, NULL, ?, '{{}}', 'agent:builtin-gardener')",
                (version_id,),
            )
        # (e) the foreign keys cascade: an undone version and an undone node take
        # their annotations with them.
        conn.execute("DELETE FROM versions WHERE node_id = ?", (node.id,))
        conn.execute("DELETE FROM nodes WHERE id = ?", (node.id,))
        conn.commit()
        assert conn.execute("SELECT 1 FROM annotations WHERE id = 'a1'").fetchone() is None
        assert conn.execute("SELECT 1 FROM annotations WHERE id = 'a4'").fetchone() is None
    finally:
        conn.close()


def test_deleting_an_edge_takes_its_annotation_with_it(fresh_db):
    """The exclusive arc's edge leg cascades like the node and version legs do."""
    src = service.create_node(type="note", title="src", principal=owner())
    dst = service.create_node(type="note", title="dst", principal=owner())
    edge = service.create_edge(src.id, dst.id, "mentions", principal=owner())
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO annotations (id, target_node_id, target_edge_id,"
            " target_version_id, body, actor)"
            " VALUES ('a7', NULL, ?, NULL, '{\"rate\": 0.8}',"
            " 'agent:builtin-gardener')",
            (edge.id,),
        )
        assert conn.execute("SELECT 1 FROM annotations WHERE id = 'a7'").fetchone() is not None
        conn.execute("DELETE FROM edges WHERE id = ?", (edge.id,))
        conn.commit()
        assert conn.execute("SELECT 1 FROM annotations WHERE id = 'a7'").fetchone() is None
    finally:
        conn.close()


def test_a_database_recorded_at_0016_without_the_table_is_refused(tmp_path, monkeypatch):
    """Every recorded migration with a checkable guarantee has a check (Q13 S6).

    The drift is the shape the check exists for: a file that records 0016 and
    lacks the table would die deep inside `list_proposals` with `no such table:
    annotations` the first time an item's annotation is looked up — nothing in
    the runtime catches it first. The refusal names the table and the statement
    that puts it back, in place rather than by deleting the graph.
    """
    _at_0015(tmp_path, monkeypatch, "stale.db")
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ('0016_conventions_and_annotations')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        with pytest.raises(db.SchemaConsistencyError, match="annotations") as refused:
            db.init_db(conn)
        message = str(refused.value)
        assert "delete the database file" not in message, "it told a human to bin their graph"
        assert db.ANNOTATIONS_TABLE_SQL in message
    finally:
        conn.close()


def test_a_database_recorded_at_0016_without_the_conventions_space_is_refused(
    tmp_path, monkeypatch
):
    """The space-node half of the write-seam check is pinned, not just the table.

    The table test proves the table check fires; this proves the space check
    fires on its own — the table and the grant both exist, only the space node
    is gone. A database recording 0016 without it would fail a gardener job
    scoped to `conventions` with `space not found`.
    """
    _at_0015(tmp_path, monkeypatch, "stale.db")
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ('0016_conventions_and_annotations')"
        )
        conn.executescript(db.ANNOTATIONS_TABLE_SQL)
        conn.execute(db.CONVENTIONS_SPACE_SQL)
        conn.execute(db.CONVENTIONS_GRANT_SQL)
        conn.commit()
        # The grant's `space_id` foreign key forbids the delete under normal
        # enforcement — the drift is exactly what a writer with FKs off leaves
        # behind, so take it back off for the delete.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM nodes WHERE id = ?", (db.CONVENTIONS_SPACE_ID,))
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        with pytest.raises(db.SchemaConsistencyError, match="conventions") as refused:
            db.init_db(conn)
        message = str(refused.value)
        # The migration's own name carries "annotations", so pin the *table
        # check* by its phrasing: it must not have fired — the table exists.
        assert "table 'annotations' is missing" not in message
        assert db.CONVENTIONS_SPACE_SQL in message
    finally:
        conn.close()


def test_a_database_recorded_at_0016_without_the_gardener_grant_is_refused(tmp_path, monkeypatch):
    """The grant-row half of the write-seam check is pinned, not just the table.

    Table and space node both exist; only the gardener's `edit` row on
    `conventions` is gone. A database recording 0016 without it would silently
    land the gardener's conventions writes `proposed` instead of the workspace
    they were designed for.
    """
    _at_0015(tmp_path, monkeypatch, "stale.db")
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ('0016_conventions_and_annotations')"
        )
        conn.executescript(db.ANNOTATIONS_TABLE_SQL)
        conn.execute(db.CONVENTIONS_SPACE_SQL)
        conn.execute(
            "DELETE FROM grants WHERE agent_id = ? AND space_id = ?",
            (db.GARDENER_AGENT_ID, db.CONVENTIONS_SPACE_ID),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    conn = db.connect()
    try:
        with pytest.raises(db.SchemaConsistencyError, match="edit") as refused:
            db.init_db(conn)
        message = str(refused.value)
        assert "conventions" in message
        assert db.CONVENTIONS_GRANT_SQL in message
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
