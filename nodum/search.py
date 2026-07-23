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

#: One-hop graph-expansion weights by edge type (design §7); unlisted types
#: default to 0.5. The edge's confidence (default 1.0) multiplies the weight.
_EXPANSION_TYPE_WEIGHTS = {"supports": 1.0, "relates_to": 0.5}

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
    created_by: str | None,
    created_after: str | None,
    created_before: str | None,
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
    if created_by is not None:
        clauses.append("n.created_by = ?")
        params.append(created_by)
    if created_after is not None:
        clauses.append("n.created_at > ?")
        params.append(created_after)
    if created_before is not None:
        clauses.append("n.created_at < ?")
        params.append(created_before)
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


def _expand_hits(
    conn: sqlite3.Connection,
    hits: list[SearchHit],
    *,
    k: int,
    state: str | None,
    type_id: str | None,
) -> list[SearchHit]:
    """One-hop graph expansion: active-edge neighbors of the BM25 hits.

    Each neighbor's score is the strongest edge weight reaching it (type
    weight × confidence, design §7) and its ``signals`` carries only the
    ``graph`` signal, so expansion hits are distinguishable from direct
    matches. BM25 hits are never re-emitted. Capped at ``k`` extra hits.
    """
    seen = {hit.node_id for hit in hits}
    weights: dict[str, float] = {}
    for hit in hits:
        rows = conn.execute(
            "SELECT * FROM edges WHERE state = 'active' AND (src_id = ? OR dst_id = ?)",
            (hit.node_id, hit.node_id),
        ).fetchall()
        for edge in rows:
            other = edge["dst_id"] if edge["src_id"] == hit.node_id else edge["src_id"]
            if other in seen:
                continue
            confidence = edge["confidence"] if edge["confidence"] is not None else 1.0
            weight = _EXPANSION_TYPE_WEIGHTS.get(edge["type_id"], 0.5) * confidence
            weights[other] = max(weights.get(other, 0.0), weight)
    expanded = []
    for node_id, weight in sorted(weights.items(), key=lambda item: -item[1])[:k]:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            continue
        if state is not None and row["state"] != state:
            continue
        if type_id is not None and row["type_id"] != type_id:
            continue
        expanded.append(
            SearchHit(
                node_id=node_id,
                type=row["type_id"],
                title=row["title"],
                snippet=row["title"] or "",
                score=weight,
                signals={"graph": weight},
            )
        )
    return expanded


def search(
    query: str,
    *,
    k: int = 10,
    state: str | None = "active",
    type: str | None = None,
    created_by: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    expand: bool = False,
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
        created_by: Optional writer filter (e.g. ``agent:researcher``).
        created_after: Only nodes created after this timestamp.
        created_before: Only nodes created before this timestamp.
        expand: Append one-hop active-edge neighbors of the hits (design §7
            graph expansion), scored by edge type weight × confidence.
        path: Explicit database path.

    Returns:
        BM25-ranked hits, best first, then expansion hits when ``expand``.

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
        hits = _search_bm25(
            conn,
            match,
            k=k,
            state=state,
            type_id=type_id,
            created_by=created_by,
            created_after=created_after,
            created_before=created_before,
        )
        if expand and hits:
            hits += _expand_hits(conn, hits, k=k, state=state, type_id=type_id)
        return SearchResult(query=query, k=k, hits=hits)
    finally:
        conn.close()
