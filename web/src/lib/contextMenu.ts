/**
 * The context menu's pure model: where the panel goes, and how keys move
 * through it.
 *
 * The menu is a fixed-position portal, so nothing in the layout constrains it
 * and everything about placement has to be decided here rather than by CSS.
 * Two rules, and both exist because a menu that opens off-screen is a menu with
 * no items:
 *
 * - **It prefers down-and-right from the anchor point and never leaves the
 *   viewport.** Horizontally it shifts left; vertically it flips *above* the
 *   anchor, which is why an anchor carries its own height — a menu opened from
 *   a button must flip over the button, not over the pointer position that
 *   happens to be inside it.
 * - **Keyboard movement skips what cannot be chosen.** A disabled item is
 *   rendered (its reason is the whole point of showing it) but never focused,
 *   so arrow keys walk the enabled ones and wrap. With nothing enabled the
 *   index stays -1 and focus stays on the panel, which is a state the archive
 *   menu of a node in a structural space actually reaches.
 *
 * The component in `components/ContextMenu.tsx` is the wiring; everything worth
 * asserting is here.
 */

/**
 * The keys an open menu takes for itself, and the exact boundary of what it
 * takes.
 *
 * The panel is portalled into `document.body`, and React's `stopPropagation`
 * forwards to the native event — so whatever the menu stops is stopped for the
 * whole app, not merely for the list the panel is rendered inside. Both edges
 * of this set were bugs:
 *
 * - **Too narrow** and a list's roving-focus handler runs beside the menu's,
 *   because a portal bubbles through the React tree rather than the DOM one.
 *   That is why the arrows the menu does *not* act on are in here too: the
 *   search results treat ArrowRight as "open the subgraph", and an unhandled
 *   one navigated the reader away from an open menu.
 * - **Too wide** — everything — and every `document`/`window` shortcut in the
 *   app goes dead while a menu is up.
 *
 * `Escape`, `Tab` and the arrows stay in the set deliberately: the menu owns
 * them, and handing `Escape` back would let one keypress close both the menu
 * and the dialog behind it. `PageUp`/`PageDown`/`Home`/`End`/`" "` are here
 * because they scroll, and a menu closes on scroll — a page moving under an
 * open panel is the panel disappearing.
 */
export const MENU_KEYS: ReadonlySet<string> = new Set([
  "Escape",
  "Tab",
  "Enter",
  " ",
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "Home",
  "End",
  "PageUp",
  "PageDown",
]);

/** The minimum distance the panel keeps from a viewport edge, in px. */
export const MENU_MARGIN = 8;

/** The gap between an anchor and the panel, in px. */
export const MENU_GAP = 4;

/** Where a menu was asked to appear, in viewport coordinates. */
export interface MenuAnchor {
  /** The panel's preferred left edge. */
  x: number;
  /** The panel's preferred top edge — the anchor's *bottom* for a button. */
  y: number;
  /**
   * The height of the element the anchor came from, or 0 for a pointer.
   *
   * Only used when the panel has to flip upwards: it is what puts the flipped
   * panel above the button rather than on top of it.
   */
  anchorHeight: number;
}

/** A box the panel has to fit inside. */
export interface MenuViewport {
  width: number;
  height: number;
}

/** The panel's measured size. */
export interface MenuSize {
  width: number;
  height: number;
}

/** Where the panel is painted, in viewport coordinates. */
export interface MenuPlacement {
  left: number;
  top: number;
}

/**
 * Place the panel near its anchor without letting it leave the viewport.
 *
 * The preferred position is down-and-right of the anchor. When the panel would
 * overflow the bottom it flips above the anchor; when the flipped position
 * would overflow the *top* instead, it stays below and is clamped, because a
 * panel whose bottom is cut off still shows its first items while one whose top
 * is cut off shows none of them.
 *
 * @param anchor Where the menu was asked to appear.
 * @param size The panel's measured size.
 * @param viewport The viewport's size.
 * @returns The panel's `left`/`top`, both at least {@link MENU_MARGIN}.
 */
export function placeMenu(
  anchor: MenuAnchor,
  size: MenuSize,
  viewport: MenuViewport,
): MenuPlacement {
  let left = anchor.x;
  if (left + size.width > viewport.width - MENU_MARGIN) {
    left = viewport.width - size.width - MENU_MARGIN;
  }
  left = Math.max(MENU_MARGIN, left);

  let top = anchor.y + MENU_GAP;
  if (top + size.height > viewport.height - MENU_MARGIN) {
    const flipped = anchor.y - anchor.anchorHeight - size.height - MENU_GAP;
    top = flipped >= MENU_MARGIN ? flipped : viewport.height - size.height - MENU_MARGIN;
  }
  top = Math.max(MENU_MARGIN, top);

  return { left: Math.round(left), top: Math.round(top) };
}

/**
 * The anchor for a `contextmenu` event.
 *
 * A right-click carries the pointer position. The **keyboard** context-menu key
 * (and Shift+F10) fires the same event with `clientX`/`clientY` at 0 in every
 * engine that supports it, so a menu placed at the raw coordinates would open
 * in the top-left corner, nowhere near the row the user was on. Falling back to
 * the target's own box is what makes the keyboard path land on the row.
 *
 * @param event The event's pointer coordinates.
 * @param targetRect The event target's bounding box, for the keyboard fallback.
 * @returns The anchor to place the panel against.
 */
export function anchorForContextMenu(
  event: { clientX: number; clientY: number },
  targetRect: { left: number; bottom: number; height: number },
): MenuAnchor {
  if (event.clientX === 0 && event.clientY === 0) {
    return { x: targetRect.left, y: targetRect.bottom, anchorHeight: targetRect.height };
  }
  return { x: event.clientX, y: event.clientY, anchorHeight: 0 };
}

/** What {@link nextMenuIndex} needs to know about an item. */
export interface MenuItemState {
  disabled: boolean;
}

/**
 * The next focusable item in a direction, wrapping, skipping disabled ones.
 *
 * @param items The items in render order.
 * @param current The focused index, or -1 when focus is on the panel itself.
 * @param delta 1 for Down, -1 for Up.
 * @returns The next enabled index, or -1 when no item can be focused.
 */
export function nextMenuIndex(
  items: readonly MenuItemState[],
  current: number,
  delta: 1 | -1,
): number {
  const count = items.length;
  if (count === 0) return -1;
  // From "nothing focused", Down opens on the first item and Up on the last,
  // which is what both make the panel's own focus a real starting point.
  const start = current === -1 ? (delta === 1 ? 0 : count - 1) : current + delta;
  for (let step = 0; step < count; step += 1) {
    const index = (((start + step * delta) % count) + count) % count;
    if (items[index]?.disabled === false) return index;
  }
  return -1;
}

/**
 * The first focusable item, or -1 when none is.
 *
 * @param items The items in render order.
 */
export function firstMenuIndex(items: readonly MenuItemState[]): number {
  return nextMenuIndex(items, -1, 1);
}

/**
 * The last focusable item, or -1 when none is.
 *
 * @param items The items in render order.
 */
export function lastMenuIndex(items: readonly MenuItemState[]): number {
  return nextMenuIndex(items, -1, -1);
}
