import { describe, expect, it } from "vitest";
import { focusProgrammatically, isProgrammaticFocus } from "./programmaticFocus";

/** Enough of an HTMLElement to be focused, plus what it observed while focusing. */
function target(onFocus?: () => void) {
  const element = {
    focused: 0,
    /** What `isProgrammaticFocus()` answered *during* the focus dispatch. */
    sawProgrammatic: null as boolean | null,
    focus(_options?: FocusOptions) {
      element.focused += 1;
      element.sawProgrammatic = isProgrammaticFocus();
      onFocus?.();
    },
  };
  return element as unknown as HTMLElement & typeof element;
}

/** Let the queued microtasks run. */
const settle = () => Promise.resolve();

describe("focusProgrammatically", () => {
  it("focuses the element and passes the options through", () => {
    let seen: FocusOptions | undefined;
    const element = target();
    element.focus = (options?: FocusOptions) => {
      seen = options;
    };
    focusProgrammatically(element, { preventScroll: true });
    expect(seen).toEqual({ preventScroll: true });
  });

  it("is flagged during the synchronous focus dispatch", () => {
    // `focus()` dispatches `focusin` synchronously, so a watcher running inside
    // it is exactly the reader this flag is for.
    const element = target();
    focusProgrammatically(element);
    expect(element.sawProgrammatic).toBe(true);
  });

  it("clears after a microtask, so a later focus is not suppressed", async () => {
    focusProgrammatically(target());
    expect(isProgrammaticFocus()).toBe(true);
    await settle();
    expect(isProgrammaticFocus()).toBe(false);
  });

  it("stays flagged across the microtask a watcher re-checks focus in", async () => {
    // NodePeek's title resolution re-checks focus one microtask later; the
    // window has to cover it or the suppression misses the case it exists for.
    let duringMicrotask: boolean | null = null;
    focusProgrammatically(
      target(() => {
        queueMicrotask(() => {
          duringMicrotask = isProgrammaticFocus();
        });
      }),
    );
    await settle();
    expect(duringMicrotask).toBe(true);
  });

  it("counts, so an inner hand-back does not release an outer one", () => {
    // Reached for real: a closing menu restores focus to a trigger whose own
    // card dismisses and restores in turn.
    const inner = target();
    const outer = target(() => focusProgrammatically(inner));
    focusProgrammatically(outer);
    expect(inner.sawProgrammatic).toBe(true);
    // Still claimed after the inner call returns, before any microtask runs.
    expect(isProgrammaticFocus()).toBe(true);
  });

  it("releases the flag even when focus() throws", async () => {
    // A detached node, a hostile custom element: a raised flag that never
    // falls would suppress every focus for the life of the page.
    const throwing = target(() => {
      throw new Error("cannot focus");
    });
    expect(() => focusProgrammatically(throwing)).toThrow("cannot focus");
    await settle();
    expect(isProgrammaticFocus()).toBe(false);
  });

  it("answers false when nothing is in flight", () => {
    expect(isProgrammaticFocus()).toBe(false);
  });
});
