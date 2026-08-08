/**
 * The dismissal rules for a transient overlay that owns focus.
 *
 * Extracted from `components/ContextMenu.tsx` because none of it is React: it
 * is five DOM listeners and the conditions under which each one means "the
 * reader is done with this panel". Nine review rounds landed on those
 * conditions and the unit harness could not reach a single one of them, because
 * the logic lived inside a component the harness never renders. Here it takes
 * the element as an argument and a jsdom suite can drive every branch.
 *
 * {@link isProgrammaticFocus} is the other half: a DOM focus event does not say
 * who caused it, and every rule below is about *reader* focus.
 */

import { isProgrammaticFocus } from "./programmaticFocus";

/** What {@link attachDismissWatchers} needs from its host. */
export interface DismissOptions {
  /** Called once per dismissal, for any of the reasons below. */
  onDismiss(): void;
  /**
   * Whether the document currently has focus.
   *
   * Injected so a suite can stage a window blur, which is otherwise
   * unreachable: jsdom's `document.hasFocus()` cannot be moved from a test.
   * Defaults to the real reading.
   */
  hasFocus?(): boolean;
}

/**
 * Watch for every gesture that should dismiss `panel`, and return the detach.
 *
 * The five listeners, and why each one is not redundant:
 *
 * - **`focusin` on `document`** — fires when some *other element* takes focus,
 *   which is what any shortcut the panel does not own does (search's `/` and
 *   Ctrl-K put the caret in the query box). Leaving the panel painted over the
 *   page with no keyboard route out was the bug.
 * - **`focusout` on the panel** — covers the one case `focusin` cannot see, and
 *   only that one: focus falling back to `<body>`, which is what a focused item
 *   going `disabled` under a refetch produces. It acts on a null
 *   `relatedTarget` alone. It cannot be the whole answer because a null
 *   `relatedTarget` is *also* what a window losing focus reports, so
 *   alt-tabbing dismissed open menus until `hasFocus()` told the two apart.
 * - **`pointerdown` on `document`, capture phase** — a press on another
 *   surface's trigger, or on the `⋯` that opened this one, must close this
 *   panel before that surface opens its own. **Nothing is exempt**: exempting
 *   the opener let focus come to rest on it with the panel still open and
 *   outside the portal, where none of its keys reach.
 * - **`scroll` (capture) and `resize` on `window`** — the panel is anchored to a
 *   point in the viewport, and either one moves the content out from under it.
 *
 * Both focus watchers bail on {@link isProgrammaticFocus}, because focus the app
 * moved is not the reader saying anything.
 *
 * @param panel The overlay element; must already be in the document.
 * @param options The dismissal callback, and an optional focus probe.
 * @returns A function that removes every listener.
 */
export function attachDismissWatchers(panel: HTMLElement, options: DismissOptions): () => void {
  const { onDismiss } = options;
  const hasFocus = options.hasFocus ?? (() => document.hasFocus());

  const onFocusIn = (event: FocusEvent) => {
    if (isProgrammaticFocus()) return;
    const target = event.target as Node | null;
    if (target !== null && panel.contains(target)) return;
    onDismiss();
  };

  const onFocusOut = (event: FocusEvent) => {
    if (isProgrammaticFocus()) return;
    // Only the fell-to-nothing case; a move to a real element is `focusin`'s.
    if (event.relatedTarget !== null) return;
    // And only while this document still has focus — otherwise the reader has
    // alt-tabbed away, and their menu should be here when they come back.
    if (!hasFocus()) return;
    onDismiss();
  };

  const onPointerDown = (event: Event) => {
    if (panel.contains(event.target as Node)) return;
    onDismiss();
  };

  const onScrollOrResize = () => onDismiss();

  document.addEventListener("focusin", onFocusIn);
  panel.addEventListener("focusout", onFocusOut);
  document.addEventListener("pointerdown", onPointerDown, true);
  window.addEventListener("scroll", onScrollOrResize, true);
  window.addEventListener("resize", onScrollOrResize);

  return () => {
    document.removeEventListener("focusin", onFocusIn);
    panel.removeEventListener("focusout", onFocusOut);
    document.removeEventListener("pointerdown", onPointerDown, true);
    window.removeEventListener("scroll", onScrollOrResize, true);
    window.removeEventListener("resize", onScrollOrResize);
  };
}
