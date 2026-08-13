import { useState } from "react";
import { Modal } from "./Modal";
import { useToast } from "./Toast";
import { edgeArchiveConsequences, edgeArchiveLabel } from "./edgeArchive";
import type { EdgeArchiveSubject } from "./edgeArchive";

interface ArchiveEdgeDialogProps {
  /** The exact directed relationship being archived. */
  subject: EdgeArchiveSubject;
  /** Performs the archive; throws on refusal so the dialog remains open. */
  onConfirm(): Promise<void>;
  /** Cancel handler for every dismissal route. */
  onClose(): void;
}

/** Confirm archiving one relationship after stating its edge-specific consequences. */
export function ArchiveEdgeDialog({ subject, onConfirm, onClose }: ArchiveEdgeDialogProps) {
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

  return (
    <Modal
      title={`Archive ${edgeArchiveLabel(subject)}?`}
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
            {busy ? "Archiving…" : "Archive relationship"}
          </button>
        </>
      }
    >
      <ul className="nd-consequences">
        {edgeArchiveConsequences(subject).map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
    </Modal>
  );
}
