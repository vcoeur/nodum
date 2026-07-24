/**
 * The policy rule model, mirrored from the service layer.
 *
 * Everything here is a client-side restatement of two functions in
 * `nodum/service.py` and must not drift from them:
 *
 * - `_validate_rules` — what the server will *store*: an action from
 *   `POLICY_ACTIONS`, at least one of `job` / `edge_type`, `min_confidence` in
 *   `[0, 1]`, a boolean `trust_self_reported_confidence`, and an `edge_type`
 *   that resolves against the type catalog. Unknown keys pass through
 *   untouched, so the editor preserves them instead of dropping them.
 * - `_auto_accept_rule` — what the server will *do*: only the actor's own
 *   policy applies, only `edge_type` rules with action `auto_accept` are
 *   evaluated on the direct write path, and a `min_confidence` gate is **inert
 *   unless the same rule sets `trust_self_reported_confidence: true`**, because
 *   the only confidence available there is the one the writing agent reports
 *   about its own write.
 *
 * Client-side validation exists to catch a mistake before it is submitted, not
 * to be authoritative: the server's answer always wins and is surfaced verbatim.
 */

import type { PolicyRule } from "../../api/types";

/** `service.POLICY_ACTIONS`. */
export const POLICY_ACTIONS = ["auto_accept", "auto_apply", "always_propose"] as const;

/** One of the three actions a rule may carry. */
export type PolicyAction = (typeof POLICY_ACTIONS)[number];

/** `service.TRUST_SELF_CONFIDENCE`. */
export const TRUST_SELF_CONFIDENCE = "trust_self_reported_confidence";

/** Which key a rule is scoped by. A rule needs exactly one in this editor. */
export type RuleScope = "edge_type" | "job";

/** The keys the editor owns; anything else on a rule is preserved verbatim. */
const MANAGED_KEYS = new Set<string>([
  "job",
  "edge_type",
  "action",
  "min_confidence",
  TRUST_SELF_CONFIDENCE,
]);

/**
 * One rule as the editor holds it.
 *
 * `minConfidence` stays a raw string while being typed — an empty box means "no
 * gate", which is not the same as `0`, and a half-typed `"0."` must not
 * round-trip through a number.
 */
export interface DraftRule {
  /** React key; never sent. */
  localId: string;
  scope: RuleScope;
  /** The edge-type id/name or the job name, depending on `scope`. */
  target: string;
  action: PolicyAction;
  /** Raw text of the confidence floor; empty means no gate. */
  minConfidence: string;
  trustSelfReported: boolean;
  /** Keys the editor does not manage, kept so a save never silently drops them. */
  extras: Record<string, unknown>;
}

let localIdCounter = 0;

/** A fresh key for a draft rule. */
function nextLocalId(): string {
  localIdCounter += 1;
  return `rule-${localIdCounter}`;
}

/** A new, empty `auto_accept` edge-type rule — the shape that actually does something. */
export function emptyDraft(): DraftRule {
  return {
    localId: nextLocalId(),
    scope: "edge_type",
    target: "",
    action: "auto_accept",
    minConfidence: "",
    trustSelfReported: false,
    extras: {},
  };
}

/** Read a stored rule into the editor's shape, preserving unknown keys. */
export function toDraft(rule: PolicyRule): DraftRule {
  const scope: RuleScope = rule.edge_type !== undefined ? "edge_type" : "job";
  const target = scope === "edge_type" ? (rule.edge_type ?? "") : (rule.job ?? "");
  const action = POLICY_ACTIONS.includes(rule.action as PolicyAction)
    ? (rule.action as PolicyAction)
    : "auto_accept";
  const extras: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(rule)) {
    if (!MANAGED_KEYS.has(key)) extras[key] = value;
  }
  return {
    localId: nextLocalId(),
    scope,
    target: String(target),
    action,
    minConfidence:
      typeof rule.min_confidence === "number" ? String(rule.min_confidence) : "",
    trustSelfReported: rule[TRUST_SELF_CONFIDENCE] === true,
    extras,
  };
}

