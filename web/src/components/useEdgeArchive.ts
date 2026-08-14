import { useCallback, useRef } from "react";
import { api } from "../api/client";
import { undoableSeq } from "../lib/undoTarget";
import { edgeArchiveLabel } from "./edgeArchive";
import type { EdgeArchiveSubject } from "./edgeArchive";
import { useToast } from "./Toast";

/** What {@link useEdgeArchive} hands to an archive confirmation. */
export interface EdgeArchiveApi {
  /** Archive the exact edge and offer undo only for its proven event sequence. */
  archive(subject: EdgeArchiveSubject): Promise<void>;
}

/** Archive one relationship and sequence-target its optional undo. */
export function useEdgeArchive(onChanged: () => void): EdgeArchiveApi {
  const toast = useToast();
  const changed = useRef(onChanged);
  changed.current = onChanged;

  const archive = useCallback(
    async (subject: EdgeArchiveSubject) => {
      await api.archiveEdge(subject.edge.id);
      changed.current();
      const label = edgeArchiveLabel(subject);
      let seq: number | null = null;
      let logRead = true;
      try {
        seq = undoableSeq(await api.listEvents(1), {
          op: "edge.archive",
          rowId: subject.edge.id,
        });
      } catch {
        logRead = false;
      }

      if (seq === null) {
        toast.show(
          "success",
          "Relationship archived",
          logRead
            ? `${label} is archived. Something else was written after it, so undo is not offered here.`
            : `${label} is archived. The event log could not be read, so this cannot identify an event to undo.`,
        );
        return;
      }

      toast.show("success", "Relationship archived", `${label} is archived.`, {
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
