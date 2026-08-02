/**
 * The cost of a cycle's model use — `cycles.report["llm"]`, filed by the
 * abstraction job's run (A1) — shown in the journal beside the coherence
 * metrics.
 *
 * Rendered only when a report is present, so this section never draws a table
 * of dashes for a cycle that made no model call. Two absences are kept in two
 * voices, exactly as the runtime reports them (B3): a provider that is not
 * configured is `available: false` with a reason — a stable fact about this
 * install, true until somebody configures one — while a budget that ran out is
 * `exhausted: true`, a fact about this one run, false again tomorrow. Neither
 * is a failure and neither is coloured like one.
 *
 * The per-job rows are the number a human checks a night against: `calls` is
 * every call that reached the wire, `failed_calls` the subset that produced no
 * usable result, and a declared job that never got a turn still appears (with
 * zeroes), so "no work" and "no turn" stay distinguishable.
 */

import type { LlmReport } from "./journal";

/** A value as a readable string, with thousands separators for tokens. */
function valueText(value: number): string {
  return value.toLocaleString();
}

/**
 * Render the model-cost table for one cycle.
 *
 * @param report `cycles.report["llm"]` read by `journal.readLlmReport`.
 */
export function CostSection({ report }: { report: LlmReport }) {
  const posture = report.enabled ? "on" : "off";
  const provider = report.provider ?? "none";
  const model = report.modelId ?? "—";
  const budget = `${valueText(report.budgetTokens)} tokens / ${report.budgetSeconds}s`;

  return (
    <section className="nd-jn-section" aria-label="Model cost">
      <h2 className="nd-jn-section__title">Cost</h2>
      <div className="nd-jn-scroll">
        <table className="nd-jn-metrics">
          <caption className="nd-sr-only">
            What the cycle's model use cost, and what it did not reach.
          </caption>
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">Value</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">
                <span className="nd-jn-metrics__label">Enabled</span>
                <span className="nd-meta nd-jn-metrics__note">
                  A provider and a non-zero budget
                </span>
              </th>
              <td className="nd-mono">{posture}</td>
            </tr>
            <tr>
              <th scope="row">
                <span className="nd-jn-metrics__label">Provider</span>
              </th>
              <td className="nd-mono">{provider}</td>
            </tr>
            <tr>
              <th scope="row">
                <span className="nd-jn-metrics__label">Model</span>
              </th>
              <td className="nd-mono">{model}</td>
            </tr>
            <tr>
              <th scope="row">
                <span className="nd-jn-metrics__label">Calls</span>
                <span className="nd-meta nd-jn-metrics__note">
                  {report.failedCalls} produced no usable result
                </span>
              </th>
              <td className="nd-mono">{valueText(report.calls)}</td>
            </tr>
            <tr>
              <th scope="row">
                <span className="nd-jn-metrics__label">Prompt tokens</span>
              </th>
              <td className="nd-mono">{valueText(report.promptTokens)}</td>
            </tr>
            <tr>
              <th scope="row">
                <span className="nd-jn-metrics__label">Output tokens</span>
                <span className="nd-meta nd-jn-metrics__note">
                  {report.reasoningTokens} spent thinking
                </span>
              </th>
              <td className="nd-mono">{valueText(report.outputTokens)}</td>
            </tr>
            <tr>
              <th scope="row">
                <span className="nd-jn-metrics__label">Total tokens</span>
              </th>
              <td className="nd-mono">{valueText(report.totalTokens)}</td>
            </tr>
            <tr>
              <th scope="row">
                <span className="nd-jn-metrics__label">Budget</span>
                <span className="nd-meta nd-jn-metrics__note">
                  {report.elapsedSeconds}s elapsed
                </span>
              </th>
              <td className="nd-mono">{budget}</td>
            </tr>
            <tr>
              <th scope="row">
                <span className="nd-jn-metrics__label">Exhausted</span>
                <span className="nd-meta nd-jn-metrics__note">
                  A ceiling stopped the work
                </span>
              </th>
              <td className="nd-mono">{report.exhausted ? "yes" : "no"}</td>
            </tr>
          </tbody>
        </table>
      </div>
      {!report.available ? (
        <p className="nd-meta nd-jn-section__note">
          No provider is configured{report.unavailableReason === null ? "." : `: ${report.unavailableReason}`}{" "}
          The abstraction job's model spend is what this section costs; without a provider it files
          this report and runs nothing.
        </p>
      ) : null}
      {report.perJob.length === 0 ? null : (
        <ul className="nd-jn-jobs">
          {report.perJob.map((job) => (
            <li key={job.job} className="nd-jn-job">
              <div className="nd-jn-job__head">
                <span className="nd-mono nd-jn-job__name">{job.job}</span>
                <span className="nd-meta">
                  calls {job.calls} · prompt {valueText(job.promptTokens)} · output{" "}
                  {valueText(job.outputTokens)}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
