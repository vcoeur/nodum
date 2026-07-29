"""SQLite connection management and the append-only migration runner."""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

import sqlite_vec

from nodum.migrations import MIGRATIONS

#: Environment variable overriding the database path.
ENV_DB_VAR = "NODUM_DB"

#: Shape a migration name must have. ``executescript`` takes no parameters, so
#: the name is inlined into the transaction script and is checked first.
MIGRATION_NAME_RE = re.compile(r"^[0-9a-z_]+$")

#: Default database location when ``NODUM_DB`` is not set.
DEFAULT_DB_PATH = Path("~/.local/share/nodum/nodum.db").expanduser()


class SchemaConsistencyError(RuntimeError):
    """Raised when the live schema contradicts the recorded migrations.

    :func:`init_db` keys purely on migration *name*, so a database that applied
    an older, since-consolidated version of a migration keeps that migration's
    name in ``schema_migrations`` — the name matches, the schema does not, and
    the migration is silently skipped forever. The one such case in the shipped
    history is ``0007_assets_and_renditions``: a dev database built from an
    intermediate branch commit stored asset bytes on the filesystem
    (``renditions.path``, no ``asset_blobs``) before the assets tables were
    consolidated to in-database blobs. That drift would otherwise surface only
    much later, as ``no such table: asset_blobs`` deep inside ``register_asset``
    — this turns it into a loud failure at init time instead.
    """


def db_path() -> Path:
    """Return the configured database path (``NODUM_DB`` or the default)."""
    raw = os.environ.get(ENV_DB_VAR)
    return Path(raw).expanduser() if raw else DEFAULT_DB_PATH


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection to the graph database in WAL mode.

    Args:
        path: Explicit database path; defaults to :func:`db_path`. The parent
            directory is created if needed. Use ``":memory:"`` for tests.

    Returns:
        A connection with row access by column name, an 8 KiB page size, WAL
        journaling, foreign-key enforcement enabled, and the sqlite-vec
        extension loaded (the ``node_vec`` vec0 table and its KNN queries
        need it).
    """
    db_file = Path(path).expanduser() if path is not None else db_path()
    if str(db_file) != ":memory:":
        db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    # Asset bytes live in this file, and sqlite.org's blob benchmarks put peak
    # blob I/O at 8-16 KiB pages. This only takes effect on an empty database
    # and is silently ignored once WAL is on, so it must precede the WAL pragma
    # — on an existing database the page size is already fixed.
    conn.execute("PRAGMA page_size=8192")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def applied_migrations(conn: sqlite3.Connection) -> list[str]:
    """Return the names of migrations already applied, in application order."""
    rows = conn.execute("SELECT name FROM schema_migrations ORDER BY rowid").fetchall()
    return [row["name"] for row in rows]


def apply_migration(conn: sqlite3.Connection, name: str, sql: str) -> None:
    """Run one migration's script and record it, as a single transaction.

    The script and its ``schema_migrations`` row commit together or not at
    all: an interruption partway through (a crash, a full disk, a statement
    that fails) rolls the whole thing back, so the next run retries the
    migration against a clean schema instead of hitting "table … already
    exists" forever on a half-applied one.

    Foreign-key **enforcement is off for the duration**, and the whole
    database is checked with ``PRAGMA foreign_key_check`` before the commit
    — SQLite's own recipe for the create-copy-drop-rename table rebuild that
    0009 performs. Deferring the constraints instead (what 0009 originally
    asked for) does not work on a populated database: dropping a parent table
    counts every referencing row as an outstanding deferred violation, and
    renaming the replacement in does not clear that counter, so ``COMMIT``
    fails with a bare "FOREIGN KEY constraint failed" even though the
    resulting schema is sound. One node plus its version row was enough
    (Q13 review B5) — an empty database, which is all the suite had, was not.

    The script runs through ``executescript``, whose leading implicit commit
    would silently commit any transaction the caller left open — so the
    caller must not have one (asserted below).

    Args:
        conn: The open connection, with no transaction in flight.
        name: The migration's name, recorded in ``schema_migrations``.
        sql: The migration script (no transaction control of its own).

    Raises:
        ValueError: If ``name`` is not a plain ``[0-9a-z_]`` identifier.
        RuntimeError: If the connection has an open transaction.
        sqlite3.IntegrityError: If the migrated database has dangling
            references, naming them, after the rollback.
        sqlite3.Error: Whatever the script raised, after the rollback.
    """
    if not MIGRATION_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid migration name {name!r}: expected {MIGRATION_NAME_RE.pattern}")
    if conn.in_transaction:
        raise RuntimeError(
            f"cannot apply migration {name!r} with a transaction in flight: "
            "executescript would commit it as a side effect"
        )
    # Both pragmas are no-ops inside a transaction, hence out here.
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(f"BEGIN;\n{sql}")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            tables = sorted({row[0] for row in violations})
            raise sqlite3.IntegrityError(
                f"migration {name!r} leaves dangling references in: {', '.join(tables)}"
            )
        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (name,))
        conn.execute("COMMIT")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _tables(conn: sqlite3.Connection) -> set[str]:
    """Every table name in the live schema."""
    return {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Every column name of ``table`` (empty when the table is absent)."""
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


