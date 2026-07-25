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

    Args:
        conn: The open connection.
        name: The migration's name, recorded in ``schema_migrations``.
        sql: The migration script (no transaction control of its own).

    Raises:
        ValueError: If ``name`` is not a plain ``[0-9a-z_]`` identifier — it is
            inlined into the script, which takes no parameters.
        sqlite3.Error: Whatever the script raised, after the rollback.
    """
    if not MIGRATION_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid migration name {name!r}: expected {MIGRATION_NAME_RE.pattern}")
    try:
        conn.executescript(
            f"BEGIN;\n{sql}\nINSERT INTO schema_migrations (name) VALUES ('{name}');\nCOMMIT;"
        )
    except Exception:
        conn.rollback()
        raise


def _verify_schema_consistency(conn: sqlite3.Connection) -> None:
    """Refuse a database whose live schema contradicts its recorded migrations.

    :func:`init_db` skips any migration whose name is already recorded, so a
    database that applied a since-consolidated version of a migration keeps its
    stale schema. The only such case in the shipped history is
    ``0007_assets_and_renditions``: if the name is recorded, the assets store
    must be the consolidated in-database one — ``asset_blobs`` present, and
    ``renditions`` carrying ``data`` rather than the old filesystem ``path``.

    Raises:
        SchemaConsistencyError: If ``0007_assets_and_renditions`` is recorded
            but the live schema is the pre-consolidation path-based one.
    """
    if "0007_assets_and_renditions" not in set(applied_migrations(conn)):
        return
    tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    rendition_columns = {row["name"] for row in conn.execute("PRAGMA table_info(renditions)")}
    problems: list[str] = []
    if "asset_blobs" not in tables:
        problems.append("table 'asset_blobs' is missing")
    if "data" not in rendition_columns:
        problems.append("table 'renditions' has no 'data' column")
    if "path" in rendition_columns:
        problems.append("table 'renditions' still carries a filesystem 'path' column")
    if problems:
        raise SchemaConsistencyError(
            "database schema is inconsistent with its recorded migrations: "
            + "; ".join(problems)
            + ". This database predates the consolidation of the assets tables into "
            "migration 0007 (bytes now live in 'asset_blobs' and 'renditions.data', never "
            "on a filesystem path); it cannot be auto-migrated — delete the database file "
            "and re-run 'nodum init' to recreate it."
        )
    _verify_spaces_consistency(conn)


def _verify_spaces_consistency(conn: sqlite3.Connection) -> None:
    """Refuse a database whose 0009 record contradicts its live schema.

    If ``0009_spaces_and_type_nodes`` is recorded, the type catalogs must be
    gone and nodes must carry ``space_id`` — a database that applied an
    intermediate version of the spaces migration keeps the name and skips the
    fix forever otherwise.

    Raises:
        SchemaConsistencyError: If the name is recorded but the live schema
            still has the type tables or lacks ``nodes.space_id``.
    """
    if "0009_spaces_and_type_nodes" not in set(applied_migrations(conn)):
        return
    tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    node_columns = {row["name"] for row in conn.execute("PRAGMA table_info(nodes)")}
    problems: list[str] = []
    if "types" in tables or "edge_types" in tables:
        problems.append("type catalog tables still present")
    if "space_id" not in node_columns:
        problems.append("table 'nodes' has no 'space_id' column")
    if problems:
        raise SchemaConsistencyError(
            "database schema is inconsistent with its recorded migrations: "
            + "; ".join(problems)
            + ". This database predates the final shape of migration 0009 "
            "(spaces and types-as-nodes); it cannot be auto-migrated — delete "
            "the database file and re-run 'nodum init' to recreate it."
        )


def init_db(conn: sqlite3.Connection) -> list[str]:
    """Apply any pending migrations; return the names applied in this call.

    Idempotent: a fully migrated database applies nothing and returns ``[]``.
    Each migration is applied atomically (:func:`apply_migration`), so a
    failure leaves the database exactly as the last successful migration left
    it. After applying, the live schema is checked against the recorded
    migrations (:func:`_verify_schema_consistency`) so a database that carries
    a since-consolidated migration name fails loudly here rather than deep in a
    later call.

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
    applied: list[str] = []
    already = set(applied_migrations(conn))
    for name, sql in MIGRATIONS:
        if name in already:
            continue
        apply_migration(conn, name, sql)
        applied.append(name)
    _verify_schema_consistency(conn)
    return applied
