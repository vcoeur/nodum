import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ingestUpload } from "../../api/client";
import { nameSpace, spaceNameNote, Spinner } from "../../components";
import type { SpaceName } from "../../components";
import type { NodeOut } from "../../api/types";
import { formatBytes } from "./formatting";
import { describeUploadFailure, readIngestion, uploadDetailLine } from "./uploadOutcome";
import {
  batchSummary,
  describeRefusedDrop,
  nextBatch,
  statusLabel,
  type UploadItem,
} from "./uploadQueue";

/**
 * Upload panel and queue — the drop-zone that turns a document into knowledge.
 *
 * Every file dropped here goes through the **capability flow**: `POST
 * /api/uploads` mints a single-use grant, `PUT /api/uploads/{token}` spends it,
 * and the pipeline on the far side writes the subgraph — an `asset_ref`
 * describing the bytes in one space, a `source` node holding the extracted
 * text, a `derived_from` edge, and one `block` per page. That is the whole
 * reason it is not `POST /api/assets`, which registers bytes and stops: bytes
 * with no describing node are readable by humans and by nobody else, and carry
 * no `node_fts` row, so a document registered that way is invisible to agents
 * and to search (design decision D1).
 *
 * Images included, and deliberately: an image with no OCR handler yields a
 * description and no text, which is exactly what `nodum ingest file <image>`
 * does on the CLI. Branching on the file's type inside one drop-zone would
 * rebuild the split this change removes, one level up where the human cannot
 * see which of the two things happened.
 *
 * Three things this deliberately does *not* do:
 *
 * 1. **Fake a percentage.** `fetch` exposes no upload-progress event, so a
 *    progress bar here would be an animation rather than a measurement. The
 *    queue reports the state each file is actually in instead.
 * 2. **Treat a duplicate as a failure.** Ingestion is idempotent per
 *    `(hash, space)`: re-dropping a document the target space already describes
 *    returns the existing subgraph and writes nothing. That is a non-event, and
 *    it is reported as one — by the server, through `created`, not guessed here.
 * 3. **Offer to resume a grant.** `urls.consume` spends the token *before* the
 *    body is read, so a refused upload has already spent its grant. Dropping
 *    the file again mints a fresh one, which is the only correct retry; there is
 *    no revoke endpoint and nothing to resume.
 *
 * Files are sent one at a time because ingestion holds the single SQLite writer
 * while it registers and writes; a parallel fan-out would queue behind itself
 * with `database is locked` in between. **One batch at a time, for the same
 * reason**: a drop arriving while one is running is refused and said so, not
 * interleaved.
 *
 * The state lives in {@link useAssetUploads} rather than in the component so
 * the view can also accept a drop anywhere on the page; everything about that
 * state a test can hold — the batch, the labels, the announcement, the refusal
 * — lives in `uploadQueue.ts`, because the harness renders nothing.
 */

/** What {@link useAssetUploads} hands back. */
export interface UploadQueue {
  items: UploadItem[];
  /** True while a batch is in flight. */
  busy: boolean;
  /**
   * A drop this queue would not start, in the words the panel shows — or null.
   *
   * Cleared when the next batch actually begins. Refusing silently would lose
   * a drag of ten files with nothing on screen about it.
   */
  refusedDrop: string | null;
  /** Ingest these files, one after another. */
  upload: (files: File[]) => Promise<void>;
  /** Drop the finished queue from the screen. */
  clear: () => void;
}

interface UseAssetUploadsOptions {
  /**
   * The current write target — the space the describing nodes land in.
   *
   * Passed in rather than read from the store here, because D1a is about the
   * value being *rendered*: the caller shows it (see {@link AssetUploader}) and
   * hands the same value to the write.
   */
  writeTarget: string;
  /** Called once after a batch finishes, so the caller can refresh the grid. */
  onIngested: () => void;
}

/**
 * Own the upload queue for the assets view.
 *
 * @param writeTarget The space every file in the next batch is filed into.
 * @param onIngested Fired after a batch, whether or not anything was new.
 */