#: The remedy for drift that cannot be repaired in place. Four of the five
#: checks below are missing *tables and columns*, or rows that predate a
#: rewrite: nothing can derive them from what the file holds, so recreating it
#: is genuinely the only cure.
_RECREATE_THE_FILE = (
    "This database predates the final shape of that migration; it cannot be "
    "auto-migrated — delete the database file and re-run 'nodum init' to recreate it."
)

#: ``0014``'s one-running-consolidation index, as the statement that creates it.
#: Kept next to the check that looks for it so the refusal can print the cure
#: rather than a shape of the cure.
CYCLES_RUNNING_INDEX_SQL = (
    "CREATE UNIQUE INDEX idx_cycles_one_running_consolidation ON cycles(status) "
    "WHERE status = 'running' AND trigger IN ('manual', 'scheduled');"
)

#: The remedy for the fifth check, and the reason each check carries its own.
#: A missing index is derived state — every row it constrains is already in the
#: file — so one statement repairs it, and telling a human to delete their graph
#: over it is the wrong instruction by an enormous margin.
_CREATE_THE_CYCLES_INDEX = (
    "Nothing else about that migration is missing and no data is lost: the index "
    "constrains rows the file already has, so it can be created in place. Run this "
    f"once against the database: {CYCLES_RUNNING_INDEX_SQL} If it fails, two "
    "consolidation cycles are recorded 'running' at once — close the stale one with "
    "'nodum cycle-abandon <id>' first."
)

#: ``0015``'s two stop-switch columns, in the order they must be added: the
#: second one's CHECK names the first, so adding them the other way round fails
#: on an unknown column. Kept next to the check that looks for them, exactly as
#: :data:`CYCLES_RUNNING_INDEX_SQL` is, so a refusal prints the cure rather than
#: a shape of the cure — and the refusal names only the columns it actually
#: found missing, since ``ADD COLUMN`` has no ``IF NOT EXISTS`` and re-adding a
#: column that is already there fails.
CYCLE_STOP_COLUMN_SQL: tuple[tuple[str, str], ...] = (
    ("stop_requested_at", "ALTER TABLE cycles ADD COLUMN stop_requested_at TEXT;"),
    (
        "stop_requested_by",
        'ALTER TABLE cycles ADD COLUMN stop_requested_by TEXT CONSTRAINT "a stop records '
        'who asked and when, or neither" CHECK ((stop_requested_by IS NULL) = '
        "(stop_requested_at IS NULL));",
    ),
)

