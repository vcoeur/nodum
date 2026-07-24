/**
 * The one place nodum timestamps are turned into `Date`s.
 *
 * Every `created_at` / `updated_at` in the schema defaults to SQLite's
 * `datetime('now')`, which renders **UTC** as `YYYY-MM-DD HH:MM:SS` — a space
 * separator and no zone marker. `new Date("2026-07-24 21:18:33")` reads that as
 * *local* time, so every view that formatted a timestamp with a bare `new Date`
 * was off by the reader's UTC offset. Parsing goes through
 * {@link parseTimestamp} everywhere; nothing else in `web/src` may call
 * `new Date()` on a server string.
 */

/** SQLite's `datetime('now')` rendering: `YYYY-MM-DD HH:MM:SS`, always UTC. */
const SQLITE_UTC = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?$/;

/** A trailing `Z` or `±HH:MM` — the string already says which zone it is in. */
const HAS_ZONE = /(?:Z|[+-]\d{2}:?\d{2})$/;

/**
 * Parse a nodum timestamp into a Date, reading a zone-less string as UTC.
 *
 * @param value The stored timestamp, or null/undefined.
 * @returns The parsed instant, or null when absent or unparseable.
 */
export function parseTimestamp(value: string | null | undefined): Date | null {
  if (!value) return null;
  const trimmed = value.trim();
  const normalised =
    !HAS_ZONE.test(trimmed) && SQLITE_UTC.test(trimmed)
      ? `${trimmed.replace(" ", "T")}Z`
      : trimmed;
  const date = new Date(normalised);
  return Number.isNaN(date.getTime()) ? null : date;
}

/**
 * Milliseconds since the epoch for a nodum timestamp.
 *
 * @param value The stored timestamp.
 * @returns The epoch milliseconds, or null when it does not parse.
 */
export function timestampMs(value: string | null | undefined): number | null {
  const date = parseTimestamp(value);
  return date === null ? null : date.getTime();
}

/**
 * Date and time to the minute, in the reader's locale and zone.
 *
 * @param value The stored timestamp.
 * @returns e.g. `"Jul 24, 2026, 11:12 PM"`, or the raw string when it does not
 *   parse.
 */
export function formatTimestamp(value: string | null | undefined): string {
  const date = parseTimestamp(value);
  if (date === null) return value ?? "—";
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * The same instant to the second — for a column where the minute is not enough.
 *
 * @param value The stored timestamp.
 * @returns A local date-time with seconds, or the raw string when unparseable.
 */
export function formatAbsolute(value: string | null | undefined): string {
  const date = parseTimestamp(value);
  if (date === null) return value ?? "—";
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/**
 * The instant spelled out in full, including the zone — for a `title` tooltip.
 *
 * @param value The stored timestamp.
 * @returns The full local rendering, or the raw string when unparseable.
 */
export function formatTimestampLong(value: string | null | undefined): string {
  const date = parseTimestamp(value);
  return date === null ? (value ?? "—") : date.toString();
}

/**
 * A coarse "how long ago", for scanning a queue by age.
 *
 * @param value The stored timestamp.
 * @param now Epoch milliseconds to measure against; defaults to `Date.now()`.
 * @returns e.g. `"12 min ago"`, falling back to {@link formatAbsolute} past a
 *   month.
 */
export function formatRelative(value: string | null | undefined, now: number = Date.now()): string {
  const ms = timestampMs(value);
  if (ms === null) return "unknown age";
  const seconds = Math.max(0, Math.round((now - ms) / 1000));
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days} d ago`;
  return formatAbsolute(value);
}