/**
 * Render a draft back into the wire shape `set_policy` validates.
 *
 * Keys that are absent stay absent: sending `min_confidence: null` is not the
 * same as omitting it, and `_validate_rules` only range-checks it when present.
 */
export function toRule(draft: DraftRule): PolicyRule {
  const rule: PolicyRule = { ...draft.extras, action: draft.action };
  if (draft.scope === "edge_type") rule.edge_type = draft.target.trim();
  else rule.job = draft.target.trim();
  const gate = draft.minConfidence.trim();
  if (gate !== "") rule.min_confidence = Number(gate);
  if (draft.trustSelfReported) rule[TRUST_SELF_CONFIDENCE] = true;
  return rule;
}

/** A problem the server would reject the ruleset for. */
export interface RuleError {
  localId: string;
  message: string;
}

/**
 * Validate a draft against `_validate_rules`.
 *
 * @returns The problems, empty when the server would accept the rule. An
 *   unresolvable `edge_type` is *not* checked here beyond emptiness — the
 *   catalog check is the server's, and the editor offers a picker instead.
 */
export function validateDraft(draft: DraftRule): string[] {
  const problems: string[] = [];
  if (draft.target.trim() === "") {
    problems.push(
      draft.scope === "edge_type"
        ? "pick an edge type — a rule needs a 'job' or an 'edge_type' key"
        : "name a job — a rule needs a 'job' or an 'edge_type' key",
    );
  }
  if (!POLICY_ACTIONS.includes(draft.action)) {
    problems.push(`action must be one of ${POLICY_ACTIONS.join(", ")}`);
  }
  const gate = draft.minConfidence.trim();
  if (gate !== "") {
    const value = Number(gate);
    if (!Number.isFinite(value)) problems.push("min_confidence must be a number");
    else if (value < 0 || value > 1) {
      problems.push(`min_confidence must be between 0 and 1, got ${value}`);
    }
  }
  return problems;
}

/** Every error across a ruleset, keyed back to the draft that caused it. */
export function validateDrafts(drafts: readonly DraftRule[]): RuleError[] {
  const errors: RuleError[] = [];
  for (const draft of drafts) {
    for (const message of validateDraft(draft)) {
      errors.push({ localId: draft.localId, message });
    }
  }
  return errors;
}

/** How much of a warning a rule's reading deserves. */
export type BlastLevel = "grant" | "gated-grant" | "inert" | "stored";

/** What a rule actually does, in the reviewer's language. */
export interface BlastRadius {
  level: BlastLevel;
  /** One line: the consequence, stated as a consequence. */
  headline: string;
  /** The reason, and where it comes from in the service layer. */
  detail: string;
  /** Set when the rule reads like a guard it is not. */
  trap: string | null;
}

/**
 * Describe what a rule does once stored — the blast radius, before saving.
 *
 * The four readings, straight off `_auto_accept_rule`:
 *
 * - `auto_accept` on an `edge_type` with no gate — an unconditional grant. Every
 *   such edge the agent writes lands `active` with no review at all.
 * - `auto_accept` with a gate **and** the trust flag — a grant conditioned on a
 *   number the agent picks for itself.
 * - `auto_accept` with a gate and **no** trust flag — inert. The gate can never
 *   be satisfied on the direct write path, so the rule grants nothing. This is
 *   the trap: it reads like a careful, conservative rule and is in fact a
 *   no-op.
 * - `job` rules, and the `auto_apply` / `always_propose` actions — stored and
 *   validated, but not evaluated on the direct write path today. They govern
 *   the internal agent runtime, which is Phase 5.
 *
 * @param draft The rule being edited.
 * @param agent The actor the policy governs; only its *own* writes are matched.
 */
