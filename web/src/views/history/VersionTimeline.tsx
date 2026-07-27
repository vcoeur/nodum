import type { VersionOut } from "../../api/types";
import { formatTimestamp, formatTimestampLong } from "../../lib";

/**
 * The version timeline.
 *
 * A node's history is not a straight line of applied edits: an agent's
 * `update_node` stages a `proposed` version that records *exactly* the fields
 * it named, and a reject archives that version without ever applying it. So a
 * row has to say three separate things — what state the snapshot is in, who
 * made it, and which fields it touches — because "touches" and "changed" are
 * not the same claim for a proposal that never landed.
 */

/** Version lifecycle, coloured on the shared state ramp. */
const RAMP: Record<string, string> = {
  // `applied` is a landed snapshot: the same thing `active` means for a node,
  // so it takes the same colour rather than inventing a fourth.
  applied: "nd-badge--active",
  proposed: "nd-badge--proposed",
  archived: "nd-badge--archived",
};

/** What each state means, for the badge's tooltip. */
const STATE_HELP: Record<string, string> = {
  applied: "Applied — this snapshot is part of the node's history",
  proposed: "Proposed — staged by an agent, waiting for a human accept",
  archived: "Archived — the proposal was rejected and never applied",
};

interface VersionTimelineProps {
  /** Versions in the server's chronological order (oldest first). */
  versions: VersionOut[];
  /** Version ids currently picked for comparison, at most two. */
  selected: number[];
  /** Toggle a version in or out of the comparison. */
  onToggle: (id: number) => void;
}

/**
 * Render the timeline, newest first.
 *
 * @param versions Chronological version list from `getHistory`.
 * @param selected The one or two ids picked for the diff.
 * @param onToggle Called with a version id when its checkbox changes.
 */
export function VersionTimeline({ versions, selected, onToggle }: VersionTimelineProps) {
  const ordinals = new Map(versions.map((version, index) => [version.id, index + 1]));

  return (
    <ol className="nd-timeline">
      {[...versions].reverse().map((version, reverseIndex) => {
        const ordinal = ordinals.get(version.id) ?? 0;
        const previous = versions[versions.length - reverseIndex - 2];
        const isSelected = selected.includes(version.id);
        const fields = touchedFields(version, previous);

        return (
          <li
            key={version.id}
            className={isSelected ? "nd-timeline__row nd-timeline__row--selected" : "nd-timeline__row"}
          >
            <label className="nd-timeline__pick">
              <input
                name={`compare-v${version.id}`}
                type="checkbox"
                checked={isSelected}
                onChange={() => onToggle(version.id)}
                aria-label={`Compare version ${ordinal} (id ${version.id})`}
              />
              <span className="nd-timeline__ordinal nd-mono">v{ordinal}</span>
            </label>

            <div className="nd-timeline__body">
              <div className="nd-timeline__head">
                <span
                  className={`nd-badge ${RAMP[version.state] ?? ""}`}
                  title={STATE_HELP[version.state] ?? `State: ${version.state}`}
                >
                  <span className="nd-badge__dot" aria-hidden="true" />
                  {version.state}
                </span>
                <span className="nd-mono nd-timeline__actor">{version.actor}</span>
                <span className="nd-meta" title={formatTimestampLong(version.created_at)}>
                  {formatTimestamp(version.created_at)}
                </span>
                <span className="nd-meta nd-timeline__seq">
                  version {version.id} · event {version.event_seq}
                </span>
              </div>

              <p className="nd-timeline__title nd-truncate" title={version.title ?? undefined}>
                {version.title ?? <span className="nd-meta">no title</span>}
              </p>

              <p className="nd-timeline__fields">
                <span className="nd-label">{fields.label}</span>
                {fields.names.length === 0 ? (
                  <span className="nd-meta">{fields.emptyNote}</span>
                ) : (
                  fields.names.map((name) => (
                    <span key={name} className="nd-badge nd-badge--type">
                      {name}
                    </span>
                  ))
                )}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** The fields a row claims to touch, and how that claim should be worded. */
interface TouchedFields {
  label: string;
  names: string[];
  emptyNote: string;
}

/**
 * Work out which fields a version touches.
 *
 * For a `proposed` version the answer is recorded, not inferred: the service
 * stores the exact field list the agent named and an accept writes only those.
 * For an applied snapshot there is no such record, so the fields are computed
 * against the previous snapshot and labelled as the comparison it is.
 *
 * @param version The version being rendered.
 * @param previous The snapshot immediately before it, if any.
 */
function touchedFields(version: VersionOut, previous: VersionOut | undefined): TouchedFields {
  if (version.proposed_fields !== null) {
    return {
      label: "proposes",
      names: version.proposed_fields,
      emptyNote: "no fields named",
    };
  }
  if (!previous) {
    return { label: "initial", names: [], emptyNote: "first snapshot of this node" };
  }
  const names: string[] = [];
  if (version.title !== previous.title) names.push("title");
  if (version.content !== previous.content) names.push("content");
  if (stableJson(version.props) !== stableJson(previous.props)) names.push("props");
  return {
    label: "changed",
    names,
    emptyNote: "nothing changed against the previous snapshot",
  };
}

/**
 * Serialise a value with every object's keys sorted.
 *
 * Props round-trip through SQLite as JSON text, so key order can differ
 * between two snapshots that hold the same data; comparing raw
 * `JSON.stringify` output would report a change that never happened.
 */
function stableJson(value: unknown): string {
  return JSON.stringify(sortKeysDeep(value));
}

/** Recursively rebuild objects with their keys in sorted order. */
function sortKeysDeep(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeysDeep);
  if (value !== null && typeof value === "object") {
    const sorted = Object.entries(value as Record<string, unknown>).sort(([a], [b]) =>
      a.localeCompare(b),
    );
    return Object.fromEntries(sorted.map(([key, item]) => [key, sortKeysDeep(item)]));
  }
  return value;
}

export default VersionTimeline;
