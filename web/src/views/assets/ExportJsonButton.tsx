import { useId, useState } from "react";
import { api } from "../../api/client";
import { Spinner, useToast } from "../../components";
import { describeFailure } from "../../lib";
import "./export.css";

/**
 * The thin JSON export (plan slice 6, decision 4: minimal JSON now).
 *
 * Deliberately not a page. Export is an *action* available where a node is
 * already on screen — the history view's header and the lightbox's
 * referencing-node list — because the full export story is Markdown Mirror in
 * Phase 6 and a general "Export" destination built now would be the wrong
 * shape to grow into it. There are no format options here for the same reason:
 * one format, one snapshot, said plainly.
 */

/** Depths offered. Beyond one hop the "snapshot" framing stops being honest. */
const DEPTHS = [
  { value: 0, label: "this node only" },
  { value: 1, label: "+ direct neighbours" },
  { value: 2, label: "+ two hops" },
] as const;

interface ExportJsonButtonProps {
  /** Node whose snapshot is downloaded. */
  nodeId: string;
  /**
   * Drop the depth picker and export the node alone.
   *
   * Used where the node is incidental to what is on screen (the lightbox),
   * and a subgraph depth control would be a question nobody asked.
   */
  compact?: boolean;
}

/**
 * Download a node's JSON snapshot as a file.
 *
 * @param nodeId The node to export.
 * @param compact Hide the depth picker and export depth 0.
 */
export function ExportJsonButton({ nodeId, compact = false }: ExportJsonButtonProps) {
  const [depth, setDepth] = useState(0);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const selectId = useId();

  const download = async () => {
    setBusy(true);
    try {
      const snapshot = await api.exportNode(nodeId, { depth });
      triggerJsonDownload(snapshot, `nodum-${safeFilename(nodeId)}-depth${depth}.json`);
      toast.show("success", "JSON snapshot downloaded", `${nodeId} at depth ${depth}`);
    } catch (error) {
      const failure = describeFailure(error, "this node");
      toast.show("error", failure.title, failure.body);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="nd-export">
      <div className="nd-export__controls">
        {compact ? null : (
          <>
            <label className="nd-export__label" htmlFor={selectId}>
              Depth
            </label>
            <select
              id={selectId}
              className="nd-select nd-export__depth"
              value={depth}
              onChange={(event) => setDepth(Number(event.target.value))}
              disabled={busy}
            >
              {DEPTHS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.value} — {option.label}
                </option>
              ))}
            </select>
          </>
        )}
        <button
          type="button"
          className="nd-button nd-button--small"
          onClick={() => void download()}
          disabled={busy}
        >
          {busy ? <Spinner label="Building the snapshot" /> : null}
          Download JSON
        </button>
      </div>
      <p className="nd-export__note">
        A JSON snapshot of the node{depth > 0 ? " and the subgraph around it" : ""} — not a full
        export. Markdown Mirror lands in Phase 6.
      </p>
    </div>
  );
}

/**
 * Serialise a payload and hand it to the browser as a file download.
 *
 * The client parses the response as JSON (every route in it does), so the
 * bytes are re-serialised here rather than streamed; at snapshot sizes that is
 * the simpler trade, and it means the file is pretty-printed and readable.
 *
 * @param payload The parsed JSON body.
 * @param filename Name offered to the browser.
 */
function triggerJsonDownload(payload: unknown, filename: string): void {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

/** Reduce an id to characters that survive a download filename intact. */
function safeFilename(value: string): string {
  return value.replace(/[^A-Za-z0-9._-]+/g, "-").slice(0, 64) || "node";
}

export default ExportJsonButton;