#: The remedy for ``0015``'s drift, and it is the *fifth* check's kind rather
#: than the first four's: both columns are pure additions with no back-fill —
#: every row that predates them is a cycle nobody asked to stop, which is what
#: two NULLs say — so nothing is lost and nothing has to be derived. Telling a
#: human to delete their graph over a missing column with a constant default
#: would be as wrong here as it was for the missing index.
_ADD_THE_STOP_COLUMNS = (
    "Nothing else about that migration is missing and no data is lost: the columns "
    "are additions with no back-fill, so they can be added in place. Run the "
    "statement each problem above names, in the order printed — the second column's "
    "CHECK names the first."
)


def _verify_schema_consistency(conn: sqlite3.Connection) -> None:
    """Refuse a database whose live schema contradicts its recorded migrations.

    :func:`init_db` skips any migration whose name is already recorded, so a
    database that applied a since-consolidated version of a migration keeps
    its stale schema — the name matches, the schema does not, and the fix is
    skipped forever. Each check below states what its migration's name
    *guarantees*, and every recorded migration with a checkable guarantee has
    one (Q13 review S6): drift in a later migration used to pass init and
    surface much later as a missing table deep inside a write.

    **The remedy is per check, not shared.** It was one sentence for all of them
    — "delete the database file and re-run 'nodum init'" — which is true of the
    first four and wildly disproportionate for the fifth: a missing index is
    derived state, repairable by one ``CREATE UNIQUE INDEX``, and a refusal that
    reads as *your graph is unrecoverable* over it would cost a human every node
    they own. A refusal names what to do about the thing it found.

    Raises:
        SchemaConsistencyError: If any recorded migration's guarantee does not
            hold, naming every problem found, the migration to blame, and the
            cure for that migration.
    """
    recorded = set(applied_migrations(conn))
    for name, check, remedy in (
        ("0007_assets_and_renditions", _assets_problems, _RECREATE_THE_FILE),
        ("0009_spaces_and_type_nodes", _spaces_problems, _RECREATE_THE_FILE),
        ("0010_principals", _principals_problems, _RECREATE_THE_FILE),
        ("0011_actor_strings", _actor_string_problems, _RECREATE_THE_FILE),
        ("0014_cycles_and_gardener", _cycles_problems, _CREATE_THE_CYCLES_INDEX),
        ("0015_cycle_stop_switch", _cycle_stop_problems, _ADD_THE_STOP_COLUMNS),
    ):
        if name not in recorded:
            continue
        problems = check(conn)
        if problems:
            raise SchemaConsistencyError(
                f"database schema is inconsistent with its recorded migrations "
                f"({name}): " + "; ".join(problems) + ". " + remedy
            )


def _assets_problems(conn: sqlite3.Connection) -> list[str]:
    """0007 guarantees the consolidated in-database assets store.

    A dev database built from an intermediate branch commit stored asset bytes
    on the filesystem (``renditions.path``, no ``asset_blobs``).
    """
    tables, rendition_columns = _tables(conn), _columns(conn, "renditions")
    problems: list[str] = []
    if "asset_blobs" not in tables:
        problems.append("table 'asset_blobs' is missing (asset bytes live in the database)")
    if "data" not in rendition_columns:
        problems.append("table 'renditions' has no 'data' column")
    if "path" in rendition_columns:
        problems.append("table 'renditions' still carries a filesystem 'path' column")
    return problems


def _spaces_problems(conn: sqlite3.Connection) -> list[str]:
    """0009 guarantees types-are-nodes: no catalogs, ``space_id``, bootstrap ids."""
    tables, node_columns = _tables(conn), _columns(conn, "nodes")
    problems: list[str] = []
    if "types" in tables or "edge_types" in tables:
        problems.append("type catalog tables still present")
    if "space_id" not in node_columns:
        problems.append("table 'nodes' has no 'space_id' column")
        return problems  # the bootstrap check below would fail on the column
    present = {
        row["id"]
        for row in conn.execute("SELECT id FROM nodes WHERE id IN ('type','space','meta','main')")
    }
    missing = sorted({"type", "space", "meta", "main"} - present)
    if missing:
        problems.append(f"bootstrap nodes missing: {', '.join(missing)}")
    return problems


