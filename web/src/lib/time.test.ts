/**
 * The regression guard for the zone-less-timestamp bug.
 *
 * Every `created_at` / `updated_at` nodum stores is SQLite's `datetime('now')`:
 * `YYYY-MM-DD HH:MM:SS`, UTC, **with no zone marker**. `new Date(s)` reads that
 * as local time, so every view that formatted one with a bare `new Date` was
 * off by the reader's UTC offset.
 *
 * The reason this file pins a timezone rather than trusting the ambient one:
 * **in UTC the bug and the fix produce the same instant**, so a test run on a
 * UTC machine — which is every CI runner — cannot tell them apart. `TZ` is set
 * to `Asia/Kathmandu` in `vitest.config.ts`, and the first test asserts that
 * the pin actually took effect, so this file fails loudly if the harness ever
 * stops applying it instead of quietly becoming a tautology.
 */

import { describe, expect, it } from "vitest";
import {
  formatAbsolute,
  formatRelative,
  formatTimestamp,
  formatTimestampLong,
  parseTimestamp,
  timestampMs,
} from "./time";

/** A real timestamp captured from `GET /api/nodes/{id}` during the Phase-3 integration pass. */
const SERVER_STRING = "2026-07-24 21:49:13";

/** The instant that string names, spelled so the assertion cannot inherit a zone. */
const SERVER_INSTANT = Date.UTC(2026, 6, 24, 21, 49, 13);

/** Kathmandu is UTC+05:45 year round; `getTimezoneOffset` reports it west-positive. */
const KATHMANDU_OFFSET_MINUTES = -345;

describe("the test harness itself", () => {
  it("runs in a non-UTC zone, or nothing below can detect the bug", () => {
    // Not `not.toBe(0)`: an unexpected zone would still pass that while
    // changing what every other expectation here means.
    expect(new Date(SERVER_INSTANT).getTimezoneOffset()).toBe(KATHMANDU_OFFSET_MINUTES);
  });
});

describe("parseTimestamp", () => {
  it("reads a zone-less SQLite timestamp as UTC", () => {
    expect(parseTimestamp(SERVER_STRING)?.getTime()).toBe(SERVER_INSTANT);
  });

  it("disagrees with a bare `new Date` by exactly the local offset", () => {
    // This is the regression: replace parseTimestamp's normalisation with a
    // bare `new Date(value)` and the two sides become equal, which this
    // rejects. In UTC they are equal either way — hence the pinned zone.
    const naive = new Date(SERVER_STRING).getTime();
    const parsed = parseTimestamp(SERVER_STRING)!.getTime();
    expect(naive).not.toBe(parsed);
    expect(naive - parsed).toBe(KATHMANDU_OFFSET_MINUTES * 60_000);
  });

  it("puts the instant on the local calendar day, not the stored one", () => {
    // 21:49 UTC on the 24th is 03:34 on the 25th in Kathmandu. A view that
    // renders the stored date verbatim is wrong here, and so is a naive parse.
    const parsed = parseTimestamp(SERVER_STRING)!;
    expect(parsed.getDate()).toBe(25);
    expect(parsed.getUTCDate()).toBe(24);
  });

  it("accepts the ISO `T` separator as the same zone-less form", () => {
    expect(parseTimestamp("2026-07-24T21:49:13")?.getTime()).toBe(SERVER_INSTANT);
  });

  it("keeps fractional seconds", () => {
    expect(parseTimestamp("2026-07-24 21:49:13.25")?.getTime()).toBe(SERVER_INSTANT + 250);
  });

  it("tolerates surrounding whitespace", () => {
    expect(parseTimestamp("  2026-07-24 21:49:13  ")?.getTime()).toBe(SERVER_INSTANT);
  });

  it("leaves a string that already names its zone alone", () => {
    // The normalisation must not fire twice: a `Z` string is already UTC, and
    // an offset string means what it says.
    expect(parseTimestamp("2026-07-24T21:49:13Z")?.getTime()).toBe(SERVER_INSTANT);
    expect(parseTimestamp("2026-07-24T23:49:13+02:00")?.getTime()).toBe(SERVER_INSTANT);
    expect(parseTimestamp("2026-07-24T23:49:13+0200")?.getTime()).toBe(SERVER_INSTANT);
    expect(parseTimestamp("2026-07-24T16:49:13-05:00")?.getTime()).toBe(SERVER_INSTANT);
  });

  it("returns null for absent or unparseable input rather than an Invalid Date", () => {
    expect(parseTimestamp(null)).toBeNull();
    expect(parseTimestamp(undefined)).toBeNull();
    expect(parseTimestamp("")).toBeNull();
    expect(parseTimestamp("not a timestamp")).toBeNull();
  });
});

