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

The keyword signal matches on a **quorum of the query's discriminating
weight**, not on every term: see :func:`_compile_match`. A conjunctive matcher
returns nothing as soon as one term of a question is absent, which on an
install with no embedding provider — the default — makes a question-shaped
query answer with silence. The weight is measured over the query's *content*
words only: a graph small enough that "what" sits in 7 rows of 47 gives its
question words more inverse-document-frequency weight than the term that
answers the question, so function words are named in a list
(:data:`_QUERY_STOPWORDS`) rather than inferred from a frequency that has not
got the corpus to say it with. They never decide a search on their own: a query
the graph knows no content word of answers with **nothing from the keyword
arm**, rather than with the notes that happen to share its phrasing.

**That refusal is the keyword arm's, and it is not the whole of `search`.**
:func:`_search_vector` has no similarity threshold — the ANN list is always
``k`` deep — so on an install that *has* an embedding provider the vector arm
answers the query the keyword arm just refused, and the caller gets ``k`` hits
each carrying the ``vector`` signal alone. On the default install, which has no
provider, the refusal is the whole answer. Both are pinned:
``tests/test_search.py`` for the keyword arm, ``tests/test_hybrid_search.py``
for what the vector arm does beside it. Closing the gap means a similarity
floor, and a floor is a number nothing here can measure yet: it needs a real
embedding model over a real graph, because the test provider's similarity is
token overlap and every threshold measured against it would look free while
costing the paraphrase recall the vector arm exists for. Until then the
disagreement is stated rather than papered over.

Both projectors are caught up before querying, so search always reflects the
latest committed events without a manual projector run.
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import NamedTuple

import sqlite_vec

from nodum import db, embeddings, projectors
from nodum.migrations import META_SPACE_ID
from nodum.models import SearchHit, SearchResult
from nodum.principal import READ, Principal
from nodum.service import require_positive_limit

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

#: The quorum: a node is a keyword candidate when the query terms it carries
#: are worth at least this fraction of the query's total term weight. Half —
#: measured, not chosen: below it the result list fills with nodes that share
#: one word with the question, and above it the rule stops doing anything at
#: all on a small graph, which is the size every graph starts at.
_QUORUM_WEIGHT_FRACTION = 0.5

#: A term in more than this fraction of the indexed rows is dropped from the
#: query before the quorum is computed. It is a *cost* rule, not a relevance
#: one: such a term's weight is near zero either way, but leaving it in the
#: match expression makes FTS5 walk a doclist the size of the graph.
_COMMON_TERM_DF_FRACTION = 0.5

#: English function words, dropped from the query beside the ubiquitous ones.
#: The ceiling above is an *estimator* of "this word separates nothing", and on
#: a small graph it is a broken one: a graph of short claims holds "what" in 7
#: rows of 47, which is under any fraction worth setting, so the question words
#: of a question outweigh the one term that answers it. Document frequency
#: cannot tell a function word from a term at that size, and 47 rows is what
#: every graph starts at — so the property is stated directly instead, as a
#: list that does not move with corpus size. Nothing here carries topic meaning
#: in a technical graph: `state`, `store`, `key`, `value`, `log`, `set`,
#: `order`, `time`, `point`, `case`, `long`, `work`, `mean`, `group` and
#: `number` are all deliberately absent, and a query left with nothing else
#: falls back to searching these words like any other.
#:
#: Split from a string rather than written as a list literal (SIM905) because
#: the formatter puts a list literal one word to a line, and 200 lines of that
#: is a list nobody will read to check what is in it.
_QUERY_STOPWORDS = frozenset(
    """
a an the this that these those
i me my mine myself we us our ours you your yours he him his she her hers
it its they them their theirs one ones oneself
what which who whom whose when where why how whether
am is are was were be been being
do does did doing done
have has had having
can could will would shall should may might must ought
let lets get gets got go goes going gone went
make makes made making need needs want wants
about above across after against along among and any anybody anyone anything
around as at away
back because before behind below beneath beside besides between beyond both
but by
despite down during
each either else enough even ever every everybody everyone everything except
few for from further
here hereby however
if in inside instead into
just
least less like likely
many maybe more most much
near neither never no nobody none nor not nothing now
of off often on once only onto or other others otherwise out outside over own
per perhaps please
quite
rather really
same several since so some somebody someone something sometimes still such
than then there therefore they though through throughout
thus till to together too toward towards
under underneath unless until up upon
very via
whatever whenever wherever while whilst with within without
yet
""".split()  # noqa: SIM905
)

