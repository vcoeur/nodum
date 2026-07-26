/**
 * Collecting the mandatory reject reason.
 *
 * A reject requires a reason in both spellings — single-item and batch — and
 * the service records it in every reject event's payload. That makes this
 * dialog the only route to a reject anywhere in the view: there is no
 * quick-reject button, no keyboard shortcut, and no default text. The reason is
 * the reviewer's, and pre-filling a plausible one on their behalf would put
 * words they never wrote into the audit log.
 */

import { useId, useState } from "react";
import { Modal } from "../../components";
import { ProposalManifest } from "./ProposalManifest";
import type { ProposalOut } from "../../api/types";
import { plural } from "./format";

/** Below this the reason is not a reason. Deliberately low — a real one is longer. */
const MIN_REASON_LENGTH = 3;

interface RejectDialogProps {
  proposals: readonly ProposalOut[];
  /** Where the set came from, shown above the manifest. */
  scope: string;
  busy: boolean;
  /** Called with the trimmed reason. Never called with an empty one. */
  onConfirm: (reason: string) => void;
  onCancel: () => void;
}

/**
 * The reject dialog.
 *
 * @param proposals The exact set that will be rejected.
 * @param scope Human-readable description of where the set came from.
 * @param busy True while the request is in flight.
 * @param onConfirm Receives the trimmed, non-empty reason.
 * @param onCancel Escape, backdrop, or Cancel.
 */
export function RejectDialog({
  proposals,
  scope,
  busy,
  onConfirm,
  onCancel,
}: RejectDialogProps) {
  const [reason, setReason] = useState("");
  const reasonId = useId();
  const trimmed = reason.trim();
  const valid = trimmed.length >= MIN_REASON_LENGTH;

  return (
    <Modal
      title={`Reject ${plural(proposals.length, "proposal")}?`}
      onClose={busy ? () => undefined : onCancel}
      wide
      footer={
        <>
          <button type="button" className="nd-button" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="nd-button nd-button--danger"
            onClick={() => onConfirm(trimmed)}
            disabled={busy || !valid || proposals.length === 0}
            title={valid ? undefined : "A reject needs a reason — it is recorded in the event log"}
          >
            {busy ? "Rejecting…" : `Reject ${proposals.length}`}
          </button>
        </>
      }
    >
      <p className="nd-meta nd-rv-dialog__scope">{scope}</p>

      <div className="nd-field nd-rv-dialog__reason">
        <label className="nd-label" htmlFor={reasonId}>
          Reason (required)
        </label>
        <textarea
          id={reasonId}
          className="nd-textarea"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="Why is this wrong? The text is recorded verbatim in every reject event."
          autoFocus
          disabled={busy}
          aria-describedby={`${reasonId}-help`}
        />
        <p id={`${reasonId}-help`} className="nd-meta">
          Recorded in the reject event's payload for{" "}
          {plural(proposals.length, "proposal")}. There is no default and none is
          filled in for you — this is the audit trail for why live state did not
          change.
        </p>
      </div>

      <ProposalManifest proposals={proposals} action="reject" />
    </Modal>
  );
}
