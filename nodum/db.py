"""SQLite connection management and the append-only migration runner."""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

import sqlite_vec

from nodum.migrations import GARDENER_AGENT_ID, MIGRATIONS

#: Environment variable overriding the database path.
ENV_DB_VAR = "NODUM_DB"

#: Shape a migration name must have. ``executescript`` takes no parameters, so
#: the name is inlined into the transaction script and is checked first.
MIGRATION_NAME_RE = re.compile(r"^[0-9a-z_]+$")

#: Default database location when ``NODUM_DB`` is not set.
DEFAULT_DB_PATH = Path("~/.local/share/nodum/nodum.db").expanduser()

#: How long a connection waits for SQLite's single write lock before failing
#: with "database is locked".
#:
#: The 5 s default Python applies (the ``timeout`` argument to
#: ``sqlite3.connect``) is shorter than a big registration can hold the lock
#: for: :func:`nodum.assets.register_asset` deliberately streams a whole asset
#: in one write transaction, and the measured 200 MB → 1.22 s copy rate
#: extrapolates to ≈ 6 s of write-lock hold at the documented 1 GB ceiling
#: (``SQLITE_LIMIT_LENGTH``). 15 s is >2× headroom over that projected worst
#: case, so a writer that collides with a big registration waits it out
#: instead of failing — which is what the architecture doc's "Known
#: limitation" on registration (docs/architecture.md) now promises.
BUSY_TIMEOUT_MS = 15000


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
        A connection with row access by column name, a busy timeout of
        :data:`BUSY_TIMEOUT_MS` (see there for why it is not Python's 5 s
        default), an 8 KiB page size, WAL journaling, foreign-key enforcement
        enabled, and the sqlite-vec extension loaded (the ``node_vec`` vec0
        table and its KNN queries need it).
    """
    db_file = Path(path).expanduser() if path is not None else db_path()
    if str(db_file) != ":memory:":
        db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    # First pragma on purpose: ``busy_timeout`` is a silent no-op once a
    # transaction is open, and nothing before this point opens one.
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    # Asset bytes live in this file, and sqlite.org's blob benchmarks put peak
    # blob I/O at 8-16 KiB pages. This only takes effect on an empty database
    # and is silently ignored once WAL is on, so it must precede the WAL pragma
    # — on an existing database the page size is already fixed.
    conn.execute("PRAGMA page_size=8192")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def begin_immediate(conn: sqlite3.Connection) -> None:
    """Open an IMMEDIATE write transaction: no writer can interleave after it.

    A read-then-write service path (a review, a cycle close) must see the row
    it checks and the row it writes as one atomic fact, or two concurrent
    callers both pass the check and both write — two accepts of one proposal,
    two closes of one cycle. SQLite's own serialisation does not do this for
    free: with the driver's default ``isolation_level`` the connection runs
    each statement in its own implicit transaction, so the read and the write
    are separate lock acquisitions with an interleavable window between them.
    ``BEGIN IMMEDIATE`` takes the single write lock up front, so any other
    writer blocks until this transaction commits or rolls back, and a caller
    that reads a row here cannot race a caller that wrote it.

    **It must be the first statement on the connection.** A SELECT opens no
    implicit transaction, but the first DML does — and ``BEGIN IMMEDIATE``
    after any DML raises ``cannot start a transaction within a transaction``,
    while a DEFERRED transaction opened by an earlier statement would make
    this a no-op at best. :func:`connect` + :func:`init_db` leave a fresh
    connection with no transaction in flight, which is where every service
    caller starts.

    Args:
        conn: A connection with no transaction in flight.

    Raises:
        RuntimeError: If a transaction is already open on ``conn`` — the write
            lock would already be DEFERRED, and the immediate guarantee is the
            entire point of this function.
    """
    if conn.in_transaction:
        raise RuntimeError(
            "cannot BEGIN IMMEDIATE with a transaction already open on the connection: "
            "the write lock would already be DEFERRED"
        )
    conn.execute("BEGIN IMMEDIATE")


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


def foreign_keys_into(conn: sqlite3.Connection, table: str) -> frozenset[tuple[str, str]]:
    """The foreign keys other tables hold into ``table``, as ``(child, column)`` pairs.

    ``PRAGMA foreign_key_list`` answers the reverse question — the constraints a
    *child* table's own DDL declares, naming its parent — so every table in the
    schema is asked, and the rows that name ``table`` as their parent are kept.
    This is the enumeration a guard that must name every reference into
    ``nodes(id)`` walks: the delete-guard completeness test in ``test_rollback``
    asserts :func:`nodum.service._delete_blocker`'s coverage against it, so a
    migration that adds a foreign key into ``nodes`` fails that test on the
    commit that adds it — the honest way to keep a hand-written guard list from
    rotting.

    Args:
        conn: The open connection.
        table: The parent table whose referrers are wanted.

    Returns:
        One ``(child table, referencing column)`` pair per referencing column.
    """
    referrers: set[tuple[str, str]] = set()
    for child in _tables(conn):
        for row in conn.execute(f"PRAGMA foreign_key_list({child})").fetchall():
            if row["table"] == table:
                referrers.add((child, str(row["from"])))
    return frozenset(referrers)


#: ``--`` to end of line, and ``/* … */`` across lines. Stripped from stored DDL
#: before anything searches it: SQLite keeps a ``CREATE TABLE`` verbatim,
#: comments included, and a comment is text that looks like schema and
#: constrains nothing.
_SQL_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    """The ``CREATE TABLE`` text SQLite stored for ``table``, comments removed.

    ``PRAGMA table_info`` answers what the columns are and says nothing about
    the constraints over them, so a check about a CHECK has to read the schema
    SQLite kept — which is the statement as written, ``ALTER TABLE ADD COLUMN``
    clauses appended verbatim included.

    **As written includes its comments**, and that is a hole in any search over
    this text rather than a detail: the ``cycles`` DDL this repo ships is itself
    heavily commented, so a hand-edited or externally-migrated file can perfectly
    well name a constraint in a comment while carrying no constraint at all —
    and a search that matched it would report a sound schema over a file where
    the half-stop goes straight in. That is a check failing *open*, which is the
    direction that costs something here (failing noisily is a human reading a
    repair they did not need, and :func:`_cycle_stop_problems` will not print a
    rebuild that drops anything).

    **It is a lexer's job done with a regex, and it can be wrong both ways.** A
    ``--`` inside a string literal takes the rest of that line with it, hiding a
    constraint that is really there — a false alarm, the cheap direction. But
    removing text also *joins* what was on either side of it, so a string
    literal carrying the constraint's exact prose split by a ``/* */`` comes out
    of here spelling the name, and the check reads a constraint the table has
    not got. That needs the name, word for word, inside a literal in the
    ``cycles`` DDL with a comment through the middle of it — contrived enough to
    accept, not so contrived that the docstring gets to claim this can only fail
    safe.

    Returns:
        The stored statement with SQL comments removed, or ``""`` when the table
        is absent.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if row is None or row["sql"] is None:
        return ""
    return _SQL_COMMENT_RE.sub("", str(row["sql"]))


