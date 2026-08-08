/**
 * The shared contextual-action menu — one primitive, every surface's item list.
 *
 * Until this existed, every action in the app lived on exactly one screen: to
 * archive a node you opened the node, to re-root the graph you opened the
 * graph. This is the counterweight — actions where the content is — and it is
 * deliberately one component rather than a menu per view, because a second
 * implementation is how two surfaces end up disagreeing about what a
 * right-click does.
 *
 * **Two openings, one menu.** `onContextMenu` is the pointer path; the `⋯`
 * button {@link MenuButton} renders is the twin, and it is not optional
 * decoration — a right-click is unavailable on touch and undiscoverable to
 * anyone who has not tried it, so a surface that offers the menu offers both.
 * Both funnel through {@link useContextMenu}, so the item list is written once.
 *
 * **Nothing here confirms a destructive action.** A menu item marked `danger`
 * opens a dialog; it never performs the write itself. That is the same rule the
 * review queue's modal follows, and it is why the menu can close on Enter
 * without any risk of a keypress moving live state.
 *
 * The placement and keyboard-movement rules are pure and live in
 * `lib/contextMenu.ts` with their tests; this file is the wiring.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import { createPortal } from "react-dom";
import {
  anchorForContextMenu,
  firstMenuIndex,
  lastMenuIndex,
  nextMenuIndex,
  placeMenu,
  MENU_KEYS,
} from "../lib/contextMenu";
import type { MenuAnchor } from "../lib/contextMenu";
import { focusProgrammatically, isProgrammaticFocus } from "../lib/programmaticFocus";
import "./ContextMenu.css";

/**
 * The subset of {@link MENU_KEYS} whose default action scrolls the page.
 *
 * Prevented rather than merely stopped, because the panel closes on scroll:
 * a key that scrolls is a key that dismisses the menu.
 *
 * **Space is deliberately not in here**, though it scrolls too. On a focused
 * `<button role="menuitem">` its default action is the activation the ARIA
 * menu pattern requires, and preventing it made every item unreachable by
 * Space. It is handled on its own below: prevented only while focus is on the
 * panel itself, where there is no item for it to activate.
 */
const SCROLLING_KEYS: ReadonlySet<string> = new Set([
  "PageUp",
  "PageDown",
  "Home",
  "End",
  "ArrowUp",
  "ArrowDown",
]);

/** One action in a menu. */
export interface MenuAction {
  /** Stable key, unique within the menu. */
  id: string;
  /** What the item reads. */
  label: string;
  /**
   * A group name; a divider is drawn wherever it changes.
   *
   * Used to keep travel actions apart from the ones that write.
   */
  group?: string;
  /** Rendered in the danger hue — for anything that retires live state. */
  danger?: boolean;
  /**
   * Why this action is not available here.
   *
   * Present means disabled, and the reason is rendered under the label rather
   * than hidden in a `title`: an action a surface offers and refuses without
   * saying why is worse than one it does not offer at all.
   */
  unavailable?: string;
  /** Performs the action, or opens the dialog that will. */
  onSelect(): void;
}

/**
 * Open, close, and place a menu.
 *
 * The anchor is null while the menu is closed, which is also what a surface
 * renders on: `menu.anchor ? <ContextMenu …/> : null`.
 */
export interface ContextMenuController {
  /** Where the menu is open, or null while it is closed. */
  anchor: MenuAnchor | null;
  /** Open at the pointer — pass a `contextmenu` event straight in. */
  openAt(event: ReactMouseEvent): void;
  /** Open under an element — the `⋯` button's path. */
  openFrom(element: HTMLElement | null): void;
  /** Close it. */
  close(): void;
}

/**
 * The open/close half of a contextual menu.
 *
 * Every opening replaces whatever was open: a second right-click, or a press
 * on the `⋯`, dismisses the current panel through the ordinary watchers and
 * opens a new one at the new position. No dismissal is exempt for any element,
 * which is what keeps the panel's behaviour independent of the order a
 * browser happens to dispatch pointerdown, mousedown, focus and click in —
 * see {@link MenuButton} for the four attempts that established the need.
 *
 * @returns The controller a surface hands to {@link ContextMenu}.
 */
