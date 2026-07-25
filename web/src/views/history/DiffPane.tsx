import { useMemo } from "react";
import type { DiffOut } from "../../api/types";
import { formatTimestamp } from "../../lib";
import { parseUnifiedDiff } from "./unifiedDiff";

/**
 * Side-by-side diff between two versions.
 *
 * The server sends one unified diff over a stable text rendering of each
 * version (title line, props JSON, blank, content) plus a `changed_fields`
 * list. Both are shown: the field list is the server's own answer to "what
 * changed", and the two columns are how a human checks it.
 *
 * The table is a real `<table>` with row and column headers, so a screen
 * reader can walk it, and the raw unified diff stays available underneath for
 * anyone who wants the text the CLI would print.
 */

interface DiffPaneProps {
  diff: DiffOut;
  /** Ordinal (v1, v2, …) for a version id, for column headings. */
  ordinalOf: (versionId: number) => number;
}

/** Render the diff between `diff.a` (older) and `diff.b` (newer). */
export function DiffPane({ diff, ordinalOf }: DiffPaneProps) {
  const rows = useMemo(() => parseUnifiedDiff(diff.diff), [diff.diff]);

  return (
    <section className="nd-diff" aria-label="Version comparison">
      <header className="nd-diff__header">
        <h2 className="nd-diff__title">
          v{ordinalOf(diff.a.id)} → v{ordinalOf(diff.b.id)}
        </h2>
        <p className="nd-meta">
          {diff.changed_fields.length === 0
            ? "No field differs between these two versions."
            : `Changed: ${diff.changed_fields.join(", ")}`}
        </p>
      </header>

      {rows.length === 0 ? (
        <p className="nd-diff__identical">
          These two snapshots render identically — the versions differ only in metadata (actor,
          state, or timestamp).
        </p>
      ) : (
        <div className="nd-diff__scroll">
          <table className="nd-diff__table">
            <caption className="nd-sr-only">
              Line-by-line comparison of version {diff.a.id} and version {diff.b.id}
            </caption>
            <thead>
              <tr>
                <th scope="col" className="nd-diff__gutter">
                  <span className="nd-sr-only">Line in the older version</span>
                </th>
                <th scope="col">
                  v{ordinalOf(diff.a.id)}{" "}
                  <span className="nd-meta">
                    {diff.a.actor} · {formatTimestamp(diff.a.created_at)}
                  </span>
                </th>
                <th scope="col" className="nd-diff__gutter">
                  <span className="nd-sr-only">Line in the newer version</span>
                </th>
                <th scope="col">
                  v{ordinalOf(diff.b.id)}{" "}
                  <span className="nd-meta">
                    {diff.b.actor} · {formatTimestamp(diff.b.created_at)}
                  </span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) =>
                row.kind === "hunk" ? (
                  <tr key={index} className="nd-diff__row nd-diff__row--hunk">
                    <td colSpan={4} className="nd-mono">
                      {row.left}
                    </td>
                  </tr>
                ) : (
                  <tr key={index} className={`nd-diff__row nd-diff__row--${row.kind}`}>
                    <td className="nd-diff__gutter">{row.leftNumber ?? ""}</td>
                    <td className="nd-diff__cell nd-diff__cell--left">
                      {row.left === null ? null : <code>{row.left || " "}</code>}
                    </td>
                    <td className="nd-diff__gutter">{row.rightNumber ?? ""}</td>
                    <td className="nd-diff__cell nd-diff__cell--right">
                      {row.right === null ? null : <code>{row.right || " "}</code>}
                    </td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
        </div>
      )}

      <details className="nd-diff__raw">
        <summary>Unified diff</summary>
        <pre className="nd-diff__raw-body">{diff.diff || "(empty)"}</pre>
      </details>
    </section>
  );
}

export default DiffPane;
