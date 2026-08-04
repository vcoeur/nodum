import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { describeError, describeFailure } from "../../lib";
import type { SearchFilters, SearchHit, SearchResult, TypeOut } from "../../api/types";
import {
  ANY_SPACE,
  EmptyState,
  Spinner,
  unresolvedSpaceIds,
  useArchivedSpaces,
  useSpaces,
} from "../../components";
import { ResultRow } from "./ResultRow";
import { SearchFilterBar } from "./SearchFilterBar";
import { SignalLegend } from "./SignalBreakdown";
import {
  CLEARABLE_FILTERS,
  hasActiveFilters,
  readSearchState,
  toSearchParams,
} from "./searchState";
import type { SearchState } from "./searchState";
import { hitSpaceName } from "./resultSpace";
import { describeNoResults } from "./noResults";
import { describeSpaceFilterFailure } from "./spaceFailure";
import type { SpaceFilterFailure } from "./spaceFailure";
import { SIGNAL_HELP, SIGNAL_KEYS, describeSignals, readVectorEvidence } from "./signals";
import type { SignalKey } from "./signals";
import { queryTerms } from "./snippetTerms";
import "./search.css";

/**
 * The search view — one box over `GET /api/search`.
 *
 * Three decisions shape everything below.
 *
 * **The server's order is the product.** `nodum.search` fuses BM25 and vector
 * lists by reciprocal rank fusion and then appends graph-expansion neighbours,
 * whose scores are edge weights on a completely different scale. Sorting the
 * returned array by `score` on the client would float every neighbour above
 * every real match. So the list is rendered in arrival order, always, and the
 * "by signal" mode *partitions* it rather than re-ranking it.
 *
 * **The vector signal can be silently absent.** With no embedding provider the
 * server skips it and returns keyword-only results with no error and no flag.
 * The view infers it from the hits themselves and says so quietly — see
 * {@link readVectorEvidence} and the notice below.
 *
 * **Keyboard first.** The box takes focus on mount, `/` or Cmd/Ctrl-K returns to
 * it, arrows walk the results, Enter opens the editor (native link activation),
 * and → opens the hit as a subgraph.
 */

/** How long the query box waits after the last keystroke before searching. */
const DEBOUNCE_MS = 200;

/** Skeleton rows drawn during the very first search, so the list does not jump. */
const SKELETON_ROWS = 5;

/** The stable empty list a no-result render reads: a literal `[]` would be a
 * fresh identity every render, re-running every memo keyed on the hits. */
const EMPTY_HITS: SearchHit[] = [];

/** Group headers for the "by signal" arrangement. */
const GROUP_LABEL: Record<SignalKey, string> = {
  bm25: "Matched the words",
  vector: "Matched the meaning",
  graph: "Neighbours of matches",
};

/** What the view is doing right now. */
type Status = "idle" | "loading" | "ready" | "error";

/** One rendered section of the result list. */
interface DisplayGroup {
  key: string;
  label: string;
  help: string;
  hits: SearchHit[];
}

/**
 * Session-level evidence about the vector signal.
 *
 * Latched on purpose: a query that returns nothing tells us nothing, and a note
 * that blinks in and out as the user types is worse than no note at all. Once a
 * `vector` signal is seen the notice is gone for good — which is also how it
 * disappears if the provider becomes available mid-session.
 */
interface VectorEvidence {
  seen: boolean;
  missing: boolean;
}