#: The remedy for drift that cannot be repaired in place. Four of the six
#: checks below are missing *tables and columns*, or rows that predate a
#: rewrite: nothing can derive them from what the file holds, so recreating it
#: is genuinely the only cure.
_RECREATE_THE_FILE = (
    "This database predates the final shape of that migration; it cannot be "
    "auto-migrated — delete the database file and re-run 'nodum init' to recreate it."
)

#: The column width every statement a refusal prints is written to.
#:
#: SQL a human is told to paste ships through whatever renders the refusal, and
#: in a terminal that is rich, which re-wraps any line wider than the window.
#: Re-wrapping SQL is *usually* harmless — SQL does not care where its
#: whitespace falls — but the CHECK these repairs carry is a **named** one and
#: the name has spaces in it, so a break inside ``"a stop records who asked and
#: when, or neither"`` stores a name with a newline in it: a constraint that
#: enforces the rule and does not answer to the name, which
#: :func:`_cycle_stop_problems` then goes on reporting forever. A renderer only
#: re-wraps a line that does not fit, so the statements are written pre-wrapped
#: narrower than any terminal anyone runs, and nothing has to be re-wrapped at
#: all. (The other half of that answer is in
#: :data:`_CYCLE_STOP_CHECK_NAME_RE`, which accepts a name a renderer already
#: broke — because a file repaired by a mangled paste is out there either way.)
_SQL_WIDTH = 58

#: ``0014``'s one-running-consolidation index, as the statement that creates it.
#: Kept next to the check that looks for it so the refusal can print the cure
#: rather than a shape of the cure, and pre-wrapped for :data:`_SQL_WIDTH`'s
#: reason.
CYCLES_RUNNING_INDEX_SQL = (
    "CREATE UNIQUE INDEX idx_cycles_one_running_consolidation\n"
    "  ON cycles(status)\n"
    "  WHERE status = 'running'\n"
    "    AND trigger IN ('manual', 'scheduled');"
)

#: The remedy for the fifth check, and the reason each check carries its own.
#: A missing index is derived state — every row it constrains is already in the
#: file — so one statement repairs it, and telling a human to delete their graph
#: over it is the wrong instruction by an enormous margin.
_CREATE_THE_CYCLES_INDEX = (
    "Nothing else about that migration is missing and no data is lost: the index "
    "constrains rows the file already has, so it can be created in place. Run this "
    f"once against the database:\n{CYCLES_RUNNING_INDEX_SQL}\nIf it fails, two "
    "consolidation cycles are recorded 'running' at once — close the stale one with "
    "'nodum cycle-abandon <id>' first."
)

#: The name ``0015`` gives the cross-column CHECK under its two stamps. SQLite
#: prints it verbatim (``CHECK constraint failed: <name>``) and stores it
#: verbatim in ``sqlite_master``, so one string is both the message a human
#: meets and what :func:`_cycle_stop_problems` looks for in the live schema.
CYCLE_STOP_CHECK_NAME = "a stop records who asked and when, or neither"

