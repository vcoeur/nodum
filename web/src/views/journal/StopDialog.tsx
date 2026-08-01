/**
 * The confirm in front of asking a running cycle to stop — the kill switch.
 *
 * **What it is for.** `service.request_stop` and migration `0015` shipped
 * together and no surface reached either: no CLI verb, no route, no button, on
 * the one screen that displays a running cycle. A switch nothing can be thrown
 * by is not a switch.
 *
 * **What it says.** Two dangerous misreadings rather than one, and they are the
 * two controls this button sits between. That a stop *reverses* what the run has
 * written — it does not; that is the rollback, afterwards, once the entry has
 * closed. And that a stop is a gentler *abandon* — it is not: abandoning closes
 * a dead process's entry from outside, a stop is an instruction a live run obeys
 * and records itself, and the journal keeps them apart precisely so a `failed`
 * entry read the next morning says which of the two happened. The copy lives in
 * `journal.ts` (`STOP_CONFIRM`) because the harness renders no components, so a
 * claim made inside one is a claim nothing checks — and every line of it has to
 * be something this system actually delivers, including the last, which admits
 * that the deterministic jobs make no provider call and so notice no stop.
 *
 * Modelled on `AbandonDialog` beside it: busy state on the affirmative button,
 * the dialog left standing on failure, Escape and the backdrop cancelling, and
 * nothing confirming on a keypress (the `Modal` contract). There is no preflight
 * — the service's one refusal is decidable from the row and is stated by
 * `stopAvailability` in front of the button that opens this.
 */

import { useState } from "react";
import { api } from "../../api/client";
import { Modal, useToast } from "../../components";
import type { CycleOut } from "../../api/types";
import { describeError } from "../../lib";
import { shortId, STOP_CONFIRM } from "./journal";

interface StopDialogProps {
  /** The running cycle being asked to stop. */
  cycle: CycleOut;
  /** Called with the stamped row once the server has answered. */
  onStopRequested: (cycle: CycleOut) => Promise<void> | void;
  /** Cancel handler for every dismissal route. */
  onClose: () => void;
}

/**
 * Confirm asking a running cycle to wind down.
 *
 * @param cycle The cycle being stopped.
 * @param onStopRequested Called with the stamped row; the dialog closes after it.
 * @param onClose Cancel handler.
 */
export function StopDialog({ cycle, onStopRequested, onClose }: StopDialogProps) {
  const toast = useToast();
  const [committing, setCommitting] = useState(false);

  const commit = () => {
    setCommitting(true);
    void api.requestCycleStop(cycle.id).then(
      async (stopped) => {
        setCommitting(false);
        await onStopRequested(stopped);
        onClose();
      },
      (error: unknown) => {
        setCommitting(false);
        // The live race is `InvalidTransition`: the run finished on its own
        // between the page loading and the click, so there is nothing left to
        // obey the instruction. A second tab getting there first is *not* one —
        // the service makes that a no-op that answers 200 — so the dialog stays
        // up and the reader reloads.
        toast.show("error", "The cycle was not asked to stop", describeError(error));
      },
    );
  };

  return (
    <Modal
      title={`Stop cycle ${shortId(cycle.id)}?`}
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
            {committing ? "Asking it to stop…" : "Ask the cycle to stop"}
          </button>
        </>
      }
    >
      <div className="nd-jn-verdict nd-jn-verdict--blocked">
        <p className="nd-jn-verdict__headline">This does not undo anything the cycle wrote.</p>
        <p>{STOP_CONFIRM[0]}</p>
      </div>
      {STOP_CONFIRM.slice(1).map((line) => (
        <p key={line} className="nd-meta">
          {line}
        </p>
      ))}
    </Modal>
  );
}