#: The most distinct terms one query may carry. Above 500 usable terms the
#: quorum's ``UNION ALL`` hits ``SQLITE_LIMIT_COMPOUND_SELECT`` and SQLite
#: raises — which reached the caller as a **503** from ``GET /api/search`` and
#: ``POST /api/ask`` and as *"database error"* from the CLI, three storage-voice
#: failures for what is plainly an oversized request. The cap is what makes it
#: a 400 instead, and it is set far above any query this system produces: the
#: model's own rewrite is capped at eight terms
#: (:data:`nodum.answers.MAX_REWRITE_TERMS`) and the longest question measured
#: is eleven.
_MAX_QUERY_TERMS = 64

#: Characters trimmed off both ends of a token before it is compared to another
#: token or to :data:`_QUERY_STOPWORDS`. The double quote is FTS5's own phrase
#: syntax, the rest is the punctuation a human types around a word — a question
#: mark rides along on the last word of every question. Only the *ends* are
#: trimmed, which is what keeps ``min.insync.replicas`` and ``c++`` one term.
_TERM_TRIM = "\"?!.,;:()[]{}'…"


def _bare_word(token: str) -> str:
    """Fold a raw or quoted token to the word FTS5 will tokenize it to.

    Case and edge punctuation are exactly what the ``porter unicode61``
    tokenizer discards, so this is the comparison two tokens are "the same
    word" under. One helper rather than two, because the two callers
    disagreeing about that was a defect: :func:`_is_function_word` stripped
    punctuation before its lookup and :func:`_query_terms` did not before its
    dedup, so ``kafka,`` and ``kafka`` were one function-word question and two
    distinct quorum terms at the same time.
    """
    return token.strip(_TERM_TRIM).casefold()


def _connect(path: str | Path | None) -> sqlite3.Connection:
    """Open a connection and apply any pending migrations (idempotent)."""
    conn = db.connect(path)
    db.init_db(conn)
    return conn


def _query_terms(query: str) -> list[str]:
    """Split a free-text query into safe FTS5 terms, in order, without repeats.

    Each whitespace-separated token becomes one double-quoted term, so FTS5
    operators and punctuation in the raw input can never break (or hijack) the
    expression: ``OR`` is the word "or", ``c++`` is a term rather than a syntax
    error, and an embedded quote is doubled rather than closing the string.
    Nothing else in this module builds a MATCH expression, so this is the one
    place a user string becomes FTS5 syntax.

    Repeats are dropped because the quorum weighs terms: typing a word twice
    would otherwise count its weight twice, both in what a document must reach
    and in what it can collect. "The same word" is :func:`_bare_word` — what
    the FTS5 tokenizer will reduce the token to — and not the raw token, which
    is where this went wrong: folding case alone left ``kafka,`` and ``kafka``
    two terms carrying one word's document frequency twice, enough to clear a
    bar half of itself and turn the two-equal-terms rule back into the bare
    disjunction it was chosen over. The token kept is the first one seen, still
    quoted whole: FTS5 tokenizes the punctuation away itself, so ``"kafka,"``
    matches the same rows as ``"kafka"`` and there is nothing to gain by
    rewriting what the caller typed.

    Raises:
        ValueError: If the query has no terms, or more than
            :data:`_MAX_QUERY_TERMS` distinct ones — both are malformed
            requests, and the second used to be a storage failure (see the
            constant).
    """
    seen: dict[str, str] = {}
    for token in query.split():
        if token.strip():
            seen.setdefault(_bare_word(token), '"' + token.replace('"', '""') + '"')
    if not seen:
        raise ValueError("query must contain at least one term")
    if len(seen) > _MAX_QUERY_TERMS:
        raise ValueError(
            f"query has {len(seen)} distinct terms; at most {_MAX_QUERY_TERMS} are searched"
        )
    return list(seen.values())


