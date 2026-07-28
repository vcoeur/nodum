/**
 * The five coherence metrics, before and after, with the delta.
 *
 * The direction arrow is an arrow and **not** a colour, on purpose: whether a
 * metric rising is good is a judgement this view cannot make. A cycle that flags
 * duplicates raises `duplicate_candidates` *by doing its job*, and a green-down /
 * red-up ramp would call that a regression. So the table reports the movement
 * and says what each metric measures, and leaves the reading to the human.
 *
 * `{}` is a real answer rather than missing data — a rollback and a one-op
 * curative cycle compute no metrics — so it gets a sentence saying so instead of
 * a table of dashes.
 */

import type { CycleMetrics } from "../../api/types";
import { metricRows } from "./journal";

/** Arrow per direction; `aria-hidden`, since the delta text already says it. */
const ARROW: Record<string, string> = { up: "↑", down: "↓", flat: "→", unknown: "" };

/** Render the before/after metric table for one cycle. */
export function MetricTable({ metrics }: { metrics: CycleMetrics }) {
  const rows = metricRows(metrics);

  return (
    <section className="nd-jn-section" aria-label="Coherence metrics">
      <h2 className="nd-jn-section__title">Coherence</h2>
      {rows.length === 0 ? (
        <p className="nd-meta nd-jn-section__note">
          This cycle recorded no coherence metrics. A rollback and a one-op curative cycle do not
          compute them — they change specific rows rather than sweeping the file.
        </p>
      ) : (
        <div className="nd-jn-scroll">
          <table className="nd-jn-metrics">
            <caption className="nd-sr-only">
              Each coherence metric before and after the cycle, with the change.
            </caption>
            <thead>
              <tr>
                <th scope="col">Metric</th>
                <th scope="col">Before</th>
                <th scope="col">After</th>
                <th scope="col">Change</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.key}>
                  <th scope="row">
                    <span className="nd-jn-metrics__label">{row.label}</span>
                    {row.note === "" ? null : (
                      <span className="nd-meta nd-jn-metrics__note">{row.note}</span>
                    )}
                  </th>
                  <td className="nd-mono">{row.before}</td>
                  <td className="nd-mono">{row.after}</td>
                  <td className="nd-mono">
                    <span aria-hidden="true" className="nd-jn-metrics__arrow">
                      {ARROW[row.direction] ?? ""}
                    </span>
                    {row.delta}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
