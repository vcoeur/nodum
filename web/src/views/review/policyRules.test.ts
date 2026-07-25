/**
 * The policy rule model, and the trap it exists to expose.
 *
 * `_auto_accept_rule` in `nodum/service.py` only evaluates `auto_accept` rules
 * scoped by `edge_type`, and a `min_confidence` gate on one is **inert unless
 * the same rule sets `trust_self_reported_confidence: true`** — because the only
 * confidence available on the direct write path is the number the writing agent
 * reports about its own write. A rule with a floor and no trust flag therefore
 * reads like a careful, conservative grant and in fact grants nothing.
 *
 * That reading is what `describeBlastRadius` is for and what most of this file
 * pins. The rest guards the editor's other promise: a save never silently drops
 * a key the editor does not manage, and an absent `min_confidence` stays absent
 * rather than being sent as a null.
 */

import { describe, expect, it } from "vitest";
import type { PolicyRule } from "../../api/types";
import {
  describeBlastRadius,
  emptyDraft,
  isGrant,
  isInert,
  POLICY_ACTIONS,
  ruleSignature,
  toDraft,
  toRule,
  TRUST_SELF_CONFIDENCE,
  validateDraft,
  validateDrafts,
} from "./policyRules";
import type { DraftRule } from "./policyRules";

/** A draft with the given overrides on top of a blank edge-type rule. */
const draft = (overrides: Partial<DraftRule> = {}): DraftRule => ({
  ...emptyDraft(),
  target: "mentions",
  ...overrides,
});

describe("emptyDraft", () => {
  it("starts on the shape that actually does something", () => {
    // `auto_accept` on an `edge_type` is the only combination the service
    // evaluates today; defaulting elsewhere would invite a no-op rule.
    const blank = emptyDraft();
    expect(blank.scope).toBe("edge_type");
    expect(blank.action).toBe("auto_accept");
    expect(blank.target).toBe("");
    expect(blank.minConfidence).toBe("");
    expect(blank.trustSelfReported).toBe(false);
  });

  it("hands out a distinct React key each time", () => {
    expect(emptyDraft().localId).not.toBe(emptyDraft().localId);
  });
});

describe("toDraft / toRule", () => {
  it("round-trips an edge-type rule", () => {
    const rule: PolicyRule = { edge_type: "mentions", action: "auto_accept" };
    expect(toRule(toDraft(rule))).toEqual(rule);
  });

  it("round-trips a job rule", () => {
    const rule: PolicyRule = { job: "summarise", action: "always_propose" };
    expect(toRule(toDraft(rule))).toEqual(rule);
  });

  it("preserves a key the editor does not manage", () => {
    // `_validate_rules` passes unknown keys through untouched, so a rule
    // written by hand or by a later version must survive an unrelated edit.
    const rule: PolicyRule = {
      edge_type: "cites",
      action: "auto_accept",
      note: "set during the 2026 backfill",
      priority: 3,
    };
    const back = toRule(toDraft(rule));
    expect(back.note).toBe("set during the 2026 backfill");
    expect(back.priority).toBe(3);
    expect(back).toEqual(rule);
  });

  it("keeps a confidence floor of zero, which is not the same as no floor", () => {
    // The obvious falsy-value bug: `0` is a real floor.
    const parsed = toDraft({ edge_type: "mentions", action: "auto_accept", min_confidence: 0 });
    expect(parsed.minConfidence).toBe("0");
    expect(toRule(parsed).min_confidence).toBe(0);
  });

  it("omits an absent floor rather than sending a null", () => {
    // `_validate_rules` only range-checks `min_confidence` when it is present;
    // sending an explicit null is a different request.
    const rule = toRule(draft());
    expect("min_confidence" in rule).toBe(false);
  });

  it("omits the trust flag when it is off, because absent already means false", () => {
    const rule = toRule(draft({ minConfidence: "0.8" }));
    expect(TRUST_SELF_CONFIDENCE in rule).toBe(false);
    expect(toRule(draft({ minConfidence: "0.8", trustSelfReported: true }))[TRUST_SELF_CONFIDENCE]).toBe(
      true,
    );
  });

  it("holds a half-typed floor as text instead of round-tripping it through a number", () => {
    // Typing "0.75" passes through "0." and "0.7". Holding the box's value as
    // a number would rewrite it under the cursor — "0." would become "0" and
    // the next keystroke would produce "07".
    expect(draft({ minConfidence: "0." }).minConfidence).toBe("0.");
    expect(draft({ minConfidence: "0.7" }).minConfidence).toBe("0.7");
    // Conversion happens only on the way out, and `Number("0.")` is 0 — a
    // valid floor, so a mid-typing save stores a real gate rather than junk.
    expect(toRule(draft({ minConfidence: "0." })).min_confidence).toBe(0);
    expect(validateDraft(draft({ minConfidence: "0." }))).toEqual([]);
  });

  it("lets validation, not the number cast, reject a floor that is not a number", () => {
    // `Number("high")` is NaN and would reach the server as a null; the editor
    // stops it first.
    expect(validateDraft(draft({ minConfidence: "high" }))).toHaveLength(1);
    expect(toRule(draft({ minConfidence: "high" })).min_confidence).toBeNaN();
  });

  it("trims whitespace out of the target before sending it", () => {
    expect(toRule(draft({ target: "  mentions  " })).edge_type).toBe("mentions");
    expect(toRule(draft({ scope: "job", target: "  summarise " })).job).toBe("summarise");
  });

  it("reads a rule with neither key as an unscoped job rule", () => {
    const parsed = toDraft({ action: "auto_accept" });
    expect(parsed.scope).toBe("job");
    expect(parsed.target).toBe("");
  });

  it("falls back to auto_accept for an action this build does not know", () => {
    expect(toDraft({ edge_type: "x", action: "teleport" }).action).toBe("auto_accept");
  });
});