export function useContextMenu(): ContextMenuController {
  const [anchor, setAnchor] = useState<MenuAnchor | null>(null);

  const openAt = useCallback((event: ReactMouseEvent) => {
    // The browser's own menu would cover this one, and the surfaces that offer
    // this menu have nothing the native one can do for them.
    event.preventDefault();
    event.stopPropagation();
    const rect = event.currentTarget.getBoundingClientRect();
    setAnchor(anchorForContextMenu(event, rect));
  }, []);

  const openFrom = useCallback((element: HTMLElement | null) => {
    if (element === null) return;
    const rect = element.getBoundingClientRect();
    setAnchor({ x: rect.left, y: rect.bottom, anchorHeight: rect.height });
  }, []);

  const close = useCallback(() => setAnchor(null), []);

  return { anchor, openAt, openFrom, close };
}

interface ContextMenuProps {
  /** The menu's accessible name — what it is a menu *for*. */
  label: string;
  /** Where it opens; from the controller. */
  anchor: MenuAnchor;
  /** The actions, in render order. */
  items: readonly MenuAction[];
  /** Called for Escape, a click outside, a scroll, and after a selection. */
  onClose(): void;
}

/**
 * A contextual menu, portalled to `document.body` and placed near its anchor.
 *
 * @param label The menu's accessible name.
 * @param anchor Where it opens.
 * @param items The actions.
 * @param onClose Dismissal handler.
 */
