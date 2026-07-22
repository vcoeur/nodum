"""Projectors — derived-index consumers of the append-only event log.

A projector maintains *derived* state (today: the ``node_fts`` full-text
index; later: embeddings, renditions, the Markdown mirror) by replaying
events from the log. Each projector owns a checkpoint row in
``projector_checkpoints`` — the highest event ``seq`` it has applied — so
runs are incremental and every projector can be reset and rebuilt from event
0 independently.

Everything here is deterministic and LLM-free (design Constraint 4): the
service layer never calls into this module on the write path — the event log
is the only coupling.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from nodum import db
from nodum.models import ProjectorRun, ProjectorStatus


class Projector:
    """Base class for derived-state consumers of the event log.

    Subclasses implement :meth:`reset` (drop all derived state) and
    :meth:`apply` (fold one event into the derived state). ``apply`` receives
    the raw ``events`` row; events irrelevant to the projector are ignored.
    """

    #: Registry key (also the checkpoint row's primary key).
    name: str

    def reset(self, conn: sqlite3.Connection) -> None:
        """Drop every row of this projector's derived state."""
        raise NotImplementedError

    def apply(self, conn: sqlite3.Connection, event: sqlite3.Row) -> None:
        """Fold one event-log row into the derived state."""
        raise NotImplementedError

    def count(self, conn: sqlite3.Connection) -> int:
        """Return the number of rows in the derived store (for status)."""
        raise NotImplementedError


