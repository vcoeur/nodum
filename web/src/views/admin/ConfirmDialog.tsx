/**
 * Confirmation for the admin view's destructive actions.
 *
 * Revoking a grant, disabling an agent, and rotating a token all take effect
 * immediately and sever something a running agent may depend on, so each gets
 * one explicit confirm. Escape and the backdrop cancel; nothing confirms on a
 * keypress (the Modal contract).
 */

import { useState } from "react";
import { Modal, useToast } from "../../components";

interface ConfirmDialogProps {
  /** Dialog heading. */
  title: string;
  /** What the action does, in one or two sentences. */
  body: string;
  /** The confirm button's verb, e.g. "Revoke". */
  confirmLabel: string;
  /** The action itself; throws on failure (a toast reports it, dialog stays). */
  onConfirm: () => Promise<void>;
  /** Cancel handler. */
  onClose: () => void;
}

/** One explicit confirm before a destructive admin action. */
export function ConfirmDialog({ title, body, confirmLabel, onConfirm, onClose }: ConfirmDialogProps) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  const confirm = () => {
    setBusy(true);
    void onConfirm().then(
      () => onClose(),
      (error: unknown) => {
        setBusy(false);
        toast.showError(error);
      },
    );
  };

  return (
    <Modal
      title={title}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="nd-button nd-button--ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="nd-button nd-button--danger"
            onClick={confirm}
            disabled={busy}
          >
            {busy ? `${confirmLabel}…` : confirmLabel}
          </button>
        </>
      }
    >
      <p>{body}</p>
    </Modal>
  );
}
