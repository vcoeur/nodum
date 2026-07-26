// @vitest-environment jsdom
/**
 * The sticky write target (`writeTarget.ts`).
 *
 * The semantics under test are the ones design decision D1a rests on, not the
 * line coverage:
 *
 * - it **defaults to `main`** and survives a reload, which is what makes it
 *   sticky in the first place;
 * - it is **renderable** — every subscriber hears every change, so a surface
 *   showing the target can never fall behind the value nodes are filed under;
 * - it keeps the reference **verbatim**, because a space id and a space name
 *   both resolve server-side and only the server can say which still does;
 * - **two tabs cannot disagree.** A target set in one tab reaches every other
 *   open one, because a tab still holding the old value would file a node into
 *   a space the human had already moved away from — D1a's failure, one level
 *   out from a single screen;
 * - storage being unavailable **degrades**, it does not throw. The write target
 *   sits on the node-create path, and a browser blocking site data must not
 *   take the editor down with it.
 *
 * The module caches the stored value on first read, so each case re-imports it
 * through {@link freshStore} — that is the only way to exercise the read a
 * fresh page load performs.
 */

// jsdom gives this suite `window.localStorage`; the global harness is `node`.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/** Re-import the module with its cache cleared, as a fresh page load would. */
async function freshStore() {
  vi.resetModules();
  return await import("./writeTarget");
}

const KEY = "nodum.write-target";

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the default", () => {
  it("is main when nothing has ever been stored", async () => {
    const store = await freshStore();
    expect(store.getWriteTarget()).toBe("main");
    expect(store.DEFAULT_WRITE_TARGET).toBe("main");
  });

  it("is main when the stored value is blank", async () => {
    window.localStorage.setItem(KEY, "   ");
    const store = await freshStore();
    expect(store.getWriteTarget()).toBe("main");
  });
});

describe("persistence across sessions", () => {
  it("reads back what a previous session stored", async () => {
    window.localStorage.setItem(KEY, "research");
    const store = await freshStore();
    expect(store.getWriteTarget()).toBe("research");
  });

  it("stores what this session sets, so the next one starts there", async () => {
    const store = await freshStore();
    store.setWriteTarget("research");
    expect(window.localStorage.getItem(KEY)).toBe("research");

    const reloaded = await freshStore();
    expect(reloaded.getWriteTarget()).toBe("research");
  });

  it("keeps the reference verbatim — an id is as valid as a name", async () => {
    const store = await freshStore();
    const spaceId = "01J8ZQ4C7K9V0MZ0R2N6S3XA7B";
    store.setWriteTarget(spaceId);
    expect(store.getWriteTarget()).toBe(spaceId);
    expect((await freshStore()).getWriteTarget()).toBe(spaceId);
  });

  it("trims surrounding whitespace rather than storing an unresolvable target", async () => {
    const store = await freshStore();
    store.setWriteTarget("  research  ");
    expect(store.getWriteTarget()).toBe("research");
  });

  it("treats a blank target as a reset to main, never as an empty space", async () => {
    const store = await freshStore();
    store.setWriteTarget("research");
    store.setWriteTarget("   ");
    expect(store.getWriteTarget()).toBe("main");
  });
});

