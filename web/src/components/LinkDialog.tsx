/**
 * The create-edge dialog — the first caller of `api.createEdge`.
 *
 * One dialog, three entry points: the reading view's header, the graph
 * panel's actions, and the editor's `/link` slash command. The From node is
 * fixed (whatever surface opened it), and everything else is a form: a
 * direction toggle that swaps the selected edge type for its catalog inverse
 * (locked, with a reason, when the selected type declares no inverse), the
 * live edge-type catalog as chips, a debounced target search, and an
 * optional confidence.
 *
 * The pure model behind the form lives in `lib/linkDialog.ts` with its tests;
 * this file is the wiring — the fetches, the submission, and the copy. A
 * human-created edge lands `active` via the HTTP surface, so there is no
 * review-queue affordance here and no state to offer one.
 *
 * On success the dialog toasts, closes itself, and hands the created edge to
 * the host through {@link LinkDialogProps.onCreated} — which is also how the
 * reading-view rail and the graph panel learn to refetch. When the dialog was
 * opened from the editor, the same callback receives the target's title so
 * the host can drop `[[Title]]` — or `[[id]]`, when the title carries a `|`
 * or a bracket that the wikilink grammar cannot — into the buffer.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import type { EdgeOut, EdgeTypeOut, NodeOut } from "../api/types";
import { describeError } from "../lib";
import {
  createDebouncer,
  edgeBody,
  fetchTargetCandidates,
  inverseEdgeType,
  parseConfidence,
  preferredEdgeType,
  targetCrossing,
} from "../lib/linkDialog";
import type { LinkDirection, TargetCandidate } from "../lib/linkDialog";
import { formatRelative } from "../lib/time";
import { Modal } from "./Modal";
import { NodeBadge } from "./NodeBadge";
import { nameSpace, spaceNameNote, unresolvedSpaceIds } from "./spaceNaming";
import { useArchivedSpaces } from "./useArchivedSpaces";
import { useSpaces } from "./useSpaces";
import { useToast } from "./Toast";
import "./LinkDialog.css";

/** How long the target search waits after the last keystroke. */
const SEARCH_DEBOUNCE_MS = 250;
/** How many candidates each read is asked for. */
const TARGET_LIMIT = 8;

interface LinkDialogProps {
  /** The From node — the edge's anchor. Never changes while the dialog is up. */
  source: NodeOut;
  /** Cancel handler for every dismissal route (Escape, backdrop, Close). */
  onClose: () => void;
  /**
   * Called once the edge exists, before the dialog closes. The host decides
   * what to refetch; the editor additionally inserts `[[Title]]`.
   *
   * @param edge The created edge.
   * @param targetTitle The chosen target's title.
   */
  onCreated(edge: EdgeOut, targetTitle: string): void;
}

/** A chosen target — the two facts the submission needs, nothing more. */
interface TargetPick {
  id: string;
  title: string;
}