def _principals_problems(conn: sqlite3.Connection) -> list[str]:
    """0010 guarantees the principal tables exist and the policy layer is gone."""
    tables = _tables(conn)
    problems = [
        f"table {table!r} is missing"
        for table in ("humans", "agents", "grants", "sessions")
        if table not in tables
    ]
    if "policies" in tables:
        problems.append("table 'policies' still present (grants replaced it)")
    return problems


def _actor_string_problems(conn: sqlite3.Connection) -> list[str]:
    """0011 guarantees no bare ``human`` actor survives in the log."""
    problems: list[str] = []
    for table in ("events", "versions"):
        row = conn.execute(f"SELECT 1 FROM {table} WHERE actor = 'human' LIMIT 1").fetchone()
        if row is not None:
            problems.append(f"table {table!r} still carries unstructured 'human' actors")
    return problems


def _cycles_problems(conn: sqlite3.Connection) -> list[str]:
    """0014 guarantees the one-running-consolidation index that serialises runs.

    The index is the cross-process lock: without it two ``nodum consolidate``
    runs both open a cycle and every duplicate pair is proposed twice. ``0014``
    was amended in place while it was still unreleased, so a database built from
    its first cut carries the recorded name and not the index — and
    :func:`init_db` skips a migration whose name it already has, so nothing else
    would ever notice.
    """
    indexes = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    if "idx_cycles_one_running_consolidation" not in indexes:
        return ["index 'idx_cycles_one_running_consolidation' is missing (two runs could overlap)"]
    return []


def _cycle_stop_problems(conn: sqlite3.Connection) -> list[str]:
    """0015 guarantees the two columns the kill switch is written in.

    Every recorded migration with a checkable guarantee has an entry here (Q13
    review S6), and this one's guarantee is as checkable as they come: two
    columns on ``cycles``, or the switch has nothing to write to. Without the
    entry the drift would surface where the four earlier ones used to — deep
    inside a write, as ``no such column: stop_requested_at`` from
    :func:`nodum.service.request_stop`, on the run a human was trying to stop.

    The route in is 0014's, not a hypothetical: ``init_db`` skips a migration
    whose name it already holds, so a database built from an earlier cut of
    ``0015`` — the three-column shape ``cycle_stop_check``'s docstring proposes,
    say — carries the recorded name and not the columns, and nothing else would
    ever notice. ``stop_switch_available()`` gates on the *service function*
    existing, which it does on any install carrying this code, so it would read
    ``armed`` on a database that cannot store a stop.
    """
    columns = _columns(conn, "cycles")
    return [
        f"table 'cycles' has no {column!r} column (a stop has nowhere to be "
        f"recorded) — repair: {sql}"
        for column, sql in CYCLE_STOP_COLUMN_SQL
        if column not in columns
    ]


def init_db(conn: sqlite3.Connection) -> list[str]:
    """Apply any pending migrations; return the names applied in this call.

    Idempotent: a fully migrated database applies nothing and returns ``[]``.
    Each migration is applied atomically (:func:`apply_migration`), so a
    failure leaves the database exactly as the last successful migration left
    it.

    The live schema is checked against the recorded migrations
    (:func:`_verify_schema_consistency`) **before** anything is applied (Q13
    review S5): a database whose only cure is deletion must not have new —
    and sometimes irreversible, like 0010's ``DROP TABLE policies`` —
    migrations committed onto it on the way to being told so.

    Raises:
        SchemaConsistencyError: If the live schema contradicts the recorded
            migrations (see :func:`_verify_schema_consistency`).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name       TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    _verify_schema_consistency(conn)
    applied: list[str] = []
    already = set(applied_migrations(conn))
    for name, sql in MIGRATIONS:
        if name in already:
            continue
        apply_migration(conn, name, sql)
        applied.append(name)
    return applied