describe("the change broadcast — what makes the target renderable", () => {
  it("tells every subscriber the new value", async () => {
    const store = await freshStore();
    const first = vi.fn();
    const second = vi.fn();
    const offFirst = store.onWriteTargetChange(first);
    const offSecond = store.onWriteTargetChange(second);

    store.setWriteTarget("research");

    expect(first).toHaveBeenCalledWith("research");
    expect(second).toHaveBeenCalledWith("research");
    offFirst();
    offSecond();
  });

  it("stops calling a listener once unsubscribed", async () => {
    const store = await freshStore();
    const listener = vi.fn();
    const off = store.onWriteTargetChange(listener);

    store.setWriteTarget("research");
    off();
    store.setWriteTarget("reference");

    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("says nothing when the target is set to what it already is", async () => {
    const store = await freshStore();
    store.setWriteTarget("research");
    const listener = vi.fn();
    const off = store.onWriteTargetChange(listener);

    store.setWriteTarget("research");

    expect(listener).not.toHaveBeenCalled();
    off();
  });

  it("does not let a throwing subscriber leave the others showing a stale target", async () => {
    const store = await freshStore();
    const offBad = store.onWriteTargetChange(() => {
      throw new Error("a broken subscriber");
    });
    const after = vi.fn();
    const offAfter = store.onWriteTargetChange(after);

    expect(() => store.setWriteTarget("research")).not.toThrow();
    expect(after).toHaveBeenCalledWith("research");

    offBad();
    offAfter();
  });
});

describe("a second tab", () => {
  /**
   * What the browser delivers to *this* document when another one writes.
   *
   * The real event is never dispatched by the tab that made the change, so a
   * test that called `setWriteTarget` would be testing the wrong path.
   */
  function fromAnotherTab(key: string | null, newValue: string | null) {
    window.dispatchEvent(
      new StorageEvent("storage", { key, newValue, storageArea: window.localStorage }),
    );
  }

  it("moves this tab's target when another one changes it", async () => {
    const store = await freshStore();
    expect(store.getWriteTarget()).toBe("main");

    fromAnotherTab(KEY, "research");

    expect(store.getWriteTarget()).toBe("research");
  });

  it("re-renders every subscriber, which is what stops the stale write", async () => {
    const store = await freshStore();
    const listener = vi.fn();
    const off = store.onWriteTargetChange(listener);

    fromAnotherTab(KEY, "research");

    expect(listener).toHaveBeenCalledWith("research");
    off();
  });

  it("falls back to main when the other tab cleared the key", async () => {
    window.localStorage.setItem(KEY, "research");
    const store = await freshStore();
    expect(store.getWriteTarget()).toBe("research");

    fromAnotherTab(KEY, null);

    expect(store.getWriteTarget()).toBe("main");
  });

  it("falls back to main when the other tab cleared all of storage", async () => {
    // `localStorage.clear()` reports a null key: every key went, ours included.
    window.localStorage.setItem(KEY, "research");
    const store = await freshStore();

    fromAnotherTab(null, null);

    expect(store.getWriteTarget()).toBe("main");
  });

  it("ignores another key entirely", async () => {
    const store = await freshStore();
    store.setWriteTarget("research");
    const listener = vi.fn();
    const off = store.onWriteTargetChange(listener);

    fromAnotherTab("some.other.key", "reference");

    expect(store.getWriteTarget()).toBe("research");
    expect(listener).not.toHaveBeenCalled();
    off();
  });

  it("says nothing when the other tab set the value this one already holds", async () => {
    const store = await freshStore();
    store.setWriteTarget("research");
    const listener = vi.fn();
    const off = store.onWriteTargetChange(listener);

    fromAnotherTab(KEY, "research");

    expect(listener).not.toHaveBeenCalled();
    off();
  });

  it("does not write the adopted value back into storage", async () => {
    // The other tab's write is already there. Echoing it would be a redundant
    // write, and on a clear it would resurrect the key that tab just removed.
    const store = await freshStore();
    const written = vi.spyOn(Storage.prototype, "setItem");

    fromAnotherTab(KEY, "research");

    expect(store.getWriteTarget()).toBe("research");
    expect(written).not.toHaveBeenCalled();
  });
});

describe("clearing", () => {
  it("returns to main and leaves nothing behind for the next session", async () => {
    const store = await freshStore();
    store.setWriteTarget("research");

    store.clearWriteTarget();

    expect(store.getWriteTarget()).toBe("main");
    expect(window.localStorage.getItem(KEY)).toBeNull();
    expect((await freshStore()).getWriteTarget()).toBe("main");
  });
});

describe("storage that is unavailable", () => {
  it("falls back to main instead of throwing on read", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("site data is blocked");
    });
    const store = await freshStore();
    expect(store.getWriteTarget()).toBe("main");
  });

  it("still holds the target for this session when it cannot be written", async () => {
    const store = await freshStore();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("site data is blocked");
    });

    expect(() => store.setWriteTarget("research")).not.toThrow();
    expect(store.getWriteTarget()).toBe("research");
  });
});
