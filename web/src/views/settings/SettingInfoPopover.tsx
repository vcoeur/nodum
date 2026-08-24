/**
 * The per-setting info popover — a read-only anchored panel.
 *
 * **Build-vs-adopt, recorded.** This is the third transient overlay on the
 * surface, so the decision is explicit rather than accidental. Adopting
 * `ContextMenu` is refused on semantics: a menu is a roving-index action list
 * under `role="menu"`, and this panel is static prose — `role="menu"` over
 * text nobody can activate is an ARIA violation. Adopting `NodePeek` is
 * refused on trigger: that card arms on hover/focus with an intent delay, and
 * this opens on click and owns focus. So it is built — but every reusable
 * rule is adopted, not re-invented: dismissal is
 * `lib/dismissWatchers.ts` (no second dismissal module, no private flag),
 * the focus hand-back is `lib/programmaticFocus.ts`, placement is
 * `placeMenu` from `lib/contextMenu.ts` (viewport clamping and the
 * flip-above-anchor, already tested), and the only key the panel owns is
 * Escape. What is left here is the wiring.
 *
 * **The focus contract is the four checks** a transient focus-owning overlay
 * owes, the same four `ContextMenu`'s history was built on: focus lands in
 * the panel on open (it is `tabIndex={-1}` and takes focus itself, like the
 * menu), focus returns to the opener on close — only if the panel still
 * holds it, and only if the opener is still connected — Escape and an
 * outside press both dismiss (Escape here, the outside press through the
 * watchers), and a document-level shortcut still reaches the surface behind,
 * because the panel stops Escape and nothing else: Ctrl/Cmd-K lands in the
 * command palette. The palette is a `Modal` that moves focus
 * programmatically, so the focus watcher suppresses that move and the panel
 * survives behind it — dismissible, not orphaned — exactly as the context
 * menu does. The panel closes on scroll like the menu does,
 * because it is anchored to a point in the viewport.
 *
 * The copy is the registry's: `summary` and `help` come from the row the
 * server serialised, and the model assembles them with the row's default and
 * liveness in `settingsModel.ts`.
 */

import { useEffect, useLayoutEffect, useRef } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { createPortal } from "react-dom";
import type { MenuAnchor } from "../../lib/contextMenu";
import { placeMenu } from "../../lib/contextMenu";
import { focusProgrammatically } from "../../lib/programmaticFocus";
import { attachDismissWatchers } from "../../lib/dismissWatchers";
import type { SettingPopup } from "./settingsModel";

interface SettingInfoPopoverProps {
  /** The row the popover explains — its accessible name, "About NODUM_LLM_MODEL". */
  label: string;
  /** The assembled content for this row, from `settingsModel.settingPopup`. */
  content: SettingPopup;
  /** Where it opens, in viewport coordinates; from the opener button's rect. */
  anchor: MenuAnchor;
  /** Called for every dismissal: Escape, outside press, scroll, focus leaving. */
  onClose(): void;
}

/**
 * A read-only info popover, portalled to `document.body` and placed by
 * `placeMenu` rather than by CSS, so no ancestor's `overflow` can clip it.
 *
 * @param label The row the popover explains.
 * @param content The registry's copy plus default and liveness.
 * @param anchor The opener button's position.
 * @param onClose Dismissal handler.
 */
export function SettingInfoPopover({ label, content, anchor, onClose }: SettingInfoPopoverProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const restoreTo = useRef<HTMLElement | null>(null);

  // Placed before the browser paints, so the panel never flashes at (0, 0).
  // Focus is taken here and handed back on close — the same rule `Modal` and
  // `ContextMenu` follow, and for the same reason: a keyboard reader who
  // dismisses this must not be left on `<body>`.
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
      // Restore only if this panel still holds focus — anything else means
      // focus was deliberately moved elsewhere while it was up, and grabbing
      // it back from there is worse than never having taken it.
      if (!panel.contains(document.activeElement)) return;
      const opener = restoreTo.current;
      // And only if the opener is still in the document: focusing a detached
      // node drops focus onto `<body>`, which is what this prevents.
      if (opener !== null && opener !== document.body && opener.isConnected) {
        // Marked as the app's move, not the reader's: a watcher would
        // otherwise re-dismiss or re-arm on it as a user focus.
        focusProgrammatically(opener, { preventScroll: true });
      }
    };
  }, [anchor]);

  // Every dismissal except Escape — focus leaving, a press outside, a scroll,
  // a resize — lives in `lib/dismissWatchers.ts`, the shared module
  // `ContextMenu` already uses. None of it is React, and it is tested there
  // in jsdom rather than re-implemented here.
  useEffect(() => {
    const panel = panelRef.current;
    if (panel === null) return;
    return attachDismissWatchers(panel, { onDismiss: onClose });
  }, [onClose]);

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    // The panel owns Escape and nothing else. A blanket stopPropagation would
    // kill every document-level shortcut on the surface behind — search's `/`
    // and Ctrl/Cmd-K among them; and any other key that moves focus out is
    // already a dismissal through the focus watcher.
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
    }
  };

  return createPortal(
    <div
      ref={panelRef}
      className="nd-set-info-popover"
      role="dialog"
      aria-label={label}
      tabIndex={-1}
      onKeyDown={onKeyDown}
    >
      <p className="nd-set-info-popover__summary">{content.summary}</p>
      {content.help === null ? null : (
        <p className="nd-set-info-popover__help">{content.help}</p>
      )}
      <dl className="nd-set-info-popover__meta">
        <div className="nd-set-info-popover__meta-row">
          <dt>Default</dt>
          <dd>{content.defaultLabel}</dd>
        </div>
        {content.livenessLabel === null ? null : (
          <div className="nd-set-info-popover__meta-row">
            <dt>Takes effect</dt>
            <dd>{content.livenessLabel}</dd>
          </div>
        )}
      </dl>
    </div>,
    document.body,
  );
}
