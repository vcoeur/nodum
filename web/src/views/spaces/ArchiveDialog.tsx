/**
 * The confirm in front of archiving a space.
 *
 * Modelled on `views/admin/ConfirmDialog.tsx` — busy state on the affirmative
 * button, a toast on failure with the dialog left standing, Escape and the
 * backdrop cancelling, nothing confirming on a keypress (the `Modal` contract).
 * It is a separate component rather than an import because a view never imports
 * another view's module, and because this confirm is not generic: the body it
 * has to show is the list of things archiving does *and does not* do, which is
 * the whole reason the action needs a dialog at all.
 *
 * The one thing a human reliably assumes about "archive" is that the contents
 * go with it. Here they do not — nodes keep their `space_id` and stay exactly
 * as readable as they were — while the name *does* go with it and stays
 * reserved, and this screen offers no way back. All three belong in front of
 * the button, not in a toast afterwards.
 */

import { useState } from "react";
import { Modal, useToast } from "../../components";
import { archiveConsequences, describeSpaceFailure } from "./spaces";
import type { SpaceRow } from "./spaces";

interface ArchiveDialogProps {
  /** The space being archived. */
  row: SpaceRow;
  /** Performs the archive; throws on failure, which keeps the dialog up. */
  onConfirm: () => Promise<void>;
  /** Cancel handler for every dismissal route. */
  onClose: () => void;
}

/**
 * Confirm archiving a space, having said what that costs.
 *
 * @param row The space being archived.
 * @param onConfirm The archive itself.
 * @param onClose Cancel handler.
 */
export function ArchiveDialog({ row, onConfirm, onClose }: ArchiveDialogProps) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  const confirm = () => {
    setBusy(true);
    void onConfirm().then(
      () => onClose(),
      (error: unknown) => {
        setBusy(false);
        // Through the space-aware classifier, never `toast.showError`: the
        // shared 404 copy says the server "has no record of" the space, which
        // is precisely the claim the refusal is worded to avoid making.
        const described = describeSpaceFailure(error, row.label);
        toast.show("error", described.title, described.body);
      },
    );
  };

  return (
    <Modal
      title={`Archive ${row.label}?`}
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
      <ul className="nd-sp-consequences">
        {archiveConsequences(row).map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
    </Modal>
  );
}