#: :data:`CYCLE_STOP_CHECK_NAME` as a pattern that accepts any run of whitespace
#: where the name has a space, which is what the check searches the stored DDL
#: with instead of the plain string.
#:
#: The name is 45 characters of prose with spaces in it, and it travels to the
#: human inside a repair statement rendered by a terminal — so a window narrower
#: than the line puts a newline inside the identifier, and the paste installs a
#: constraint that enforces the rule under a name spelled with a newline.
#: :data:`_SQL_WIDTH` is why that no longer happens; this is why the files where
#: it already did are not stuck. An exact-substring check refuses those forever,
#: with the identical message, *after* a repair that did everything it claimed —
#: which is the worst refusal this module can produce, because the advice was
#: followed and the file is sound. SQLite is indifferent to the whitespace, so
#: this is a check that had a stricter opinion about the schema than the engine
#: does.
#:
#: It does not loosen the narrowing in the direction that matters: the words and
#: their order are still matched exactly, and a run of whitespace cannot appear
#: between two of them by accident (see :func:`_cycle_stop_problems` for what
#: the search must not do).
_CYCLE_STOP_CHECK_NAME_RE = re.compile(
    r"\s+".join(re.escape(word) for word in CYCLE_STOP_CHECK_NAME.split())
)

#: That CHECK as SQL, written once and spliced into both statements that carry
#: it — the ``ADD COLUMN`` below and the rebuild that puts it back under columns
#: a drifted file already has. Two copies of a constraint are two constraints
#: the day one of them is edited. Its own line, and the name alone on it, for
#: :data:`_SQL_WIDTH`'s reason: this is the one fragment in the module whose
#: meaning changes if a renderer breaks it.
CYCLE_STOP_CHECK_SQL = (
    f'CONSTRAINT "{CYCLE_STOP_CHECK_NAME}"\n'
    "  CHECK ((stop_requested_by IS NULL)\n"
    "       = (stop_requested_at IS NULL))"
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
        f"ALTER TABLE cycles ADD COLUMN stop_requested_by TEXT\n{CYCLE_STOP_CHECK_SQL};",
    ),
)

#: ``cycles`` as the migrations leave it: every column, in schema order, as the
#: fragment that declares it. Written once and spliced into both halves of
#: :data:`CYCLE_STOP_CHECK_REBUILD_SQL`, so the table that repair builds and the
#: columns it carries across cannot say different things.
#:
#: It is still a list written by hand, and what keeps it honest is a test rather
#: than anything here: ``test_the_rebuilds_column_list_is_every_column_a_
#: migrated_cycles_has`` compares it against ``PRAGMA table_info(cycles)`` on a
#: freshly migrated database. So a column added by ``0016`` fails that test on
#: the commit that adds it — which is the only moment anyone can be expected to
#: remember this constant exists, and the alternative is a repair that drops
#: that column and its data with nothing left to notice.
#: The declarations are written pre-wrapped, and continuation lines carry their
#: own indent, for the reason :data:`_SQL_WIDTH` gives — this list is spliced
#: into a statement a human reads off a terminal.
CYCLES_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "id TEXT PRIMARY KEY"),
    (
        "trigger",
        "trigger TEXT NOT NULL CHECK (trigger IN\n"
        "    ('manual','scheduled','curative','rollback'))",
    ),
    ("triggered_by", "triggered_by TEXT NOT NULL"),
    ("scope", "scope TEXT"),
    ("dry_run", "dry_run INTEGER NOT NULL DEFAULT 0"),
    (
        "status",
        "status TEXT NOT NULL CHECK (status IN\n"
        "    ('running','completed','failed','rolled_back'))",
    ),
    ("report", "report TEXT"),
    ("started_at", "started_at TEXT NOT NULL DEFAULT (datetime('now'))"),
    ("finished_at", "finished_at TEXT"),
    ("rolled_back_by", "rolled_back_by TEXT REFERENCES cycles(id)"),
    ("stop_requested_at", "stop_requested_at TEXT"),
    ("stop_requested_by", "stop_requested_by TEXT"),
)


def _cycles_column_lines() -> str:
    """Every ``cycles`` column name, comma-separated, wrapped to :data:`_SQL_WIDTH`."""
    lines: list[str] = []
    for name, _ in CYCLES_COLUMNS:
        piece = f"{name},"
        if lines and len(lines[-1]) + 1 + len(piece) <= _SQL_WIDTH:
            lines[-1] += f" {piece}"
        else:
            lines.append(f"  {piece}")
    return "\n".join(lines).rstrip(",")


