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
} from "../lib/contextMenu";
import type { MenuAnchor } from "../lib/contextMenu";
import "./ContextMenu.css";

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
 * @returns The controller a surface hands to {@link ContextMenu}.
 */
export function useContextMenu(): ContextMenuController {
  const [anchor, setAnchor] = useState<MenuAnchor | null>(null);

  const openAt = useCallback((event: ReactMouseEvent) => {
    // The browser's own menu would cover this one, and the surfaces that offer
    // this menu have nothing the native one can do for them.
    event.preventDefault();
    event.stopPropagation();
    const target = event.currentTarget;
    const rect = target.getBoundingClientRect();
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
  const [focused, setFocused] = useState(-1);

  const states = items.map((item) => ({ disabled: item.unavailable !== undefined }));

  // Placed before the browser paints, so the panel never flashes at (0, 0)
  // before landing next to the anchor.
  useLayoutEffect(() => {
    const panel = panelRef.current;
    if (panel === null) return;
    const rect = panel.getBoundingClientRect();
    const at = placeMenu(
      anchor,
      { width: rect.width, height: rect.height },
      { width: window.innerWidth, height: window.innerHeight },
    );
    panel.style.left = `${at.left}px`;
    panel.style.top = `${at.top}px`;
    panel.focus();
  }, [anchor]);

  // A menu is anchored to a point in the viewport, and a scroll moves the
  // content out from under it — so a scroll closes it rather than letting it
  // hover over an unrelated row. The pointerdown listener is on `document` in
  // the capture phase: a click on another surface's trigger must close this
  // menu before that surface opens its own.
  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (panelRef.current?.contains(event.target as Node)) return;
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
    switch (event.key) {
      case "Escape":
        event.stopPropagation();
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
