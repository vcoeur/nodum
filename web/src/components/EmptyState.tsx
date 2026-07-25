import type { ReactNode } from "react";

/** Placeholder shown where a list, result set, or queue has nothing in it. */

interface EmptyStateProps {
  /** What is empty, stated plainly — "No proposals waiting". */
  title: string;
  /** One line on what to do about it. An empty screen is an invitation to act. */
  body?: ReactNode;
  /** The action that fills it, if there is one. */
  action?: ReactNode;
}

/**
 * An empty state.
 *
 * @param title What is empty.
 * @param body Optional guidance on how to fill it.
 * @param action Optional primary action.
 */
export function EmptyState({ title, body, action }: EmptyStateProps) {
  return (
    <div className="nd-empty">
      <p className="nd-empty__title">{title}</p>
      {body ? <p className="nd-empty__body">{body}</p> : null}
      {action ? <div className="nd-empty__action">{action}</div> : null}
    </div>
  );
}
