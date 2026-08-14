import { describe, expect, it } from "vitest";
import { clampPaletteIndex, nextPaletteIndex, paletteItems } from "./commandPalette";

describe("command palette model", () => {
  it("shows previous reads only while idle and keeps their unavailable caveat", () => {
    const idle = paletteItems("", [{ id: "alpha", title: "Alpha" }], []);
    expect(idle[0]).toMatchObject({ kind: "recent", nodeId: "alpha" });
    expect(idle[0]?.detail).toContain("may no longer be available");
    expect(paletteItems("a", [{ id: "alpha", title: "Alpha" }], [])).not.toContainEqual(idle[0]);
  });

  it("clamps keyboard selection at either end", () => {
    expect(nextPaletteIndex(-1, "ArrowDown", 2)).toBe(0);
    expect(nextPaletteIndex(1, "ArrowDown", 2)).toBe(1);
    expect(nextPaletteIndex(0, "ArrowUp", 2)).toBe(0);
  });

  it("lands a stale empty-list selection on the first row once results arrive", () => {
    expect(clampPaletteIndex(-1, 0)).toBe(-1);
    expect(clampPaletteIndex(-1, 2)).toBe(0);
    expect(clampPaletteIndex(4, 2)).toBe(1);
    expect(clampPaletteIndex(0, 2)).toBe(0);
  });

});
