/**
 * Telling *the app moved focus* apart from *the reader moved focus*.
 *
 * A DOM focus event says nothing about who caused it, and several surfaces here
 * hand focus back deliberately — a dismissed peek returns it to its trigger, a
 * closed menu returns it to whatever held it. Every one of those looks exactly
 * like a user focus to anything watching, and watchers act on user focus: the
 * peek card arms on focus with no intent delay, the context menu treats focus
 * landing outside its panel as a dismissal. So a hand-back re-armed the card it
 * had just dismissed, and a menu closing re-armed a peek on the row underneath.
 *
 * Both components had grown the same private flag to suppress their own
 * re-arm. This is that flag, shared, so a hand-back by *one* of them is legible
 * to the *other* — which is the case neither private version could cover, and
 * the one that put an unrequested preview card over the search results.
 *
 * A **counter**, not a boolean: two hand-backs can overlap (a menu closing
 * restores focus to a trigger whose own card is dismissing), and the inner
 * one's release must not clear the outer one's claim.
 *
 * The window is one microtask. That is not arbitrary: `focus()` dispatches
 * `focusin` synchronously, and the listeners that re-check focus before acting
 * do so in a microtask (`NodePeek`'s title resolution). Anything later is a
 * genuine user focus and must not be suppressed.
 */

/** How many hand-backs are in flight. */
let inFlight = 0;

/**
 * Move focus, marked as the app's doing rather than the reader's.
 *
 * @param element The element to focus.
 * @param options Passed to `focus()` — `preventScroll` above all.
 */
export function focusProgrammatically(element: HTMLElement, options?: FocusOptions): void {
  inFlight += 1;
  try {
    element.focus(options);
  } finally {
    // In a microtask even if `focus()` threw: leaving the flag raised would
    // suppress every later focus for the life of the page.
    queueMicrotask(() => {
      inFlight -= 1;
    });
  }
}

/**
 * Whether the focus being handled right now was moved by the app.
 *
 * Call it at the top of any focus/focusin handler that would otherwise treat
 * the move as something the reader asked for.
 */
export function isProgrammaticFocus(): boolean {
  return inFlight > 0;
}