export function ContextMenu({ label, anchor, items, onClose }: ContextMenuProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const restoreTo = useRef<HTMLElement | null>(null);
  const [focused, setFocused] = useState(-1);

  const states = items.map((item) => ({ disabled: item.unavailable !== undefined }));

  // Placed before the browser paints, so the panel never flashes at (0, 0)
  // before landing next to the anchor. Focus is taken here and handed back on
  // close — the same rule `Modal` follows, and for the same reason: a keyboard
  // reader who dismisses this must not be left on `<body>` with the roving
  // focus of the list behind it broken.
  useLayoutEffect(() => {
    const panel = panelRef.current;
    if (panel === null) return;
    restoreTo.current = document.activeElement as HTMLElement | null;
    const rect = panel.getBoundingClientRect();
    const at = placeMenu(
      anchor,
      { width: rect.width, height: rect.height },
      { width: window.innerWidth, height: window.innerHeight },
    );
    panel.style.left = `${at.left}px`;
    panel.style.top = `${at.top}px`;
    panel.focus();
    return () => {
      // Restore **only if this panel still holds focus**. Anything else means
      // focus was deliberately moved somewhere else while the menu was up — a
      // shortcut outside `MENU_KEYS` reaching the app, say, since search's `/`
      // and Ctrl-K put the caret in the query box — and grabbing it back from
      // there is worse than never having taken it.
      if (!panel.contains(document.activeElement)) return;
      // And only if the opener is still in the document: focusing a detached
      // node drops focus onto `<body>`, which is the thing this prevents.
      const opener = restoreTo.current;
      // `preventScroll`, because this menu closes *on* scroll: without it a
      // wheel gesture yanks the viewport back to the row the reader was
      // scrolling away from, and on the outside-click path the scroll can move
      // the page between pointerdown and pointerup so the click that closed
      // the menu never lands anywhere. The keys that would scroll while the
      // panel has focus are prevented in the key handler, so the case this
      // leaves — a focused opener now off-screen — is only ever reached by a
      // pointer gesture, where no focus ring is drawn anyway.
      if (opener !== null && opener !== document.body && opener.isConnected) {
        // Marked as the app's move, not the reader's. Two watchers would
        // otherwise act on it as a user focus: this component's own focusin
        // close, and — the case a private flag could not have covered —
        // `NodePeek`, when the opener is a peek trigger, which is every search
        // result title. That pinned a preview card open over the results.
        focusProgrammatically(opener, { preventScroll: true });
      }
    };
  }, [anchor]);

  // Focus leaving the panel dismisses the menu, and it takes **two** watchers
  // to say that, because neither DOM event covers it alone.
  //
  // `focusin` on `document` is the one that fires when something else takes
  // focus — which is what any shortcut outside `MENU_KEYS` does (search's `/`
  // and Ctrl-K put the caret in the query box), and leaving the panel painted
  // over the page with no keyboard route out was the bug. But `focusin` fires
  // only when an *element* takes focus, so it is silent for the case where
  // focus falls back to `<body>` — a focused menu item that becomes `disabled`
  // under a refetch does exactly that, and the assets lightbox documents the
  // same browser behaviour.
  //
  // The panel's own `focusout` covers that gap, and only that gap: it acts on
  // a null `relatedTarget` alone, which is the fall-back-to-nothing case. The
  // reason it cannot be the whole answer is that a null `relatedTarget` is
  // *also* what a window losing focus reports, so alt-tabbing away dismissed
  // open menus — `document.hasFocus()` is what tells those two apart.
  //
  // **No element is exempt from either watcher**, the `⋯` included. Exempting
  // the opener let focus come to rest on it with the panel still open and
  // outside the portal, where none of its keys reach; every scheme for
  // reconciling that with a toggling button depended on an event ordering that
  // is not stable across how the menu was opened or which engine is running.
  // The button opens rather than toggles, so there is nothing left to
  // reconcile — see {@link MenuButton}.
  useEffect(() => {
    const leftPanel = (target: Node | null): boolean =>
      target === null || panelRef.current?.contains(target) !== true;

    const onFocusIn = (event: FocusEvent) => {
      if (isProgrammaticFocus()) return;
      const target = event.target as Node | null;
      if (target !== null && !leftPanel(target)) return;
      onClose();
    };
    const onFocusOut = (event: FocusEvent) => {
      if (isProgrammaticFocus()) return;
      // Only the fell-to-nothing case; a move to a real element is `focusin`'s.
      if (event.relatedTarget !== null) return;
      // And only while this document still has focus — otherwise this is the
      // reader alt-tabbing away, and their menu should be here when they come
      // back.
      if (!document.hasFocus()) return;
      onClose();
    };
    document.addEventListener("focusin", onFocusIn);
    const panel = panelRef.current;
    panel?.addEventListener("focusout", onFocusOut);
    return () => {
      document.removeEventListener("focusin", onFocusIn);
      panel?.removeEventListener("focusout", onFocusOut);
    };
  }, [onClose]);

  // A menu is anchored to a point in the viewport, and a scroll moves the
  // content out from under it — so a scroll closes it rather than letting it
  // hover over an unrelated row. The pointerdown listener is on `document` in
  // the capture phase: a press on another surface's trigger — or on the `⋯`
  // that opened this one — must close this menu before that surface opens its
  // own. Nothing is exempt.
  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (panelRef.current?.contains(target)) return;
      onClose();
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("scroll", onClose, true);
    window.addEventListener("resize", onClose);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("scroll", onClose, true);
      window.removeEventListener("resize", onClose);
    };
  }, [onClose]);

  const moveTo = (index: number) => {
    if (index === -1) return;
    setFocused(index);
    itemRefs.current[index]?.focus();
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    // The menu owns `MENU_KEYS` and nothing else; both edges of that set were
    // bugs, and `lib/contextMenu.ts` carries the reasoning beside the set.
    // Here: a key it owns is stopped, and a key that would scroll the page is
    // also prevented — this panel closes *on* scroll, so letting Space or
    // PageDown through would be letting the key dismiss the menu out from
    // under the reader.
    if (MENU_KEYS.has(event.key)) {
      event.stopPropagation();
      if (SCROLLING_KEYS.has(event.key)) event.preventDefault();
      // Space on the panel itself would scroll the page out from under the
      // menu — which closes it. Space on an item is that item's activation,
      // and preventing it is how the whole menu became unreachable by Space.
      if (event.key === " " && event.target === panelRef.current) event.preventDefault();
    }
    switch (event.key) {
      case "Escape":
        onClose();
        return;
      case "ArrowDown":
        event.preventDefault();
        moveTo(nextMenuIndex(states, focused, 1));
        return;
      case "ArrowUp":
        event.preventDefault();
        moveTo(nextMenuIndex(states, focused, -1));
        return;
      case "Home":
        event.preventDefault();
        moveTo(firstMenuIndex(states));
        return;
      case "End":
        event.preventDefault();
        moveTo(lastMenuIndex(states));
        return;
      case "Tab":
        // Tabbing out of a transient menu closes it: there is nothing beyond
        // its last item, and leaving it open behind the next focus would put a
        // floating panel over content the user has moved on to.
        onClose();
        return;
      default:
    }
  };

  const select = (item: MenuAction) => {
    if (item.unavailable !== undefined) return;
    // Closed before the action runs: an item that navigates would otherwise
    // leave the panel painted over the destination for a frame.
    onClose();
    item.onSelect();
  };

  return createPortal(
    <div
      ref={panelRef}
      className="nd-menu"
      role="menu"
      aria-label={label}
      aria-orientation="vertical"
      tabIndex={-1}
      onKeyDown={onKeyDown}
      // A right-click inside the panel stops here. The portal is a DOM child
      // of `<body>` but a **React** child of whatever rendered it — and on a
      // search row that is the `<li>` carrying `onContextMenu={openAt}`, so
      // right-clicking an item re-anchored this very menu under the cursor,
      // moving the action out from under the pointer that was aiming at it.
      // The native menu is suppressed for the same reason the host suppresses
      // it: there is nothing it can do for a reader here.
      onContextMenu={(event) => {
        event.preventDefault();
        event.stopPropagation();
      }}
    >
      {items.map((item, index) => {
        const disabled = item.unavailable !== undefined;
        const divider = index > 0 && items[index - 1]?.group !== item.group;
        return (
          <button
            key={item.id}
            ref={(element) => {
              itemRefs.current[index] = element;
            }}
            type="button"
            role="menuitem"
            className={menuItemClass(item, divider)}
            disabled={disabled}
            tabIndex={-1}
            onMouseEnter={() => {
              if (!disabled) setFocused(index);
            }}
            onClick={() => select(item)}
          >
            <span className="nd-menu__label">{item.label}</span>
            {item.unavailable === undefined ? null : (
              <span className="nd-menu__reason">{item.unavailable}</span>
            )}
          </button>
        );
      })}
    </div>,
    document.body,
  );
}

