/**
 * Archiving one node, and the undo that makes it survivable.
 *
 * The hook rather than the dialog owns this, and that is not a style choice:
 * the undo is offered *after* the dialog closes, so a flow living inside the
 * dialog would be handing a callback to a component that has already unmounted.
 * The host mounts this, the host outlives the toast, and the toast's Undo
 * therefore always has something live to call.
 *
 * **The undo names one seq.** `POST /api/undo` with no `seq` reverses whatever
 * the latest reversible event happens to be, which in a multi-writer store is
 * not necessarily the write the human is looking at — an agent holding `edit`
 * can have written in between. `lib/undoTarget.ts` decides whether the log head
 * provably *is* this archive; when it is not, the confirmation appears with no
 * Undo on it rather than with one that would reverse a stranger's write.
 *
 * Failing to read the event log costs the Undo button and nothing else: the
 * archive landed either way, and a confirmation that lied about that would be
 * worse than one without an undo.
 */

import { useCallback, useRef } from "react";
import { api } from "../api/client";
import { useToast } from "./Toast";
import { undoableSeq } from "../lib/undoTarget";
import type { NodeOut } from "../api/types";

/** The event `service.transition(…, "archive")` emits for a node. */
const ARCHIVE_OP = "node.archive";

/** What {@link useNodeArchive} hands back. */
export interface NodeArchiveApi {
  /**
   * Archive a node, then confirm it with an undo bound to that exact event.
   *
   * @param node The node to archive.
   * @returns Resolves once the archive has landed; rejects if it did not, so a
   *   dialog can stay standing on a failure.
   */
  archive(node: NodeOut): Promise<void>;
}

/**
 * The archive-with-undo flow, for a surface that can show a node.
 *
 * @param onChanged Called after the archive lands, and again after an undo puts
 *   the node back — the host refetches what it holds.
 */
export function useNodeArchive(onChanged: () => void): NodeArchiveApi {
  const toast = useToast();
  // Through a ref so `archive` stays stable across the host's renders: the
  // toast's Undo callback is held for as long as the toast is on screen.
  const changed = useRef(onChanged);
  changed.current = onChanged;

  const archive = useCallback(
    async (node: NodeOut) => {
      await api.archiveNode(node.id);
      changed.current();

      const label = node.title?.trim() ? node.title : node.id;
      let seq: number | null = null;
      try {
        seq = undoableSeq(await api.listEvents(1), { op: ARCHIVE_OP, rowId: node.id });
      } catch {
        // The archive is done; the log read is only what the Undo hangs off.
      }

      if (seq === null) {
        toast.show(
          "success",
          "Node archived",
          `${label} is archived. Another write landed after it, so the undo is not offered here — ` +
            "reverse it by seq on the CLI.",
        );
        return;
      }

      toast.show("success", "Node archived", `${label} is archived.`, {
        label: "Undo",
        onAct: () => {
          void (async () => {
            try {
              await api.undo(seq);
              changed.current();
              toast.show("success", "Archive undone", `${label} is active again.`);
            } catch (error) {
              toast.showError(error, "Not undone");
            }
          })();
        },
      });
    },
    [toast],
  );

  return { archive };
}
