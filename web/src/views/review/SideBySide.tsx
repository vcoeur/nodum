/**
 * The two-pane diff renderer: current on the left, proposed on the right.
 *
 * Rows are aligned by {@link diffLines}, so a changed line sits opposite the
 * line it replaced rather than opposite whatever happened to be at that offset.
 * Long unchanged runs collapse to a "n lines unchanged" rule.
 */

import { useMemo } from "react";
import { collapseEqual, diffLines } from "./linediff";
import type { DiffRow } from "./linediff";
import { plural } from "./format";

interface SideBySideProps {
  before: string;
  after: string;
  /** Column headings, e.g. "on the node now" / "proposed". */
  beforeLabel: string;
  afterLabel: string;
  /** Rendered when both sides are identical. */
  identicalNote?: string;
}

/** Row background class per diff kind. */
const ROW_CLASS: Record<DiffRow["kind"], string> = {
  equal: "nd-rv-diff__row",
  added: "nd-rv-diff__row nd-rv-diff__row--added",
  removed: "nd-rv-diff__row nd-rv-diff__row--removed",
  changed: "nd-rv-diff__row nd-rv-diff__row--changed",
};

/**
 * A side-by-side text diff.
 *
 * @param before The value on the node now.
 * @param after The value the proposal would write.
 * @param beforeLabel Left column heading.
 * @param afterLabel Right column heading.
 * @param identicalNote Shown instead of the panes when nothing differs.
 */
export function SideBySide({
  before,
  after,
  beforeLabel,
  afterLabel,
  identicalNote,
}: SideBySideProps) {
  const rows = useMemo(() => diffLines(before, after), [before, after]);

  if (before === after) {
    return (
      <p className="nd-rv-diff__identical">
        {identicalNote ?? "No change — the proposed value is what the node already holds."}
      </p>
    );
  }

  if (rows === null) {
    // Too large to align; show the raw panes rather than blocking the review.
    return (
      <div className="nd-rv-diff">
        <div className="nd-rv-diff__head">
          <span className="nd-label">{beforeLabel}</span>
          <span className="nd-label">{afterLabel}</span>
        </div>
        <p className="nd-rv-diff__note">
          Too long to align line by line — the two values are shown whole.
        </p>
        <div className="nd-rv-diff__panes">
          <pre className="nd-rv-diff__pane">{before}</pre>
          <pre className="nd-rv-diff__pane">{after}</pre>
        </div>
      </div>
    );
  }

  const collapsed = collapseEqual(rows);

  return (
    <div className="nd-rv-diff">
      <div className="nd-rv-diff__head">
        <span className="nd-label">{beforeLabel}</span>
        <span className="nd-label">{afterLabel}</span>
      </div>
      {/* Deliberately not an ARIA table: the gutters are decorative, so the
          role/rowgroup structure would be malformed. A labelled group reads as
          the two-column text it is. */}
      <div
        className="nd-rv-diff__body"
        role="group"
        aria-label={`${beforeLabel} versus ${afterLabel}`}
      >
        {collapsed.map((entry, index) =>
          typeof entry === "number" ? (
            <div key={`gap-${index}`} className="nd-rv-diff__gap">
              {plural(entry, "line")} unchanged
            </div>
          ) : (
            <div key={`row-${index}`} className={ROW_CLASS[entry.kind]}>
              <span className="nd-rv-diff__gutter" aria-hidden="true">
                {entry.leftNumber ?? ""}
              </span>
              <span className="nd-rv-diff__cell nd-rv-diff__cell--left">{entry.left ?? ""}</span>
              <span className="nd-rv-diff__gutter" aria-hidden="true">
                {entry.rightNumber ?? ""}
              </span>
              <span className="nd-rv-diff__cell nd-rv-diff__cell--right">{entry.right ?? ""}</span>
            </div>
          ),
        )}
      </div>
    </div>
  );
}