#: Where the rebuild parks the table it is replacing.
#:
#: The rebuild used to ``DROP TABLE cycles`` with nothing but the transaction
#: standing between a failed copy and an empty journal, and a transaction is a
#: guarantee about *one* execution model. A console that reports an error and
#: goes on to the next statement — an interactive shell, a database GUI, a
#: notebook cell — ran the ``DROP``, the ``RENAME`` and the ``COMMIT`` after the
#: copy had already failed, and took a file from four cycles to none with
#: nothing left to notice. No statement can prevent the next one from running in
#: that model, so the only structure that survives it is one where **no
#: statement destroys anything**: the rows are copied here first, into a table
#: with no constraints of its own to fail on, and every destructive step after
#: that is destroying a copy.
#:
#: It is left behind on purpose, including when the repair works perfectly.
#: :func:`_cycle_stop_problems` reports it and prints the one statement that
#: removes it — so the file does not pass ``init`` until a human has been told
#: it is there, which is what makes "the rebuild stopped half-way" a state
#: somebody hears about instead of an empty table that reads as an empty
#: journal.
CYCLES_PARKED_TABLE = "cycles_before_repair"

#: The repair for a file that has both columns and **not** the CHECK under them,
#: which is a different drift from a missing column and cannot be fixed the same
#: way: SQLite's ``ALTER TABLE`` adds a constraint only *with* a column, so
#: putting one under a column that already exists is the documented
#: create-copy-drop-rename rebuild, here with the original **parked rather than
#: dropped** (:data:`CYCLES_PARKED_TABLE`). It is still a repair in place —
#: every row is carried across and the two indexes are recreated with it — which
#: is why this is a statement to run and not another reason to delete a graph.
#:
#: The CHECK goes on as a **table-level** constraint here rather than a column
#: one. It is cross-column, the two spellings are equivalent to SQLite, and
#: this is the spelling ``0015`` would have used had ``ALTER TABLE`` allowed it.
#: :data:`CYCLES_RUNNING_INDEX_SQL` is spliced in rather than written again,
#: because that index already has one home and a second copy of it here would be
#: the next thing to drift.
#:
#: **It carries no statement that can lose a row, in any execution model.** The
#: copy into :data:`CYCLES_PARKED_TABLE` is the first thing it does and the
#: ``DROP`` is against a table whose every row is already in that copy, so a
#: console that reports an error and runs the next statement anyway — the model
#: that emptied the journal outright when the ``DROP`` came before any copy —
#: now ends with the rows parked and a file that says so at the next ``init``.
#: Nothing weaker reaches that property: SQLite has no conditional DDL and no
#: ``RAISE`` outside a trigger, so no statement here can stop the one after it
#: from running, and *not destroying anything* is the only guarantee left that
#: does not depend on the tool obeying an error.
#:
#: It can still legitimately fail — the copy goes through the constraint, and a
#: file that ran without one may hold a row it forbids — but a human is not
#: handed this statement in that state at all: :func:`_cycle_stop_problems`
#: looks for those rows first (:data:`CYCLE_STOP_HALF_STOP_SQL`) and prints the
#: statement that clears them instead, because a repair that dies on ``CHECK
#: constraint failed`` is advice nobody can carry out, which is the failure
#: shape :data:`_CREATE_THE_CYCLES_INDEX` was already written against. The
#: same check refuses to print it against a ``cycles`` whose columns are not the
#: ones below.
#:
#: ``DROP TABLE IF EXISTS cycles_rebuilt`` leads because the *first* attempt is
#: not always the only one: a failed run that a human then commits (which is
#: exactly what the "clear the half-stops, then re-run" advice asks them to do)
#: leaves that scratch table behind, and without this line every later attempt
#: died on "table cycles_rebuilt already exists" with no way forward. It is the
#: one ``DROP`` here that can be unconditional: nothing but this statement ever
#: creates that name.
#:
#: Its column list is :data:`CYCLES_COLUMNS` rather than a literal, for the
#: reason the index is spliced in: the rebuild names every column **twice** —
#: once to create the replacement table, once to copy the rows into it — and a
#: column missing from either list is a column the repair drops, *with its
#: data*, in a statement a human is told to run against their own graph.
CYCLE_STOP_CHECK_REBUILD_SQL = (
    "PRAGMA foreign_keys=off;\n"
    "BEGIN;\n"
    f"CREATE TABLE {CYCLES_PARKED_TABLE} AS SELECT * FROM cycles;\n"
    "DROP TABLE IF EXISTS cycles_rebuilt;\n"
    "CREATE TABLE cycles_rebuilt (\n"
    + "".join(f"  {declaration},\n" for _, declaration in CYCLES_COLUMNS)
    + f"{CYCLE_STOP_CHECK_SQL}\n"
    ");\n"
    "INSERT INTO cycles_rebuilt SELECT\n"
    f"{_cycles_column_lines()}\n"
    "  FROM cycles;\n"
    "DROP TABLE cycles;\n"
    "ALTER TABLE cycles_rebuilt RENAME TO cycles;\n"
    "CREATE INDEX idx_cycles_started ON cycles(started_at);\n"
    f"{CYCLES_RUNNING_INDEX_SQL}\n"
    "COMMIT;\n"
    "PRAGMA foreign_keys=on;"
)