def _is_function_word(term: str) -> bool:
    """Whether a quoted term is one of :data:`_QUERY_STOPWORDS`.

    The term is quoted FTS5 syntax by now, and a question mark rides along on
    the last word of every question, so both come off before the lookup
    (:func:`_bare_word`): ``"against?"`` is the same function word as
    ``against``. Inner punctuation stays, which is what keeps
    ``min.insync.replicas`` one term.
    """
    return _bare_word(term) in _QUERY_STOPWORDS


class _MatchPlan(NamedTuple):
    """A compiled query: the FTS5 expression plus the quorum restriction.

    ``cte`` is a ``WITH`` prefix and ``clause`` a WHERE fragment naming it.
    Both empty means only that there is no quorum to apply, and **three
    different plans share that shape** — reading it as "permissive" is wrong
    for one of them:

    * the quorum is **unnecessary**: no rows to search, one term, or one
      surviving term. The query is the bare expression and costs exactly what
      it did before.
    * the quorum is **given up**: ``plain``, every term restored because the
      drops left nothing at all.
    * the quorum is **refused**: :func:`_compile_match`'s early return, where
      ``match`` is deliberately an expression the index cannot satisfy —
      the content words of a query the graph has never seen one of. This one
      is the *opposite* of permissive, and it is the one a reader of this
      class alone would get backwards.

    Nothing downstream needs to tell them apart — :func:`_search_bm25` treats
    an empty ``cte``/``clause`` identically in all three — which is why the
    distinction lives here in prose rather than in a fourth field.
    """

    match: str
    cte: str
    cte_params: list
    clause: str


