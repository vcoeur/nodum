"""Hybrid search over the graph — BM25 + vector ANN fused, then graph expansion.

The three retrieval signals of design §7:

1. **Keyword (BM25)** — FTS5 over node title + content (the ``fts`` projector).
2. **Semantic (vector)** — sqlite-vec ANN over chunk embeddings (the ``vec``
   projector); skipped entirely when no embedding provider is available, so
   search degrades to BM25 + graph without crashing.
3. **Graph expansion** — optional one-hop expansion of the fused hits along
   ``active`` edges, weighted by edge type × confidence.

BM25 and vector lists are fused by **reciprocal rank fusion**: each signal
contributes ``1 / (RRF_K + rank)`` per hit, the fused ``score`` is the sum,
and ``signals`` carries each contribution, so the breakdown explains the
score exactly. Graph expansion runs on the fused list (post-fusion).

Both projectors are caught up before querying, so search always reflects the
latest committed events without a manual projector run.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

from nodum import db, embeddings, projectors
from nodum.models import SearchHit, SearchResult

#: bm25() column weights for (node_id, title, content, extracted_text): node_id
#: is unindexed (weight ignored); a title hit outranks a body hit.
_BM25_WEIGHTS = (0.0, 5.0, 1.0, 1.0)

#: Reciprocal-rank-fusion damping constant (design §7): the standard 60 —
#: top ranks dominate, deep ranks contribute little but still break ties.
_RRF_K = 60

#: ANN candidates fetched per query before aggregating chunks to nodes —
#: several chunks can belong to the same node, so the raw list is wider.
_VECTOR_CANDIDATES = 32

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


def _node_filters(
    state: str | None,
    type_id: str | None,
    created_by: str | None,
    created_after: str | None,
    created_before: str | None,
) -> tuple[list[str], list]:
    """Build the shared ``nodes``-table WHERE clauses (alias ``n``) and params."""
    clauses: list[str] = []
    params: list = []
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
    return clauses, params


class _RankedRow:
    """One signal's ranked candidate: a node plus its display shape."""

    __slots__ = ("node_id", "type", "title", "snippet")

    def __init__(self, node_id: str, type: str, title: str | None, snippet: str) -> None:
        self.node_id = node_id
        self.type = type
        self.title = title
        self.snippet = snippet


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
) -> list[_RankedRow]:
    """Run the BM25-ranked FTS query, best (most-negative bm25) first."""
    filters, params = _node_filters(state, type_id, created_by, created_after, created_before)
    clauses = ["node_fts MATCH ?", *filters]
    weights = ", ".join(str(w) for w in _BM25_WEIGHTS)
    rows = conn.execute(
        f"""
        SELECT n.id, n.type_id, n.title,
               snippet(node_fts, 2, ?, ?, '…', 40) AS snippet
        FROM node_fts
        JOIN nodes n ON n.id = node_fts.node_id
        WHERE {" AND ".join(clauses)}
        ORDER BY bm25(node_fts, {weights})
        LIMIT ?
        """,
        (_SNIPPET_PRE, _SNIPPET_POST, match, *params, k),
    ).fetchall()
    return [
        _RankedRow(
            node_id=row["id"],
            type=row["type_id"],
            title=row["title"],
            snippet=row["snippet"] or (row["title"] or ""),
        )
        for row in rows
    ]


def _search_vector(
    conn: sqlite3.Connection,
    query_vector: list[float],
    *,
    k: int,
    state: str | None,
    type_id: str | None,
    created_by: str | None,
    created_after: str | None,
    created_before: str | None,
) -> list[_RankedRow]:
    """Run the sqlite-vec ANN query, aggregating chunks to nodes (best chunk wins).

    The raw KNN pulls :data:`_VECTOR_CANDIDATES` chunks (a node can own
    several), then each node keeps its closest chunk's distance and text.
    There is no similarity threshold — the ANN list is always ``k`` deep,
    which is exactly what RRF expects: a weak vector hit still fuses, just
    with a small contribution.
    """
    filters, params = _node_filters(state, type_id, created_by, created_after, created_before)
    clauses = filters or ["1=1"]
    rows = conn.execute(
        f"""
        SELECT n.id, n.type_id, n.title, c.text AS chunk_text, MIN(knn.distance) AS distance
        FROM (
            SELECT rowid AS chunk_id, distance
            FROM node_vec
            WHERE embedding MATCH ? AND k = ?
        ) AS knn
        JOIN chunks c ON c.id = knn.chunk_id
        JOIN nodes n ON n.id = c.node_id
        WHERE {" AND ".join(clauses)}
        GROUP BY n.id
        ORDER BY distance
        LIMIT ?
        """,
        (
            sqlite_vec.serialize_float32(query_vector),
            max(k * 4, _VECTOR_CANDIDATES),
            *params,
            k,
        ),
    ).fetchall()
    return [
        _RankedRow(
            node_id=row["id"],
            type=row["type_id"],
            title=row["title"],
            snippet=_chunk_snippet(row["chunk_text"]) or (row["title"] or ""),
        )
        for row in rows
    ]


