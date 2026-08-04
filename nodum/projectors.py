"""Projectors — derived-index consumers of the append-only event log.

A projector maintains *derived* state (the ``node_fts`` full-text index and
the ``node_vec`` chunk embeddings; later: renditions, the Markdown mirror)
by replaying events from the log. Each projector owns a checkpoint row in
``projector_checkpoints`` — the highest event ``seq`` it has applied — so
runs are incremental and every projector can be reset and rebuilt from event
0 independently. For the ``fts`` projector that last claim needs the one
write that happens outside the log to be logged too: storing an asset's
extracted text appends an ``asset.extract`` event, and replaying one
re-projects the describing nodes — so a rebuild from event 0 indexes exactly
what an incremental replay indexed (see :meth:`FtsProjector._apply_extract`).

Everything here is deterministic and LLM-free (design Constraint 4): the
service layer never calls into this module on the write path — the event log
is the only coupling. Embedding models are deterministic transformers, not
agents; a projector with no usable embedding provider simply reports itself
unavailable and makes no progress (its backlog waits). An event whose
``apply`` refuses it — one malformed row in the log, say — is quarantined
and skipped past rather than failing forever on the same row (finding M12);
a systemic failure (a dead provider, a database error) is never quarantined,
because a whole batch of skips would hide it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import sqlite_vec

from nodum import db, embeddings
from nodum.models import ProjectorRun, ProjectorSkip, ProjectorStatus

#: The node type whose FTS row carries its asset's extracted text. Spelled here
#: rather than imported from :mod:`nodum.ingest`: a projector is derived state
#: over the event log and must not depend on the pipeline that produced it.
ASSET_REF_TYPE = "asset_ref"


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    """A live-table row as a plain dict, for the dict-only projector helpers.

    ``sqlite3.Row`` supports ``row["key"]`` but neither ``.get`` nor the
    ``dict(row)`` iteration, so the ``asset.extract`` handler — the one place
    a projection reads the live ``nodes`` table — converts before handing the
    row to :meth:`FtsProjector._upsert`.
    """
    keys = row.keys()
    return {key: row[key] for key in keys}


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
        """Fold one event-log row into the derived state.

        Args:
            conn: The open database connection.
            event: A raw ``events`` row; events irrelevant to this projector
                are ignored.
        """
        raise NotImplementedError

    def count(self, conn: sqlite3.Connection) -> int:
        """Return the number of rows in the derived store (for status)."""
        raise NotImplementedError

    def availability(self) -> tuple[bool, str | None]:
        """Return whether the projector can make progress, and why not.

        The base projector always can; subclasses with external requirements
        (the ``vec`` projector needs an embedding provider) override this. An
        unavailable projector's runs are no-ops — its checkpoint stays put so
        the backlog is picked up once the blocker clears.
        """
        return (True, None)

    def mixed_model_note(self, conn: sqlite3.Connection) -> str | None:
        """An optional staleness note for ``projector status``; the base has none.

        Unlike :meth:`availability` this never gates a run — it only adds a
        ``detail`` line a human can read. Subclasses whose derived state can
        silently go stale (the ``vec`` projector's chunks under a model swap)
        override it.
        """
        return None


class FtsProjector(Projector):
    """Maintain the ``node_fts`` FTS5 index from node events.

    Every node state (``proposed``/``active``/``archived``) is indexed; the
    search layer filters by state at query time. *Which* node to index is
    driven purely by event payloads: node creates/updates/transitions upsert
    the ``after`` (or restored) row; undoing a node create deletes the row
    from the index. Edge events do not affect the index.

    A described asset's extracted text is joined from the live ``assets``
    table (see :meth:`_extracted_text`); the ``asset.extract`` event — written
    whenever :func:`nodum.assets.set_extracted_text` stores or clears text —
    re-projects the describing nodes, so text that changed *after* the node's
    own events still reaches the index, and a rebuild from event 0 is an
    incremental replay for this index too.
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
        """Index the node affected by one event, if any.

        Args:
            conn: The open database connection.
            event: A raw ``events`` row: ``node.*`` upsert or delete the
                affected node's row, ``asset.extract`` re-projects the
                describing nodes, and ``undo`` mirrors the reversed event.
                Edge events carry no node text and are ignored.
        """
        op = event["op"]
        payload = json.loads(event["payload"])
        if op == "undo":
            self._apply_undo(conn, payload)
        elif op == "asset.extract":
            self._apply_extract(conn, payload)
        elif op.startswith("node."):
            after = payload.get("after")
            before = payload.get("before")
            if after is not None:
                self._upsert(conn, after)
            elif before is not None:
                # A node event that *removed* the row: a rollback reversing a
                # create (`node.rollback`) is the only writer of this shape. It
                # is the mirror of a create, so the index has to mirror it too —
                # ignoring it would leave the index describing a node the graph
                # no longer has.
                conn.execute("DELETE FROM node_fts WHERE node_id = ?", (before["id"],))
        # Edge events carry no node text; nothing to index.

    def _apply_extract(self, conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
        """Re-index the nodes that describe an asset whose text was (re)stored.

        ``asset.extract`` is appended by :func:`nodum.assets.set_extracted_text`
        after the ``assets`` row is updated. The describing node's own create
        already indexed it (the join below reads live state), so this branch
        matters only when the text changed *after* that projection — the one
        ordering the live join cannot see. Re-projecting through the same
        :meth:`_upsert` the node's own events use is what keeps a rebuild from
        event 0 identical to an incremental replay (finding M14).

        The event can precede the node: the ingestion pipeline stores the text
        before it creates the ``asset_ref`` node, so at replay time there may
        be no node for the hash yet. That is a skip, not an error — the node's
        own create event picks the text up through the join in the same run.
        """
        asset_hash = payload.get("asset_hash")
        if not isinstance(asset_hash, str) or not asset_hash:
            return
        rows = conn.execute(
            "SELECT * FROM nodes WHERE type_id = ? AND json_extract(props, '$.asset_hash') = ?",
            (ASSET_REF_TYPE, asset_hash),
        ).fetchall()
        for row in rows:
            self._upsert(conn, _row_dict(row))

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
            (
                node["id"],
                node["title"] or "",
                node["content"],
                self._extracted_text(conn, node),
            ),
        )

    def _extracted_text(self, conn: sqlite3.Connection, node: dict[str, Any]) -> str:
        """The extracted text of the asset an ``asset_ref`` node describes, or ``""``.

        The ``asset_ref`` node is the one that *stands for* the bytes: its own
        ``content`` is empty, so without this join the full text of a PDF would
        be findable through nothing at all.

        **Only that node type gets it**, and the restriction is load-bearing
        rather than tidiness. Ingestion also records ``asset_hash`` on the
        ``source`` node and on every per-page ``block`` — useful provenance,
        and what lets a page's id resolve to a ``page:<n>`` raster — but those
        nodes already carry their own text. Joining on the prop alone gave
        every page of a document the *whole document's* text, so searching a
        word that appears on page 3 returned pages 1, 2 and 4 just as strongly
        and destroyed per-page precision; the ``source`` node got the same text
        twice, in ``content`` and here, double-weighting it in BM25.

        **This read is of live state inside an event replay, and deliberately
        so.** ``assets`` is not event-logged (there is nothing to undo about
        content-addressed bytes), so the value read here is whatever the row
        holds *at projection time* rather than at event time. What keeps that
        safe for a rebuild is that the *write* is event-logged:
        :func:`nodum.assets.set_extracted_text` appends an ``asset.extract``
        event, and :meth:`_apply_extract` re-projects the describing nodes
        when it replays one — so text stored after a node was projected is
        indexed by the next projector run, and a rebuild from event 0 lands on
        the same index an incremental replay produced (finding M14). The
        ingestion pipeline is still written to call
        :func:`nodum.assets.set_extracted_text` **before** it creates the
        ``asset_ref`` node — then the event replays as a no-op (no node for
        the hash yet) and the node's own create does the join in the same run.

        The props value comes from an event payload, where it is the raw JSON
        *string* of the ``nodes.props`` column, so it is decoded defensively:
        a replay must not be stopped by one malformed row, and a node
        referencing a hash that was never registered simply contributes
        nothing.
        """
        if node.get("type_id") != ASSET_REF_TYPE:
            return ""
        props = node.get("props")
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except ValueError:
                return ""
        if not isinstance(props, dict):
            return ""
        asset_hash = props.get("asset_hash")
        if not isinstance(asset_hash, str) or not asset_hash:
            return ""
        row = conn.execute(
            "SELECT extracted_text FROM assets WHERE hash = ?", (asset_hash,)
        ).fetchone()
        if row is None or row["extracted_text"] is None:
            return ""
        return str(row["extracted_text"])


class VecProjector(Projector):
    """Maintain chunk embeddings (``chunks`` + ``node_vec``) from node events.

    Follows the FTS projector's event handling exactly: node creates,
    updates, and transitions re-chunk and re-embed the ``after`` (or
    restored) node; an undone create drops the node's chunks and vectors.
    Chunking and the embedding model come from :mod:`nodum.embeddings`
    (design D6); every chunk records the provider's ``model_id``, and search
    filters the KNN join to the *active* provider's id — chunks a different
    model embedded live in a different vector space and are invisible to it
    (finding M13). A full rebuild (``reset`` + replay from event 0) re-embeds
    everything with the current model, which is the model-change path.

    When no embedding provider is usable the projector is *unavailable*:
    runs are no-ops and the reason surfaces in ``projector status`` — the
    backlog waits, and search falls back to BM25 + graph expansion.
    """

    name = "vec"

    def availability(self) -> tuple[bool, str | None]:
        """Available exactly when an embedding provider resolves."""
        if embeddings.get_provider() is None:
            return (False, embeddings.unavailable_reason())
        return (True, None)

    def mixed_model_note(self, conn: sqlite3.Connection) -> str | None:
        """One sentence when chunks from another embedding model sit in the store.

        ``projector status`` shows it as the ``detail`` beside an
        ``available: true`` vec projector: the store is usable and its runs
        make progress, but every chunk not carrying the active provider's
        ``model_id`` is invisible to search (finding M13) until a
        ``projector rebuild vec`` re-embeds it.
        """
        provider = embeddings.get_provider()
        if provider is None:
            return None
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE model_id != ?", (provider.model_id,)
        ).fetchone()
        n = int(row["n"])
        if not n:
            return None
        suffix = "" if n == 1 else "s"
        return (
            f"{n} chunk{suffix} from a different model: invisible to search "
            f"until `projector rebuild vec` re-embeds them"
        )

    def reset(self, conn: sqlite3.Connection) -> None:
        """Empty the chunk and vector stores (rebuild replays to refill them)."""
        conn.execute("DELETE FROM node_vec")
        conn.execute("DELETE FROM chunks")

    def count(self, conn: sqlite3.Connection) -> int:
        """Return the number of embedded chunks."""
        row = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
        return int(row["n"])

    def apply(self, conn: sqlite3.Connection, event: sqlite3.Row) -> None:
        """Re-embed the node affected by one event, if any.

        Args:
            conn: The open database connection.
            event: A raw ``events`` row: ``node.*`` re-chunk and re-embed the
                affected node, ``undo`` drops its chunks and vectors, and
                edge events are ignored.
        """
        op = event["op"]
        payload = json.loads(event["payload"])
        if op == "undo":
            self._apply_undo(conn, payload)
        elif op.startswith("node."):
            after = payload.get("after")
            before = payload.get("before")
            if after is not None:
                self._upsert(conn, after)
            elif before is not None:
                # A rollback reversing a create — see the FTS projector's twin.
                self._delete(conn, before["id"])

    def _apply_undo(self, conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
        """Mirror an undo: re-embed the restored node or drop a reverted create."""
        if not str(payload.get("reversed_op", "")).startswith("node."):
            return
        restored = payload.get("restored")
        if restored is not None:
            self._upsert(conn, restored)
            return
        for entry in payload.get("deleted", []):
            if entry.get("table") == "nodes":
                self._delete(conn, entry["row"]["id"])

    def _upsert(self, conn: sqlite3.Connection, node: dict[str, Any]) -> None:
        """Replace a node's chunks with fresh embeddings of its current text."""
        provider = embeddings.get_provider()
        if provider is None:  # availability is checked per run; this is a guard
            reason = embeddings.unavailable_reason()
            raise RuntimeError(f"vec projector lost its provider: {reason}")
        self._delete(conn, node["id"])
        texts = embeddings.node_chunks(node)
        if not texts:
            return
        for seq, (text, vector) in enumerate(zip(texts, provider.embed(texts), strict=True)):
            cursor = conn.execute(
                "INSERT INTO chunks (node_id, seq, text, model_id) VALUES (?, ?, ?, ?)",
                (node["id"], seq, text, provider.model_id),
            )
            conn.execute(
                "INSERT INTO node_vec (rowid, embedding) VALUES (?, ?)",
                (cursor.lastrowid, sqlite_vec.serialize_float32(vector)),
            )

    def _delete(self, conn: sqlite3.Connection, node_id: str) -> None:
        """Drop a node's chunks and their vectors (vec0 rows delete by rowid)."""
        conn.execute(
            "DELETE FROM node_vec WHERE rowid IN (SELECT id FROM chunks WHERE node_id = ?)",
            (node_id,),
        )
        conn.execute("DELETE FROM chunks WHERE node_id = ?", (node_id,))


#: The projector registry, keyed by name. New derived stores register here.
REGISTRY: dict[str, Projector] = {
    projector.name: projector for projector in (FtsProjector(), VecProjector())
}


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


def _record_skip(conn: sqlite3.Connection, name: str, event: sqlite3.Row, exc: Exception) -> None:
    """Quarantine one event a projector refused, in the projector's transaction.

    The row is upserted: a rebuild that replays the same still-bad event
    refreshes the record rather than colliding on the ``(projector, seq)``
    primary key.
    """
    conn.execute(
        """
        INSERT INTO projector_skips (projector, seq, op, error, created_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(projector, seq) DO UPDATE SET
            error = excluded.error,
            created_at = excluded.created_at
        """,
        (name, event["seq"], event["op"], f"{type(exc).__name__}: {exc}"),
    )


def _run_one(conn: sqlite3.Connection, projector: Projector) -> ProjectorRun:
    """Apply every event past the checkpoint in one self-contained transaction.

    The projector's whole batch — its writes and its checkpoint together —
    commits in a single transaction (finding M11), so the batch is atomic
    and a failure in one projector can never discard another projector's
    already-committed work. Within the batch:

    * an event whose ``apply`` raises ``ValueError``, ``KeyError`` or
      ``AttributeError`` — the exceptions a malformed payload raises: the
      JSON decode, a payload that is not a dict, a missing key — is
      quarantined in ``projector_skips`` and the checkpoint advances past it,
      so one bad row cannot wedge the projector forever (finding M12);
    * any other exception is systemic — a provider outage, a database error —
      and aborts the batch: the transaction rolls back and the exception
      propagates, so a systemic failure is never hidden as a run of skips.
      The same abort applies when *every* event in the batch was skipped: an
      all-malformed backlog is a writer bug, not N independent corruptions,
      and the checkpoint stays put until a human looks.

    An unavailable projector is a no-op — the checkpoint stays put so the
    backlog is picked up once the blocker clears.
    """
    from_seq = _checkpoint(conn, projector.name)
    available, reason = projector.availability()
    if not available:
        return ProjectorRun(
            name=projector.name, applied=0, from_seq=from_seq, to_seq=from_seq, detail=reason
        )
    rows = conn.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq", (from_seq,)).fetchall()
    if not rows:
        return ProjectorRun(name=projector.name, applied=0, from_seq=from_seq, to_seq=from_seq)
    try:
        skipped = 0
        last_error: Exception | None = None
        for event in rows:
            try:
                projector.apply(conn, event)
            except (ValueError, KeyError, AttributeError) as exc:
                _record_skip(conn, projector.name, event, exc)
                skipped += 1
                last_error = exc
            # The checkpoint advances past every event, skipped or applied —
            # a skipped event must not be replayed (and re-failed) next run.
            _set_checkpoint(conn, projector.name, event["seq"])
        if skipped == len(rows) and last_error is not None:
            raise last_error
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return ProjectorRun(
        name=projector.name,
        applied=len(rows) - skipped,
        from_seq=from_seq,
        to_seq=rows[-1]["seq"],
        skipped=skipped,
    )


# ── Public API ────────────────────────────────────────────────────────────────


def run_projectors(
    *, names: list[str] | None = None, path: str | Path | None = None
) -> list[ProjectorRun]:
    """Bring projectors up to date with the event log.

    Each projector's batch runs in its own transaction (:func:`_run_one`),
    so a failure in one projector rolls back only its own batch — the other
    projectors' committed work survives (finding M11). A systemic failure (a
    provider outage, a database error, a backlog whose every event is
    malformed) propagates: the failing projector's batch rolls back and the
    exception surfaces to the caller, with the other projectors' work already
    committed.

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
        return [_run_one(conn, projector) for projector in selected]
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
        ValueError: If the name is not in the registry, or the projector is
            unavailable (rebuilding would empty the store without being able
            to refill it).
    """
    (projector,) = _resolve([name])
    available, reason = projector.availability()
    if not available:
        raise ValueError(f"cannot rebuild projector {name!r}: {reason}")
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
    """Report every projector's checkpoint, backlog, store size, and availability.

    ``skipped`` counts the events the projector has quarantined instead of
    applying (finding M12) — a non-zero count is the signal to read
    :func:`list_skips` for the rows behind it.
    """
    conn = _connect(path)
    try:
        max_seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM events").fetchone()
        max_seq = int(max_seq_row["m"])
        statuses = []
        for projector in REGISTRY.values():
            available, reason = projector.availability()
            note = projector.mixed_model_note(conn) if available else None
            checkpoint = _checkpoint(conn, projector.name)
            skipped_row = conn.execute(
                "SELECT COUNT(*) AS n FROM projector_skips WHERE projector = ?",
                (projector.name,),
            ).fetchone()
            statuses.append(
                ProjectorStatus(
                    name=projector.name,
                    last_event_seq=checkpoint,
                    pending_events=max_seq - checkpoint,
                    rows=projector.count(conn),
                    available=available,
                    detail=note or reason,
                    skipped=int(skipped_row["n"]),
                )
            )
        return statuses
    finally:
        conn.close()


def list_skips(*, path: str | Path | None = None) -> list[ProjectorSkip]:
    """Return every quarantined event, one row per (projector, seq).

    The read surface behind the ``skipped`` counts on
    :class:`ProjectorRun` / :class:`ProjectorStatus`: a human who sees a
    non-zero count comes here for the error each skip recorded (finding
    M12). Rows are newest-first within each projector.
    """
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT projector, seq, op, error, created_at FROM projector_skips"
            " ORDER BY projector, seq DESC"
        ).fetchall()
        return [
            ProjectorSkip(
                projector=row["projector"],
                seq=row["seq"],
                op=row["op"],
                error=row["error"],
                created_at=row["created_at"],
            )
            for row in rows
        ]
    finally:
        conn.close()
