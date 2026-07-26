/**
 * The confidence floor — the one control in this view that can lie to you.
 *
 * `subgraph`'s `min_confidence` drops every edge whose `confidence` is NULL:
 * unstated is not "meets the bar", the same reading the (since-removed)
 * auto-accept policy gate took. Human-created edges normally carry no
 * confidence at all, so a floor
 * left on by default would quietly delete most of a personal graph and present
 * the remainder as the whole thing.
 *
 * Three things follow, and all three are load-bearing:
 *
 * 1. **It is off unless switched on.** Off means the parameter is not sent —
 *    not sent as zero, which would still drop every unrated edge.
 * 2. **The exclusion is stated in the control**, not in a tooltip.
 * 3. **The cost is quantified before it is paid.** While the floor is off, the
 *    control counts the unrated edges in the current render, so "this would
 *    drop 14 of 17 edges" is on screen before the switch is flipped.
 */

interface ConfidenceFilterProps {
  /** The floor, or null when it is off. */
  value: number | null;
  /** Called with the new floor, or null to switch it off. */
  onChange: (next: number | null) => void;
  /** Edges in the current render carrying no confidence. */
  unratedEdges: number;
  /** Edges in the current render. */
  totalEdges: number;
}

/** The floor a first click lands on: low, but unmistakably a filter. */
const INITIAL_FLOOR = 0.5;

/**
 * Render the confidence floor control.
 *
 * @param props See {@link ConfidenceFilterProps}.
 */
export function ConfidenceFilter({
  value,
  onChange,
  unratedEdges,
  totalEdges,
}: ConfidenceFilterProps) {
  const enabled = value !== null;

  return (
    <div className="nd-graph__confidence">
      <label className="nd-graph__check">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => onChange(event.target.checked ? INITIAL_FLOOR : null)}
        />
        <span className="nd-label">Confidence floor</span>
      </label>

      {enabled ? (
        <>
          <div className="nd-graph__slider">
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={value}
              aria-label="Minimum edge confidence"
              onChange={(event) => onChange(Number(event.target.value))}
            />
            <span className="nd-mono">≥ {value.toFixed(2)}</span>
          </div>
          <p className="nd-graph__warn">
            Hiding every edge with <strong>no stated confidence</strong>. Edges you created by
            hand normally have none, so most of the human graph is excluded while this is on.
          </p>
        </>
      ) : (
        <p className="nd-graph__hint">
          Off — every edge is shown, rated or not.{" "}
          {totalEdges > 0 ? (
            <>
              {unratedEdges} of {totalEdges} edge{totalEdges === 1 ? "" : "s"} here carry no
              confidence and would be dropped.
            </>
          ) : null}
        </p>
      )}
    </div>
  );
}
