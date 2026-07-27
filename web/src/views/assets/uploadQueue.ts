/**
 * The queue's own bookkeeping, out of the hook so it can be tested at all.
 *
 * `uploadOutcome.ts` is what a *settled* row says; this is what the queue does
 * before and around that — how a drop becomes rows, what each status is called,
 * what a refused second drop says, and what one announcement per batch contains.
 * All four lived inside {@link useAssetUploads} or its component, where the
 * unit-only harness renders nothing and therefore proves nothing, and two of
 * them are load-bearing: a batch that mislabels which target a row was minted
 * against files the refusal copy against the wrong space, and a label map that
 * is not total over the status union renders `undefined` in a cell.
 *
 * Nothing here touches React or the network, deliberately.
 */

import type { UploadOutcome, UploadStatus } from "./uploadOutcome";

/** One file's journey through ingestion. */
export interface UploadItem {
  id: number;
  name: string;
  size: number;
  status: UploadStatus;
  /** The write target this file was minted against, for the refusal copy. */
  requestedSpace: string;
  /** What ingestion reported, once it settled. */
  outcome: UploadOutcome | null;
  /**
   * The caught failure, kept **raw**.
   *
   * Its copy needs the space lists, which arrive on their own schedule, so it
   * is resolved at render rather than frozen into a string at settle time — the
   * archived-space read in particular usually answers *after* the failure that
   * needed it.
   */
  error: unknown;
}

/**
 * What each status says on screen.
 *
 * A `Record` over the union rather than a lookup with a fallback: a status added
 * to `UPLOAD_STATUSES` fails the build here until it has a label, and that same
 * array is what a test walks to prove nothing renders blank.
 */
const STATUS_LABEL: Record<UploadStatus, string> = {
  queued: "waiting",
  uploading: "uploading",
  ingested: "ingested",
  "already-ingested": "already ingested",
  failed: "failed",
};

/**
 * The word a row's status cell shows.
 *
 * @param status The row's status.
 * @returns The label; never empty, and never `undefined`.
 */
export function statusLabel(status: UploadStatus): string {
  return STATUS_LABEL[status];
}

/** Queue rows for one drop, plus the id the next drop starts from. */
export interface NextBatch {
  items: UploadItem[];
  /** The first id not used by this batch. */
  nextId: number;
}

/**
 * Turn one drop into queue rows.
 *
 * **One drop is one act**, so every row carries the write target as it stood
 * when the human let go — not the current value. The target is app-wide, sticky
 * and synchronised across tabs, so a `storage` event mid-batch can change it
 * under a queue that has already minted against the old one, and a refusal
 * described against the new one would name a space that never refused anything.
 *
 * @param files The dropped files, in the order they were handed over.
 * @param target The write target every file in this batch is filed into.
 * @param firstId The first id to hand out; ids are unique per queue, not per
 *   batch, because they are React keys over a list batches append to.
 * @returns The rows, and the next free id.
 */
export function nextBatch(
  files: readonly File[],
  target: string,
  firstId: number,
): NextBatch {
  const items = files.map((file, offset) => ({
    id: firstId + offset,
    name: file.name,
    size: file.size,
    status: "queued" as UploadStatus,
    requestedSpace: target,
    outcome: null,
    error: null,
  }));
  return { items, nextId: firstId + files.length };
}

/**
 * What the panel says about a drop it would not start.
 *
 * The second concurrent batch is refused rather than interleaved, and it has to
 * be *said*: files vanishing on a drop with nothing on screen about it is worse
 * than the two loops it prevents. Two loops would also both call `setBusy(false)`
 * and refresh the grid at the first one's finish while the second was still
 * running, and would contend for the single SQLite writer — which ingestion now
 * holds for a registration, an extraction and one `create_node` per page, so
 * `database is locked` becomes a realistic per-row outcome rather than a
 * theoretical one.
 *
 * @param files How many files the refused drop carried.
 * @returns One sentence for the panel's alert.
 */
export function describeRefusedDrop(files: number): string {
  const subject = files === 1 ? "That file was" : `Those ${files} files were`;
  return (
    `${subject} not queued: a batch is already running. Ingestion holds the single database ` +
    "writer while it works, so one batch runs at a time — drop them again once this one finishes."
  );
}

/**
 * The one thing worth announcing about the queue, for a screen reader.
 *
 * A live region over the rows themselves announced every status change of every
 * row — twenty updates of long prose for a ten-file batch, each row also holding
 * a nested `role="status"` spinner. What a human wants from a batch is that it
 * started and how it ended, so that is what this says, and it says it in two
 * stable strings: the count cannot change mid-batch, because a drop arriving
 * during one is refused.
 *
 * @param items Every row in the queue.
 * @param busy Whether a batch is in flight.
 * @returns The announcement, or `""` for an empty queue — which announces
 *   nothing rather than announcing emptiness.
 */
export function batchSummary(items: readonly UploadItem[], busy: boolean): string {
  if (items.length === 0) return "";
  const files = `${items.length} ${items.length === 1 ? "file" : "files"}`;
  if (busy) return `Ingesting ${files}.`;

  const counted = (status: UploadStatus) =>
    items.filter((item) => item.status === status).length;
  const parts = [
    { count: counted("ingested"), label: "ingested" },
    { count: counted("already-ingested"), label: "already ingested" },
    { count: counted("failed"), label: "failed" },
  ]
    .filter((part) => part.count > 0)
    .map((part) => `${part.count} ${part.label}`);

  // Every row of a finished batch is in one of those three, so an empty list
  // means the batch never ran its loop — an abort, or a drop of nothing.
  if (parts.length === 0) return `${files} queued.`;
  return `Finished ${files}: ${parts.join(", ")}.`;
}