#: The rows a file without the CHECK may already hold, which the rebuild's copy
#: would meet. Run by the check itself before it prints the rebuild, and named
#: in the refusal so a human can see the same list.
CYCLE_STOP_HALF_STOP_SQL = (
    "SELECT id FROM cycles\n"
    "  WHERE (stop_requested_by IS NULL)\n"
    "     != (stop_requested_at IS NULL);"
)

#: What the constraint would have left where a half-stop is: two NULLs. The
#: statement the refusal prints when it finds one, since naming the rows without
#: saying what to do to them is half an instruction.
CYCLE_STOP_CLEAR_HALF_STOP_SQL = (
    "UPDATE cycles SET stop_requested_at = NULL,\n"
    "                  stop_requested_by = NULL\n"
    "  WHERE (stop_requested_by IS NULL)\n"
    "     != (stop_requested_at IS NULL);"
)

#: Removing the parked copy once ``cycles`` demonstrably holds everything in it.
_DROP_PARKED_SQL = f"DROP TABLE {CYCLES_PARKED_TABLE};"

#: Putting rows back that the rebuild's copy did not carry across — the other
#: end of the parked table, and the reason it is parked rather than dropped.
_RESTORE_PARKED_SQL = (
    "INSERT INTO cycles SELECT\n"
    f"{_cycles_column_lines()}\n"
    f"  FROM {CYCLES_PARKED_TABLE}\n"
    "  WHERE id NOT IN (SELECT id FROM cycles);"
)

#: :data:`CYCLE_STOP_CLEAR_HALF_STOP_SQL` against the parked copy, which is
#: where the half-stops are once the rebuild has already swapped the table out.
_CLEAR_PARKED_HALF_STOP_SQL = (
    f"UPDATE {CYCLES_PARKED_TABLE} SET stop_requested_at = NULL,\n"
    "                                 stop_requested_by = NULL\n"
    "  WHERE (stop_requested_by IS NULL)\n"
    "     != (stop_requested_at IS NULL);"
)

#: The remedy for ``0015``'s drift, and it is the *fifth* check's kind rather
#: than the first four's: nothing is lost and nothing has to be derived. Both
#: columns are pure additions with no back-fill — every row that predates them
#: is a cycle nobody asked to stop, which is what two NULLs say — and the CHECK
#: constrains rows the file already has, exactly as ``0014``'s index does.
#: Telling a human to delete their graph over either would be as wrong here as
#: it was there, so this sentence says which repair to run and lets each problem
#: carry its own statement.
_REPAIR_THE_STOP_SWITCH = (
    "No data is lost and nothing has to be derived: the columns are additions with no "
    "back-fill, and the CHECK under them constrains rows the file already has. Run the "
    "statement each problem above names, in the order printed and exactly as printed — the "
    "second column's CHECK names the first, and the rebuild copies every row into "
    f"'{CYCLES_PARKED_TABLE}' before it drops anything, so a run that stops part-way through "
    "loses nothing. Re-run 'nodum init' afterwards: it says what is left to do."
)

#: ``0016``'s annotations table and its three partial unique indexes, as the
#: statements that create them. Kept next to the check that looks for the table
#: so the refusal prints the cure rather than a shape of the cure — the table
#: is an addition nothing derives from what the file holds, so the cure is
#: re-running the migration's own statements (like ``0015``'s ``ALTER``s, and
#: unlike the delete-the-file remedy the first four checks share). Written
#: pre-wrapped for :data:`_SQL_WIDTH`'s reason: this SQL reaches a human inside
#: a refusal, and a renderer re-wraps any line it cannot fit.
ANNOTATIONS_TABLE_SQL = (
    "CREATE TABLE annotations (\n"
    "    id                TEXT PRIMARY KEY,\n"
    "    target_node_id    TEXT    REFERENCES nodes(id)\n"
    "                                 ON DELETE CASCADE,\n"
    "    target_edge_id    TEXT    REFERENCES edges(id)\n"
    "                                 ON DELETE CASCADE,\n"
    "    target_version_id INTEGER REFERENCES versions(id)\n"
    "                                 ON DELETE CASCADE,\n"
    "    body              TEXT NOT NULL,\n"
    "    actor             TEXT NOT NULL,\n"
    "    cycle_id          TEXT REFERENCES cycles(id),\n"
    "    created_at        TEXT NOT NULL DEFAULT\n"
    "                      (datetime('now')),\n"
    "    CHECK ((target_node_id IS NOT NULL)\n"
    "         + (target_edge_id IS NOT NULL)\n"
    "         + (target_version_id IS NOT NULL) = 1)\n"
    ");\n"
    "CREATE UNIQUE INDEX idx_annotations_node\n"
    "    ON annotations(target_node_id)\n"
    "    WHERE target_node_id IS NOT NULL;\n"
    "CREATE UNIQUE INDEX idx_annotations_edge\n"
    "    ON annotations(target_edge_id)\n"
    "    WHERE target_edge_id IS NOT NULL;\n"
    "CREATE UNIQUE INDEX idx_annotations_version\n"
    "    ON annotations(target_version_id)\n"
    "    WHERE target_version_id IS NOT NULL;"
)