def _compile_match(
    conn: sqlite3.Connection,
    terms: list[str],
    *,
    filters: list[str],
    filter_params: list,
) -> _MatchPlan:
    """Compile terms into an ORed expression plus a quorum over their weight.

    The shipped rule was ``AND``, and it is the reason a question-shaped query
    answered with silence: FTS5 requires every term, so one word the graph does
    not happen to hold — *"how"*, *"my"*, or a term a model invented while
    rewriting the query — empties the result set. A bare ``OR`` fixes that and
    breaks precision instead, since every document holding one common word
    becomes a hit.

    So: **OR the terms, then require a quorum of their weight.** A term's
    weight is its BM25 inverse document frequency, which is a measure of how
    much it discriminates — so a rare term counts for more than a common one,
    and a document qualifies by carrying enough of the query's *discriminating
    power* rather than enough of its *words*.

    Three kinds of term are dropped before any of that, all for the same reason
    — they separate nothing:

    * **absent** (``df = 0``): BM25 already scores it zero, and keeping it
      would put weight in the denominator that no document could ever reach.
      This is the whole of why a hallucinated term stops mattering.
    * **everywhere** (``df`` above :data:`_COMMON_TERM_DF_FRACTION`): its
      weight is near zero anyway, but leaving it in the match expression makes
      FTS5 walk a doclist the size of the graph to rank on it.
    * **a function word** (:data:`_QUERY_STOPWORDS`): the previous rule was
      the estimator for this one, and on a young graph it estimates wrong.
      A 47-row graph of short claims holds *what* in 7 rows and *does* in 8,
      both far under any ceiling worth setting, so the bar became mostly the
      weight of the question's phrasing — and the one node carrying the
      question's only rare term was excluded by exactly the words it does not
      contain. The list is the same on 47 rows and on 312, which is the whole
      point of it being a list.

    **A function word never decides a search on its own.** Every drop above
    needs a fallback, and the order they are given up in is the whole of what
    the fallbacks mean. The ubiquity cut goes first, because it is the only
    one of the three that is about cost rather than meaning: a young graph is
    usually *about* something, its subject is therefore in most of its rows,
    and dropping it left *"What is kafka?"* matching every note that says
    "what" and none that says "kafka" — the inverse of the answer. Only a
    query with **no content word at all** ("what is it") falls back to its
    function words; a query whose content words the graph has simply never
    seen answers with the nothing those words alone would have answered with,
    because a ranked list of notes sharing a question's phrasing is a
    confident answer to a word the graph has never heard of.

    **What that early return tests is "no content word *known*", not "no
    content word that *discriminates*"** — and the difference is not small.
    An ordinary question carries ordinary English nouns and verbs that
    :data:`_QUERY_STOPWORDS` deliberately leaves on the content side, so one
    of them being in the index is enough to keep the query alive: measured on
    the claim graph of ``tests/test_search.py``, **six of twelve** questions
    built around an invented subject go silent and six still answer, stable at
    40, 72, 136 and 264 rows, against none of twelve silent under the previous
    ordering. This rule closes half of that shape, not all of it.

    Widening the gate to "no content word at or under the ceiling" was measured
    and **rejected**, because it collides head-on with the ubiquity-first
    relaxation above. On a graph about one subject it takes the subject with
    it: over 38, 56, 120 and 308 rows, *"What is kafka?"* goes from 30 hits to
    **0** at every size. The surgical variant — refuse only when an unknown
    content word sits beside an over-ceiling one — keeps that question but
    takes ``kafka concretoid`` from 30 hits to 0, which is the E3 guarantee
    that a hallucinated term must not empty a query, and it re-fires on
    ``apple zarquon`` inside a six-row read set. Neither variant closes one
    extra invented-subject question at any of those sizes. **The two rules
    cannot both be maximised**, and the ubiquity relaxation is worth more,
    because a graph that is about one subject is the graph every graph starts
    as. ``tests/test_search.py`` pins both halves of that trade.

    Each surviving term's document frequency is counted **over the rows this
    search can actually return** — the same ``filters`` the ranked query
    applies. Counting the whole index instead made the bar depend on rows the
    caller cannot read: one word planted in a private space turned six hits
    into none, which is an existence oracle over every space in the file for
    anything holding ``search`` (it is in ``mcp_server.READ_TOOLS``). Scoping
    also makes the weight *right*, since a term's rarity is a property of the
    corpus being searched.

    A query left with one term needs no quorum — matching it *is* the quorum —
    so a one-word search runs exactly the statement it ran before. With exactly
    **two** terms the comparison is strict, because equal document frequency
    gives equal weight and ``>=`` then admits either term alone: the quorum
    silently becomes the bare OR it was chosen over, measured at 0.111
    precision over the returned list against 0.722. The strictness is gated on
    the two-term case rather than applied throughout: with four equal terms a
    blanket ``>`` would move the bar from two-of-four to three-of-four, which
    is a quorum nobody chose.

    The cost is one index probe per term plus one row count, all of which scale
    with the *number* of indexed nodes rather than their text (measured: 0.02 ms
    per probe over 312 rows holding 6.6 MB). The term count is capped
    (:data:`_MAX_QUERY_TERMS`), so both are bounded.
    """
    scope = " AND ".join(filters) if filters else "1 = 1"
    total_rows = conn.execute(
        f"SELECT count(*) AS n FROM node_fts JOIN nodes n ON n.id = node_fts.node_id WHERE {scope}",
        filter_params,
    ).fetchone()["n"]
    plain = _MatchPlan(match=" OR ".join(terms), cte="", cte_params=[], clause="")
    if total_rows == 0 or len(terms) == 1:
        return plain
    frequencies = [
        (
            term,
            conn.execute(
                "SELECT count(*) AS n FROM node_fts JOIN nodes n ON n.id = node_fts.node_id"
                f" WHERE node_fts MATCH ? AND {scope}",
                (term, *filter_params),
            ).fetchone()["n"],
        )
        for term in terms
    ]
    ceiling = max(1, int(total_rows * _COMMON_TERM_DF_FRACTION))
    content = [term for term in terms if not _is_function_word(term)]
    known = [(term, df) for term, df in frequencies if df > 0]
    if content:
        known_content = [(term, df) for term, df in known if not _is_function_word(term)]
        # The ubiquity cut is relaxed first, because it is a *cost* rule: on a
        # graph about one subject the subject is over the ceiling, and saving a
        # doclist walk is worth less than the word the query is about. That
        # hands the saving back on exactly the shape the cut was written for,
        # and the number is worth having: measured on a single-subject graph at
        # 80/170/320 rows, "What is kafka?" costs +19 % to +29 % with ~9 KB
        # documents (where the walk is real) and −1 % to +6 % with short ones.
        kept = [(term, df) for term, df in known_content if df <= ceiling] or known_content
        if not kept:
            # The graph has never seen a content word of this query, so the
            # honest answer is the one the content words alone give: nothing.
            # Searching the phrasing instead answered a word the graph has
            # never heard of with prose notes that share only its question
            # words — a plausible list, and nothing saying it is not an answer.
            return _MatchPlan(match=" OR ".join(content), cte="", cte_params=[], clause="")
    else:
        # Nothing but function words ("what is it"). The query the caller
        # actually typed is the best evidence available, so it is searched —
        # still as a quorum, never as the bare disjunction.
        kept = [(term, df) for term, df in known if df <= ceiling] or known
        if not kept:
            return plain
    weights = [(term, math.log(1 + (total_rows - df + 0.5) / (df + 0.5))) for term, df in kept]
    match = " OR ".join(term for term, _ in weights)
    if len(weights) == 1:
        return _MatchPlan(match=match, cte="", cte_params=[], clause="")
    branches = " UNION ALL ".join(
        "SELECT node_id, ? AS weight FROM node_fts WHERE node_fts MATCH ?" for _ in weights
    )
    params: list = []
    for term, weight in weights:
        params.extend((weight, term))
    params.append(_QUORUM_WEIGHT_FRACTION * sum(weight for _, weight in weights))
    comparison = ">" if len(weights) == 2 else ">="
    return _MatchPlan(
        match=match,
        cte=(
            f"WITH matched(node_id, weight) AS ({branches}), quorum(node_id) AS ("
            f" SELECT node_id FROM matched GROUP BY node_id HAVING SUM(weight) {comparison} ?)"
        ),
        cte_params=params,
        clause="node_fts.node_id IN (SELECT node_id FROM quorum)",
    )


