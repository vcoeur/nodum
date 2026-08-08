import { describe, expect, it } from "vitest";
import {
  anchorForContextMenu,
  firstMenuIndex,
  lastMenuIndex,
  nextMenuIndex,
  placeMenu,
  MENU_GAP,
  MENU_KEYS,
  MENU_MARGIN,
} from "./contextMenu";

describe("MENU_KEYS", () => {
  it("holds the keys the menu acts on", () => {
    for (const key of ["Escape", "Tab", "Enter", " ", "ArrowUp", "ArrowDown", "Home", "End"]) {
      expect(MENU_KEYS.has(key)).toBe(true);
    }
  });

  it("holds the arrows the menu does not act on", () => {
    // The list behind the panel does act on them: search treats ArrowRight as
    // "open the subgraph", and an unhandled one navigated the reader away from
    // an open menu. Dropping these is a silent regression of that bug.
    expect(MENU_KEYS.has("ArrowLeft")).toBe(true);
    expect(MENU_KEYS.has("ArrowRight")).toBe(true);
  });

  it("holds the keys that scroll, because a menu closes on scroll", () => {
    expect(MENU_KEYS.has("PageUp")).toBe(true);
    expect(MENU_KEYS.has("PageDown")).toBe(true);
  });

  it("holds no app-level shortcut key", () => {
    // React's stopPropagation forwards to the native event and the panel is
    // portalled into document.body, so anything in this set is dead app-wide
    // while a menu is open. `/` and Ctrl-K are search's focus shortcuts.
    expect(MENU_KEYS.has("/")).toBe(false);
    expect(MENU_KEYS.has("k")).toBe(false);
  });
});

const VIEWPORT = { width: 1000, height: 800 };
const SIZE = { width: 200, height: 300 };

describe("placeMenu", () => {
  it("opens down and right of the anchor when there is room", () => {
    const at = placeMenu({ x: 100, y: 200, anchorHeight: 0 }, SIZE, VIEWPORT);
    expect(at).toEqual({ left: 100, top: 200 + MENU_GAP });
  });

  it("shifts left rather than overflowing the right edge", () => {
    const at = placeMenu({ x: 950, y: 100, anchorHeight: 0 }, SIZE, VIEWPORT);
    expect(at.left).toBe(VIEWPORT.width - SIZE.width - MENU_MARGIN);
  });

  it("flips above the anchor when the panel would overflow the bottom", () => {
    const at = placeMenu({ x: 100, y: 700, anchorHeight: 0 }, SIZE, VIEWPORT);
    expect(at.top).toBe(700 - SIZE.height - MENU_GAP);
  });

  it("flips clear of the element it hangs off, not over it", () => {
    // A button anchor passes its bottom edge as `y`; flipping has to clear the
    // whole button, or the panel covers the control that opened it.
    const at = placeMenu({ x: 100, y: 700, anchorHeight: 24 }, SIZE, VIEWPORT);
    expect(at.top).toBe(700 - 24 - SIZE.height - MENU_GAP);
  });

  it("stays below and clamps when flipping would overflow the top", () => {
    // A panel taller than the space above it: cutting off its bottom still
    // shows the first items, cutting off its top shows none.
    const tall = { width: 200, height: 780 };
    const at = placeMenu({ x: 100, y: 400, anchorHeight: 0 }, tall, VIEWPORT);
    expect(at.top).toBe(VIEWPORT.height - tall.height - MENU_MARGIN);
  });

  it("never places the panel closer to an edge than the margin", () => {
    const at = placeMenu({ x: -50, y: -50, anchorHeight: 0 }, SIZE, VIEWPORT);
    expect(at).toEqual({ left: MENU_MARGIN, top: MENU_MARGIN });
  });

  it("clamps a panel wider than the viewport to the left margin", () => {
    const wide = { width: 1200, height: 100 };
    const at = placeMenu({ x: 300, y: 100, anchorHeight: 0 }, wide, VIEWPORT);
    expect(at.left).toBe(MENU_MARGIN);
  });
});

describe("anchorForContextMenu", () => {
  const rect = { left: 40, bottom: 90, height: 24 };

  it("uses the pointer position for a right-click", () => {
    expect(anchorForContextMenu({ clientX: 120, clientY: 240 }, rect)).toEqual({
      x: 120,
      y: 240,
      anchorHeight: 0,
    });
  });

  it("falls back to the target's box for the keyboard menu key", () => {
    // Shift+F10 and the menu key fire `contextmenu` at (0, 0): placed there the
    // panel opens in the corner, nowhere near the row that has focus.
    expect(anchorForContextMenu({ clientX: 0, clientY: 0 }, rect)).toEqual({
      x: 40,
      y: 90,
      anchorHeight: 24,
    });
  });
});

describe("nextMenuIndex", () => {
  const items = [
    { disabled: false },
    { disabled: true },
    { disabled: false },
    { disabled: false },
  ];

  it("opens on the first enabled item when nothing is focused", () => {
    expect(nextMenuIndex(items, -1, 1)).toBe(0);
  });

  it("opens on the last enabled item when moving up from nothing", () => {
    expect(nextMenuIndex(items, -1, -1)).toBe(3);
  });

  it("skips disabled items", () => {
    expect(nextMenuIndex(items, 0, 1)).toBe(2);
    expect(nextMenuIndex(items, 2, -1)).toBe(0);
  });

  it("wraps at both ends", () => {
    expect(nextMenuIndex(items, 3, 1)).toBe(0);
    expect(nextMenuIndex(items, 0, -1)).toBe(3);
  });

  it("returns -1 when every item is disabled", () => {
    // Reached for real: a menu whose only actions are archive and undo, on a
    // node in a structural space, has nothing to focus.
    expect(nextMenuIndex([{ disabled: true }, { disabled: true }], -1, 1)).toBe(-1);
  });

  it("returns -1 for an empty menu", () => {
    expect(nextMenuIndex([], -1, 1)).toBe(-1);
  });

  it("stays put when it is the only enabled item", () => {
    const one = [{ disabled: true }, { disabled: false }];
    expect(nextMenuIndex(one, 1, 1)).toBe(1);
    expect(nextMenuIndex(one, 1, -1)).toBe(1);
  });
});

describe("firstMenuIndex / lastMenuIndex", () => {
  it("bound the enabled range", () => {
    const items = [{ disabled: true }, { disabled: false }, { disabled: false }, { disabled: true }];
    expect(firstMenuIndex(items)).toBe(1);
    expect(lastMenuIndex(items)).toBe(2);
  });

  it("are -1 with nothing enabled", () => {
    expect(firstMenuIndex([{ disabled: true }])).toBe(-1);
    expect(lastMenuIndex([{ disabled: true }])).toBe(-1);
  });
});