/** The create-edge dialog. */
export function LinkDialog({ source, onClose, onCreated }: LinkDialogProps) {
  const toast = useToast();

  /* --- The live edge-type catalog ----------------------------------- */

  const [edgeTypes, setEdgeTypes] = useState<readonly EdgeTypeOut[]>([]);
  const [catalogState, setCatalogState] = useState<"loading" | "ready" | "failed">("loading");
  const [edgeType, setEdgeType] = useState("");

  const loadCatalog = useCallback(() => {
    const controller = new AbortController();
    setCatalogState("loading");
    api
      .getTypes(controller.signal)
      .then((types) => {
        const list = types.edge_types;
        setEdgeTypes(list);
        setEdgeType(preferredEdgeType(list) ?? "");
        setCatalogState("ready");
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setCatalogState("failed");
        toast.showError(error, "Could not load the edge-type catalog");
      });
    return () => controller.abort();
  }, [toast]);

  useEffect(() => loadCatalog(), [loadCatalog]);

  const sortedEdgeTypes = useMemo(
    () => [...edgeTypes].sort((a, b) => a.id.localeCompare(b.id)),
    [edgeTypes],
  );

  /* --- Direction ↔ edge-type pairing -------------------------------- */

  const [direction, setDirection] = useState<LinkDirection>("out");

  // Flipping the direction swaps the selected type for its catalog inverse,
  // so the two states describe the same fact: outgoing `supports` and
  // incoming `supported_by` both say the From node supports the target.
  // A type the catalog has no inverse for — a user-created directed type
  // (`inverse_name` null) — cannot flip: the same label with the endpoints
  // swapped would describe a different fact, so the toggle is locked.
  const flippedType = inverseEdgeType(edgeTypes, edgeType);
  const directionLocked = flippedType === null;
  const flipDirection = (next: LinkDirection) => {
    if (next === direction || directionLocked) return;
    setDirection(next);
    setEdgeType(flippedType ?? edgeType);
  };

  /* --- Target search ------------------------------------------------- */

  const [targetQuery, setTargetQuery] = useState("");
  const [target, setTarget] = useState<TargetPick | null>(null);
  const [results, setResults] = useState<TargetCandidate[]>([]);
  const [searchState, setSearchState] = useState<"idle" | "searching" | "ready" | "failed">(
    "idle",
  );
  const [searchError, setSearchError] = useState<string | null>(null);
  const debouncer = useRef(createDebouncer(SEARCH_DEBOUNCE_MS));
  // Bumped per request so a slow earlier search cannot overwrite a newer one.
  const searchSequence = useRef(0);

  useEffect(() => () => debouncer.current.cancel(), []);

  const runSearch = useCallback(
    (query: string) => {
      if (query.trim() === "") {
        // Bump the sequence like a real request would: an in-flight search
        // started under the previous query must not repopulate results under
        // an empty one.
        searchSequence.current += 1;
        setResults([]);
        setSearchState("idle");
        return;
      }
      const sequence = ++searchSequence.current;
      setSearchState("searching");
      void fetchTargetCandidates(
        query,
        (prefix) => api.suggestLinks(prefix, TARGET_LIMIT),
        (q) => api.search(q, { k: TARGET_LIMIT }),
      )
        .then((candidates) => {
          if (sequence !== searchSequence.current) return;
          setResults(candidates.filter((candidate) => candidate.nodeId !== source.id));
          setSearchState("ready");
        })
        .catch((error: unknown) => {
          if (sequence !== searchSequence.current) return;
          setSearchState("failed");
          setSearchError(describeError(error));
        });
    },
    [source.id],
  );

  const handleQueryChange = (value: string) => {
    setTargetQuery(value);
    // The selection is tied to the query it was picked under; a new query
    // starts a new pick.
    setTarget(null);
    debouncer.current.schedule(() => runSearch(value));
  };

  /* --- Confidence ---------------------------------------------------- */

  const [confidence, setConfidence] = useState("");
  const confidenceParse = parseConfidence(confidence);

  /* --- Naming spaces (the shared vocabulary, verbatim) ---------------- */

  const spaces = useSpaces();
  const spaceId = source.space_id;
  const unresolved = unresolvedSpaceIds(
    [spaceId ?? "", ...results.map((candidate) => candidate.spaceId ?? "")],
    spaces.spaces,
  );
  const archivedSpaces = useArchivedSpaces(unresolved.length > 0);
  const sourceSpace =
    spaceId === null ? null : nameSpace(spaceId, spaces.spaces, archivedSpaces.spaces);

  /* --- Submission ---------------------------------------------------- */

  const [busy, setBusy] = useState(false);

  const submit = () => {
    if (target === null || !confidenceParse.ok || busy) return;
    setBusy(true);
    void api
      .createEdge(
        edgeBody({
          sourceId: source.id,
          targetId: target.id,
          direction,
          edgeType,
          confidence: confidenceParse.value,
        }),
      )
      .then(
        (edge) => {
          toast.show(
            "success",
            "Edge created",
            `${edgeType} · ${source.title ?? source.id} → ${target.title}`,
          );
          onCreated(edge, target.title);
          onClose();
        },
        (error: unknown) => {
          setBusy(false);
          toast.showError(error, "Could not create the edge");
        },
      );
  };

  return (
    <Modal
      title="Create link"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="nd-button nd-button--ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="nd-button nd-button--primary"
            onClick={submit}
            disabled={
              target === null || !confidenceParse.ok || busy || catalogState !== "ready" || edgeType === ""
            }
          >
            {busy ? "Creating…" : "Create link"}
          </button>
        </>
      }
    >
      <div className="nd-link-dialog">
        {/* The From node, fixed for the life of the dialog. */}
        <div className="nd-card nd-link-dialog__from">
          <span className="nd-label">From</span>
          <strong className="nd-link-dialog__from-title">{source.title ?? source.id}</strong>
          {sourceSpace ? (
            <span
              className={
                sourceSpace.kind === "archived"
                  ? "nd-badge nd-badge--archived"
                  : "nd-badge nd-badge--type"
              }
              title={spaceNameNote(sourceSpace) ?? undefined}
            >
              <span className="nd-badge__dot" aria-hidden="true" />
              {sourceSpace.label}
              {sourceSpace.kind === "archived" ? " · archived" : ""}
            </span>
          ) : null}
        </div>

        {/* Direction is first-class: the toggle also swaps the selected type. */}
        <div className="nd-link-dialog__direction" role="group" aria-label="Edge direction">
          <button
            type="button"
            aria-pressed={direction === "out"}
            className={
              direction === "out"
                ? "nd-link-dialog__dir nd-link-dialog__dir--active"
                : "nd-link-dialog__dir"
            }
            disabled={directionLocked}
            onClick={() => flipDirection("out")}
          >
            outgoing →
          </button>
          <button
            type="button"
            aria-pressed={direction === "in"}
            className={
              direction === "in"
                ? "nd-link-dialog__dir nd-link-dialog__dir--active"
                : "nd-link-dialog__dir"
            }
            disabled={directionLocked}
            onClick={() => flipDirection("in")}
          >
            ← incoming
          </button>
          {directionLocked ? (
            <p className="nd-meta nd-link-dialog__direction-note">
              “{edgeType}” has no inverse, so this edge's direction is fixed.
            </p>
          ) : null}
        </div>

        <div className="nd-field">
          <span className="nd-label">Edge type</span>
          {catalogState === "loading" ? (
            <p className="nd-meta">Loading the edge-type catalog…</p>
          ) : catalogState === "failed" ? (
            <p className="nd-link-dialog__error">
              Could not load the edge-type catalog.{" "}
              <button type="button" className="nd-button nd-button--small" onClick={loadCatalog}>
                Try again
              </button>
            </p>
          ) : (
            <div className="nd-link-dialog__types">
              {sortedEdgeTypes.map((entry) => (
                <button
                  key={entry.id}
                  type="button"
                  className={
                    edgeType === entry.id
                      ? "nd-link-dialog__type nd-link-dialog__type--selected"
                      : "nd-link-dialog__type"
                  }
                  onClick={() => setEdgeType(entry.id)}
                >
                  {entry.id}
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="nd-field">
          <label className="nd-label" htmlFor="link-dialog-target">
            Target node
          </label>
          <input
            id="link-dialog-target"
            name="target"
            className="nd-input"
            value={targetQuery}
            onChange={(event) => handleQueryChange(event.target.value)}
            placeholder="Search by title…"
            autoComplete="off"
          />
          {searchState === "searching" ? <p className="nd-meta">Searching…</p> : null}
          {searchState === "failed" ? (
            <p className="nd-link-dialog__error">{searchError}</p>
          ) : null}
          {results.length > 0 ? (
            <ul className="nd-link-dialog__results">
              {results.map((candidate) => (
                <li key={candidate.nodeId}>
                  <TargetRow
                    candidate={candidate}
                    selected={target !== null && target.id === candidate.nodeId}
                    crossing={targetCrossing(source, candidate)}
                    spaces={spaces.spaces}
                    archivedSpaces={archivedSpaces.spaces}
                    onPick={() => setTarget({ id: candidate.nodeId, title: candidate.title })}
                  />
                </li>
              ))}
            </ul>
          ) : searchState === "ready" ? (
            <p className="nd-meta">No nodes match “{targetQuery.trim()}”.</p>
          ) : null}
        </div>

        <div className="nd-field">
          <label className="nd-label" htmlFor="link-dialog-confidence">
            Confidence (optional)
          </label>
          <input
            id="link-dialog-confidence"
            name="confidence"
            className="nd-input nd-input--mono"
            value={confidence}
            onChange={(event) => setConfidence(event.target.value)}
            placeholder="0.8 — between 0 and 1"
          />
          {!confidenceParse.ok ? (
            <p className="nd-link-dialog__error">{confidenceParse.reason}</p>
          ) : (
            <p className="nd-meta">Unset by default; a null confidence is not “meets the bar”.</p>
          )}
        </div>
      </div>
    </Modal>
  );
}

/** One candidate row: title, space, crossing mark, state, freshness. */
function TargetRow({
  candidate,
  selected,
  crossing,
  spaces,
  archivedSpaces,
  onPick,
}: {
  candidate: TargetCandidate;
  selected: boolean;
  crossing: boolean;
  spaces: readonly NodeOut[] | null;
  archivedSpaces: readonly NodeOut[];
  onPick: () => void;
}) {
  const spaceName = nameSpace(candidate.spaceId ?? "", spaces, archivedSpaces);
  const crossingTitle = crossing
    ? `Crosses into ${spaceName.label} — an edge between two spaces`
    : undefined;

  return (
    <button
      type="button"
      className={
        selected
          ? "nd-link-dialog__result nd-link-dialog__result--selected"
          : "nd-link-dialog__result"
      }
      onClick={onPick}
    >
      <span className="nd-truncate nd-link-dialog__result-title">
        {candidate.title}
        {candidate.snippet ? (
          <span className="nd-meta nd-link-dialog__result-snippet"> — {candidate.snippet}</span>
        ) : null}
      </span>
      <span className="nd-mono nd-link-dialog__result-space">{spaceName.label}</span>
      {/* Always a cell, empty when there is no crossing: the row is a fixed
          grid, and a conditional column would shift every other row. */}
      <span className="nd-link-dialog__crossing-mark">
        {crossing ? <span title={crossingTitle}>crossing</span> : null}
      </span>
      {candidate.state ? <NodeBadge state={candidate.state} stateOnly /> : null}
      {candidate.updatedAt ? (
        <span className="nd-meta nd-link-dialog__result-time">
          {formatRelative(candidate.updatedAt)}
        </span>
      ) : null}
    </button>
  );
}
