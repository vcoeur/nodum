/**
 * The Settings page's pure model.
 *
 * Everything the page decides about a row is derived here rather than in the
 * component, because the unit harness renders no components: grouping,
 * which rows are editable and why not, what a save will report about *when*
 * the change takes effect, what one-click revert would write from an event's
 * `before` payload, and what the adopt-from-environment preview shows.
 *
 * Two rules this module encodes come straight from the API contract:
 *
 * - **Editability is derived, never stored** (`writable` + `provenance` +
 *   capabilities): an environment-pinned row is disabled with its reason, and
 *   the server's 409 backs it if a stale client bypasses the check anyway.
 * - **Liveness honesty**: each save says what actually happens — applied
 *   immediately (the provider re-resolves), at the next agent run (the six
 *   per-run ceilings are read once per run), or within a minute (the
 *   scheduler re-reads its schedule each sleep slice). Lowering a budget
 *   never stops a cycle already spending; that is what the kill switch on
 *   the journal is for, and the copy says so instead of implying it.
 */

import type { EventOut, SettingOut } from "../../api/types";

/** One named group of setting rows, in display order. */
export interface SettingGroup {
  id: string;
  title: string;
  keys: readonly string[];
}

export const MODEL_KEYS = [
  "NODUM_LLM_MODEL",
  "NODUM_LLM_API_KEY",
  "NODUM_LLM_CONTEXT_TOKENS",
  "NODUM_LLM_THINKING",
] as const;

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

/** The env-only four: shown so the operator sees the whole ladder, never editable. */
const SERVER_KEYS = [
  "NODUM_DB",
  "NODUM_LLM_BASE_URL",
  "NODUM_EMBED_CACHE",
  "NODUM_PUBLIC_URL",
] as const;

/** Every group, in the order the page renders them. */
export const GROUPS: readonly SettingGroup[] = [
  { id: "model", title: "Model", keys: MODEL_KEYS },
  { id: "gardener", title: "Gardener", keys: GARDENER_KEYS },
  { id: "requests", title: "Requests", keys: REQUEST_KEYS },
  { id: "audio", title: "Audio", keys: AUDIO_KEYS },
  { id: "embeddings", title: "Embeddings", keys: EMBED_KEYS },
  { id: "server", title: "Server", keys: SERVER_KEYS },
];

/** When a change to a key takes effect — the liveness classes the save flow reports. */
export type Liveness = "now" | "next-run" | "minute";

/** The six per-run ceilings: read once per agent run, so a change waits for the next one. */
const RUN_CEILING_KEYS: ReadonlySet<string> = new Set([
  ...REQUEST_KEYS,
  "NODUM_LLM_CYCLE_BUDGET",
  "NODUM_LLM_CYCLE_SECONDS",
]);

const SCHEDULE_KEY = "NODUM_CONSOLIDATE_AT";

/** What a saved change reports about when it bites. */
export function liveness(key: string): Liveness {
  if (key === SCHEDULE_KEY) return "minute";
  if (RUN_CEILING_KEYS.has(key)) return "next-run";
  return "now";
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
 * The note under the download-gate row: what flipping it on costs.
 *
 * Every line is a fact the system delivers: the gate is the one way the
 * never-download-implicitly posture is lifted, and the next vector operation
 * after flipping it may fetch the model — ~0.2 GB for the default.
 */
export const EMBED_DOWNLOAD_NOTE =
  "On: the next vector operation may download the model (~0.2 GB) — nodum never downloads implicitly, so this is the one gate that allows it.";

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