describe("validateDraft", () => {
  it("accepts a well-formed rule", () => {
    expect(validateDraft(draft())).toEqual([]);
    expect(validateDraft(draft({ minConfidence: "0.75", trustSelfReported: true }))).toEqual([]);
  });

  it("requires a target, and says which key the server wants", () => {
    expect(validateDraft(draft({ target: "" }))[0]).toContain("edge_type");
    expect(validateDraft(draft({ target: "   " }))).toHaveLength(1);
    expect(validateDraft(draft({ scope: "job", target: "" }))[0]).toContain("job");
  });

  it("range-checks the floor exactly as `_validate_rules` does", () => {
    expect(validateDraft(draft({ minConfidence: "0" }))).toEqual([]);
    expect(validateDraft(draft({ minConfidence: "1" }))).toEqual([]);
    expect(validateDraft(draft({ minConfidence: "1.01" }))[0]).toContain("between 0 and 1");
    expect(validateDraft(draft({ minConfidence: "-0.1" }))[0]).toContain("between 0 and 1");
    expect(validateDraft(draft({ minConfidence: "high" }))[0]).toContain("must be a number");
  });

  it("treats an empty floor as no floor, not as an error", () => {
    expect(validateDraft(draft({ minConfidence: "  " }))).toEqual([]);
  });

  it("reports every problem on a rule at once", () => {
    expect(validateDraft(draft({ target: "", minConfidence: "9" }))).toHaveLength(2);
  });
});

describe("validateDrafts", () => {
  it("keys each problem back to the rule that caused it", () => {
    const good = draft();
    const bad = draft({ target: "" });
    const errors = validateDrafts([good, bad]);
    expect(errors).toHaveLength(1);
    expect(errors[0]!.localId).toBe(bad.localId);
  });

  it("is empty for a clean ruleset", () => {
    expect(validateDrafts([draft(), draft({ target: "cites" })])).toEqual([]);
  });
});

