import { describe, expect, it } from "vitest";
import type { EventOut, SettingOut } from "../../api/types";
import {
  adoptPreview,
  editBlocker,
  GROUPS,
  isEditable,
  layerLabel,
  liveness,
  livenessLabel,
  PROFILE_DEFAULT_NOTE,
  revertTarget,
} from "./settingsModel";

/** A row factory: every field a SettingOut carries, defaulted to editable-and-stored. */
function row(overrides: Partial<SettingOut> & { key: string }): SettingOut {
  return {
    value: "x",
    set: true,
    provenance: "settings.env",
    default: "d",
    kind: "string",
    secret: false,
    writable: true,
    refusal: null,
    stored: true,
    on_invalid: null,
    ...overrides,
  };
}

/** An event factory shaped like `settings.set`/`settings.unset` payloads. */
function event(key: string, before: string | null): EventOut {
  return {
    seq: 1,
    actor: "human:owner",
    op: "settings.unset",
    payload: { key, before, after: null },
    cycle_id: null,
    created_at: "2026-08-22 00:00:00",
  };
}

describe("grouping", () => {
  it("covers this build's seventeen names exactly once across the five groups", () => {
    const keys = GROUPS.flatMap((group) => group.keys);
    // 19 minus the two embedding names SP5 makes editable.
    expect(keys.length).toBe(17);
    expect(new Set(keys).size).toBe(17);
  });

  it("puts the env-only four in the read-only Server group", () => {
    const server = GROUPS.find((group) => group.id === "server");
    expect(server?.keys).toEqual([
      "NODUM_DB",
      "NODUM_LLM_BASE_URL",
      "NODUM_EMBED_CACHE",
      "NODUM_PUBLIC_URL",
    ]);
  });
});

describe("liveness", () => {
  it("reports the six per-run ceilings as next-run and never as live", () => {
    const ceilings = [
      "NODUM_LLM_REQUEST_BUDGET",
      "NODUM_LLM_REQUEST_SECONDS",
      "NODUM_LLM_CYCLE_BUDGET",
      "NODUM_LLM_CYCLE_SECONDS",
      "NODUM_LLM_CALL_TIMEOUT",
      "NODUM_LLM_MAX_OUTPUT_TOKENS",
    ];
    for (const key of ceilings) {
      expect(liveness(key)).toBe("next-run");
      expect(livenessLabel(liveness(key))).not.toContain("live");
    }
  });

  it("reports the schedule as within-a-minute", () => {
    expect(liveness("NODUM_CONSOLIDATE_AT")).toBe("minute");
  });

  it("reports the provider keys — including the secret — as live", () => {
    for (const key of ["NODUM_LLM_MODEL", "NODUM_LLM_API_KEY", "NODUM_LLM_THINKING"]) {
      expect(liveness(key)).toBe("now");
    }
  });
});

describe("editBlocker", () => {
  it("blocks an environment-pinned row with the pin reason even though it is writable", () => {
    const pinned = row({ key: "NODUM_LLM_MODEL", provenance: "environment" });
    expect(editBlocker(pinned, {})).toContain("environment");
    expect(isEditable(pinned, {})).toBe(false);
  });

  it("blocks an env-only row with its registry refusal sentence", () => {
    const envOnly = row({
      key: "NODUM_PUBLIC_URL",
      writable: false,
      refusal: "NODUM_PUBLIC_URL is read from the environment only",
    });
    expect(editBlocker(envOnly, {})).toBe("NODUM_PUBLIC_URL is read from the environment only");
  });

  it("blocks the audio pair when the build lacks the extra, and only then", () => {
    const audio = row({ key: "NODUM_AUDIO_MODEL" });
    expect(editBlocker(audio, { audio: false })).toMatch(/not available in this build/i);
    expect(editBlocker(audio, { audio: true })).toBeNull();
    expect(editBlocker(audio, {})).toBeNull();
  });

  it("leaves a stored file-layer row editable", () => {
    expect(isEditable(row({ key: "NODUM_LLM_THINKING" }), { audio: true })).toBe(true);
  });
});

describe("layerLabel", () => {
  it("renders every provenance constant the seam publishes", () => {
    expect(layerLabel("environment")).toBe("environment");
    expect(layerLabel("settings.env")).toBe("settings.env");
    expect(layerLabel("default")).toBe("default");
    expect(layerLabel("unset")).toBe("unset");
    expect(layerLabel("file-unreadable")).toBe("file unreadable");
  });
});

describe("revertTarget", () => {
  it("returns nothing when no settings event ever named the key", () => {
    const stored = row({ key: "NODUM_LLM_MODEL" });
    expect(revertTarget(stored, null, {})).toBeNull();
  });

  it("writes back the event's previous non-secret value", () => {
    const stored = row({ key: "NODUM_LLM_CONTEXT_TOKENS" });
    const target = revertTarget(stored, event(stored.key, "262144"), {});
    expect(target).toEqual({ kind: "write", value: "262144" });
  });

  it("removes again when the key was not stored before the last write", () => {
    const stored = row({ key: "NODUM_LLM_CONTEXT_TOKENS" });
    expect(revertTarget(stored, event(stored.key, null), {})).toEqual({ kind: "remove" });
  });

  it("offers re-entry, never a value, for a secret whose past was set", () => {
    const secret = row({ key: "NODUM_LLM_API_KEY", secret: true, value: null });
    const target = revertTarget(secret, event(secret.key, "set"), {});
    expect(target).toEqual({ kind: "reenter" });
  });

  it("removes a secret whose past was unset", () => {
    const secret = row({ key: "NODUM_LLM_API_KEY", secret: true, value: null });
    expect(revertTarget(secret, event(secret.key, "unset"), {})).toEqual({ kind: "remove" });
  });

  it("refuses to revert a row the page cannot write — a pin among them", () => {
    const pinned = row({ key: "NODUM_LLM_MODEL", provenance: "environment" });
    expect(revertTarget(pinned, event(pinned.key, "old-model"), {})).toBeNull();
    const audio = row({ key: "NODUM_AUDIO_MODEL" });
    expect(revertTarget(audio, event(audio.key, "base"), { audio: false })).toBeNull();
  });
});

describe("adoptPreview", () => {
  it("collects exactly the writable, environment-provenanced, set rows", () => {
    const rows = [
      row({ key: "NODUM_LLM_MODEL", provenance: "environment" }),
      row({ key: "NODUM_LLM_THINKING", provenance: "settings.env" }),
      row({ key: "NODUM_LLM_CONTEXT_TOKENS", provenance: "environment", set: false }),
      row({ key: "NODUM_PUBLIC_URL", provenance: "environment", writable: false }),
    ];
    expect(adoptPreview(rows).candidates).toEqual([
      { key: "NODUM_LLM_MODEL", value: "x" },
    ]);
  });

  it("shows a secret candidate as set without carrying any value", () => {
    const rows = [
      row({
        key: "NODUM_LLM_API_KEY",
        provenance: "environment",
        secret: true,
        value: null,
      }),
    ];
    expect(adoptPreview(rows).candidates).toEqual([{ key: "NODUM_LLM_API_KEY", value: null }]);
  });
});

describe("PROFILE_DEFAULT_NOTE", () => {
  it("names the profile-supplied window without claiming it is always in force", () => {
    expect(PROFILE_DEFAULT_NOTE).toContain("1M");
    expect(PROFILE_DEFAULT_NOTE).toMatch(/when the model matches/);
  });
});