def _resolve_space(conn: sqlite3.Connection, space_ref: str, principal: Principal) -> str:
    """Resolve a space id or name to its id for the filter, or raise.

    The same rule the service applies (``service._resolve_space``): a space the
    principal holds no grant on does not resolve, so an existing-but-ungranted
    space and a nonexistent one answer identically and the filter is not an
    existence oracle. ``ValueError`` rather than the service's ``TypeNotFound``,
    matching how this module already refuses an unknown ``type`` filter.
    """
    row = conn.execute(
        "SELECT id FROM nodes WHERE (id = ? OR title = ?) AND type_id = 'space'"
        " AND state = 'active'",
        (space_ref, space_ref),
    ).fetchone()
    if row is None or principal.level_on(row["id"]) < READ:
        raise ValueError(f"unknown space: {space_ref}")
    return row["id"]


def _node_filters(
    state: str | None,
    type_id: str | None,
    created_by: str | None,
    created_after: str | None,
    created_before: str | None,
    include_meta: bool,
    space_id: str | None,
    principal: Principal | None,
) -> tuple[list[str], list]:
    """Build the shared ``nodes``-table WHERE clauses (alias ``n``) and params.

    An agent principal is confined to its read set (Q13); a human (or the
    trusted-local default) just skips the meta space unless ``include_meta``.

    ``space_id`` narrows further and never wider: it is ANDed onto whichever
    of those two clauses applies, so an agent asking for a space outside its
    read set would match nothing even if the id reached here — which it cannot,
    since :func:`_resolve_space` refuses to resolve one. Naming the meta space
    is itself the ``include_meta`` opt-in, so the default exclusion applies only
    to an unnarrowed search.
    """
    clauses: list[str] = []
    params: list = []
    if principal is not None and not principal.is_human:
        spaces = sorted(principal.read_spaces or ())
        if not spaces:
            clauses.append("1 = 0")
        else:
            clauses.append(f"n.space_id IN ({','.join('?' * len(spaces))})")
            params.extend(spaces)
    elif not include_meta and space_id is None:
        clauses.append("n.space_id != ?")
        params.append(META_SPACE_ID)
    if space_id is not None:
        clauses.append("n.space_id = ?")
        params.append(space_id)
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

    __slots__ = ("node_id", "space_id", "type", "title", "snippet")

    def __init__(
        self,
        node_id: str,
        space_id: str | None,
        type: str,
        title: str | None,
        snippet: str,
    ) -> None:
        self.node_id = node_id
        self.space_id = space_id
        self.type = type
        self.title = title
        self.snippet = snippet