export function useAssetUploads({ writeTarget, onIngested }: UseAssetUploadsOptions): UploadQueue {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [refusedDrop, setRefusedDrop] = useState<string | null>(null);
  const nextId = useRef(1);

  /**
   * Whether a batch is running, readable **synchronously**.
   *
   * `busy` is state, so two drops in the same tick would both read the stale
   * `false` and both start a loop. The guard has to be a ref: the drop handler
   * is the one place that has to answer "is one already running" before React
   * has re-rendered anything.
   */
  const running = useRef(false);

  // Read at the moment of the drop rather than closed over, so a target changed
  // in another tab mid-batch (the `storage` event) cannot rewrite what an
  // already-queued file was minted against.
  const target = useRef(writeTarget);
  target.current = writeTarget;

  /**
   * Aborted when the view goes away.
   *
   * A batch is a sequence of awaits with a state write after every one, so
   * leaving the view halfway through would otherwise keep writing into a queue
   * readout nobody can see and keep the single SQLite writer occupied for it.
   *
   * **What an abort stops is the readout, and only sometimes the write.** The
   * honest scope, because the two halves of the flow are not alike:
   *
   * - aborting *between* the two requests leaves a minted, unspent grant and
   *   nothing else — no bytes, no nodes. There is nothing to clean up and no
   *   revoke endpoint to invent: the grant is single-use, expires by itself in
   *   minutes, is capped at the exact size declared, is attributed to the human
   *   who minted it, and its secret only ever existed in this tab's memory;
   * - aborting *after the last chunk has left* stops nothing server-side.
   *   `urls.consume` has already spent the token, and the refusal check and
   *   `ingest_upload` then run as synchronous calls with no disconnect check —
   *   so the bytes and the whole subgraph land while this queue has stopped
   *   listening. The human is left with no row, no verdict and no link to
   *   something that exists; `/assets` and search will show it on the next
   *   load, which is how they find out.
   *
   * Closing that second gap means owning the queue **above** the view so a batch
   * survives navigation, which is a larger change than this one and is not made
   * here. Re-dropping the file is safe either way: a fresh grant is minted, and
   * ingestion's `(hash, space)` idempotency means nothing duplicates.
   *
   * Assigned inside the effect, not lazily in render, so a StrictMode remount
   * does not inherit the controller its own cleanup just aborted.
   */
  const lifetime = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    return () => controller.abort();
  }, []);

  const update = useCallback((id: number, patch: Partial<UploadItem>) => {
    if (lifetime.current?.signal.aborted) return;
    setItems((current) => current.map((item) => (item.id === id ? { ...item, ...patch } : item)));
  }, []);

  const upload = useCallback(
    async (files: File[]) => {
      if (files.length === 0) return;
      // A second batch is refused rather than interleaved. Two loops would
      // contend for the single SQLite writer — which ingestion holds for a
      // registration, an extraction and one `create_node` per page — and the
      // first to finish would clear `busy` and refresh the grid while the
      // second was still running.
      if (running.current) {
        setRefusedDrop(describeRefusedDrop(files.length));
        return;
      }
      running.current = true;
      setRefusedDrop(null);

      // One drop is one act, so the whole batch is filed into the target as it
      // stood when the human let go of it.
      const space = target.current;
      const batch = nextBatch(files, space, nextId.current);
      nextId.current = batch.nextId;
      setItems((current) => [...current, ...batch.items]);
      setBusy(true);

      try {
        for (const [position, file] of files.entries()) {
          const signal = lifetime.current?.signal;
          if (signal?.aborted) return;
          const item = batch.items[position];
          if (!item) continue;
          update(item.id, { status: "uploading" });
          try {
            const ingested = await ingestUpload(file, { space }, signal);
            const outcome = readIngestion(ingested);
            update(item.id, { status: outcome.status, outcome });
          } catch (error) {
            if (signal?.aborted) return;
            update(item.id, { status: "failed", error });
          }
        }

        if (lifetime.current?.signal.aborted) return;
        setBusy(false);
        onIngested();
      } finally {
        // Including every early return above: a queue left `running` after an
        // abort would refuse every later drop with nothing running at all.
        running.current = false;
      }
    },
    [onIngested, update],
  );

  const clear = useCallback(() => {
    setItems([]);
    setRefusedDrop(null);
  }, []);

  return { items, busy, refusedDrop, upload, clear };
}

