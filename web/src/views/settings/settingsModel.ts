/**
 * The Settings page's pure model.
 *
 * Everything the page decides about a row is derived here rather than in the
 * component, because the unit harness renders no components: grouping,
 * which rows are editable and why not, what a save will report about *when*
 * the change takes effect, what one-click revert would write from an event's
 * `before` payload, what the adopt-from-environment preview shows, and what
 * the per-setting info popup says about a row.
 *
 * Two rules this module encodes come straight from the API contract:
 *
 * - **Editability is derived, never stored** (`writable` + `provenance` +
 *   capabilities): an environment-pinned row is disabled with its reason, and
 *   the server's 409 backs it if a stale client bypasses the check anyway.
 * - **Liveness honesty**: each save says what actually happens, read from
 *   the registry-owned `takes_effect` the server publishes with the row —
 *   applied live, at the next agent run (every provider-resolution setting
 *   is read once per run), or within a minute (the scheduler re-reads its
 *   schedule each sleep slice). Lowering a budget never stops a cycle
 *   already spending; that is what the kill switch on the journal is for,
 *   and the copy says so instead of implying it.
 */

import type { EndpointOut, EventOut, SettingOut } from "../../api/types";

/** One named group of setting rows, in display order. */
export interface SettingGroup {
  id: string;
  title: string;
  keys: readonly string[];
}

/** The endpoint select itself. Its per-endpoint key rows are added per report. */
export const ENDPOINT_KEY = "NODUM_LLM_ENDPOINT";

export const MODEL_KEYS = [
  "NODUM_LLM_MODEL",
  "NODUM_LLM_CONTEXT_TOKENS",
  "NODUM_LLM_THINKING",
] as const;

/** The operator-owned custom endpoint and the credential that belongs only to it. */
const CUSTOM_ENDPOINT_KEYS = ["NODUM_LLM_BASE_URL", "NODUM_LLM_API_KEY"] as const;

const GARDENER_KEYS = [
  "NODUM_LLM_CYCLE_BUDGET",
  "NODUM_LLM_CYCLE_SECONDS",
  "NODUM_CONSOLIDATE_AT",
] as const;

const REQUEST_KEYS = [
  "NODUM_LLM_REQUEST_BUDGET",
  "NODUM_LLM_REQUEST_SECONDS",
  "NODUM_LLM_CALL_TIMEOUT",
  "NODUM_LLM_MAX_OUTPUT_TOKENS",
] as const;

const AUDIO_KEYS = ["NODUM_AUDIO_MODEL", "NODUM_AUDIO_DOWNLOAD"] as const;

const EMBED_KEYS = ["NODUM_EMBED_MODEL", "NODUM_EMBED_DOWNLOAD"] as const;

/** The env-only five: shown so the operator sees the whole ladder, never editable. */
const SERVER_KEYS = [
  "NODUM_DB",
  "NODUM_LLM_ENDPOINTS",
  "NODUM_EMBED_CACHE",
  "NODUM_PUBLIC_URL",
] as const;

/**
 * Every group, in the order the page renders them.
 *
 * The Endpoint group's key rows are **not** listed here because they are not
 * fixed: the server generates one credential row per endpoint it offers, and
 * which endpoints those are is a deployment's decision. `groupsFor` builds that
 * group from the report; this constant is what the rest of the page is grouped
 * by and what the unit tests read.
 */
export const GROUPS: readonly SettingGroup[] = [
  { id: "endpoint", title: "Endpoint", keys: [ENDPOINT_KEY] },
  { id: "custom-endpoint", title: "Custom endpoint", keys: CUSTOM_ENDPOINT_KEYS },
  { id: "model", title: "Model", keys: MODEL_KEYS },
  { id: "gardener", title: "Gardener", keys: GARDENER_KEYS },
  { id: "requests", title: "Requests", keys: REQUEST_KEYS },
  { id: "audio", title: "Audio", keys: AUDIO_KEYS },
  { id: "embeddings", title: "Embeddings", keys: EMBED_KEYS },
  { id: "server", title: "Server", keys: SERVER_KEYS },
];