#: The id of ``0016``'s conventions space — the gardener's own workspace, and
#: the one space every learned-curation write names. It is a constant here
#: because the consistency check asks about it twice.
CONVENTIONS_SPACE_ID = "conventions"

#: ``0016``'s space node, as the statement that creates it — the repair for a
#: file recording the migration that lacks the space. Written pre-wrapped for
#: :data:`_SQL_WIDTH`'s reason, like :data:`ANNOTATIONS_TABLE_SQL`.
CONVENTIONS_SPACE_SQL = (
    "INSERT INTO nodes\n"
    "(id, space_id, type_id, title, props, state, created_by)\n"
    "VALUES ('conventions', 'meta', 'space', 'conventions',\n"
    "        '{}', 'active', 'system');"
)

#: ``0016``'s grant row, as the statement that creates it — the repair for a
#: file recording the migration that lacks the gardener's ``edit`` on the
#: conventions space. Written pre-wrapped for :data:`_SQL_WIDTH`'s reason.
CONVENTIONS_GRANT_SQL = (
    "INSERT INTO grants (agent_id, space_id, level)\n"
    "VALUES ('builtin-gardener', 'conventions', 'edit');"
)

#: The remedy for ``0016``'s drift: three additions, none derivable from what
#: the file holds, so each problem names the migration's own statement that
#: puts it back — repaired in place like ``0015``, never by deleting a graph.
_REPAIR_THE_WRITE_SEAM = (
    "No data is lost and nothing has to be derived: every missing piece is an addition "
    "with no back-fill, and the statement that creates it is the migration's own. Run the "
    "statement each problem above names, in the order printed and exactly as printed, then "
    "re-run 'nodum init' afterwards: it says what is left to do."
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
    first four and wildly disproportionate for the last two: a missing index is
    derived state, repairable by one ``CREATE UNIQUE INDEX``, and a missing
    column or constraint on ``cycles`` is repairable in place with every row
    kept. A refusal that reads as *your graph is unrecoverable* over any of
    those would cost a human every node they own. A refusal names what to do
    about the thing it found — and where one check can find two different kinds
    of drift, the statement travels with the *problem* and the shared sentence
    only says how to read them (see :func:`_cycle_stop_problems`).

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
        ("0015_cycle_stop_switch", _cycle_stop_problems, _REPAIR_THE_STOP_SWITCH),
        ("0016_conventions_and_annotations", _write_seam_problems, _REPAIR_THE_WRITE_SEAM),
    ):
        if name not in recorded:
            continue
        problems = check(conn)
        if problems:
            # One problem per line rather than "; "-joined: a problem now ends
            # in the statement that repairs it, and a statement a human has to
            # paste cannot share a line with the next sentence.
            raise SchemaConsistencyError(
                f"database schema is inconsistent with its recorded migrations "
                f"({name}):\n" + "\n".join(problems) + "\n" + remedy
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
    """0015 guarantees the two columns the kill switch is written in — **and the CHECK**.

    Two columns on ``cycles``, or the switch has nothing to write to.

    **This one's route in is not 0014's, and saying it was would be false.**
    ``0014`` really was amended in place while unreleased — the
    one-running-consolidation index was added in a commit after the migration
    itself — so a dev file built from its first cut genuinely lacks the index it
    records. ``0015`` has no such history: it landed with both columns and the
    CHECK already attached and ``nodum/migrations.py`` has not changed since, so
    **no database this repo has ever produced can record ``0015`` and lack any
    of them**. What this check guards is a file drifted by something other than
    this repo — a hand-edited schema, an externally applied migration, a
    ``cycles`` rebuilt by a human following half of the repair below — which is
    a smaller claim than ``0014``'s and still worth the entry, because every
    recorded migration with a checkable guarantee has one (Q13 review S6) and
    this guarantee is as checkable as they come.

    **Nothing in the runtime can notice the drift**, which is why the question
    is asked here: ``LLMReport.stop_switch`` reports which posture a *run* had,
    not what a file can store, so a cycle over a drifted database would say
    ``armed`` right up until the write failed — as ``no such column:
    stop_requested_at`` out of :func:`nodum.service.request_stop`, on the run a
    human was trying to stop. Whether a stop has somewhere to go is a question
    about the schema, so it is asked at ``init_db``, where the answer comes with
    the statements that repair it.

    **The columns are not the whole guarantee.** ``0015`` records a stop as two
    nullable stamps *and one cross-column CHECK*, and the constraint is the
    entire reason the pair is honest: without it a file can hold a time with no
    requester or a requester with no time — a stop nobody asked for, or one
    nobody can date — which is the state the migration chose two columns over a
    boolean specifically to make unstorable. A check that reads ``PRAGMA
    table_info`` sees the columns and nothing about what constrains them, so this
    reads the stored schema and looks for the constraint **by name**.

    By name is a deliberate narrowing, not a shortcut. The name is half of what
    ``0015`` guarantees: SQLite prints it verbatim as ``CHECK constraint failed:
    <name>``, which is the sentence a human meets when the constraint bites, and
    it is what :data:`CYCLE_STOP_CHECK_REBUILD_SQL` puts back. So an *unnamed*
    CHECK enforcing the same rule is reported here, and that is the right answer
    rather than a false alarm: the file enforces the rule and does not carry the
    migration, and the cost of saying so is a rebuild that changes only the name,
    plus the statement that clears the copy it parks. That cost is bounded
    because of what this function will not print — a rebuild that drops a column
    of theirs is not one of the outcomes, which it was when the only thing this
    said about the column list was that it existed. The direction this must not
    fail in is open, which is what :func:`_table_sql` strips comments for.

    **The name is matched word for word and not whitespace for whitespace**
    (:data:`_CYCLE_STOP_CHECK_NAME_RE`). A 45-character identifier with spaces
    in it does not survive being rendered into a terminal narrower than the line
    it sits on, and the file where it did not is a file whose owner pasted the
    repair, got a working constraint, and would otherwise be told the same
    sentence forever. Refusing a sound file *after* its owner did what the
    message said is worse than every failure mode this check exists to catch.

    **Its repairs are its own, and they are different repairs.** A missing
    column is added by the migration's own ``ALTER``, which carries the CHECK
    with it — so a missing constraint is only reported when both columns are
    already there, and a file missing a column is never handed both cures at
    once. Putting a constraint under a column that already exists is the one
    thing ``ALTER TABLE`` cannot do, so that repair is the documented rebuild
    (:data:`CYCLE_STOP_CHECK_REBUILD_SQL`), which still carries every row
    across. Neither is "delete the database file and re-run ``nodum init``":
    that sentence is true of a missing table and reads as *your graph is
    unrecoverable* over a constraint the file has all the rows for, which is
    ``0014``'s lesson said once more.

    **What it refuses to print matters as much as what it prints.** The rebuild
    is a statement a human runs against their own graph, so it goes out only
    against the table it was written for: not while the rebuild's parked copy is
    still there (:data:`CYCLES_PARKED_TABLE`), not over a ``cycles`` carrying
    columns :data:`CYCLES_COLUMNS` does not list — it would drop them with their
    data — and not over a file already holding a row the CHECK forbids, where
    the copy is guaranteed to fail. Each of those states has an answer of its
    own, and each of them was, until this round, either a silent loss or a dead
    end at the end of the only instruction given.
    """
    if CYCLES_PARKED_TABLE in _tables(conn):
        return [_parked_copy_problem(conn)]
    columns = _columns(conn, "cycles")
    missing = [
        f"table 'cycles' has no {column!r} column (a stop has nowhere to be recorded)"
        f" — repair:\n{sql}"
        for column, sql in CYCLE_STOP_COLUMN_SQL
        if column not in columns
    ]
    if missing:
        return missing
    if _CYCLE_STOP_CHECK_NAME_RE.search(_table_sql(conn, "cycles")):
        return []
    return [_missing_stop_check_problem(conn, columns)]


def _named_rows(ids: list[str], limit: int = 5) -> str:
    """``ids`` as a readable list, capped — a refusal is read, not parsed."""
    shown = ", ".join(ids[:limit])
    return shown if len(ids) <= limit else f"{shown} and {len(ids) - limit} more"


def _missing_stop_check_problem(conn: sqlite3.Connection, columns: set[str]) -> str:
    """The CHECK is missing: the rebuild, or the reason it is not being printed.

    Three states get an answer other than the rebuild, and all three are
    detectable from here — which is the point, because each of them is a way for
    the rebuild to fail *after* a human has already run it.
    """
    head = (
        "table 'cycles' has both stop columns and not the CHECK under them "
        f"({CYCLE_STOP_CHECK_NAME!r}), so a half-stop — a time with no requester, or a "
        "requester with no time — is storable by anything that does not go through "
        "request_stop"
    )
    known = [name for name, _ in CYCLES_COLUMNS]
    unknown = sorted(columns - set(known))
    absent = [name for name in known if name not in columns]
    if unknown or absent:
        differences = []
        if unknown:
            differences.append(
                f"it carries {', '.join(repr(name) for name in unknown)}, which the rebuild "
                "does not list and would drop, with the data in them"
            )
        if absent:
            differences.append(
                f"it has no {', '.join(repr(name) for name in absent)}, which the rebuild copies"
            )
        return (
            f"{head}. The repair for that is a rebuild of the table, and it is not printed "
            "here, because this 'cycles' is not the table this version of nodum knows: "
            f"{'; and '.join(differences)}. Reconcile the table with the schema this version "
            "ships — or write the rebuild by hand from the one in nodum/db.py, with those "
            "columns in both of its lists — and run 'nodum init' again."
        )
    half_stops = [row["id"] for row in conn.execute(CYCLE_STOP_HALF_STOP_SQL)]
    if half_stops:
        return (
            f"{head}, and {len(half_stops)} row(s) already in it are exactly that "
            f"({_named_rows(half_stops)}). The rebuild that puts the CHECK on copies every "
            "row through it, so it cannot run until those are cleared, and it is not printed "
            "until they are. Each is a cycle whose stop was recorded half-way, and clearing "
            "both of its columns is what the constraint would have left:\n"
            f"{CYCLE_STOP_CLEAR_HALF_STOP_SQL}\n"
            f"Name them yourself with:\n{CYCLE_STOP_HALF_STOP_SQL}\n"
            "Then run 'nodum init' again for the rebuild."
        )
    return (
        f"{head}. Run this repair as one script, exactly as printed, on a connection with "
        "no transaction already open (the leading PRAGMA is a no-op inside one):\n"
        f"{CYCLE_STOP_CHECK_REBUILD_SQL}\n"
        f"It copies every row into '{CYCLES_PARKED_TABLE}' before it drops anything, so no "
        "way of running it can lose one; 'nodum init' then names that copy and the single "
        "statement that removes it."
    )


def _parked_copy_problem(conn: sqlite3.Connection) -> str:
    """The rebuild's parked copy is still there — which of the two ways it can be.

    Either ``cycles`` holds everything in the copy, and the copy is one
    statement from gone; or it does not, and the copy is the only place those
    cycles still exist. The difference is the difference between a repair that
    finished and one that stopped in the middle, and a human cannot be asked to
    work out which from a message that reports the table and nothing else.
    """
    head = (
        f"table {CYCLES_PARKED_TABLE!r} is present — it is the copy of 'cycles' the "
        "stop-switch rebuild parks before it swaps, and while it is there that repair is "
        "not finished"
    )
    if "id" not in _columns(conn, CYCLES_PARKED_TABLE) or "id" not in _columns(conn, "cycles"):
        return (
            f"{head}. This check cannot tell whether it holds rows 'cycles' has not got — "
            "one of the two has no 'id' column — so compare them yourself before running:\n"
            f"{_DROP_PARKED_SQL}"
        )
    stranded = [
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM {CYCLES_PARKED_TABLE} WHERE id NOT IN (SELECT id FROM cycles)"
        )
    ]
    if not stranded:
        kept = conn.execute(f"SELECT count(*) FROM {CYCLES_PARKED_TABLE}").fetchone()[0]
        return (
            f"{head}: all {kept} of its rows are in 'cycles', so it holds nothing 'cycles' "
            f"has not got and the rebuild's copy came across whole. Remove it with:\n"
            f"{_DROP_PARKED_SQL}"
        )
    return (
        f"{head}, and {len(stranded)} of its rows are not in 'cycles' "
        f"({_named_rows(stranded)}) — the rebuild's copy did not finish, and this table is "
        "the only place those cycles still exist. Do not drop it. Put them back with:\n"
        f"{_RESTORE_PARKED_SQL}\n"
        "If that fails with 'CHECK constraint failed', those rows are half-stops — a time "
        "with no requester, or a requester with no time — which is what stopped the copy in "
        f"the first place. Clear them in the copy, then insert again:\n"
        f"{_CLEAR_PARKED_HALF_STOP_SQL}"
    )


