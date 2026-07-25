/** Type + state pill for a node or an edge. */

/** The lifecycle states the service layer defines. */
type State = "proposed" | "active" | "archived";

const KNOWN_STATES: readonly string[] = ["proposed", "active", "archived"];

interface NodeBadgeProps {
  /** Node-type or edge-type id, shown verbatim in mono. */
  type?: string | null;
  /** Lifecycle state; an unrecognised value falls back to the neutral style. */
  state?: string | null;
  /** Hide the type half and show state alone. */
  stateOnly?: boolean;
}

/**
 * Render a node's type and state as two small pills.
 *
 * The type is never coloured — colour is reserved for state, so a row's
 * lifecycle reads at a glance without decoding a second colour axis.
 *
 * @param type Type id (omitted when only the state matters).
 * @param state Lifecycle state: proposed, active, or archived.
 * @param stateOnly Show the state pill alone.
 */
export function NodeBadge({ type, state, stateOnly = false }: NodeBadgeProps) {
  const normalised = state && KNOWN_STATES.includes(state) ? (state as State) : null;

  return (
    <span className="nd-row" style={{ ["--nd-row-gap" as string]: "var(--nd-space-2)" }}>
      {!stateOnly && type ? <span className="nd-badge nd-badge--type">{type}</span> : null}
      {state ? (
        <span
          className={normalised ? `nd-badge nd-badge--${normalised}` : "nd-badge"}
          title={`State: ${state}`}
        >
          <span className="nd-badge__dot" aria-hidden="true" />
          {state}
        </span>
      ) : null}
    </span>
  );
}
