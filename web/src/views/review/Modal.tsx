/**
 * The dialog shell the review view's confirmations are built on.
 *
 * Duplication note: this is a generic modal wrapper over the shared
 * `.nd-modal` primitives and belongs in `src/components/` the moment a second
 * view needs one — it lives here only because that directory is a coordination
 * surface between slices.
 *
 * Two behaviours are deliberate and safety-relevant:
 *
 * - **Escape cancels; nothing confirms.** No key is bound to the affirmative
 *   action, and the dialog never submits on Enter, so an accept or a reject can
 *   only happen on a pointer or an explicitly focused-and-activated button. A
 *   stray keypress must never move live state.
 * - **Focus moves into the dialog** on open, is trapped inside it while it is
 *   up, and returns to whatever opened it on close, so the keyboard path is the
 *   same as the pointer path. The trap is what `aria-modal="true"` promises: a
 *   reviewer who tabs past the confirm button must not land on the queue behind
 *   the dialog, where the next Enter would act on a card they cannot see.
 *
 * The restore is conditional on the opener still being in the document. After a
 * *successful* confirm the opener is usually gone — the sticky selection bar's
 * "Accept selected…" unmounts when the selection clears — and focusing a
 * detached element silently drops focus onto `<body>`, losing a keyboard user's
 * place entirely. When that happens this component does nothing and leaves the
 * choice to the view, which is the only thing that knows where the reader
 * should end up (`ReviewInbox` sends them to the outcome panel).
 */

import { useCallback, useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";

/** Everything inside the dialog a Tab can reach, in document order. */
const FOCUSABLE =
  'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])';

/**
 * Keep Tab inside the dialog, wrapping at both ends.
 *
 * Disabled controls are filtered out, so a confirm button that is disabled
 * while a reject reason is empty drops out of the cycle instead of becoming a
 * dead stop. With nothing focusable at all, focus stays on the dialog itself.
 *
 * @param event The Tab keydown.
 * @param dialog The dialog element, or null if it has unmounted.
 */
function trapTab(event: KeyboardEvent, dialog: HTMLElement | null): void {
  if (!dialog) return;
  const focusable = [...dialog.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
    (element) =>
      !element.hasAttribute("disabled") && element.tabIndex !== -1 && element.offsetParent !== null,
  );
  event.preventDefault();
  if (focusable.length === 0) {
    dialog.focus();
    return;
  }
  const count = focusable.length;
  // -1 means focus is on the dialog itself, which is where it starts.
  const current = focusable.indexOf(document.activeElement as HTMLElement);
  const next = event.shiftKey
    ? current <= 0
      ? count - 1
      : current - 1
    : current === -1 || current === count - 1
      ? 0
      : current + 1;
  focusable[next]?.focus();
}

interface ModalProps {
  /** Dialog heading; also its accessible name. */
  title: string;
  /** Called for Escape, the backdrop, and the close button — always a cancel. */
  onClose: () => void;
  /** Body content. */
  children: ReactNode;
  /** Footer actions, rendered right-aligned. */
  footer?: ReactNode;
  /** Widen past the default for a diff or a manifest. */
  wide?: boolean;
}

/**
 * A modal dialog.
 *
 * @param title Heading and accessible name.
 * @param onClose Cancel handler for every dismissal route.
 * @param children Dialog body.
 * @param footer Right-aligned actions.
 * @param wide Use the wider layout.
 */
export function Modal({ title, onClose, children, footer, wide = false }: ModalProps) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const restoreTo = useRef<HTMLElement | null>(null);
  const titleId = useId();

  const close = useCallback(() => onClose(), [onClose]);

  useEffect(() => {
    restoreTo.current = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();

    // The page behind a modal must not scroll under it. The assets lightbox
    // already does this; the two dialogs in the app behave the same way.
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        close();
        return;
      }
      if (event.key === "Tab") trapTab(event, dialogRef.current);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      // Only if it still exists: focusing a detached node drops focus to
      // `<body>`, which is worse than leaving it for the view to place.
      if (restoreTo.current?.isConnected) restoreTo.current.focus();
    };
  }, [close]);

  return (
    <div
      className="nd-modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <div
        ref={dialogRef}
        className={wide ? "nd-modal nd-rv-modal--wide" : "nd-modal"}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <div className="nd-modal__header">
          <h2 id={titleId}>{title}</h2>
          <button type="button" className="nd-button nd-button--ghost nd-button--small" onClick={close}>
            Close
          </button>
        </div>
        <div className="nd-modal__body">{children}</div>
        {footer ? <div className="nd-modal__footer">{footer}</div> : null}
      </div>
    </div>
  );
}
