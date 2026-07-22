"""Keyword search over the graph — BM25 ranking via the FTS5 projector.

This is the first of the three retrieval signals (design §7). The query path
is shaped for the later hybrid fusion: every hit carries a fused ``score``
plus a per-signal ``signals`` breakdown, so reciprocal-rank fusion with the
vector signal and graph-expansion re-ranking slot in without reshaping the
API.

The ``fts`` projector is caught up before querying, so search always reflects
the latest committed events without a manual projector run.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from nodum import db, projectors
from nodum.models import SearchHit, SearchResult

#: bm25() column weights for (node_id, title, content, extracted_text): node_id
#: is unindexed (weight ignored); a title hit outranks a body hit.
_BM25_WEIGHTS = (0.0, 5.0, 1.0, 1.0)

#: Snippet markers around matched terms — Markdown bold, since content is
#: canonical Markdown and consumers (CLI agents, the future UI) render it.
_SNIPPET_PRE = "**"
_SNIPPET_POST = "**"


def _connect(path: str | Path | None) -> sqlite3.Connection:
    """Open a connection and apply any pending migrations (idempotent)."""
    conn = db.connect(path)
    db.init_db(conn)
    return conn


def _match_query(query: str) -> str:
    """Compile a free-text query into a safe FTS5 MATCH expression.

    Each whitespace-separated token becomes one double-quoted term, ANDed
    together — FTS5 operators and punctuation in the raw input can never break
    (or hijack) the expression.
    """
    terms = ['"' + token.replace('"', '""') + '"' for token in query.split() if token.strip()]
    if not terms:
        raise ValueError("query must contain at least one term")
    return " AND ".join(terms)


def _search_bm25(
    conn: sqlite3.Connection,
    match: str,
    *,
    k: int,
    state: str | None,
    type_id: str | None,
) -> list[SearchHit]:
    """Run the BM25-ranked FTS query and shape rows into search hits."""
    clauses = ["node_fts MATCH ?"]
    params: list = [match]
    if state is not None:
        clauses.append("n.state = ?")
        params.append(state)
    if type_id is not None:
        clauses.append("n.type_id = ?")
        params.append(type_id)
    weights = ", ".join(str(w) for w in _BM25_WEIGHTS)
    rows = conn.execute(
        f"""
        SELECT n.id, n.type_id, n.title,
               bm25(node_fts, {weights}) AS rank,
               snippet(node_fts, 2, ?, ?, '…', 40) AS snippet
        FROM node_fts
        JOIN nodes n ON n.id = node_fts.node_id
        WHERE {" AND ".join(clauses)}
        ORDER BY rank
        LIMIT ?
        """,
        (_SNIPPET_PRE, _SNIPPET_POST, *params, k),
    ).fetchall()
    hits = []
    for row in rows:
        # bm25() ranks more-negative-is-better; negate so a higher fused score
        # is better — the convention the later RRF fusion expects.
        score = -float(row["rank"])
        hits.append(
            SearchHit(
                node_id=row["id"],
                type=row["type_id"],
                title=row["title"],
                snippet=row["snippet"] or (row["title"] or ""),
                score=score,
                signals={"bm25": score},
            )
        )
    return hits


def search(
    query: str,
    *,
    k: int = 10,
    state: str | None = "active",
    type: str | None = None,
    path: str | Path | None = None,
) -> SearchResult:
    """Keyword-search node title + content (+ extracted asset text, later).

    Catches the ``fts`` projector up with the event log first, so results
    reflect the latest committed writes.

    Args:
        query: Free-text query; whitespace-separated terms are ANDed.
        k: Maximum hits.
        state: Node-state filter (default ``active``); ``None`` searches all
            states.
        type: Optional node-type id/name filter.
        path: Explicit database path.

    Returns:
        BM25-ranked hits, best first.

    Raises:
        ValueError: If the query has no terms or the type does not resolve.
    """
    match = _match_query(query)
    # Derived index first: the projector is incremental, so this is cheap.
    projectors.run_projectors(names=["fts"], path=path)
    conn = _connect(path)
    try:
        type_id = None
        if type is not None:
            row = conn.execute(
                "SELECT id FROM types WHERE id = ? OR name = ?", (type, type)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown node type: {type}")
            type_id = row["id"]
        hits = _search_bm25(conn, match, k=k, state=state, type_id=type_id)
        return SearchResult(query=query, k=k, hits=hits)
    finally:
        conn.close()
