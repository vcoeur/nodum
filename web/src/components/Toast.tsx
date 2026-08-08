import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { describeError, describeFailure } from "../lib";

/**
 * The app-wide notification surface.
 *
 * Wrap the tree in {@link ToastProvider} once (App.tsx does), then call
 * {@link useToast} anywhere below it. Successes and info notices dismiss
 * themselves; errors stay until dismissed, because an error a reviewer missed
 * is worse than one that lingers.
 */

/** How a toast is coloured and how long it lives. */
export type ToastTone = "info" | "success" | "error";

/**
 * One thing a toast lets you do about what it is reporting.
 *
 * Deliberately singular: a notification with a choice in it is a dialog that
 * dismisses itself. The only caller so far is the archive confirmation's Undo.
 */
export interface ToastAction {
  /** The button's text — a verb. */
  label: string;
  /** Runs on click; the toast dismisses first, so it never acts twice. */
  onAct(): void;
}

/** One queued notification. */
export interface Toast {
  id: number;
  tone: ToastTone;
  /** What happened, in the interface's voice — "Proposal accepted". */
  title: string;
  /** Optional second line: the server's message, an id, a next step. */
  detail?: string;
  /** Optional single action, e.g. undoing what the toast reports. */
  action?: ToastAction;
}

/** What {@link useToast} hands back. */
export interface ToastApi {
  /**
   * Show a notification. Returns its id, so a caller can dismiss it early.
   *
   * An `action` gives the toast a longer life than a bare one: an undo that
   * scrolls away before it can be read is not an undo.
   */
  show(tone: ToastTone, title: string, detail?: string, action?: ToastAction): number;
  /**
   * Show an error toast for a thrown value.
   *
   * Headline and detail come from the shared classifier in `src/lib`, so a
   * toast tells "the server said no" apart from "nothing was listening" the
   * same way every inline panel does.
   *
   * @param error The caught value.
   * @param title Optional override for the headline.
   */
  showError(error: unknown, title?: string): number;
  /** Dismiss one toast by id. */
  dismiss(id: number): void;
}

const ToastContext = createContext<ToastApi | null>(null);

/** How long a self-dismissing toast stays up. */
const AUTO_DISMISS_MS = 4500;

/**
 * How long a toast carrying an action stays up.
 *
 * Longer than a bare notice, because reading it is not the point — reaching
 * the button is, and the reader's eyes are on the dialog that just closed.
 */
const ACTION_DISMISS_MS = 12_000;

/**
 * Provide the toast API to the tree and render the toast region.
 *
 * @param children The app.
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);
  /**
   * Live auto-dismiss timers, by toast id.
   *
   * Kept so a toast dismissed by hand does not leave a timer to fire into an
   * id that is already gone, and so the provider can clear the lot on unmount
   * instead of leaving up to one callback per toast pointed at a dead tree.
   */
  const timers = useRef(new Map<number, number>());

  const dismiss = useCallback((id: number) => {
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      timers.current.delete(id);
    }
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  useEffect(() => {
    const live = timers.current;
    return () => {
      for (const timer of live.values()) window.clearTimeout(timer);
      live.clear();
    };
  }, []);

  const show = useCallback(
    (tone: ToastTone, title: string, detail?: string, action?: ToastAction) => {
      const id = nextId.current++;
      setToasts((current) => [
        ...current,
        {
          id,
          tone,
          title,
          ...(detail === undefined ? {} : { detail }),
          ...(action === undefined ? {} : { action }),
        },
      ]);
      // Errors are not auto-dismissed: they usually need an action.
      if (tone !== "error") {
        const life = action === undefined ? AUTO_DISMISS_MS : ACTION_DISMISS_MS;
        timers.current.set(id, window.setTimeout(() => dismiss(id), life));
      }
      return id;
    },
    [dismiss],
  );

  const showError = useCallback(
    (error: unknown, title?: string) => {
      const failure = describeFailure(error);
      return show("error", title ?? failure.title, describeError(error));
    },
    [show],
  );

  const api = useMemo<ToastApi>(() => ({ show, showError, dismiss }), [show, showError, dismiss]);

  return (
    <ToastContext.Provider value={api}>
      {children}
      <ToastRegion toasts={toasts} onDismiss={dismiss} />
    </ToastContext.Provider>
  );
}

/**
 * Access the toast API.
 *
 * @throws Error If called outside a {@link ToastProvider}.
 */
export function useToast(): ToastApi {
  const api = useContext(ToastContext);
  if (!api) throw new Error("useToast must be called inside a ToastProvider");
  return api;
}

/** The fixed stack of live toasts. */
function ToastRegion({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  if (toasts.length === 0) return null;
  return (
    <div className="nd-toast-region" role="region" aria-label="Notifications">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`nd-toast nd-toast--${toast.tone}`}
          role={toast.tone === "error" ? "alert" : "status"}
        >
          <div className="nd-toast__body">
            <span className="nd-toast__title">{toast.title}</span>
            {toast.detail ? <span className="nd-toast__detail">{toast.detail}</span> : null}
          </div>
          {toast.action ? (
            <button
              type="button"
              className="nd-button nd-button--small nd-toast__action"
              onClick={() => {
                // Dismissed first: the action is a one-shot, and a second
                // click on a toast still standing would send it twice.
                const act = toast.action?.onAct;
                onDismiss(toast.id);
                act?.();
              }}
            >
              {toast.action.label}
            </button>
          ) : null}
          <button
            type="button"
            className="nd-toast__dismiss"
            onClick={() => onDismiss(toast.id)}
            aria-label="Dismiss notification"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
