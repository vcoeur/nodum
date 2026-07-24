/**
 * One policy rule, edited with its consequence stated underneath it.
 *
 * The rule form is small; the reading of the rule is not, and that asymmetry is
 * on purpose. A policy is the only control in this app that converts an
 * external agent's proposals into direct live writes, and the difference
 * between a rule that grants that and one that quietly grants nothing is a
 * single checkbox. So every rule carries its blast radius inline, recomputed as
 * it is typed, before anything is saved.
 */

import { useId } from "react";
import type { EdgeTypeOut } from "../../api/types";
import { POLICY_ACTIONS, describeBlastRadius, validateDraft } from "./policyRules";
import type { DraftRule, PolicyAction, RuleScope } from "./policyRules";
import { plural } from "./format";

interface PolicyRuleEditorProps {
  draft: DraftRule;
  /** The actor the policy governs — only its own writes are ever matched. */
  agent: string;
  /** The live edge-type catalog, for the scope picker. */
  edgeTypes: EdgeTypeOut[];
  /** Pending proposals in the queue this rule would have auto-accepted. */
  wouldHaveSkipped: number;
  onChange: (next: DraftRule) => void;
  onRemove: () => void;
}

/** Human wording for each action in the picker. */
const ACTION_LABEL: Record<PolicyAction, string> = {
  auto_accept: "auto_accept — land the write live, skipping review",
  auto_apply: "auto_apply — internal runtime (Phase 5)",
  always_propose: "always_propose — internal runtime (Phase 5)",
};

/**
 * A single rule row.
 *
 * @param draft The rule being edited.
 * @param agent The policy's agent, used in the blast-radius wording.
 * @param edgeTypes The catalog backing the edge-type picker.
 * @param wouldHaveSkipped How many waiting proposals this rule would have let through.
 * @param onChange Receives the updated draft.
 * @param onRemove Delete this rule from the ruleset.
 */
export function PolicyRuleEditor({
  draft,
  agent,
  edgeTypes,
  wouldHaveSkipped,
  onChange,
  onRemove,
}: PolicyRuleEditorProps) {
  const fieldId = useId();
  const problems = validateDraft(draft);
  const radius = describeBlastRadius(draft, agent);

  const update = (patch: Partial<DraftRule>) => onChange({ ...draft, ...patch });

  return (
    <div className={`nd-rv-rule nd-rv-rule--${radius.level}`}>
      <div className="nd-rv-rule__grid">
        <label className="nd-field">
          <span className="nd-label">Scope</span>
          <select
            className="nd-select"
            value={draft.scope}
            onChange={(event) => update({ scope: event.target.value as RuleScope, target: "" })}
          >
            <option value="edge_type">edge_type — governs direct edge writes</option>
            <option value="job">job — per-job autonomy dial (Phase 5)</option>
          </select>
        </label>

        <label className="nd-field">
          <span className="nd-label">{draft.scope === "edge_type" ? "Edge type" : "Job"}</span>
          {draft.scope === "edge_type" ? (
            <select
              className="nd-select"
              value={draft.target}
              onChange={(event) => update({ target: event.target.value })}
            >
              <option value="">choose an edge type…</option>
              {edgeTypes.map((type) => (
                <option key={type.id} value={type.id}>
                  {type.name === type.id ? type.id : `${type.name} (${type.id})`}
                </option>
              ))}
            </select>
          ) : (
            <input
              className="nd-input nd-input--mono"
              value={draft.target}
              onChange={(event) => update({ target: event.target.value })}
              placeholder="job name"
            />
          )}
        </label>

        <label className="nd-field">
          <span className="nd-label">Action</span>
          <select
            className="nd-select"
            value={draft.action}
            onChange={(event) => update({ action: event.target.value as PolicyAction })}
          >
            {POLICY_ACTIONS.map((action) => (
              <option key={action} value={action}>
                {ACTION_LABEL[action]}
              </option>
            ))}
          </select>
        </label>

        <label className="nd-field">
          <span className="nd-label">min_confidence (optional)</span>
          <input
            id={`${fieldId}-gate`}
            className="nd-input nd-input--mono"
            value={draft.minConfidence}
            onChange={(event) => update({ minConfidence: event.target.value })}
            placeholder="none"
            inputMode="decimal"
          />
        </label>
      </div>

      <label className="nd-rv-rule__trust">
        <input
          type="checkbox"
          checked={draft.trustSelfReported}
          onChange={(event) => update({ trustSelfReported: event.target.checked })}
        />
        <span>
          <code>trust_self_reported_confidence</code> — accept this agent's own
          grading of its own writes
        </span>
      </label>

      {draft.minConfidence.trim() !== "" && !draft.trustSelfReported ? (
        <p className="nd-rv-flag nd-rv-flag--warn nd-rv-rule__trap">
          The confidence floor above is <strong>inert</strong> without this box
          ticked. <code>min_confidence</code> grades the number the agent reports
          about its own write, so the service refuses to evaluate a gated rule
          unless the policy explicitly opts in. As written, this rule never fires.
        </p>
      ) : null}

      <div className="nd-rv-rule__radius">
        <span className={`nd-rv-radius__tag nd-rv-radius__tag--${radius.level}`}>
          {radius.level === "grant"
            ? "grants unreviewed live writes"
            : radius.level === "gated-grant"
              ? "grants unreviewed live writes, gated"
              : radius.level === "inert"
                ? "does nothing"
                : "stored, not enforced"}
        </span>
        <p className="nd-rv-radius__headline">{radius.headline}</p>
        <p className="nd-rv-radius__detail">{radius.detail}</p>
        {radius.trap ? <p className="nd-rv-radius__trap">{radius.trap}</p> : null}
        {(radius.level === "grant" || radius.level === "gated-grant") && wouldHaveSkipped > 0 ? (
          <p className="nd-rv-radius__evidence">
            {plural(wouldHaveSkipped, "proposal")} from {agent} of this edge type{" "}
            {wouldHaveSkipped === 1 ? "is" : "are"} waiting in the queue right now.
            With this rule saved, {wouldHaveSkipped === 1 ? "it" : "they"} would
            have gone live without ever appearing there.
          </p>
        ) : null}
      </div>

      {problems.length > 0 ? (
        <ul className="nd-rv-rule__errors">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      ) : null}

      {Object.keys(draft.extras).length > 0 ? (
        <p className="nd-meta">
          Extra keys preserved from the stored rule:{" "}
          <span className="nd-mono">{Object.keys(draft.extras).join(", ")}</span>
        </p>
      ) : null}

      <div className="nd-rv-rule__footer">
        <button type="button" className="nd-button nd-button--ghost nd-button--small" onClick={onRemove}>
          Remove rule
        </button>
      </div>
    </div>
  );
}