interface AssetUploaderProps {
  /** The queue from {@link useAssetUploads}. */
  queue: UploadQueue;
  /** The current write target, which this panel is required to show (D1a). */
  writeTarget: string;
  /** Active spaces, or null while `GET /api/spaces` has not answered. */
  spaces: readonly NodeOut[] | null;
  /** Archived space nodes, so a retired target is named rather than printed. */
  archivedSpaces: readonly NodeOut[];
}

/**
 * The upload affordance, the write target, and the queue readout.
 *
 * The panel **shows** the write target because it is about to create nodes with
 * it: design decision D1a is explicit that a surface which creates a node
 * displays the current target, and a sticky value the human cannot see is a way
 * to file work into a space nobody chose. It is shown and not offered as a
 * choice here — the picker has one owner, in the editor's meta bar — so this
 * renders the value and names it through the shared `nameSpace`, which is what
 * keeps a space archived from another session from appearing as a 32-hex id.
 *
 * And when that target is one the server will refuse, it **warns before the
 * drop** rather than marking the state and waiting: the other D1a surface facing
 * the identical case says *"Saving will be refused until another space is
 * chosen"* in the editor's meta bar, and a badge alone here let a human drop a
 * whole batch into a target this panel already knew would fail every row. The
 * remedy is a link, not a direction — the picker lives at `/editor`, and views
 * link to each other by URL.
 */
export function AssetUploader({
  queue,
  writeTarget,
  spaces,
  archivedSpaces,
}: AssetUploaderProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const { items, busy, refusedDrop, upload, clear } = queue;
  const settled = items.length > 0 && !busy;
  const targetName = nameSpace(writeTarget, spaces, archivedSpaces);
  const targetNote = spaceNameNote(targetName);
  const summary = batchSummary(items, busy);

  return (
    <section className="nd-uploader" aria-label="Ingest documents">
      <div className="nd-uploader__prompt">
        <p className="nd-uploader__headline">Drop documents anywhere on this page</p>
        <p className="nd-meta">
          Each file is ingested: the bytes are stored under their sha256, and the graph gets a
          node describing them, a <code className="nd-mono">source</code> node holding whatever
          text came out, and one block per page. Dropping the same document into the same space
          again is a no-op, not an error.
        </p>
        <p className="nd-meta nd-uploader__target">
          Filing into{" "}
          <span
            className="nd-mono"
            title={
              targetNote === null
                ? "The write target: where every node this panel creates lands. It sticks across sessions and is chosen in the editor."
                : `${targetName.label}. ${targetNote}`
            }
          >
            {targetName.label}
          </span>
          {targetName.kind === "archived" ? (
            <span className="nd-badge nd-badge--archived">
              <span className="nd-badge__dot" aria-hidden="true" />
              archived
            </span>
          ) : null}
        </p>
        <TargetWarning name={targetName} />
      </div>

      <div className="nd-row nd-uploader__actions">
        <button type="button" className="nd-button" onClick={() => inputRef.current?.click()} disabled={busy}>
          {busy ? <Spinner label="Ingesting" /> : null}
          Choose files
        </button>
        {settled ? (
          <button type="button" className="nd-button nd-button--ghost nd-button--small" onClick={clear}>
            Clear queue
          </button>
        ) : null}
      </div>

      {refusedDrop === null ? null : (
        <p className="nd-uploader__refused" role="alert">
          {refusedDrop}
        </p>
      )}

      {/* One announcement per batch, not one per row transition: a live region
          over the rows carried twenty updates of long prose for a ten-file batch,
          each row also holding a nested `role="status"` spinner. Rendered
          unconditionally and empty, because a live region that appears already
          full is a mount rather than a change, and several readers announce only
          the change. */}
      <p className="nd-sr-only" role="status">
        {summary}
      </p>

      <input
        ref={inputRef}
        name="asset-files"
        type="file"
        multiple
        className="nd-sr-only"
        onChange={(event) => {
          const files = [...(event.target.files ?? [])];
          event.target.value = "";
          void upload(files);
        }}
      />

      {items.length > 0 ? (
        <>
          <ul className="nd-uploader__queue">
            {items.map((item) => (
              <UploadRow
                key={item.id}
                item={item}
                spaces={spaces}
                archivedSpaces={archivedSpaces}
              />
            ))}
          </ul>
          <p className="nd-meta nd-uploader__note">
            No byte-level progress bar: the client uploads through <code>fetch</code>, which
            reports no progress events. Each file&rsquo;s real state is above.
          </p>
        </>
      ) : null}
    </section>
  );
}