/**
 * The groups to render for one report: {@link GROUPS} with the endpoint group's
 * credential rows filled in from the endpoints this deployment offers.
 *
 * Each endpoint's key sits directly under the select that arms it, because the
 * two are one decision — choosing an endpoint decides both where a call goes
 * *and* which credential travels with it, and a page that put the keys
 * somewhere else would be describing a shared key that no longer exists.
 *
 * An endpoint that authenticates with nothing (the local default) contributes
 * no row: `key` is null and there is no setting behind it.
 */
export interface EndpointConfiguration {
  /** A non-empty environment base URL wins over the selected shipped endpoint. */
  baseUrlOverrides: boolean;
  /** The key row belonging to the selected endpoint, or null when none is selected. */
  selectedEndpointKey: string | null;
  /** The shared key is relevant only without a selected endpoint, or under an explicit override. */
  showGenericKey: boolean;
}

/** Derive endpoint applicability from server-owned rows, never from a client environment read. */
export function endpointConfiguration(
  endpoints: readonly EndpointOut[],
  rows: readonly SettingOut[],
): EndpointConfiguration {
  const byKey = new Map(rows.map((row) => [row.key, row]));
  const selectedLabel = byKey.get(ENDPOINT_KEY)?.value ?? null;
  const selectedEndpoint = endpoints.find((endpoint) => endpoint.label === selectedLabel) ?? null;
  const baseUrlOverrides = byKey.get("NODUM_LLM_BASE_URL")?.provenance === "environment";
  return {
    baseUrlOverrides,
    selectedEndpointKey: selectedEndpoint?.key ?? null,
    showGenericKey: baseUrlOverrides || selectedEndpoint === null,
  };
}

/** The visible state sentence for one endpoint-owned credential row. */
export function endpointKeyUse(key: string, configuration: EndpointConfiguration): string {
  if (configuration.baseUrlOverrides) {
    return "Stored for this endpoint; the custom base URL currently overrides the selection";
  }
  return configuration.selectedEndpointKey === key
    ? "Used by the selected endpoint on the next agent run"
    : "Used when this endpoint is selected for a future agent run";
}

export function groupsFor(
  endpoints: readonly EndpointOut[],
  rows: readonly SettingOut[] = [],
): readonly SettingGroup[] {
  const credentials = endpoints
    .map((endpoint) => endpoint.key)
    .filter((key): key is string => key !== null);
  const configuration = endpointConfiguration(endpoints, rows);
  return GROUPS.map((group) => {
    if (group.id === "endpoint") return { ...group, keys: [ENDPOINT_KEY, ...credentials] };
    if (group.id === "custom-endpoint") {
      return {
        ...group,
        keys: configuration.showGenericKey ? CUSTOM_ENDPOINT_KEYS : ["NODUM_LLM_BASE_URL"],
      };
    }
    return group;
  });
}

/**
 * The display title for one choice, or null when the choice is not an endpoint.
 *
 * The endpoint select stores a label (`deepseek`) and shows a title
 * (`DeepSeek`); every other closed set — the reasoning levels — is its own
 * display text, so a miss here falls back to the raw value rather than being an
 * error. Both cases go through one function so the option renderer has no
 * per-key branch.
 */
export function endpointTitle(
  endpoints: readonly EndpointOut[],
  choice: string,
): string | null {
  return endpoints.find((one) => one.label === choice)?.title ?? null;
}

/** When a change to a key takes effect — the liveness classes the save flow reports. */
export type Liveness = SettingOut["takes_effect"];

/** What a saved change reports about when it bites. */
export function liveness(row: SettingOut): Liveness {
  return row.takes_effect;
}

/** The sentence a saved row shows for its liveness class. */
export function livenessLabel(live: Liveness): string {
  switch (live) {
    case "now":
      return "Applied live";
    case "next-run":
      return "Applies at the next agent run";
    case "minute":
      return "Picked up by the scheduler within a minute";
  }
}

