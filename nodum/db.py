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
    if not MIGRATION_NAME_RE.match(name):
        raise ValueError(f"invalid migration name {name!r}: expected {MIGRATION_NAME_RE.pattern}")
    try:
        conn.executescript(
            f"BEGIN;\n{sql}\nINSERT INTO schema_migrations (name) VALUES ('{name}');\nCOMMIT;"
        )
    except Exception:
        conn.rollback()
        raise


def init_db(conn: sqlite3.Connection) -> list[str]:
    """Apply any pending migrations; return the names applied in this call.

    Idempotent: a fully migrated database applies nothing and returns ``[]``.
    Each migration is applied atomically (:func:`apply_migration`), so a
    failure leaves the database exactly as the last successful migration left
    it.
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
    return applied