class FtsProjector(Projector):
    """Maintain the ``node_fts`` FTS5 index from node events.

    Every node state (``proposed``/``active``/``archived``) is indexed; the
    search layer filters by state at query time. Indexing is driven purely by
    event payloads: node creates/updates/transitions upsert the ``after``
    (or restored) row; undoing a node create deletes the row from the index.
    Edge events do not affect the index.
    """

    name = "fts"

    def reset(self, conn: sqlite3.Connection) -> None:
        """Empty the FTS index (rebuild replays the log to refill it)."""
        conn.execute("DELETE FROM node_fts")

    def count(self, conn: sqlite3.Connection) -> int:
        """Return the number of indexed nodes."""
        row = conn.execute("SELECT COUNT(*) AS n FROM node_fts").fetchone()
        return int(row["n"])

    def apply(self, conn: sqlite3.Connection, event: sqlite3.Row) -> None:
        """Index the node affected by one event, if any."""
        op = event["op"]
        payload = json.loads(event["payload"])
        if op == "undo":
            self._apply_undo(conn, payload)
        elif op.startswith("node."):
            after = payload.get("after")
            if after is not None:
                self._upsert(conn, after)
        # Edge events carry no node text; nothing to index.

    def _apply_undo(self, conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
        """Mirror an undo: restore the ``before`` row or drop a reverted create."""
        if not str(payload.get("reversed_op", "")).startswith("node."):
            return
        restored = payload.get("restored")
        if restored is not None:
            self._upsert(conn, restored)
            return
        # A reverted create: the node row is gone from `nodes` (it is listed in
        # the undo payload's `deleted` rows), so it leaves the index too.
        for entry in payload.get("deleted", []):
            if entry.get("table") == "nodes":
                conn.execute("DELETE FROM node_fts WHERE node_id = ?", (entry["row"]["id"],))

    def _upsert(self, conn: sqlite3.Connection, node: dict[str, Any]) -> None:
        """Replace one node's index row with its current title/content."""
        conn.execute("DELETE FROM node_fts WHERE node_id = ?", (node["id"],))
        conn.execute(
            "INSERT INTO node_fts (node_id, title, content, extracted_text) VALUES (?, ?, ?, ?)",
            (node["id"], node["title"] or "", node["content"], self._extracted_text(node)),
        )

    def _extracted_text(self, node: dict[str, Any]) -> str:
        """Extracted asset text for an ``asset_ref`` node — always empty today.

        The seam for Phase 4: once the asset store lands, asset nodes carry an
        ``asset_hash`` prop and this joins against ``assets.extracted_text``.
        """
        return ""


#: The projector registry, keyed by name. New derived stores register here.
REGISTRY: dict[str, Projector] = {projector.name: projector for projector in (FtsProjector(),)}


# ── Connection and checkpoint helpers ─────────────────────────────────────────


def _connect(path: str | Path | None) -> sqlite3.Connection:
    """Open a connection and apply any pending migrations (idempotent)."""
    conn = db.connect(path)
    db.init_db(conn)
    return conn


def _checkpoint(conn: sqlite3.Connection, name: str) -> int:
    """Return a projector's last applied event seq (0 when never run)."""
    row = conn.execute(
        "SELECT last_event_seq FROM projector_checkpoints WHERE name = ?", (name,)
    ).fetchone()
    return int(row["last_event_seq"]) if row is not None else 0


def _set_checkpoint(conn: sqlite3.Connection, name: str, seq: int) -> None:
    """Persist a projector's checkpoint (insert or update)."""
    conn.execute(
        """
        INSERT INTO projector_checkpoints (name, last_event_seq, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(name) DO UPDATE SET
            last_event_seq = excluded.last_event_seq,
            updated_at = excluded.updated_at
        """,
        (name, seq),
    )


def _resolve(names: list[str] | None) -> list[Projector]:
    """Resolve projector names against the registry (None = all)."""
    if names is None:
        return list(REGISTRY.values())
    unknown = sorted(set(names) - set(REGISTRY))
    if unknown:
        raise ValueError(
            f"unknown projector(s): {', '.join(unknown)} "
            f"(registered: {', '.join(sorted(REGISTRY))})"
        )
    return [REGISTRY[name] for name in names]


def _run_one(conn: sqlite3.Connection, projector: Projector) -> ProjectorRun:
    """Apply every event past the checkpoint, advancing it per batch.

    The whole batch applies in one transaction: a failure rolls back to the
    last committed checkpoint, and replaying the same events is deterministic.
    """
    from_seq = _checkpoint(conn, projector.name)
    rows = conn.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq", (from_seq,)).fetchall()
    for event in rows:
        projector.apply(conn, event)
        _set_checkpoint(conn, projector.name, event["seq"])
    to_seq = rows[-1]["seq"] if rows else from_seq
    return ProjectorRun(name=projector.name, applied=len(rows), from_seq=from_seq, to_seq=to_seq)


# ── Public API ────────────────────────────────────────────────────────────────


def run_projectors(
    *, names: list[str] | None = None, path: str | Path | None = None
) -> list[ProjectorRun]:
    """Bring projectors up to date with the event log.

    Args:
        names: Projectors to run (default: all registered).
        path: Explicit database path.

    Returns:
        One run result per projector, in registry (or given) order.

    Raises:
        ValueError: If a name is not in the registry.
    """
    selected = _resolve(names)
    conn = _connect(path)
    try:
        runs = [_run_one(conn, projector) for projector in selected]
        conn.commit()
        return runs
    finally:
        conn.close()


def rebuild_projector(name: str, *, path: str | Path | None = None) -> ProjectorRun:
    """Drop one projector's derived state and replay the full event log.

    Args:
        name: The projector to rebuild.
        path: Explicit database path.

    Returns:
        The run result of the full replay (``from_seq`` is always 0).

    Raises:
        ValueError: If the name is not in the registry.
    """
    (projector,) = _resolve([name])
    conn = _connect(path)
    try:
        projector.reset(conn)
        _set_checkpoint(conn, projector.name, 0)
        run = _run_one(conn, projector)
        conn.commit()
        return run
    finally:
        conn.close()


def projector_status(*, path: str | Path | None = None) -> list[ProjectorStatus]:
    """Report every registered projector's checkpoint, backlog, and store size."""
    conn = _connect(path)
    try:
        max_seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM events").fetchone()
        max_seq = int(max_seq_row["m"])
        return [
            ProjectorStatus(
                name=projector.name,
                last_event_seq=(checkpoint := _checkpoint(conn, projector.name)),
                pending_events=max_seq - checkpoint,
                rows=projector.count(conn),
            )
            for projector in REGISTRY.values()
        ]
    finally:
        conn.close()
