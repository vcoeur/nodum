/**
 * The filter bar.
 *
 * Every control here writes to the URL, and the URL is the request: the
 * parameter names match `GET /api/graph/subgraph` one for one, so what you see
 * in the address bar is what the server was asked. No control here *hides*
 * things client-side — a filter that only hid things in the browser would still
 * be paying the cost of fetching them, and would disagree with the node cap
 * about what "200 nodes" means.
 *
 * **The space filter is the deliberate exception, and it hides nothing.** It is
 * a render-time control (design decision D5): the far endpoint of a cross-space
 * edge stays drawn, dimmed and clickable, so there is nothing for the server to
 * leave out and no disagreement with the cap — the node count on screen is
 * unchanged by it. The reasoning lives in `filters.ts`.
 */

import { useEffect, useRef, useState } from "react";
import { NodeBadge, SpaceFilter, Spinner } from "../../components";
import type { NodeOut, SpaceOut } from "../../api/types";
import { ConfidenceFilter } from "./ConfidenceFilter";
import { TypeFilter } from "./TypeFilter";
import { RootPicker } from "./RootPicker";
import {
  DEFAULT_FILTERS,
  EDGE_STATES,
  MAX_DEPTH,
  MAX_LIMIT,
  MIN_DEPTH,
  MIN_LIMIT,
  filterChips,
  isDefaultFilters,
} from "./filters";
import type { GraphFilters } from "./filters";

interface GraphToolbarProps {
  filters: GraphFilters;
  onFiltersChange: (next: GraphFilters) => void;
  /** The root node, once it has loaded. */
  rootNode: NodeOut | null;
  /** The root id from the URL, which is known before the node is. */
  rootId: string;
  onPickRoot: (nodeId: string) => void;
  edgeTypeOptions: readonly string[];
  nodeTypeOptions: readonly string[];
  /** Actors seen in the current render, offered as `created_by` completions. */
  actorOptions: readonly string[];
  /** Active spaces for the space picker; null while loading or after a failure. */
  spaces: readonly SpaceOut[] | null;
  /**
   * Archived space nodes, so a filter left pointing at one is named in the
   * picker rather than shown as its id. Never offered as a choice — the
   * vocabulary is `spaces` alone.
   */
  archivedSpaces: readonly NodeOut[];
  /** True once the space list request failed. */
  spacesFailed: boolean;
  /** How the filtered space is named in the chip row. */
  spaceName: string;
  /**
   * Whether the space reference actually narrows the render. False for a
   * reference the space list cannot resolve — an archived space still named in
   * the URL — where the chip must not promise dimming the banner below denies.
   */
  spaceInEffect: boolean;
  unratedEdges: number;
  totalEdges: number;
  loading: boolean;
  onReload: () => void;
  onFit: () => void;
  onRelayout: () => void;
}

/**
 * Render the filter bar.
 *
 * @param props See {@link GraphToolbarProps}.
 */