describe("describeBlastRadius", () => {
  it("calls an ungated auto-accept what it is: an unconditional grant", () => {
    const radius = describeBlastRadius(draft(), "agent:researcher");
    expect(radius.level).toBe("grant");
    expect(radius.headline).toContain("agent:researcher");
    expect(radius.headline).toContain("mentions");
    expect(radius.headline).toContain("no review");
    expect(radius.trap).toBeNull();
  });

  it("calls a gated auto-accept with the trust flag a grant on the agent's own word", () => {
    const radius = describeBlastRadius(
      draft({ minConfidence: "0.9", trustSelfReported: true }),
      "agent:researcher",
    );
    expect(radius.level).toBe("gated-grant");
    expect(radius.headline).toContain("0.9");
    // The point the reviewer has to understand before ticking the box.
    expect(radius.detail).toContain("free to");
    expect(radius.trap).toBeNull();
  });

  it("calls a gated auto-accept WITHOUT the trust flag inert, and says why", () => {
    // The trap: this reads like the careful version of the rule above and is
    // in fact a no-op, because the service refuses to evaluate a gated rule
    // that does not also trust the agent's self-reported confidence.
    const radius = describeBlastRadius(draft({ minConfidence: "0.9" }), "agent:researcher");
    expect(radius.level).toBe("inert");
    expect(radius.headline).toContain("does nothing");
    expect(radius.detail).toContain(TRUST_SELF_CONFIDENCE);
    expect(radius.trap).not.toBeNull();
    // The trap names both ways out, so the reviewer is not left guessing.
    expect(radius.trap).toContain("clear the floor");
  });

  it("treats a floor of zero as a real gate, so it falls into the trap too", () => {
    expect(describeBlastRadius(draft({ minConfidence: "0" }), "agent:a").level).toBe("inert");
  });

  it("calls a job rule stored-but-not-enforced", () => {
    // A direct write carries an edge type, not a job, so the rule cannot fire.
    const radius = describeBlastRadius(draft({ scope: "job", target: "summarise" }), "agent:a");
    expect(radius.level).toBe("stored");
    expect(radius.detail).toContain("edge_type");
    expect(radius.trap).toBeNull();
  });

  it("calls the two non-auto_accept actions stored-but-not-enforced", () => {
    for (const action of POLICY_ACTIONS.filter((value) => value !== "auto_accept")) {
      const radius = describeBlastRadius(draft({ action }), "agent:a");
      expect(radius.level).toBe("stored");
      expect(radius.headline).toContain(action);
    }
  });

  it("reads sensibly while the rule is still being typed", () => {
    const radius = describeBlastRadius(draft({ target: "" }), "");
    expect(radius.headline).toContain("this agent");
    expect(radius.headline).toContain("…");
  });

  it("always produces a headline and a detail", () => {
    const drafts = [
      draft(),
      draft({ minConfidence: "0.5" }),
      draft({ minConfidence: "0.5", trustSelfReported: true }),
      draft({ scope: "job", target: "j" }),
      draft({ action: "auto_apply" }),
      draft({ action: "always_propose" }),
    ];
    for (const rule of drafts) {
      const radius = describeBlastRadius(rule, "agent:a");
      expect(radius.headline.length).toBeGreaterThan(0);
      expect(radius.detail.length).toBeGreaterThan(0);
    }
  });
});

describe("isGrant / isInert", () => {
  it("agrees with the blast radius about which rules hand out live writes", () => {
    expect(isGrant(draft())).toBe(true);
    expect(isGrant(draft({ minConfidence: "0.9", trustSelfReported: true }))).toBe(true);
    expect(isGrant(draft({ minConfidence: "0.9" }))).toBe(false);
    expect(isGrant(draft({ scope: "job", target: "j" }))).toBe(false);
    expect(isGrant(draft({ action: "auto_apply" }))).toBe(false);
  });

  it("flags only the min_confidence trap as inert", () => {
    expect(isInert(draft({ minConfidence: "0.9" }))).toBe(true);
    expect(isInert(draft({ minConfidence: "0.9", trustSelfReported: true }))).toBe(false);
    expect(isInert(draft())).toBe(false);
    // A job rule does nothing either, but it is not the trap — it does not
    // read like a guard.
    expect(isInert(draft({ scope: "job", target: "j" }))).toBe(false);
  });

  it("never calls a rule both a grant and inert", () => {
    const drafts = [
      draft(),
      draft({ minConfidence: "0" }),
      draft({ minConfidence: "0.5", trustSelfReported: true }),
      draft({ scope: "job", target: "j" }),
      draft({ action: "always_propose" }),
    ];
    for (const rule of drafts) expect(isGrant(rule) && isInert(rule)).toBe(false);
  });
});

describe("ruleSignature", () => {
  it("identifies a rule by what it is, for a list of rules about to be discarded", () => {
    expect(ruleSignature(draft())).toBe('auto_accept on edge_type "mentions"');
  });

  it("marks an inert floor in the signature too", () => {
    expect(ruleSignature(draft({ minConfidence: "0.9" }))).toContain("inert");
    expect(ruleSignature(draft({ minConfidence: "0.9", trustSelfReported: true }))).toContain(
      "self-grading trusted",
    );
  });

  it("names an unset target rather than rendering an empty pair of quotes", () => {
    expect(ruleSignature(draft({ target: "" }))).toContain("(unset)");
  });

  it("names the keys the editor is carrying but not showing", () => {
    const parsed = toDraft({ edge_type: "cites", action: "auto_accept", note: "x", priority: 1 });
    expect(ruleSignature(parsed)).toContain("extra keys: note, priority");
  });
});