/** The search view. Route: `/search` (also the app's landing view). */
export default function SearchView() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();

  const uiState = useMemo(() => readSearchState(params), [params]);

  const [draft, setDraft] = useState(uiState.query);
  const [result, setResult] = useState<SearchResult | null>(null);
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<unknown>(null);
  const [nodeTypes, setNodeTypes] = useState<TypeOut[] | null>(null);
  const [typesFailed, setTypesFailed] = useState(false);
  // The space filter's vocabulary. A failure is not fatal to searching — the
  // control says so and the filter stays whatever the URL carried, which is the
  // only honest thing to do with a space reference nothing can currently name.
  const { spaces, failed: spacesFailed } = useSpaces();
  const [vector, setVector] = useState<VectorEvidence>({ seen: false, missing: false });
  const [retryToken, setRetryToken] = useState(0);

  const inputRef = useRef<HTMLInputElement | null>(null);
  const linkRefs = useRef<(HTMLAnchorElement | null)[]>([]);
  const draftRef = useRef(draft);
  const committedRef = useRef(uiState.query);
  const requestSeq = useRef(0);

  const {
    query,
    type,
    state: stateFilter,
    createdBy,
    space,
    includeMeta,
    limit,
    expand,
    group,
  } = uiState;

  /* --- URL state ---------------------------------------------------- */

  useEffect(() => {
    draftRef.current = draft;
  }, [draft]);

  /**
   * Apply a partial change to the URL state.
   *
   * The pending draft rides along, so changing a filter mid-typing applies the
   * query the user has already entered rather than discarding it. `replace`
   * keeps a session of refinement out of the back button — Back should leave
   * the search, not rewind it one keystroke.
   */
  const applyPatch = useCallback(
    (patch: Partial<SearchState>) => {
      const next: SearchState = { ...uiState, query: draftRef.current, ...patch };
      committedRef.current = next.query;
      setParams(toSearchParams(next), { replace: true });
    },
    [uiState, setParams],
  );

  // The URL changed from outside the box (back/forward, a pasted link).
  useEffect(() => {
    if (query !== committedRef.current) {
      committedRef.current = query;
      setDraft(query);
    }
  }, [query]);

  // Debounced commit of the query box into the URL.
  useEffect(() => {
    if (draft === committedRef.current) return;
    const timer = window.setTimeout(() => applyPatch({ query: draft }), DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [draft, applyPatch]);

  /* --- Data --------------------------------------------------------- */

  useEffect(() => {
    const controller = new AbortController();
    api
      .getTypes(controller.signal)
      .then((catalog) => setNodeTypes(catalog.node_types))
      .catch(() => {
        if (!controller.signal.aborted) {
          setNodeTypes([]);
          setTypesFailed(true);
        }
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const trimmed = query.trim();
    if (trimmed.length === 0) {
      requestSeq.current += 1;
      setResult(null);
      setError(null);
      setStatus("idle");
      return;
    }

    const controller = new AbortController();
    const ticket = ++requestSeq.current;
    setStatus("loading");

    const filters: SearchFilters = { k: limit, state: stateFilter };
    if (type) filters.type = type;
    if (createdBy.trim()) filters.created_by = createdBy.trim();
    // The filter narrows and never widens: omitting it reads every space in
    // scope, which is the default, so an unset filter sends nothing at all.
    if (space) filters.space = space;
    // Only sent when on: a server that reads presence rather than value must not
    // see `expand=false` and turn expansion on.
    if (expand) filters.expand = true;
    if (includeMeta) filters.include_meta = true;

    api
      .search(trimmed, filters, controller.signal)
      .then((next) => {
        if (ticket !== requestSeq.current) return;
        setResult(next);
        setError(null);
        setStatus("ready");
        setVector((previous) => nextEvidence(previous, next.hits));
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted || ticket !== requestSeq.current) return;
        setError(caught);
        setStatus("error");
      });

    return () => controller.abort();
  }, [query, type, stateFilter, createdBy, space, includeMeta, limit, expand, retryToken]);

  /* --- Display ------------------------------------------------------ */

  const hits = result?.hits ?? EMPTY_HITS;
  const terms = useMemo(() => queryTerms(query), [query]);

  // A hit in a space `GET /api/spaces` does not list is a hit in a space that
  // was archived after it was written — and the row would otherwise read
  // `in 4affabf6…`. One extra listing names all of them, fired only when a
  // result actually carries such a space, which under a narrowed filter cannot
  // happen at all (the filter's own vocabulary is the active list).
  const unresolvedHitSpaces = useMemo(
    () => unresolvedSpaceIds(hits.map((each) => each.space_id ?? ""), spaces),
    [hits, spaces],
  );
  // Under a narrowed filter the rows say nothing about their space, but the
  // *filter* does — in the picker and, when the server refuses it, in a panel.
  // A filter left pointing at a space the human archived is the way both of
  // those are reached, and the reference is all either had to show.
  const unresolvedFilterSpace = useMemo(
    () => unresolvedSpaceIds([space], spaces),
    [space, spaces],
  );
  const archivedSpaces = useArchivedSpaces(
    unresolvedFilterSpace.length > 0 || (space === ANY_SPACE && unresolvedHitSpaces.length > 0),
  );

  const groups = useMemo<DisplayGroup[]>(() => {
    if (hits.length === 0) return [];
    if (group === "score") {
      return [{ key: "all", label: "", help: "", hits }];
    }
    const buckets = new Map<SignalKey, SearchHit[]>();
    for (const hit of hits) {
      const key = describeSignals(hit).dominant;
      const bucket = buckets.get(key);
      if (bucket) bucket.push(hit);
      else buckets.set(key, [hit]);
    }
    return SIGNAL_KEYS.filter((key) => buckets.has(key)).map((key) => ({
      key,
      label: GROUP_LABEL[key],
      help: SIGNAL_HELP[key],
      hits: buckets.get(key) ?? [],
    }));
  }, [hits, group]);

  const groupOffsets = useMemo(() => {
    const offsets: number[] = [];
    let running = 0;
    for (const displayGroup of groups) {
      offsets.push(running);
      running += displayGroup.hits.length;
    }
    return offsets;
  }, [groups]);

  const orderedHits = useMemo(() => groups.flatMap((displayGroup) => displayGroup.hits), [groups]);
  const neighbourCount = useMemo(
    () => orderedHits.filter((hit) => describeSignals(hit).isNeighbour).length,
    [orderedHits],
  );

  linkRefs.current.length = orderedHits.length;

  /* --- Keyboard ----------------------------------------------------- */

  const focusInput = useCallback((select: boolean) => {
    const input = inputRef.current;
    if (!input) return;
    input.focus();
    if (select) input.select();
  }, []);

  const focusRow = useCallback((index: number) => {
    const target = linkRefs.current[index];
    if (!target) return;
    target.focus();
    target.scrollIntoView({ block: "nearest" });
  }, []);

  // `/` and Cmd/Ctrl-K return to the box from anywhere in the view.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const chord = (event.key === "k" || event.key === "K") && (event.metaKey || event.ctrlKey);
      const slash = event.key === "/" && !event.metaKey && !event.ctrlKey && !event.altKey;
      if (!chord && !slash) return;
      if (slash && isTypingTarget(event.target)) return;
      event.preventDefault();
      focusInput(true);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [focusInput]);

  useEffect(() => {
    focusInput(false);
  }, [focusInput]);

  const onInputKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown" && orderedHits.length > 0) {
      event.preventDefault();
      focusRow(0);
      return;
    }
    if (event.key === "Enter") {
      // Skip the debounce: the user has said they are done typing.
      event.preventDefault();
      applyPatch({ query: draft });
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      if (draft.length > 0) {
        setDraft("");
        applyPatch({ query: "" });
      } else {
        inputRef.current?.blur();
      }
    }
  };

  const onRowKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLElement>, index: number) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        focusRow(Math.min(index + 1, orderedHits.length - 1));
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        if (index === 0) focusInput(false);
        else focusRow(index - 1);
        return;
      }
      if (event.key === "ArrowRight") {
        const hit = orderedHits[index];
        if (!hit) return;
        event.preventDefault();
        navigate(`/graph/${encodeURIComponent(hit.node_id)}`);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        focusInput(false);
      }
    },
    [orderedHits, focusRow, focusInput, navigate],
  );

  /* --- Render ------------------------------------------------------- */

  const searching = query.trim().length > 0;
  const showSkeleton = status === "loading" && result === null;
  // A refused space filter is not "the search was refused": nothing about the
  // query is wrong, and the one useful action is dropping the filter rather
  // than retrying the same call.
  const spaceFailure =
    status === "error"
      ? describeSpaceFilterFailure(error, spaces, archivedSpaces.spaces)
      : null;
  const showDegradedNote = vector.missing && !vector.seen;
  // Every hit carries the filtered state; under "any" it is genuinely unknown.
  const knownState = stateFilter === "any" ? null : stateFilter;

  return (
    <div className="nd-view nd-search">
      <header className="nd-view__header">
        <h1>Search</h1>
        <p className="nd-search__hints" aria-hidden="true">
          <kbd>/</kbd> focus <span>·</span> <kbd>↑</kbd>
          <kbd>↓</kbd> move <span>·</span> <kbd>↵</kbd> open <span>·</span> <kbd>→</kbd> subgraph
        </p>
      </header>

      <div className="nd-search__controls">
        <div className="nd-search__box">
          <SearchGlyph />
          <input
            ref={inputRef}
            name="q"
            type="search"
            className="nd-input nd-search__input"
            value={draft}
            placeholder="Search the graph…"
            aria-label="Search the graph"
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onInputKeyDown}
          />
          <span className="nd-search__box-status">
            {status === "loading" ? <Spinner label="Searching" /> : null}
          </span>
        </div>

        <SearchFilterBar
          state={uiState}
          nodeTypes={nodeTypes}
          typesFailed={typesFailed}
          spaces={spaces}
          archivedSpaces={archivedSpaces.spaces}
          spacesFailed={spacesFailed}
          onChange={applyPatch}
        />
      </div>

      <div className="nd-search__statusline">
        <span aria-live="polite">
          {status === "ready" ? (
            <>
              {hits.length === 0
                ? "No results"
                : `${hits.length} result${hits.length === 1 ? "" : "s"}`}
              {hits.length > 0 && neighbourCount > 0
                ? ` · ${neighbourCount} neighbour${neighbourCount === 1 ? "" : "s"}`
                : ""}
              {hits.length >= limit ? ` · limit ${limit} reached` : ""}
              {hits.length > 0
                ? group === "score"
                  ? " · fused rank"
                  : " · grouped by signal"
                : ""}
            </>
          ) : status === "loading" ? (
            "Searching…"
          ) : (
            " "
          )}
        </span>
        {hits.length > 0 ? <SignalLegend neighbours={expand || neighbourCount > 0} /> : null}
      </div>

      {showDegradedNote ? <DegradedVectorNote /> : null}

      {status === "error" ? (
        spaceFailure ? (
          <SpaceFilterPanel
            failure={spaceFailure}
            onClearSpace={() => applyPatch({ space: ANY_SPACE })}
          />
        ) : (
          <ErrorPanel error={error} onRetry={() => setRetryToken((token) => token + 1)} />
        )
      ) : null}

      {!searching && status !== "error" ? <IdleState /> : null}

      {searching && status === "ready" && hits.length === 0 ? (
        <EmptyState
          title={`Nothing matched “${query.trim()}”`}
          body={describeNoResults(uiState).join(" ")}
          action={
            stateFilter !== "any" ? (
              <button
                type="button"
                className="nd-button"
                onClick={() => applyPatch({ state: "any" })}
              >
                Search every state
              </button>
            ) : hasActiveFilters(uiState) ? (
              <button
                type="button"
                className="nd-button"
                onClick={() => applyPatch(CLEARABLE_FILTERS)}
              >
                Clear filters
              </button>
            ) : null
          }
        />
      ) : null}

      {showSkeleton ? (
        <ul className="nd-search__results" aria-hidden="true">
          {Array.from({ length: SKELETON_ROWS }, (_, index) => (
            <SkeletonRow key={index} />
          ))}
        </ul>
      ) : null}

      {groups.length > 0 ? (
        <div
          className={status === "loading" ? "nd-search__list nd-search__list--stale" : "nd-search__list"}
          aria-busy={status === "loading"}
        >
          {groups.map((displayGroup, groupIndex) => (
            <section key={displayGroup.key} className="nd-search__group">
              {displayGroup.label ? (
                <h2 className="nd-label nd-search__group-title" title={displayGroup.help}>
                  {displayGroup.label}
                  <span className="nd-search__group-count">{displayGroup.hits.length}</span>
                </h2>
              ) : null}
              <ul className="nd-search__results">
                {displayGroup.hits.map((hit, position) => {
                  const index = (groupOffsets[groupIndex] ?? 0) + position;
                  return (
                    <ResultRow
                      key={hit.node_id}
                      hit={hit}
                      index={index}
                      knownState={knownState}
                      spaceName={hitSpaceName(hit, spaces, archivedSpaces.spaces, space)}
                      terms={terms}
                      linkRef={(element) => {
                        linkRefs.current[index] = element;
                      }}
                      onKeyDown={onRowKeyDown}
                    />
                  );
                })}
              </ul>
            </section>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Pieces                                                                */
/* -------------------------------------------------------------------- */

/**
 * Fold a response's hits into the latched vector evidence.
 *
 * @param previous What the session has concluded so far.
 * @param hits The hits of the response just received.
 */
function nextEvidence(previous: VectorEvidence, hits: SearchHit[]): VectorEvidence {
  const evidence = readVectorEvidence(hits);
  if (evidence === "unknown") return previous;
  const seen = previous.seen || evidence === "contributed";
  const missing = previous.missing || evidence === "absent";
  if (seen === previous.seen && missing === previous.missing) return previous;
  return { seen, missing };
}

/** True when a keystroke landed in something the user is typing into. */
function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

/**
 * The quiet notice for the degraded-vector case.
 *
 * Deliberately not a toast, a modal, or an error: nothing is broken. Search
 * works, it just cannot match on meaning, and the user is owed that fact
 * because the alternative is wondering for weeks why semantic matches never
 * appear.
 */
function DegradedVectorNote() {
  return (
    <p className="nd-search__notice" role="note">
      <span className="nd-search__notice-mark" aria-hidden="true">
        i
      </span>
      <span>
        <strong>Keyword results only.</strong> No hit carries a semantic (vector) signal, so the
        embedding provider is unavailable or the <code>vec</code> projector has indexed nothing
        yet. Search still works — it is matching words, not meaning.
      </span>
    </p>
  );
}

/** What the view shows before anything has been typed. */
function IdleState() {
  return (
    <EmptyState
      title="Search the graph"
      body={
        <>
          Keyword (BM25) and semantic (vector) retrieval, fused by reciprocal rank fusion. Each
          result shows which signals fired and how far up each one it ranked. Press{" "}
          <kbd>/</kbd> or <kbd>Ctrl</kbd>+<kbd>K</kbd> to come back to the box from anywhere.
        </>
      }
    />
  );
}

/**
 * A failed search, stated plainly, with the one useful action.
 *
 * The distinction that matters to the reader is *whose* fault it is: a query the
 * server rejected (fix the query) versus a server that is not answering (start
 * it). That split — including the dev proxy's 502, which is a live response
 * from a gateway whose upstream is dead rather than a refusal — is the shared
 * classifier's job; only the headline for a refused *search* is local.
 */
function ErrorPanel({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const failure = describeFailure(error, "that search");
  const unreachable = failure.kind === "unreachable";
  const title = unreachable
    ? "The nodum server did not answer"
    : failure.kind === "busy"
      ? "The database is busy"
      : "The search was refused";
  const detail = describeError(error);

  return (
    <div className="nd-search__error" role="alert">
      <div className="nd-search__error-body">
        <strong>{title}</strong>
        <span className="nd-mono nd-search__error-detail">{detail}</span>
        {unreachable ? (
          <span className="nd-meta">
            Start it with <code>uv run nodum serve</code>, then retry.
          </span>
        ) : null}
      </div>
      <button type="button" className="nd-button nd-button--small" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}

/**
 * The panel for a space filter the server would not resolve.
 *
 * Its own panel rather than a headline on the error one, because the reader's
 * next move is different: nothing is wrong with the query and retrying it
 * changes nothing — the filter is what has to go. The copy lives in
 * `spaceFailure.ts` under a test, since the rule it obeys (never claim the
 * space does not exist) is a property of the refusal rather than of this view.
 */
function SpaceFilterPanel({
  failure,
  onClearSpace,
}: {
  failure: SpaceFilterFailure;
  onClearSpace: () => void;
}) {
  return (
    <div className="nd-search__error" role="alert">
      <div className="nd-search__error-body">
        <strong>{failure.title}</strong>
        <span className="nd-meta">{failure.detail}</span>
      </div>
      <button type="button" className="nd-button nd-button--small" onClick={onClearSpace}>
        Search every space
      </button>
    </div>
  );
}

/** A placeholder row, sized like a real one so the list does not jump. */
function SkeletonRow() {
  return (
    <li className="nd-search-hit nd-search-hit--skeleton">
      <span className="nd-search-skeleton nd-search-skeleton--title" />
      <span className="nd-search-skeleton nd-search-skeleton--body" />
      <span className="nd-search-skeleton nd-search-skeleton--foot" />
    </li>
  );
}

/** The magnifier in the query box. */
function SearchGlyph() {
  return (
    <svg
      className="nd-search__glyph"
      width="15"
      height="15"
      viewBox="0 0 15 15"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      aria-hidden="true"
    >
      <circle cx="6.5" cy="6.5" r="4.5" />
      <path d="M10 10 L13.5 13.5" strokeLinecap="round" />
    </svg>
  );
}

export { SearchView };