def _chunk_snippet(text: str, *, length: int = 200) -> str:
    """Collapse a chunk to a single-line snippet of at most ``length`` chars."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= length:
        return collapsed
    return collapsed[: length - 1].rstrip() + "…"


def _fuse(
    bm25_rows: list[_RankedRow],
    vector_rows: list[_RankedRow],
    *,
    k: int,
) -> list[SearchHit]:
    """Reciprocal-rank fusion of the BM25 and vector lists (design §7).

    Each signal contributes ``1 / (RRF_K + rank)`` (1-based rank); a hit's
    fused ``score`` is the sum of its contributions and ``signals`` carries
    them per signal, so the breakdown sums to the score exactly. Hits in only
    one list fuse with their single contribution. Ties break by node id for
    determinism.
    """
    signals: dict[str, dict[str, float]] = {}
    shapes: dict[str, _RankedRow] = {}
    for rank, row in enumerate(bm25_rows, start=1):
        signals.setdefault(row.node_id, {})["bm25"] = 1.0 / (_RRF_K + rank)
        shapes[row.node_id] = row
    for rank, row in enumerate(vector_rows, start=1):
        signals.setdefault(row.node_id, {})["vector"] = 1.0 / (_RRF_K + rank)
        shapes.setdefault(row.node_id, row)
    ordered = sorted(signals, key=lambda node_id: (-sum(signals[node_id].values()), node_id))
    hits = []
    for node_id in ordered[:k]:
        shape = shapes[node_id]
        hits.append(
            SearchHit(
                node_id=node_id,
                type=shape.type,
                title=shape.title,
                snippet=shape.snippet,
                score=sum(signals[node_id].values()),
                signals=signals[node_id],
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
    """One-hop graph expansion: active-edge neighbors of the fused hits.

    Each neighbor's score is the strongest edge weight reaching it (type
    weight × confidence, design §7) and its ``signals`` carries only the
    ``graph`` signal, so expansion hits are distinguishable from direct
    matches. Fused hits are never re-emitted. Capped at ``k`` extra hits.
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
    """Hybrid-search node title + content: BM25 and vector signals, RRF-fused.

    Catches the ``fts`` and ``vec`` projectors up with the event log first,
    so results reflect the latest committed writes. The vector signal is
    skipped when no embedding provider is available — search then degrades
    to BM25 (+ graph expansion) without failing.

    Args:
        query: Free-text query; whitespace-separated terms are ANDed for
            BM25 and embedded whole for the vector signal.
        k: Maximum hits.
        state: Node-state filter (default ``active``); ``None`` searches all
            states.
        type: Optional node-type id/name filter.
        created_by: Optional writer filter (e.g. ``agent:researcher``).
        created_after: Only nodes created after this timestamp.
        created_before: Only nodes created before this timestamp.
        expand: Append one-hop active-edge neighbors of the fused hits
            (design §7 graph expansion), scored by edge type weight ×
            confidence.
        path: Explicit database path.

    Returns:
        RRF-fused hits, best first, then expansion hits when ``expand``.
        ``signals`` on each hit names the contributing signals (``bm25``,
        ``vector``, ``graph``).

    Raises:
        ValueError: If the query has no terms or the type does not resolve.
    """
    match = _match_query(query)
    # Derived indexes first: the projectors are incremental, so this is cheap.
    projectors.run_projectors(names=["fts", "vec"], path=path)
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
        bm25_rows = _search_bm25(
            conn,
            match,
            k=k,
            state=state,
            type_id=type_id,
            created_by=created_by,
            created_after=created_after,
            created_before=created_before,
        )
        vector_rows: list[_RankedRow] = []
        provider = embeddings.get_provider()
        if provider is not None:
            (query_vector,) = provider.embed([query])
            vector_rows = _search_vector(
                conn,
                query_vector,
                k=k,
                state=state,
                type_id=type_id,
                created_by=created_by,
                created_after=created_after,
                created_before=created_before,
            )
        hits = _fuse(bm25_rows, vector_rows, k=k)
        if expand and hits:
            hits += _expand_hits(conn, hits, k=k, state=state, type_id=type_id)
        return SearchResult(query=query, k=k, hits=hits)
    finally:
        conn.close()
