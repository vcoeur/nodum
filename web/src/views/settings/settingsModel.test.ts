import { describe, expect, it } from "vitest";
import type { EndpointOut, EventOut, SettingOut } from "../../api/types";
import {
  adoptPreview,
  editBlocker,
  endpointConfiguration,
  endpointKeyUse,
  endpointTitle,
  GROUPS,
  groupsFor,
  isEditable,
  isModelChange,
  layerLabel,
  liveness,
  livenessLabel,
  modelChangeConfirm,
  nextPopoverKey,
  PROFILE_DEFAULT_NOTE,
  revertTarget,
  settingPopup,
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
    summary: "A summary.",
    help: null,
    takes_effect: "now",
    // Free-form by default: a closed set is the exception, and a row that
    // claimed one would render a select in every test that did not ask for it.
    choices: null,
    ...overrides,
  };
}

/** An endpoint factory: the fields the select and its key row read. */
function endpoint(overrides: Partial<EndpointOut> & { label: string }): EndpointOut {
  return {
    title: overrides.label,
    base_url: "https://example.invalid/v1",
    key: null,
    window_note: null,
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
  it("covers this build's twenty-one static names exactly once across the eight groups", () => {
    // The per-endpoint credential rows are not among them: they are built
    // per report by `groupsFor`, because how many there are is a
    // deployment's decision rather than this build's.
    const keys = GROUPS.flatMap((group) => group.keys);
    expect(keys.length).toBe(21);
    expect(new Set(keys).size).toBe(21);
  });

  it("puts the env-only menu and server paths in the read-only Server group", () => {
    const server = GROUPS.find((group) => group.id === "server");
    expect(server?.keys).toEqual([
      "NODUM_DB",
      // The menu the endpoint select may choose from. It sits here rather than
      // beside the select because it is the deployment's bound on that select,
      // not a thing the page can change.
      "NODUM_LLM_ENDPOINTS",
      "NODUM_EMBED_CACHE",
      "NODUM_PUBLIC_URL",
    ]);
  });

  it("puts each endpoint's key under the select that arms it", () => {
    const groups = groupsFor([
      endpoint({ label: "local", title: "Local (ollama)", key: null }),
      endpoint({ label: "deepseek", title: "DeepSeek", key: "NODUM_LLM_KEY_DEEPSEEK" }),
      endpoint({ label: "kimi", title: "Kimi (Moonshot)", key: "NODUM_LLM_KEY_KIMI" }),
    ]);
    expect(groups.find((group) => group.id === "endpoint")?.keys).toEqual([
      "NODUM_LLM_ENDPOINT",
      // `local` contributes none: it authenticates with nothing, so there is no
      // setting behind it and a field for it would be a field that saves nowhere.
      "NODUM_LLM_KEY_DEEPSEEK",
      "NODUM_LLM_KEY_KIMI",
    ]);
  });

  it("renders no credential rows for a deployment that offers none", () => {
    expect(groupsFor([]).find((group) => group.id === "endpoint")?.keys).toEqual([
      "NODUM_LLM_ENDPOINT",
    ]);
  });

  it("shows an endpoint's title for its label and falls back for every other set", () => {
    const offered = [endpoint({ label: "deepseek", title: "DeepSeek", key: null })];
    expect(endpointTitle(offered, "deepseek")).toBe("DeepSeek");
    // The reasoning levels are their own display text, so a miss is a fallback
    // rather than an error — one option renderer, no per-key branch.
    expect(endpointTitle(offered, "high")).toBeNull();
  });

  it("groups the two embedding names under Embeddings", () => {
    const embed = GROUPS.find((group) => group.id === "embeddings");
    expect(embed?.keys).toEqual(["NODUM_EMBED_MODEL", "NODUM_EMBED_DOWNLOAD"]);
  });

  it("moves the generic key beside the custom base URL, not Model", () => {
    expect(GROUPS.find((group) => group.id === "model")?.keys).not.toContain(
      "NODUM_LLM_API_KEY",
    );
    expect(GROUPS.find((group) => group.id === "custom-endpoint")?.keys).toEqual([
      "NODUM_LLM_BASE_URL",
      "NODUM_LLM_API_KEY",
    ]);
  });

  it("hides the generic key for a selected endpoint but leaves every endpoint key manageable", () => {
    const endpoints = [
      endpoint({ label: "deepseek", key: "NODUM_LLM_KEY_DEEPSEEK" }),
      endpoint({ label: "kimi", key: "NODUM_LLM_KEY_KIMI" }),
    ];
    const rows = [
      row({ key: "NODUM_LLM_ENDPOINT", value: "deepseek" }),
      row({ key: "NODUM_LLM_BASE_URL", provenance: "default" }),
    ];
    const groups = groupsFor(endpoints, rows);
    expect(groups.find((group) => group.id === "endpoint")?.keys).toEqual([
      "NODUM_LLM_ENDPOINT",
      "NODUM_LLM_KEY_DEEPSEEK",
      "NODUM_LLM_KEY_KIMI",
    ]);
    expect(groups.find((group) => group.id === "custom-endpoint")?.keys).toEqual([
      "NODUM_LLM_BASE_URL",
    ]);
    expect(endpointKeyUse("NODUM_LLM_KEY_DEEPSEEK", endpointConfiguration(endpoints, rows))).toContain(
      "next agent run",
    );
    expect(endpointKeyUse("NODUM_LLM_KEY_KIMI", endpointConfiguration(endpoints, rows))).toContain(
      "future agent run",
    );
  });

  it("shows the generic key and marks endpoint rows overridden for an environment custom URL", () => {
    const endpoints = [endpoint({ label: "deepseek", key: "NODUM_LLM_KEY_DEEPSEEK" })];
    const rows = [
      row({ key: "NODUM_LLM_ENDPOINT", value: "deepseek" }),
      row({ key: "NODUM_LLM_BASE_URL", provenance: "environment", writable: false }),
    ];
    const configuration = endpointConfiguration(endpoints, rows);
    expect(configuration.baseUrlOverrides).toBe(true);
    expect(groupsFor(endpoints, rows).find((group) => group.id === "custom-endpoint")?.keys).toEqual([
      "NODUM_LLM_BASE_URL",
      "NODUM_LLM_API_KEY",
    ]);
    expect(endpointKeyUse("NODUM_LLM_KEY_DEEPSEEK", configuration)).toContain("overrides");
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
      expect(liveness(row({ key, takes_effect: "next-run" }))).toBe("next-run");
      expect(livenessLabel(liveness(row({ key, takes_effect: "next-run" })))).not.toContain("live");
    }
  });

  it("reports the schedule as within-a-minute", () => {
    expect(liveness(row({ key: "NODUM_CONSOLIDATE_AT", takes_effect: "minute" }))).toBe("minute");
  });

  it("reports provider-resolution inputs — including secrets — for the next agent run", () => {
    for (const key of [
      "NODUM_LLM_MODEL",
      "NODUM_LLM_ENDPOINT",
      "NODUM_LLM_BASE_URL",
      "NODUM_LLM_API_KEY",
      "NODUM_LLM_KEY_DEEPSEEK",
      "NODUM_LLM_CONTEXT_TOKENS",
      "NODUM_LLM_THINKING",
    ]) {
      expect(liveness(row({ key, takes_effect: "next-run" }))).toBe("next-run");
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

describe("the embedding-model confirm", () => {
  it("names the exact consequence with the chunk count the change would blind", () => {
    const lines = modelChangeConfirm(4);
    expect(lines[0]).toContain("4 chunks");
    expect(lines[0]).toContain("invisible to search");
    expect(lines[0]).toContain("rebuild re-embeds them");
    expect(modelChangeConfirm(1)[0]).toContain("1 chunk");
  });

  it("says plainly that a change with no stored chunks blinds nothing yet", () => {
    expect(modelChangeConfirm(0)[0]).toContain("No chunks are stored yet");
    expect(modelChangeConfirm(0)[0]).not.toContain("invisible");
  });

  it("does not promise the rebuild happens by itself", () => {
    const joined = modelChangeConfirm(3).join(" ");
    expect(joined).toContain("re-embeds nothing by itself");
    expect(joined).toContain("offers the vector rebuild");
  });

  it("is keyed to exactly the model row", () => {
    expect(isModelChange("NODUM_EMBED_MODEL")).toBe(true);
    expect(isModelChange("NODUM_EMBED_DOWNLOAD")).toBe(false);
    expect(isModelChange("NODUM_LLM_MODEL")).toBe(false);
  });
});

describe("settingPopup", () => {
  it("carries the registry's summary and help through unchanged", () => {
    const popup = settingPopup(
      row({ key: "NODUM_LLM_MODEL", summary: "The model name.", help: "No default." }),
      {},
    );
    expect(popup.summary).toBe("The model name.");
    expect(popup.help).toBe("No default.");
  });

  it("shows no help paragraph when the registry has none", () => {
    const popup = settingPopup(row({ key: "NODUM_LLM_THINKING", help: null }), {});
    expect(popup.help).toBeNull();
  });

  it("shows the built-in default, and a dash for a secret", () => {
    expect(
      settingPopup(row({ key: "NODUM_LLM_CYCLE_BUDGET", default: "0" }), {}).defaultLabel,
    ).toBe("0");
    expect(
      settingPopup(row({ key: "NODUM_LLM_API_KEY", secret: true, default: null }), {}).defaultLabel,
    ).toBe("—");
    expect(settingPopup(row({ key: "NODUM_LLM_MODEL", default: null }), {}).defaultLabel).toBe(
      "none",
    );
  });

  it("names the liveness class for an editable row", () => {
    // A per-run ceiling: the class the page's own save flow reports.
    expect(
      settingPopup(row({ key: "NODUM_LLM_REQUEST_BUDGET", takes_effect: "next-run" }), {})
        .livenessLabel,
    ).toContain("next agent run");
  });

  it("states no liveness for a row the page cannot change", () => {
    // An env-only row is read at process start, so "Applied live" would be a
    // false claim; a pinned row's change is a host-side step, not this page's.
    const envOnly = settingPopup(
      row({ key: "NODUM_PUBLIC_URL", writable: false, refusal: "env-only" }),
      {},
    );
    expect(envOnly.livenessLabel).toBeNull();
    const pinned = settingPopup(
      row({ key: "NODUM_LLM_MODEL", provenance: "environment" }),
      {},
    );
    expect(pinned.livenessLabel).toBeNull();
  });

  it("states no liveness for a row this build cannot serve", () => {
    const audio = settingPopup(row({ key: "NODUM_AUDIO_MODEL" }), { audio: false });
    expect(audio.livenessLabel).toBeNull();
  });
});

describe("nextPopoverKey", () => {
  it("opens the requested row's popover from nothing", () => {
    expect(nextPopoverKey(null, "NODUM_LLM_MODEL")).toBe("NODUM_LLM_MODEL");
  });

  it("replaces the open popover with the newly requested one", () => {
    expect(nextPopoverKey("NODUM_LLM_MODEL", "NODUM_EMBED_MODEL")).toBe("NODUM_EMBED_MODEL");
  });

  it("never toggles: requesting the open key keeps it open", () => {
    // The press on the opener dismissed the old popover before this click
    // arrived, so a toggling decision here would depend on an event ordering
    // that is not stable — the MenuButton lesson. Open always.
    expect(nextPopoverKey("NODUM_LLM_MODEL", "NODUM_LLM_MODEL")).toBe("NODUM_LLM_MODEL");
  });
});