describe("timestampMs", () => {
  it("is parseTimestamp in epoch milliseconds", () => {
    expect(timestampMs(SERVER_STRING)).toBe(SERVER_INSTANT);
  });

  it("is null when the value does not parse, never NaN", () => {
    // Callers do arithmetic on this (`grouping.ts` clusters on it); a NaN would
    // propagate silently through every comparison.
    expect(timestampMs("nonsense")).toBeNull();
    expect(timestampMs(null)).toBeNull();
  });
});

describe("the formatters", () => {
  it("format the UTC-normalised instant, not the naive one", () => {
    const reference = new Date(SERVER_INSTANT);
    // Compared against an explicitly-built instant rather than a literal
    // string, so the assertion does not depend on the runner's locale.
    expect(formatTimestamp(SERVER_STRING)).toBe(
      reference.toLocaleString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }),
    );
    expect(formatAbsolute(SERVER_STRING)).toBe(
      reference.toLocaleString(undefined, {
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }),
    );
    expect(formatTimestampLong(SERVER_STRING)).toBe(reference.toString());
  });

  it("differ from what a bare `new Date` would render", () => {
    expect(formatAbsolute(SERVER_STRING)).not.toBe(
      new Date(SERVER_STRING).toLocaleString(undefined, {
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }),
    );
  });

  it("echo an unparseable value instead of printing `Invalid Date`", () => {
    expect(formatTimestamp("whenever")).toBe("whenever");
    expect(formatAbsolute("whenever")).toBe("whenever");
    expect(formatTimestampLong("whenever")).toBe("whenever");
  });

  it("print an em dash for a missing value", () => {
    expect(formatTimestamp(null)).toBe("—");
    expect(formatAbsolute(undefined)).toBe("—");
    expect(formatTimestampLong(null)).toBe("—");
  });
});

describe("formatRelative", () => {
  /** `now` is measured against the same UTC instant the string names. */
  const at = (msAfter: number) => formatRelative(SERVER_STRING, SERVER_INSTANT + msAfter);

  it("measures against the normalised instant, so an age is not offset by the zone", () => {
    // The whole point: with a naive parse this reads "5 h ago" in Kathmandu
    // for something that just happened.
    expect(at(0)).toBe("just now");
  });

  it("walks the units", () => {
    expect(at(44_000)).toBe("just now");
    expect(at(45_000)).toBe("1 min ago");
    expect(at(12 * 60_000)).toBe("12 min ago");
    expect(at(90 * 60_000)).toBe("2 h ago");
    expect(at(5 * 3600_000)).toBe("5 h ago");
    expect(at(50 * 3600_000)).toBe("2 d ago");
    expect(at(20 * 24 * 3600_000)).toBe("20 d ago");
  });

  it("falls back to an absolute date past a month", () => {
    const old = at(60 * 24 * 3600_000);
    expect(old).toBe(formatAbsolute(SERVER_STRING));
    expect(old).not.toMatch(/ago/);
  });

  it("clamps a future timestamp to `just now` rather than counting backwards", () => {
    expect(at(-5 * 3600_000)).toBe("just now");
  });

  it("says so when there is no timestamp to age", () => {
    expect(formatRelative(null)).toBe("unknown age");
    expect(formatRelative("nonsense")).toBe("unknown age");
  });
});