export function GraphToolbar({
  filters,
  onFiltersChange,
  rootNode,
  rootId,
  onPickRoot,
  edgeTypeOptions,
  nodeTypeOptions,
  actorOptions,
  spaces,
  archivedSpaces,
  spacesFailed,
  spaceName,
  spaceInEffect,
  unratedEdges,
  totalEdges,
  loading,
  onReload,
  onFit,
  onRelayout,
}: GraphToolbarProps) {
  const [rootPickerOpen, setRootPickerOpen] = useState(false);
  const rootWrapperRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!rootPickerOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!rootWrapperRef.current?.contains(event.target as Node)) setRootPickerOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setRootPickerOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [rootPickerOpen]);

  const toggleEdgeState = (state: (typeof EDGE_STATES)[number]) => {
    const next = filters.edgeStates.includes(state)
      ? filters.edgeStates.filter((it) => it !== state)
      : EDGE_STATES.filter((it) => it === state || filters.edgeStates.includes(it));
    // Never leave the walk with no state to follow: an empty list would mean
    // "the server's default" rather than "nothing", which is a different graph.
    onFiltersChange({
      ...filters,
      edgeStates: next.length > 0 ? [...next] : [...DEFAULT_FILTERS.edgeStates],
    });
  };

  const chips = filterChips(filters, spaceName, spaceInEffect);

  return (
    <div className="nd-graph__toolbar">
      <div className="nd-graph__toolbar-row">
        <div className="nd-graph__root" ref={rootWrapperRef}>
          <span className="nd-label">Root</span>
          <button
            type="button"
            className="nd-button nd-button--small nd-graph__root-button"
            aria-expanded={rootPickerOpen}
            onClick={() => setRootPickerOpen((open) => !open)}
          >
            <span className="nd-truncate">{rootNode?.title ?? rootId}</span>
            <span aria-hidden="true">▾</span>
          </button>
          {rootNode ? <NodeBadge type={rootNode.type} state={rootNode.state} /> : null}
          {rootPickerOpen ? (
            <div className="nd-graph__popover-panel nd-graph__popover-panel--wide">
              <RootPicker
                compact
                autoFocus
                onPick={(nodeId) => {
                  setRootPickerOpen(false);
                  onPickRoot(nodeId);
                }}
              />
            </div>
          ) : null}
        </div>

        <label className="nd-graph__control">
          <span className="nd-label">Depth</span>
          <span className="nd-graph__slider">
            <input
              name="graph-depth"
              type="range"
              min={MIN_DEPTH}
              max={MAX_DEPTH}
              step={1}
              value={filters.depth}
              onChange={(event) =>
                onFiltersChange({ ...filters, depth: Number(event.target.value) })
              }
            />
            <span className="nd-mono">{filters.depth}</span>
          </span>
        </label>

        <label className="nd-graph__control">
          <span className="nd-label">Node limit</span>
          <input
            name="graph-limit"
            className="nd-input nd-input--mono nd-graph__number"
            type="number"
            min={MIN_LIMIT}
            max={MAX_LIMIT}
            step={25}
            value={filters.limit}
            onChange={(event) => {
              const raw = Number(event.target.value);
              if (!Number.isFinite(raw)) return;
              onFiltersChange({
                ...filters,
                limit: Math.min(MAX_LIMIT, Math.max(MIN_LIMIT, Math.round(raw))),
              });
            }}
          />
        </label>

        <div className="nd-graph__control">
          <span className="nd-label">Edge state</span>
          <div className="nd-graph__states">
            {EDGE_STATES.map((state) => (
              <label key={state} className="nd-graph__check">
                <input
                  name={`graph-edge-state-${state}`}
                  type="checkbox"
                  checked={filters.edgeStates.includes(state)}
                  onChange={() => toggleEdgeState(state)}
                />
                <NodeBadge state={state} stateOnly />
              </label>
            ))}
          </div>
        </div>

        <TypeFilter
          label="Edge types"
          options={edgeTypeOptions}
          selected={filters.edgeTypes}
          onChange={(edgeTypes) => onFiltersChange({ ...filters, edgeTypes })}
          anyLabel="any"
        />

        <TypeFilter
          label="Node types"
          options={nodeTypeOptions}
          selected={filters.nodeTypes}
          onChange={(nodeTypes) => onFiltersChange({ ...filters, nodeTypes })}
          anyLabel="any"
          note="The root is exempt: it is what you asked for, so it is shown whatever its type."
        />

        {/* Same row as the type and confidence filters, and deliberately not
            marked out as special — it *reads* like the others. What it does
            differs (it dims rather than drops), which the chip row states in
            words rather than leaving to a colour nobody has been taught. */}
        <SpaceFilter
          className="nd-graph__space"
          value={filters.space}
          onChange={(space) => onFiltersChange({ ...filters, space })}
          spaces={spaces}
          archivedSpaces={archivedSpaces}
          failed={spacesFailed}
        />

        <label className="nd-graph__control">
          <span className="nd-label">Edges by</span>
          <input
            name="graph-created-by"
            className="nd-input nd-input--mono nd-graph__actor"
            type="text"
            list="nd-graph-actors"
            placeholder="any actor"
            value={filters.createdBy}
            onChange={(event) => onFiltersChange({ ...filters, createdBy: event.target.value })}
          />
          <datalist id="nd-graph-actors">
            {actorOptions.map((actor) => (
              <option key={actor} value={actor} />
            ))}
          </datalist>
        </label>

        <ConfidenceFilter
          value={filters.minConfidence}
          onChange={(minConfidence) => onFiltersChange({ ...filters, minConfidence })}
          unratedEdges={unratedEdges}
          totalEdges={totalEdges}
        />

        <div className="nd-graph__toolbar-actions">
          {loading ? <Spinner label="Loading subgraph" /> : null}
          <button type="button" className="nd-button nd-button--small" onClick={onFit}>
            Fit
          </button>
          <button type="button" className="nd-button nd-button--small" onClick={onRelayout}>
            Re-layout
          </button>
          <button type="button" className="nd-button nd-button--small" onClick={onReload}>
            Reload
          </button>
          <button
            type="button"
            className="nd-button nd-button--ghost nd-button--small"
            disabled={isDefaultFilters(filters)}
            onClick={() => onFiltersChange({ ...DEFAULT_FILTERS })}
          >
            Reset filters
          </button>
        </div>
      </div>

      {chips.length > 0 ? (
        <div className="nd-graph__chips">
          <span className="nd-label">Filtering</span>
          {chips.map((chip) => (
            <button
              key={chip.key}
              type="button"
              className={
                chip.tone === "warn"
                  ? "nd-graph__chip nd-graph__chip--warn"
                  : "nd-graph__chip"
              }
              title="Remove this filter"
              onClick={() => onFiltersChange(chip.cleared)}
            >
              {chip.label}
              <span aria-hidden="true">×</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
