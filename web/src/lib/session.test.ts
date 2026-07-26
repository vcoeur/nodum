/**
 * The 401 broadcast (`session.ts`).
 *
 * The semantics that matter: every subscriber hears every report (the redirect
 * is not optional), an unsubscribed listener never fires, and a throwing
 * listener cannot silence the rest.
 */

import { describe, expect, it, vi } from "vitest";
import { onUnauthorized, reportUnauthorized } from "./session";

describe("the 401 broadcast", () => {
  it("calls every subscriber on each report", () => {
    const first = vi.fn();
    const second = vi.fn();
    const offFirst = onUnauthorized(first);
    const offSecond = onUnauthorized(second);

    reportUnauthorized();
    reportUnauthorized();

    expect(first).toHaveBeenCalledTimes(2);
    expect(second).toHaveBeenCalledTimes(2);
    offFirst();
    offSecond();
  });

  it("stops calling a listener once unsubscribed", () => {
    const listener = vi.fn();
    const off = onUnauthorized(listener);

    reportUnauthorized();
    off();
    reportUnauthorized();

    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("does not let a throwing listener silence the rest", () => {
    const offBad = onUnauthorized(() => {
      throw new Error("a broken listener");
    });
    const after = vi.fn();
    const offAfter = onUnauthorized(after);

    expect(() => reportUnauthorized()).not.toThrow();
    expect(after).toHaveBeenCalledTimes(1);

    offBad();
    offAfter();
  });

  it("takes a report with no subscribers as a no-op", () => {
    expect(() => reportUnauthorized()).not.toThrow();
  });
});
