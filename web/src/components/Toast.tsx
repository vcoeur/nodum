import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";
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

/** One queued notification. */
export interface Toast {
  id: number;
  tone: ToastTone;
  /** What happened, in the interface's voice — "Proposal accepted". */
  title: string;
  /** Optional second line: the server's message, an id, a next step. */
  detail?: string;
}

/** What {@link useToast} hands back. */
export interface ToastApi {
  /** Show a notification. Returns its id, so a caller can dismiss it early. */
  show(tone: ToastTone, title: string, detail?: string): number;
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
 * Provide the toast API to the tree and render the toast region.
 *
 * @param children The app.
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const show = useCallback(
    (tone: ToastTone, title: string, detail?: string) => {
      const id = nextId.current++;
      setToasts((current) => [...current, detail === undefined
        ? { id, tone, title }
        : { id, tone, title, detail }]);
      // Errors are not auto-dismissed: they usually need an action.
      if (tone !== "error") {
        window.setTimeout(() => dismiss(id), AUTO_DISMISS_MS);
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
