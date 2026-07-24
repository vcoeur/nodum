import { useCallback, useRef, useState } from "react";
import { uploadAsset } from "../../api/client";
import { Spinner } from "../../components";
import { describeFailure } from "../../lib";
import { formatBytes } from "./formatting";

/**
 * Upload panel and queue.
 *
 * Two things this deliberately does *not* do:
 *
 * 1. **Fake a percentage.** `uploadAsset` goes through `fetch` + `FormData`,
 *    which exposes no upload-progress event, so a progress bar here would be
 *    an animation rather than a measurement. The queue reports the state each
 *    file is actually in instead.
 * 2. **Treat a duplicate as a failure.** Registration is idempotent sha256
 *    dedup: re-uploading identical bytes returns the existing asset and writes
 *    nothing. That is a non-event, and it is reported as one.
 *
 * Files are sent one at a time because a registration streams into the single
 * SQLite writer and holds it for the duration; a parallel fan-out would just
 * queue behind itself with `database is locked` in between.
 *
 * The state lives in {@link useAssetUploads} rather than in the component so
 * the view can also accept a drop anywhere on the page.
 */

/** One file's journey through registration. */
export interface UploadItem {
  id: number;
  name: string;
  size: number;
  status: "queued" | "uploading" | "registered" | "duplicate" | "failed";
  /** The asset hash on success, or the failure message. */
  detail: string | null;
}

/** What each status says on screen. */
const STATUS_LABEL: Record<UploadItem["status"], string> = {
  queued: "waiting",
  uploading: "uploading",
  registered: "registered",
  duplicate: "already registered",
  failed: "failed",
};

/** What {@link useAssetUploads} hands back. */
export interface UploadQueue {
  items: UploadItem[];
  /** True while a batch is in flight. */
  busy: boolean;
  /** Register these files, one after another. */
  upload: (files: File[]) => Promise<void>;
  /** Drop the finished queue from the screen. */
  clear: () => void;
}

interface UseAssetUploadsOptions {
  /** True when the hash is already in the loaded list — i.e. a dedup hit. */
  isKnownHash: (hash: string) => boolean;
  /** Called once after a batch finishes, so the caller can refresh the grid. */
  onRegistered: () => void;
}

/**
 * Own the upload queue for the assets view.
 *
 * @param isKnownHash Lets the queue tell a fresh registration from a dedup hit.
 * @param onRegistered Fired after a batch, whether or not anything was new.
 */
export function useAssetUploads({ isKnownHash, onRegistered }: UseAssetUploadsOptions): UploadQueue {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [busy, setBusy] = useState(false);
  const nextId = useRef(1);

  const update = useCallback((id: number, patch: Partial<UploadItem>) => {
    setItems((current) => current.map((item) => (item.id === id ? { ...item, ...patch } : item)));
  }, []);

  const upload = useCallback(
    async (files: File[]) => {
      if (files.length === 0) return;
      const queued: UploadItem[] = files.map((file) => ({
        id: nextId.current++,
        name: file.name,
        size: file.size,
        status: "queued",
        detail: null,
      }));
      setItems((current) => [...current, ...queued]);
      setBusy(true);

      // The grid only reloads after the batch, so hashes registered *within*
      // this batch would otherwise read as new the second time they appear.
      const registeredHere = new Set<string>();

      for (const [position, file] of files.entries()) {
        const item = queued[position];
        if (!item) continue;
        update(item.id, { status: "uploading" });
        try {
          const asset = await uploadAsset(file);
          const duplicate = isKnownHash(asset.hash) || registeredHere.has(asset.hash);
          registeredHere.add(asset.hash);
          update(item.id, {
            status: duplicate ? "duplicate" : "registered",
            detail: asset.hash,
          });
        } catch (error) {
          update(item.id, { status: "failed", detail: describeFailure(error, "this upload").body });
        }
      }

      setBusy(false);
      onRegistered();
    },
    [isKnownHash, onRegistered, update],
  );

  const clear = useCallback(() => setItems([]), []);

  return { items, busy, upload, clear };
}

/**
 * The upload affordance and the queue readout.
 *
 * @param queue The queue from {@link useAssetUploads}.
 */
export function AssetUploader({ queue }: { queue: UploadQueue }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const { items, busy, upload, clear } = queue;
  const settled = items.length > 0 && !busy;

  return (
    <section className="nd-uploader" aria-label="Upload assets">
      <div className="nd-uploader__prompt">
        <p className="nd-uploader__headline">Drop files anywhere on this page</p>
        <p className="nd-meta">
          Bytes are stored in the database under their sha256. Re-registering a file that is
          already there is a no-op, not an error.
        </p>
      </div>

      <div className="nd-row nd-uploader__actions">
        <button type="button" className="nd-button" onClick={() => inputRef.current?.click()} disabled={busy}>
          {busy ? <Spinner label="Uploading" /> : null}
          Choose files
        </button>
        {settled ? (
          <button type="button" className="nd-button nd-button--ghost nd-button--small" onClick={clear}>
            Clear queue
          </button>
        ) : null}
      </div>

      <input
        ref={inputRef}
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
          <ul className="nd-uploader__queue" aria-live="polite">
            {items.map((item) => (
              <li key={item.id} className={`nd-uploader__item nd-uploader__item--${item.status}`}>
                <span className="nd-uploader__name nd-truncate" title={item.name}>
                  {item.name}
                </span>
                <span className="nd-meta">{formatBytes(item.size)}</span>
                <span className="nd-uploader__status">
                  {item.status === "uploading" ? <Spinner label="Uploading" /> : null}
                  {STATUS_LABEL[item.status]}
                </span>
                <span className="nd-uploader__detail nd-mono" title={item.detail ?? undefined}>
                  {item.detail === null
                    ? ""
                    : item.status === "failed"
                      ? item.detail
                      : `${item.detail.slice(0, 12)}…`}
                </span>
              </li>
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

export default AssetUploader;
