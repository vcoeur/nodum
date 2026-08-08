/**
 * The confirm in front of archiving one node.
 *
 * The same shape as `views/spaces/ArchiveDialog.tsx` one scale down — busy
 * state on the affirmative button, a toast on failure with the dialog left
 * standing, Escape and the backdrop cancelling, nothing confirming on a
 * keypress. It is shared rather than view-local because the reading view and
 * the editor both reach it, and a second copy is how two surfaces end up
 * promising different things about the same write.
 *
 * The body is `nodeArchive.ts`'s consequence list. Its last line says the
 * archive is **one reversible event** and deliberately promises neither the
 * Undo button nor a condition for it: `useNodeArchive` withholds the button
 * whenever it cannot prove the event-log head is this write, and a confirm
 * naming a control the next screen does not show is the defect that module
 * exists to keep out.
 */

import { useState } from "react";
import { Modal } from "./Modal";
import { useToast } from "./Toast";
import { archiveConsequences } from "./nodeArchive";
import type { NodeOut } from "../api/types";

interface ArchiveNodeDialogProps {
  /** The node being archived. */
  node: NodeOut;
  /**
   * Incident edges, when the host has read the neighbourhood; null otherwise.
   * The line about edges surviving is stated either way.
   */
  edgeCount: number | null;
  /** Performs the archive; throws on failure, which keeps the dialog up. */
  onConfirm(): Promise<void>;
  /** Cancel handler for every dismissal route. */
  onClose(): void;
}

/**
 * Confirm archiving a node, having said what that costs.
 *
 * @param node The node being archived.
 * @param edgeCount Incident edge count, or null when unknown.
 * @param onConfirm The archive itself.
 * @param onClose Cancel handler.
 */
export function ArchiveNodeDialog({
  node,
  edgeCount,
  onConfirm,
  onClose,
}: ArchiveNodeDialogProps) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  const confirm = () => {
    setBusy(true);
    void onConfirm().then(
      () => onClose(),
      (error: unknown) => {
        setBusy(false);
        toast.showError(error, "Not archived");
      },
    );
  };

  const label = node.title?.trim() ? node.title : node.id;

  return (
    <Modal
      title={`Archive ${label}?`}
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
            {busy ? "Archiving…" : "Archive"}
          </button>
        </>
      }
    >
      <ul className="nd-consequences">
        {archiveConsequences(node, edgeCount).map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
    </Modal>
  );
}