export function describeBlastRadius(draft: DraftRule, agent: string): BlastRadius {
  const who = agent.trim() === "" ? "this agent" : agent;
  const target = draft.target.trim() === "" ? "…" : draft.target.trim();
  const gate = draft.minConfidence.trim();

  if (draft.action !== "auto_accept") {
    return {
      level: "stored",
      headline: `Stored, not yet enforced — '${draft.action}' governs the internal agent runtime.`,
      detail:
        "Only 'auto_accept' is evaluated on the direct write path today. " +
        "'auto_apply' and 'always_propose' are validated and stored for the " +
        "Phase-5 runtime, so this rule changes nothing about what lands live now.",
      trap: null,
    };
  }

  if (draft.scope === "job") {
    return {
      level: "stored",
      headline: `Stored, not yet enforced — job rules do not govern direct writes.`,
      detail:
        "Auto-accept is only evaluated for 'edge_type' rules: a direct write " +
        "carries an edge type, not a job. A job rule is the per-job autonomy " +
        "dial for the Phase-5 internal runtime — it is recorded here and has no " +
        "effect on what an external agent can land live today.",
      trap: null,
    };
  }

  if (gate === "") {
    return {
      level: "grant",
      headline: `Every '${target}' edge ${who} writes lands active immediately, with no review.`,
      detail:
        "An auto-accept rule with no confidence floor is an unconditional " +
        `grant: those writes never reach this queue. It applies only to ${who}'s ` +
        "own writes and only to this edge type — other agents and other types " +
        "still land as proposals.",
      trap: null,
    };
  }

  if (!draft.trustSelfReported) {
    return {
      level: "inert",
      headline: `This rule does nothing. The confidence floor is not a guard.`,
      detail:
        `A min_confidence of ${gate} grades the confidence ${who} reports about ` +
        "its own write — untrusted input. The service refuses to evaluate a " +
        "gated rule unless it also carries trust_self_reported_confidence: true, " +
        `so as written, every '${target}' edge still lands proposed and waits ` +
        "here. That may be exactly what you want — but it is not what a " +
        "confidence floor looks like it is doing.",
      trap:
        "Tick 'trust the agent's self-reported confidence' to make the floor " +
        "live — which turns this into a grant — or clear the floor to say " +
        "plainly that the rule is not meant to fire.",
    };
  }

  return {
    level: "gated-grant",
    headline: `Every '${target}' edge ${who} writes claiming confidence ≥ ${gate} lands active, with no review.`,
    detail:
      `The number is ${who}'s own: it grades its writes itself and is free to ` +
      `claim 1.0. Ticking the trust flag is the written record that you accept ` +
      "that self-grading for this edge type. Writes below the floor, and writes " +
      "reporting no confidence at all, still land proposed and wait here.",
    trap: null,
  };
}

/**
 * Identify a rule in one line, by what it *is* rather than what it does.
 *
 * {@link describeBlastRadius} states consequences, which is the right thing
 * while editing and the wrong thing when listing rules that are about to be
 * discarded — there, the reviewer needs to recognise the rule they wrote.
 */
export function ruleSignature(draft: DraftRule): string {
  const target = draft.target.trim() === "" ? "(unset)" : draft.target.trim();
  const parts = [`${draft.action} on ${draft.scope} "${target}"`];
  const gate = draft.minConfidence.trim();
  if (gate !== "") {
    parts.push(
      draft.trustSelfReported
        ? `min_confidence ${gate}, self-grading trusted`
        : `min_confidence ${gate}, self-grading not trusted — inert`,
    );
  }
  const extras = Object.keys(draft.extras);
  if (extras.length > 0) parts.push(`extra keys: ${extras.join(", ")}`);
  return parts.join(" · ");
}

/** True when a rule, as written, grants unreviewed live writes. */
export function isGrant(draft: DraftRule): boolean {
  const level = describeBlastRadius(draft, "").level;
  return level === "grant" || level === "gated-grant";
}

/** True when a rule reads like a guard but is inert (the min_confidence trap). */
export function isInert(draft: DraftRule): boolean {
  return describeBlastRadius(draft, "").level === "inert";
}
