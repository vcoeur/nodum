/**
 * Text formatting local to the review view.
 *
 * Timestamp parsing and formatting moved to `src/lib/time.ts` — every view
 * needs the same UTC normalisation and only one of them may own it. What is
 * left here is review-shaped presentation: id abbreviation, pluralisation, and
 * the props rendering the proposed-version diff compares against.
 */

/**
 * Shorten an id for a dense list, keeping the full value for the `title`.
 *
 * Ids are uuid-shaped for nodes and edges and plain integers for versions, so
 * anything already short is returned untouched.
 */
export function shortId(id: string, keep = 8): string {
  return id.length <= keep + 2 ? id : `${id.slice(0, keep)}…`;
}

/** Truncate a single-line preview, appending an ellipsis when it bit. */
export function truncate(text: string, limit: number): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length <= limit ? flat : `${flat.slice(0, limit)}…`;
}

/** Pluralise a count: `plural(1, "proposal")` → `"1 proposal"`. */
export function plural(count: number, noun: string, pluralNoun?: string): string {
  return `${count} ${count === 1 ? noun : (pluralNoun ?? `${noun}s`)}`;
}

/**
 * Render a confidence value, or say it is absent.
 *
 * Confidence on the write path is *self-reported by the writing agent*, so it
 * is never rendered without that caveat nearby — see `_auto_accept_rule`.
 */
export function formatConfidence(confidence: number | null | undefined): string {
  if (confidence === null || confidence === undefined) return "none reported";
  return confidence.toFixed(2);
}

/**
 * Sort object keys recursively so two props objects render comparably.
 *
 * The service diffs versions over a key-sorted rendering (`_render_version`);
 * matching that here keeps a locally computed diff from reporting a change that
 * is only key order.
 */
function sortKeysDeep(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeysDeep);
  if (value !== null && typeof value === "object") {
    const sorted: Record<string, unknown> = {};
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      sorted[key] = sortKeysDeep((value as Record<string, unknown>)[key]);
    }
    return sorted;
  }
  return value;
}

/** Stable, readable JSON for a props object; `{}` renders as an em dash. */
export function formatProps(props: unknown): string {
  if (props === null || props === undefined) return "—";
  if (typeof props === "object" && Object.keys(props as object).length === 0) return "—";
  try {
    return JSON.stringify(sortKeysDeep(props), null, 2);
  } catch {
    return String(props);
  }
}

/** Stable single-line JSON, for equality checks between two props objects. */
export function canonicalJson(value: unknown): string {
  try {
    return JSON.stringify(sortKeysDeep(value));
  } catch {
    return String(value);
  }
}
