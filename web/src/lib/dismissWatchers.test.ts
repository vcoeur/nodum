// @vitest-environment jsdom
/**
 * Every case below is a confirmed review finding from the nine rounds on
 * `ContextMenu`, and none of them could be a test while the logic lived inside
 * the component: the harness renders no components. Extracting the watchers is
 * what made them reachable.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { attachDismissWatchers } from "./dismissWatchers";
import { focusProgrammatically } from "./programmaticFocus";

let panel: HTMLDivElement;
let outside: HTMLButtonElement;
let inside: HTMLButtonElement;
let detach: (() => void) | null = null;

beforeEach(() => {
  document.body.innerHTML = "";
  panel = document.createElement("div");
  panel.tabIndex = -1;
  inside = document.createElement("button");
  panel.append(inside);
  outside = document.createElement("button");
  document.body.append(panel, outside);
});

afterEach(() => {
  detach?.();
  detach = null;
});

/** Attach with a dismissal spy; `hasFocus` defaults to "the window has focus". */
function watch(hasFocus = () => true) {
  const onDismiss = vi.fn();
  detach = attachDismissWatchers(panel, { onDismiss, hasFocus });
  return onDismiss;
}

describe("attachDismissWatchers", () => {
  it("dismisses when another element takes focus", () => {
    const onDismiss = watch();
    outside.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    expect(onDismiss).toHaveBeenCalledOnce();
  });

  it("does not dismiss when focus moves within the panel", () => {
    const onDismiss = watch();
    inside.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it("dismisses when focus falls back to nothing — the case focusin cannot see", () => {
    // A focused item going `disabled` under a refetch produces exactly this:
    // focusout with a null relatedTarget, and no focusin at all.
    const onDismiss = watch();
    panel.dispatchEvent(new FocusEvent("focusout", { bubbles: true, relatedTarget: null }));
    expect(onDismiss).toHaveBeenCalledOnce();
  });

  it("does not treat a window blur as focus loss", () => {
    // A window losing focus reports the same null relatedTarget. Alt-tabbing
    // away used to dismiss an open menu; the reader's menu should still be
    // there when they come back.
    const onDismiss = watch(() => false);
    panel.dispatchEvent(new FocusEvent("focusout", { bubbles: true, relatedTarget: null }));
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it("leaves a move to a real element to focusin, not focusout", () => {
    const onDismiss = watch();
    panel.dispatchEvent(new FocusEvent("focusout", { bubbles: true, relatedTarget: outside }));
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it("ignores focus the app moved itself", () => {
    // The closing panel hands focus back to its opener. Read as reader focus,
    // that hand-back dismisses the thing being restored into — and when the
    // opener is a peek trigger, it pins a preview card open over the results.
    const onDismiss = watch();
    focusProgrammatically(outside);
    outside.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it("dismisses on a press outside, in the capture phase", () => {
    const onDismiss = watch();
    outside.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    expect(onDismiss).toHaveBeenCalledOnce();
  });

  it("exempts nothing from the outside press — not even the opener", () => {
    // Four attempts at a toggling opener produced eleven defects, all of them
    // turning on whether this press was exempt. It is not.
    const onDismiss = watch();
    panel.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    expect(onDismiss).not.toHaveBeenCalled();
    outside.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    expect(onDismiss).toHaveBeenCalledOnce();
  });

  it("does not dismiss on a press inside the panel", () => {
    const onDismiss = watch();
    inside.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it("dismisses on scroll and on resize", () => {
    const onDismiss = watch();
    window.dispatchEvent(new Event("scroll"));
    window.dispatchEvent(new Event("resize"));
    expect(onDismiss).toHaveBeenCalledTimes(2);
  });

  it("removes every listener on detach", () => {
    const onDismiss = watch();
    detach?.();
    detach = null;
    outside.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    outside.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    panel.dispatchEvent(new FocusEvent("focusout", { bubbles: true, relatedTarget: null }));
    window.dispatchEvent(new Event("scroll"));
    window.dispatchEvent(new Event("resize"));
    expect(onDismiss).not.toHaveBeenCalled();
  });
});