def _search_bm25(
    conn: sqlite3.Connection,
    terms: list[str],
    *,
    k: int,
    state: str | None,
    type_id: str | None,
    created_by: str | None,
    created_after: str | None,
    created_before: str | None,
    include_meta: bool,
    space_id: str | None,
    principal: Principal | None,
) -> list[_RankedRow]:
    """Run the BM25-ranked FTS query, best (most-negative bm25) first.

    The quorum restricts *which rows are candidates* and nothing else: ranking
    is still ``bm25()`` with the same weights over the same index, and the
    ``LIMIT`` is still ``k``. That is what keeps the change local — the list
    handed to fusion is the same shape and the same length it always was, so
    RRF's rank arithmetic and the post-fusion graph expansion are untouched.
    The quorum has to sit in the ``WHERE`` rather than filter the result:
    ranking first and filtering after would drop good rows off the end of
    ``LIMIT k`` before anything looked at them.

    The filters are built first and handed to :func:`_compile_match`, which
    counts document frequencies through them: the weights are over the corpus
    being searched, never over the whole index.
    """
    filters, params = _node_filters(
        state, type_id, created_by, created_after, created_before, include_meta, space_id, principal
    )
    plan = _compile_match(conn, terms, filters=filters, filter_params=params)
    clauses = ["node_fts MATCH ?", *([plan.clause] if plan.clause else []), *filters]
    weights = ", ".join(str(w) for w in _BM25_WEIGHTS)
    rows = conn.execute(
        f"""
        {plan.cte}
        SELECT n.id, n.space_id, n.type_id, n.title,
               snippet(node_fts, 2, ?, ?, '…', 40) AS snippet
        FROM node_fts
        JOIN nodes n ON n.id = node_fts.node_id
        WHERE {" AND ".join(clauses)}
        ORDER BY bm25(node_fts, {weights})
        LIMIT ?
        """,
        (*plan.cte_params, _SNIPPET_PRE, _SNIPPET_POST, plan.match, *params, k),
    ).fetchall()
    return [
        _RankedRow(
            node_id=row["id"],
            space_id=row["space_id"],
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
    include_meta: bool,
    space_id: str | None,
    principal: Principal | None,
) -> list[_RankedRow]:
    """Run the sqlite-vec ANN query, aggregating chunks to nodes (best chunk wins).

    The raw KNN pulls :data:`_VECTOR_CANDIDATES` chunks (a node can own
    several), then each node keeps its closest chunk's distance and text.
    There is no similarity threshold — the ANN list is always ``k`` deep,
    which is exactly what RRF expects: a weak vector hit still fuses, just
    with a small contribution.

    The cost of that is :func:`_compile_match`'s refusal not reaching here: a
    query whose content words the graph has never seen is answered ``k`` deep
    by this function, vector-signal-only, while the keyword arm returns
    nothing. See the module docstring for why a floor is not simply added.
    """
    filters, params = _node_filters(
        state, type_id, created_by, created_after, created_before, include_meta, space_id, principal
    )
    clauses = filters or ["1=1"]
    rows = conn.execute(
        f"""
        SELECT n.id, n.space_id, n.type_id, n.title, c.text AS chunk_text,
               MIN(knn.distance) AS distance
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
            space_id=row["space_id"],
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
                space_id=shape.space_id,
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
    include_meta: bool,
    space_id: str | None,
    principal: Principal | None,
) -> list[SearchHit]:
    """One-hop graph expansion: active-edge neighbors of the fused hits.

    Each neighbor's score is the strongest edge weight reaching it (type
    weight × confidence, design §7) and its ``signals`` carries only the
    ``graph`` signal, so expansion hits are distinguishable from direct
    matches. Fused hits are never re-emitted. Capped at ``k`` extra hits.

    Every filter the fused query applied applies here too, ``space_id``
    included: a search narrowed to one space does not reach back out of it
    one hop later. (What the *graph view* does with a cross-space edge is a
    different question — there the far endpoint is drawn dimmed rather than
    dropped, because a graph asserting a connection ends is asserting
    something false. A ranked list asserts nothing of the sort.)
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
        if principal is not None and not principal.is_human:
            if row["space_id"] not in (principal.read_spaces or ()):
                continue
        elif not include_meta and space_id is None and row["space_id"] == META_SPACE_ID:
            continue
        if space_id is not None and row["space_id"] != space_id:
            continue
        if state is not None and row["state"] != state:
            continue
        if type_id is not None and row["type_id"] != type_id:
            continue
        expanded.append(
            SearchHit(
                node_id=node_id,
                space_id=row["space_id"],
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
    include_meta: bool = False,
    space: str | None = None,
    expand: bool = False,
    principal: Principal,
    path: str | Path | None = None,
) -> SearchResult:
    """Hybrid-search node title + content: BM25 and vector signals, RRF-fused.

    Catches the ``fts`` and ``vec`` projectors up with the event log first,
    so results reflect the latest committed writes. The vector signal is
    skipped when no embedding provider is available — search then degrades
    to BM25 (+ graph expansion) without failing.

    Args:
        query: Free-text query, at most :data:`_MAX_QUERY_TERMS` distinct
            terms. The keyword signal keeps a node when the query terms it
            carries are worth at least half the query's total
            inverse-document-frequency weight, counting content words only
            (:func:`_compile_match`), so a question keeps working when the
            graph does not hold every one of its words; the vector signal
            embeds the query whole.
        k: Maximum hits.
        state: Node-state filter (default ``active``); ``None`` searches all
            states.
        type: Optional node-type id/name filter.
        created_by: Optional writer filter (e.g. ``agent:researcher``).
        created_after: Only nodes created after this timestamp.
        created_before: Only nodes created before this timestamp.
        include_meta: Include meta-space nodes (the type vocabulary, the
            spaces themselves) in an unnarrowed search.
        space: Optional space id or name to narrow the search to. It composes
            with the principal's scope — an agent is still confined to its
            grants, and a space it holds none on does not resolve — so this is
            the human's convenience filter, never a boundary. Naming the meta
            space is itself the ``include_meta`` opt-in.
        expand: Append one-hop active-edge neighbors of the fused hits
            (design §7 graph expansion), scored by edge type weight ×
            confidence.
        path: Explicit database path.

    Returns:
        RRF-fused hits, best first, then expansion hits when ``expand``.
        ``signals`` on each hit names the contributing signals (``bm25``,
        ``vector``, ``graph``).

    Raises:
        ValueError: If the query has no terms or more than
            :data:`_MAX_QUERY_TERMS`, if ``k`` is below 1, or if the
            type or space does not resolve — an ungranted space and a
            nonexistent one read alike. ``k`` goes through
            :func:`nodum.service.require_positive_limit`, the same helper every
            capped read in the service layer uses: it reaches three ranked
            queries as a SQL ``LIMIT``, so ``--k -2`` was read as *unbounded*
            and answered with everything the index held.
    """
    require_positive_limit(k, "k")
    # Term-splitting is the one validation that can fail before any work: an
    # empty query (or an oversized one) must not cost a projector run.
    terms = _query_terms(query)
    # Derived indexes first: the projectors are incremental, so this is cheap.
    projectors.run_projectors(names=["fts", "vec"], path=path)
    conn = _connect(path)
    try:
        type_id = None
        if type is not None:
            row = conn.execute(
                "SELECT id, space_id FROM nodes WHERE (id = ? OR title = ?) AND type_id = 'type'"
                " AND json_extract(props, '$.type_kind') = 'node' AND state = 'active'",
                (type, type),
            ).fetchone()
            # A type in an unreadable space does not resolve — the catalog is
            # not an existence oracle for the search filter either (review N1).
            if row is None or principal.level_on(row["space_id"]) < READ:
                raise ValueError(f"unknown node type: {type}")
            type_id = row["id"]
        space_id = _resolve_space(conn, space, principal) if space is not None else None
        bm25_rows = _search_bm25(
            conn,
            terms,
            k=k,
            state=state,
            type_id=type_id,
            created_by=created_by,
            created_after=created_after,
            created_before=created_before,
            include_meta=include_meta,
            space_id=space_id,
            principal=principal,
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
                include_meta=include_meta,
                space_id=space_id,
                principal=principal,
            )
        hits = _fuse(bm25_rows, vector_rows, k=k)
        if expand and hits:
            hits += _expand_hits(
                conn,
                hits,
                k=k,
                state=state,
                type_id=type_id,
                include_meta=include_meta,
                space_id=space_id,
                principal=principal,
            )
        return SearchResult(query=query, k=k, hits=hits)
    finally:
        conn.close()