def _write_seam_problems(conn: sqlite3.Connection) -> list[str]:
    """0016 guarantees the annotations table, the conventions space, and its grant.

    Three additions, each checkable on its own. The ``annotations`` table is
    the one the runtime meets as a bare drift — a file recording ``0016``
    without it fails ``list_proposals`` with ``no such table: annotations`` the
    first time an item's annotation is looked up — while a missing space node
    fails a gardener job scoped to ``conventions`` with ``space not found`` and
    a missing grant silently lands that job's writes ``proposed`` instead of
    the workspace they were designed for. None of the three is derivable from
    what the file holds, so each problem names the migration's own statement
    that puts it back (:data:`ANNOTATIONS_TABLE_SQL` and the two INSERTs).
    """
    problems: list[str] = []
    if "annotations" not in _tables(conn):
        problems.append(
            "table 'annotations' is missing (a proposal listing would die on "
            f"'no such table') — repair:\n{ANNOTATIONS_TABLE_SQL}"
        )
    if (
        conn.execute(
            "SELECT 1 FROM nodes WHERE id = ? AND type_id = 'space'",
            (CONVENTIONS_SPACE_ID,),
        ).fetchone()
        is None
    ):
        problems.append(
            f"space node {CONVENTIONS_SPACE_ID!r} is missing (a gardener job "
            f"scoped to it would not resolve) — repair:\n{CONVENTIONS_SPACE_SQL}"
        )
    if (
        conn.execute(
            "SELECT 1 FROM grants WHERE agent_id = ? AND space_id = ?",
            (GARDENER_AGENT_ID, CONVENTIONS_SPACE_ID),
        ).fetchone()
        is None
    ):
        problems.append(
            f"the gardener's 'edit' grant on {CONVENTIONS_SPACE_ID!r} is missing "
            f"(its writes would land 'proposed' instead) — repair:\n{CONVENTIONS_GRANT_SQL}"
        )
    return problems


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
