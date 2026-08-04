"""`nodum backup` — the disaster-recovery story, and the guard rails around it.

The database runs in WAL mode, so committed rows can live in the ``-wal``
companion file rather than the main ``.db`` whenever a connection stays open.
A plain ``copyfile`` of the ``.db`` loses those rows silently (the B4 review
finding); ``nodum backup`` must fold them in. These tests prove the fold and
pin the refusals (missing source, destination is the source, occupied
destination) to one readable line each.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from nodum import db
from nodum.cli import app

runner = CliRunner()


def _insert_node(conn: sqlite3.Connection, node_id: str, title: str) -> None:
    """Insert one committed node row through ``conn`` — a WAL-resident write."""
    conn.execute(
        "INSERT INTO nodes (id, space_id, type_id, title, props, state, created_by)"
        " VALUES (?, 'main', 'note', ?, '{}', 'active', 'human:owner')",
        (node_id, title),
    )


def test_backup_folds_committed_wal_rows_into_the_snapshot(fresh_db, tmp_path):
    """B4: a raw copyfile of the .db alone loses WAL rows; `backup` keeps them.

    The first commit makes the ``-wal`` file non-empty; the second is written
    on top of it, so both rows are only reachable through the WAL while the
    source connection stays open. The raw-copy assertion below is the B4
    mechanism in miniature — copyfile loses both rows — and the backup is the
    closure: both survive in the snapshot.
    """
    conn = db.connect(fresh_db)
    try:
        _insert_node(conn, "committed-row", "committed while open")
        conn.commit()
        _insert_node(conn, "wal-framed-row", "framed in the WAL")
        conn.commit()

        wal = Path(str(fresh_db) + "-wal")
        assert wal.is_file() and wal.stat().st_size > 0

        raw = tmp_path / "raw-copy.db"
        shutil.copyfile(fresh_db, raw)
        with sqlite3.connect(str(raw)) as raw_conn:
            raw_conn.row_factory = sqlite3.Row
            copied = {row["id"] for row in raw_conn.execute("SELECT id FROM nodes")}
        assert not {"committed-row", "wal-framed-row"} & copied

        dest = tmp_path / "backup.db"
        result = runner.invoke(app, ["backup", str(dest)])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["destination"] == str(dest)
        assert payload["bytes"] == dest.stat().st_size
        assert payload["bytes"] > 0
        assert payload["integrity"] == "ok"

        restored = db.connect(dest)
        try:
            titles = {
                row["title"]: row["id"] for row in restored.execute("SELECT id, title FROM nodes")
            }
            assert titles["committed while open"] == "committed-row"
            assert titles["framed in the WAL"] == "wal-framed-row"
            # A standalone database: the graph's own connection machinery
            # opens it and moves it into WAL.
            assert restored.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            restored.close()
    finally:
        conn.close()


def test_backup_takes_an_explicit_source_path(tmp_path, monkeypatch, fresh_db):
    """`--path` names the source; it wins over NODUM_DB."""
    monkeypatch.setenv("NODUM_DB", str(tmp_path / "wrong.db"))
    conn = db.connect(fresh_db)
    try:
        _insert_node(conn, "explicit-path-row", "via --path")
        conn.commit()
    finally:
        conn.close()

    dest = tmp_path / "via-path.db"
    result = runner.invoke(app, ["backup", str(dest), "--path", str(fresh_db)])
    assert result.exit_code == 0, result.output
    with db.connect(dest) as restored:
        rows = {row["id"] for row in restored.execute("SELECT id FROM nodes")}
        assert "explicit-path-row" in rows


def test_backup_refuses_a_missing_source(tmp_path, monkeypatch):
    """Backing up a graph that was never created is a caller error, not an empty file."""
    monkeypatch.setenv("NODUM_DB", str(tmp_path / "never-created.db"))
    dest = tmp_path / "backup.db"
    result = runner.invoke(app, ["backup", str(dest)])
    assert result.exit_code == 1
    assert "no database at" in result.stderr
    assert not dest.exists()


def test_backup_refuses_the_source_as_destination(fresh_db):
    """The snapshot must never clobber the source itself (resolved-path compare)."""
    result = runner.invoke(app, ["backup", str(fresh_db)])
    assert result.exit_code == 1
    assert "source database itself" in result.stderr

    via_path = runner.invoke(app, ["backup", str(fresh_db), "--path", str(fresh_db)])
    assert via_path.exit_code == 1
    assert "source database itself" in via_path.stderr


def test_backup_refuses_a_nonempty_existing_destination(fresh_db, tmp_path):
    """An occupied destination is refused (and left untouched), not overwritten."""
    dest = tmp_path / "occupied.db"
    dest.write_bytes(b"occupied")
    result = runner.invoke(app, ["backup", str(dest)])
    assert result.exit_code == 1
    assert "already exists and is not empty" in result.stderr
    assert dest.read_bytes() == b"occupied"