/** What the per-setting info popup shows for one row. */
export interface SettingPopup {
  /** The registry's one-line description of the setting. */
  summary: string;
  /**
   * The registry's longer explanation, or null when the summary says it all.
   * Rendered verbatim — the server owns every sentence here.
   */
  help: string | null;
  /** The built-in default as the row renders it; secrets show a dash. */
  defaultLabel: string;
  /**
   * When a change to this key takes effect, or null when the page cannot
   * change it. An env-only or environment-pinned row has no subject for that
   * sentence — and for the env-only names "Applied live" would be a false
   * claim, because they are read at process start rather than re-resolved.
   */
  livenessLabel: string | null;
}

/**
 * The popup's content for one row — assembled from the row and the
 * editability rules above, never a client-authored sentence about a key.
 */
export function settingPopup(
  row: SettingOut,
  capabilities: Record<string, boolean>,
): SettingPopup {
  return {
    summary: row.summary,
    help: row.help,
    defaultLabel: row.secret ? "—" : (row.default ?? "none"),
    livenessLabel: isEditable(row, capabilities)
      ? livenessLabel(liveness(row))
      : null,
  };
}

/** Which row's info popover is open, or null. */
export type PopoverKey = string | null;

/**
 * The one-popover-at-a-time rule for the per-setting info buttons.
 *
 * A popover opens unconditionally and never toggles: a press on the opener of
 * the popover that is already open dismisses it through the ordinary watchers
 * *before* the click arrives, so deciding at the click whether to open or
 * close would depend on whether the popover survived until the click — the
 * same unstable ordering `MenuButton`'s history condemned. Opening always,
 * whatever the current state (`_current` is deliberately ignored), has no
 * ordering to get wrong. Closing is not a transition of this function: the
 * view drops the state to null through its own dismissal handlers.
 */
export function nextPopoverKey(_current: PopoverKey, requested: string): string {
  return requested;
}

/**
 * Why a row cannot be edited, or null when it can.
 *
 * Derived, never stored: `writable` covers the env-only names, a
 * `provenance` of `environment` means the host owns the value (a browser
 * write would be inert — the server answers it with a 409), and the audio
 * pair needs the `audio` extra installed to mean anything.
 */
export function editBlocker(
  row: SettingOut,
  capabilities: Record<string, boolean>,
): string | null {
  if (!row.writable) return row.refusal ?? "Environment-only";
  if (row.provenance === "environment") {
    return "Pinned by the environment — unset the variable on the host first";
  }
  if (row.key.startsWith("NODUM_AUDIO_") && capabilities.audio === false) {
    return "Not available in this build — install the 'audio' extra";
  }
  return null;
}

/** Whether the page may offer a write on this row. */
export function isEditable(
  row: SettingOut,
  capabilities: Record<string, boolean>,
): boolean {
  return editBlocker(row, capabilities) === null;
}

/**
 * A layer badge's text for a row's provenance.
 *
 * An adopted key still reads `environment` — adopt stores the same value in
 * the file without touching the variable — so the badge alone does not say
 * whether the file carries it; the page pairs the badge with the row's
 * `stored` flag where that matters.
 */
export function layerLabel(provenance: string): string {
  switch (provenance) {
    case "environment":
      return "environment";
    case "settings.env":
      return "settings.env";
    case "default":
      return "default";
    case "unset":
      return "unset";
    case "file-unreadable":
      return "file unreadable";
    default:
      return provenance;
  }
}

/** The note under CONTEXT_TOKENS' default: a shipped profile may serve more. */
export const PROFILE_DEFAULT_NOTE =
  "A recognised provider profile serves its own larger window (DeepSeek's hosted endpoint: 1M) when the model matches.";

/** The embedding model row, and the write that blinds the vector store. */
export const EMBED_MODEL_KEY = "NODUM_EMBED_MODEL";