/**
 * Warn that the current write target will refuse every drop, or render nothing.
 *
 * Same register as the editor's meta bar, which faces the identical state: name
 * the target, mark it, say that the write will be refused, and point at the one
 * place the value can be changed. `pending` is deliberately silent — a space
 * list still in flight has ruled nothing out, and a warning that appears and
 * vanishes on a healthy file trains the reader to ignore it.
 *
 * @param name The write target, resolved through `nameSpace`.
 */
function TargetWarning({ name }: { name: SpaceName }) {
  if (name.kind !== "archived" && name.kind !== "unknown") return null;
  return (
    <p className="nd-uploader__warning" role="alert">
      {name.label}{" "}
      {name.kind === "archived"
        ? "has been archived, so it is out of every picker. Every drop will be refused until another space is chosen."
        : "is not in the space list — it may have been archived or renamed. Every drop will be refused until another space is chosen."}{" "}
      <Link to="/editor">Choose another write target in the editor</Link>
    </p>
  );
}

/**
 * One queue row: what happened to this file, and where to go and read it.
 *
 * The link is not decoration. A "reviewable subgraph" the human cannot reach is
 * a claim rather than an outcome, so a settled row opens the `source` node the
 * ingestion created — the node that carries the text — in the editor.
 */
function UploadRow({
  item,
  spaces,
  archivedSpaces,
}: {
  item: UploadItem;
  spaces: readonly NodeOut[] | null;
  archivedSpaces: readonly NodeOut[];
}) {
  const landed =
    item.outcome === null || item.outcome.spaceId === null
      ? null
      : nameSpace(item.outcome.spaceId, spaces, archivedSpaces);
  const detail =
    item.status === "failed"
      ? describeUploadFailure(item.error, item.requestedSpace, spaces, archivedSpaces)
      : item.outcome === null
        ? ""
        : uploadDetailLine(item.outcome, landed);
  const label = statusLabel(item.status);

  return (
    <li className={`nd-uploader__item nd-uploader__item--${item.status}`}>
      <span className="nd-uploader__name nd-truncate" title={item.name}>
        {item.name}
      </span>
      <span className="nd-meta">{formatBytes(item.size)}</span>
      <span className="nd-uploader__status">
        {item.status === "uploading" ? (
          <>
            {/* The spinner announces the row's own word, so the visible copy of
                it is hidden from assistive tech rather than read twice. */}
            <Spinner label={label} />
            <span aria-hidden="true">{label}</span>
          </>
        ) : (
          label
        )}
      </span>
      {/* Wraps. The whole readout lives here — a settled line runs to about
          sixty characters and a refusal to a hundred and seventy — and the copy
          this cell exists to get right is the copy an ellipsis would cut. */}
      <span className="nd-uploader__detail">{detail}</span>
      <span className="nd-uploader__open">
        {item.outcome === null ? null : (
          <Link
            to={`/editor/${encodeURIComponent(item.outcome.sourceId)}`}
            // Every row's link said "Open source" and nothing else, which is
            // identical across ten rows and reads as the adjective. Name the
            // file and where it goes; the visible label stays a substring of
            // the accessible name.
            aria-label={`Open source for ${item.name} in the editor`}
          >
            Open source
          </Link>
        )}
      </span>
    </li>
  );
}

export default AssetUploader;
