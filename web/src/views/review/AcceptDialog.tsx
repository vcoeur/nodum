/**
 * Confirmation for accepting a set of proposals.
 *
 * Shown for every batch and multi-select accept, and skipped for a single
 * card's own Accept button — one explicit click on one visible item is already
 * deliberate, and a dialog in front of it is the kind of friction that trains
 * people to click through dialogs. The set actions get the dialog because their
 * risk is precisely that the set is not what the reviewer pictured.
 */

import { Modal } from "./Modal";
import { ProposalManifest } from "./ProposalManifest";
import type { ProposalOut } from "../../api/types";
import { plural } from "./format";

interface AcceptDialogProps {
  proposals: readonly ProposalOut[];
  /** Where the set came from, e.g. "agent:researcher · batch of 12:00:03". */
  scope: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * The accept confirmation.
 *
 * @param proposals The exact set that will be sent.
 * @param scope Human-readable description of where the set came from.
 * @param busy True while the request is in flight.
 * @param onConfirm Fired only by the footer button.
 * @param onCancel Escape, backdrop, or Cancel.
 */
export function AcceptDialog({
  proposals,
  scope,
  busy,
  onConfirm,
  onCancel,
}: AcceptDialogProps) {
  return (
    <Modal
      title={`Accept ${plural(proposals.length, "proposal")}?`}
      onClose={busy ? () => undefined : onCancel}
      wide
      footer={
        <>
          <button type="button" className="nd-button" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="nd-button nd-button--primary"
            onClick={onConfirm}
            disabled={busy || proposals.length === 0}
          >
            {busy ? "Accepting…" : `Accept ${proposals.length}`}
          </button>
        </>
      }
    >
      <p className="nd-meta nd-rv-dialog__scope">{scope}</p>
      <ProposalManifest proposals={proposals} action="accept" />
      <p className="nd-rv-dialog__note">
        This writes live state, so it is a human-only operation. The server
        attributes it to <code>human</code> — the reviewer identity is not
        something this interface can choose.
      </p>
    </Modal>
  );
}