/** The item's class list: base, plus the danger hue and a leading divider. */
function menuItemClass(item: MenuAction, divider: boolean): string {
  const classes = ["nd-menu__item"];
  if (item.danger === true) classes.push("nd-menu__item--danger");
  if (divider) classes.push("nd-menu__item--divided");
  return classes.join(" ");
}

interface MenuButtonProps {
  /** What the button opens a menu for — its accessible name. */
  label: string;
  /** The controller whose `openFrom` it calls. */
  controller: ContextMenuController;
}

/**
 * The overflow button that opens the same menu a right-click does.
 *
 * The pointer twin exists for the two readers a `contextmenu` handler alone
 * leaves out: a touch user, who has no right-click, and anyone who has never
 * thought to try one on a row in a web app.
 *
 * **It opens the menu; it does not toggle it.** That is a deliberate
 * narrowing, arrived at after four attempts at a toggle and eleven confirmed
 * defects between them. Every one had the same shape: whether the click should
 * open or close depends on whether the menu was still open by the time the
 * click arrived, and that depends on an ordering — document capture
 * `pointerdown`, then `mousedown` and the focus it moves, then React's
 * synchronous flush, then `click` — that varies with how the menu was opened,
 * whether the press produced a click at all, and which engine is running it.
 * Deciding at pointer-down removed the ordering and cost the focus default,
 * the touch-scroll cancellation, and every environment without Pointer Events.
 * Snapshotting the flag earlier only moved the race.
 *
 * Opening unconditionally has no ordering to get wrong. A press on this button
 * dismisses any open menu the ordinary way — through the outside-pointerdown
 * and focus watchers, which need no exemption for it — and the click that
 * follows opens one here. Escape, a selection, a click elsewhere, a scroll and
 * a focus move all still dismiss, so nothing is unreachable; what is gone is a
 * second way to close that never worked in every configuration.
 *
 * @param label What the menu is for; the button's accessible name.
 * @param controller The menu controller.
 */
export function MenuButton({ label, controller }: MenuButtonProps) {
  const ref = useRef<HTMLButtonElement | null>(null);
  return (
    <button
      ref={ref}
      type="button"
      className="nd-button nd-button--ghost nd-button--small nd-menu-button"
      aria-haspopup="menu"
      aria-expanded={controller.anchor !== null}
      aria-label={label}
      title={label}
      onClick={() => controller.openFrom(ref.current)}
    >
      ⋯
    </button>
  );
}
