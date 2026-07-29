/**
 * The confirm in front of abandoning an interrupted cycle.
 *
 * **What it is for.** A cycle left `running` by a `SIGKILL`, a power cut, or a
 * shutdown that cancelled the nightly task in flight is not a cosmetic wart in
 * the journal: `service._rollback_plan` refuses a cycle that has not closed and
 * `undo` refuses every event a cycle stamped, so whatever the run wrote before it
 * died is irreversible on **every** surface until somebody closes the row.
 * `nodum cycle-abandon` and `POST /api/cycles/{id}/abandon` both shipped with
 * Phase 5a and this screen offered neither — a browser user looking at the stuck
 * entry was told a running cycle cannot be rolled back and nothing else.
 *
 * **What it says.** The dangerous misreading is that abandoning cancels the run
 * or takes its writes back; it does neither, and the confirm says so before the
 * button rather than in a toast afterwards. The copy lives in `journal.ts`
 * (`ABANDON_CONFIRM`) because the harness renders no components, so a claim made
 * inside one is a claim nothing checks — and every line of it has to be something
 * `service.abandon_cycle` actually delivers.
 *
 * Modelled on `RollbackDialog` and `views/spaces/ArchiveDialog.tsx`: busy state
 * on the affirmative button, the dialog left standing on failure, Escape and the
 * backdrop cancelling, and nothing confirming on a keypress (the `Modal`
 * contract). There is no preflight, because there is nothing to rehearse — the
 * service's one refusal is decidable from the row and is stated by
 * `abandonAvailability` in front of the button that opens this.
 */

import { useState } from "react";
import { api } from "../../api/client";
import { Modal, useToast } from "../../components";
import type { CycleOut } from "../../api/types";
import { describeError } from "../../lib";
import { ABANDON_CONFIRM, shortId } from "./journal";

interface AbandonDialogProps {
  /** The cycle being closed. */
  cycle: CycleOut;
  /** Called with the closed row once the server has answered. */
  onAbandoned: (cycle: CycleOut) => Promise<void> | void;
  /** Cancel handler for every dismissal route. */
  onClose: () => void;
}

/**
 * Confirm abandoning a cycle nobody is going to finish.
 *
 * @param cycle The cycle being closed.
 * @param onAbandoned Called with the closed row; the dialog closes after it.
 * @param onClose Cancel handler.
 */
export function AbandonDialog({ cycle, onAbandoned, onClose }: AbandonDialogProps) {
  const toast = useToast();
  const [committing, setCommitting] = useState(false);

  const commit = () => {
    setCommitting(true);
    void api.abandonCycle(cycle.id).then(
      async (closed) => {
        setCommitting(false);
        await onAbandoned(closed);
        onClose();
      },
      (error: unknown) => {
        setCommitting(false);
        // The live races this can meet are both `InvalidTransition`: the run
        // finished on its own between the page loading and the click, or another
        // tab abandoned it first. Either way the row has said how it ended, so
        // the dialog stays up and the reader reloads.
        toast.show("error", "The cycle was not abandoned", describeError(error));
      },
    );
  };

  return (
    <Modal
      title={`Abandon cycle ${shortId(cycle.id)}?`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="nd-button nd-button--ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="nd-button nd-button--danger"
            onClick={commit}
            disabled={committing}
          >
            {committing ? "Abandoning…" : "Abandon the cycle"}
          </button>
        </>
      }
    >
      <div className="nd-jn-verdict nd-jn-verdict--blocked">
        <p className="nd-jn-verdict__headline">This does not undo anything the cycle wrote.</p>
        <p>{ABANDON_CONFIRM[0]}</p>
      </div>
      {ABANDON_CONFIRM.slice(1).map((line) => (
        <p key={line} className="nd-meta">
          {line}
        </p>
      ))}
    </Modal>
  );
}