/** Whether a key is the embedding model — the write that needs the confirm. */
export function isModelChange(key: string): boolean {
  return key === EMBED_MODEL_KEY;
}

/**
 * The confirmation before an embedding-model write — the copy that names the
 * coupling.
 *
 * `chunks` is the store's chunk count at confirm time
 * (`SettingsOut.embed_chunks`), which is exactly what a model change blinds:
 * the store holds nothing under the new model id until the rebuild runs, so
 * the number the confirm states is the number the mixed-model note will state
 * after the write. Zero chunks gets its own honest sentence rather than a
 * zero-count version of a consequence that has not happened yet.
 */
export function modelChangeConfirm(chunks: number): readonly string[] {
  const blinded =
    chunks === 0
      ? "No chunks are stored yet, so a model change blinds nothing until the first projection."
      : `${chunks} chunk${chunks === 1 ? "" : "s"} become${chunks === 1 ? "s" : ""} invisible to search until the rebuild re-embeds them.`;
  return [
    blinded,
    "Changing the model re-embeds nothing by itself — this page then offers the vector rebuild, and search stays blind until it runs.",
  ];
}

/** What reverting one key from its last settings event would do. */
export type RevertTarget =
  | { kind: "write"; value: string }
  | { kind: "remove" }
  | { kind: "reenter" };

/**
 * Derive the revert action for one row from its most recent settings event.
 *
 * The event payload's `before` is what the file held before the last write
 * through nodum — with any secret already reduced to the words `"set"` /
 * `"unset"`, so a secret's previous *value* is unrecoverable by construction:
 * reverting one means re-entering it, and the page says so rather than
 * offering a button that cannot do what it promises.
 *
 * @param row The row as it stands now.
 * @param event The newest `settings.set`/`settings.unset` event naming this
 *   key, or null when the log holds none (nothing written through nodum).
 */
export function revertTarget(
  row: SettingOut,
  event: EventOut | null,
  capabilities: Record<string, boolean>,
): RevertTarget | null {
  if (event === null) return null;
  if (!isEditable(row, capabilities)) return null;
  const before = event.payload.before;
  if (before !== null && typeof before !== "string") return null;
  if (row.secret) {
    // "set" names a value the log deliberately does not carry; "unset" (and
    // a null) mean the key was not stored before, so reverting removes it.
    return before === "set" ? { kind: "reenter" } : { kind: "remove" };
  }
  return before === null || before === "" ? { kind: "remove" } : { kind: "write", value: before };
}

/**
 * The adopt preview derived from the current rows: every editable name the
 * environment pins with a non-empty value — exactly what
 * `POST /api/settings/adopt-env` will try to store.
 *
 * Secrets show as `set`; their values cross neither the API nor this page.
 * A candidate whose value the registry refuses is adopted-as-skipped rather
 * than blocked: the preview states that the outcome, not the preview, is
 * authoritative.
 */
export function adoptPreview(rows: readonly SettingOut[]): {
  candidates: { key: string; value: string | null }[];
} {
  return {
    candidates: rows
      .filter((row) => row.writable && row.provenance === "environment" && row.set)
      .map((row) => ({ key: row.key, value: row.secret ? null : row.value })),
  };
}

/**
 * The export: two downloads, and copy that states what the system actually
 * delivers.
 *
 * Redacted and with-keys are different downloads, not a toggle — one carries
 * the real API key and demands the account password (the server re-verifies
 * it through the login path), the other never touches a secret. Both save
 * through the browser, so the landing spot is the browser's default download
 * location, and the copy says so rather than implying the page controls it.
 */
export const EXPORT_FILENAME = "nodum-settings.env";

export const WITH_KEYS_EXPORT_CONFIRM: readonly string[] = [
  "The file contains your real API key in plain text — treat it like a password.",
  "You will be asked for your account password before the download starts; a wrong password downloads nothing.",
  "The export renders the values in force now, including ones pinned by the environment.",
  `The file saves as ${EXPORT_FILENAME} in the browser's default download location.`,
];
