/**
 * The acceptance section of a cycle — the curation job's
 * `detail["acceptance"]` (L4): per proposer, per type, how often their
 * proposals were accepted against how often they were rejected, over the
 * rolling window.
 *
 * The rates are row-state statistics computed by the job, not judgements: the
 * section renders the numbers the job recorded and never colours them — a low
 * rate is a fact about a proposer's record, and which way to read it belongs
 * to the human reviewing the queue.
 *
 * Deltas are not computed here. The design composes them from the convention
 * nodes' own versions — a second read this page does not make — so the note
 * says where they live rather than pretending to have fetched them.
 */

import type { AcceptanceEntry } from "./journal";

/** A rate as a whole percentage, e.g. `67 %`. */
function rateText(rate: number): string {
  return `${Math.round(rate * 100)} %`;
}

/**
 * The kind of row state a rate was counted over, said in a word a reviewer
 * can read beside a type id.
 */
function kindLabel(kind: string): string {
  if (kind === "edge") return "edge type";
  if (kind === "version") return "node type (updates)";
  return "node type";
}

/**
 * Render the acceptance table for one cycle.
 *
 * @param entries The curation job's `detail["acceptance"]`, read by
 *   `journal.readAcceptance`.
 */
export function AcceptanceSection({ entries }: { entries: AcceptanceEntry[] }) {
  return (
    <section className="nd-jn-section" aria-label="Proposer acceptance">
      <h2 className="nd-jn-section__title">Acceptance</h2>
      <div className="nd-jn-scroll">
        <table className="nd-jn-metrics">
          <caption className="nd-sr-only">
            Each proposer's acceptance and rejection counts per type, over the
            curation window.
          </caption>
          <thead>
            <tr>
              <th scope="col">Proposer</th>
              <th scope="col">Type</th>
              <th scope="col">Accepted</th>
              <th scope="col">Rejected</th>
              <th scope="col">Rate</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={`${entry.proposer}-${entry.kind}-${entry.type}`}>
                <th scope="row" className="nd-mono">
                  {entry.proposer}
                </th>
                <td className="nd-mono">
                  {entry.type}{" "}
                  <span className="nd-meta">{kindLabel(entry.kind)}</span>
                </td>
                <td>{entry.accepted}</td>
                <td>{entry.rejected}</td>
                <td className="nd-mono">{rateText(entry.rate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="nd-meta nd-jn-section__note">
        Acceptance is read from row state over the curation window (90 days), never from
        the event log. Deltas between cycles are the diff of the convention nodes' own
        versions — this view shows the rates tonight's cycle computed.
      </p>
    </section>
  );
}
