import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DragEvent as ReactDragEvent } from "react";
import { listAssets, listNodes } from "../../api/client";
import { EmptyState, Spinner } from "../../components";
import type { AssetOut, NodeOut } from "../../api/types";
import { AssetGrid } from "./AssetGrid";
import { AssetLightbox } from "./AssetLightbox";
import { AssetUploader, useAssetUploads } from "./AssetUploader";
import { describeFailure, type FailureDescription } from "../../lib";
import "./assets.css";

/**
 * The asset library — route `/assets`.
 *
 * The constraint that shapes every part of this view is design §5.7: **the
 * original bytes are never served**. Nothing here fetches, links to, or offers
 * to download an original; the only two representations that exist on the wire
 * are the `thumb` and `preview` WebP renditions, and both are reached as an
 * `<img src>` so the browser caches them and the JSON surface never carries
 * image bytes.
 *
 * Renditions are generated lazily and are fully regenerable, so a first view
 * is allowed to be slow — every image here has an explicit loading state and a
 * reserved frame rather than an assumption that it will appear instantly.
 */

/** How many nodes to scan when resolving which notes reference an asset. */
const REFERENCE_SCAN_LIMIT = 500;

/** Loading / loaded / failed for the asset list itself. */
type ListState =
  | { status: "loading" }
  | { status: "ready"; assets: AssetOut[] }
  | { status: "failed"; failure: FailureDescription };

export default function AssetsView() {
  const [list, setList] = useState<ListState>({ status: "loading" });
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);
  const dragDepth = useRef(0);

  const assets = list.status === "ready" ? list.assets : [];

  /**
   * Aborted when the view goes away.
   *
   * Most loads here are fired from a button — Refresh, Try again, and the reload
   * after an upload batch — not from an effect, so without a signal that outlives
   * the call site there is nothing to stop them writing state into an unmounted
   * view. Assigned inside the mount effect rather than lazily in render so a
   * StrictMode remount gets a fresh, un-aborted controller.
   */
  const lifetime = useRef<AbortController | null>(null);

  const load = useCallback((signal?: AbortSignal) => {
    const active = signal ?? lifetime.current?.signal;
    setList((current) => (current.status === "ready" ? current : { status: "loading" }));
    return listAssets(active)
      .then((loaded) => {
        if (active?.aborted) return;
        setList({ status: "ready", assets: loaded });
      })
      .catch((error: unknown) => {
        if (active?.aborted) return;
        setList({ status: "failed", failure: describeFailure(error, "the asset library") });
      });
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  // Captured in a ref so the upload queue's dedup check always sees the
  // hashes as they were *before* the batch, without re-creating the callback.
  const knownHashes = useRef<Set<string>>(new Set());
  knownHashes.current = useMemo(() => new Set(assets.map((asset) => asset.hash)), [assets]);

  const uploads = useAssetUploads({
    isKnownHash: useCallback((hash: string) => knownHashes.current.has(hash), []),
    onRegistered: useCallback(() => void load(), [load]),
  });

  const { references, referencesLoading } = useAssetReferences(openIndex !== null);

  /* --- Drag and drop over the whole view ------------------------------ */

  const carriesFiles = (event: ReactDragEvent) =>
    [...event.dataTransfer.types].includes("Files");

  const onDragEnter = (event: ReactDragEvent) => {
    if (!carriesFiles(event)) return;
    dragDepth.current += 1;
    setDragging(true);
  };

  const onDragLeave = (event: ReactDragEvent) => {
    if (!carriesFiles(event)) return;
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragging(false);
  };

  const onDrop = (event: ReactDragEvent) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    void uploads.upload([...event.dataTransfer.files]);
  };

  /* --- Lightbox ------------------------------------------------------- */

  const navigate = useCallback(
    (delta: number) =>
      setOpenIndex((current) => {
        if (current === null) return current;
        const next = current + delta;
        return next < 0 || next >= assets.length ? current : next;
      }),
    [assets.length],
  );

  const close = useCallback(() => setOpenIndex(null), []);

  const openAsset = openIndex !== null ? assets[openIndex] : undefined;

  return (
    <div
      className={dragging ? "nd-view nd-assets nd-assets--dragging" : "nd-view nd-assets"}
      onDragEnter={onDragEnter}
      onDragOver={(event) => {
        if (carriesFiles(event)) event.preventDefault();
      }}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      <header className="nd-view__header">
        <div>
          <h1>Assets</h1>
          <p className="nd-meta">
            Content-addressed binaries stored in the database. Only the derived{" "}
            <code className="nd-mono">thumb</code> and <code className="nd-mono">preview</code>{" "}
            renditions are served — originals never leave the database.
          </p>
        </div>
        <div className="nd-row">
          {list.status === "ready" ? (
            <span className="nd-meta">
              {assets.length} {assets.length === 1 ? "asset" : "assets"}
            </span>
          ) : null}
          <button
            type="button"
            className="nd-button nd-button--small"
            onClick={() => void load()}
            disabled={list.status === "loading"}
          >
            Refresh
          </button>
        </div>
      </header>

      <AssetUploader queue={uploads} />

      {list.status === "loading" ? (
        <div className="nd-empty">
          <Spinner large label="Loading assets" />
        </div>
      ) : null}

      {list.status === "failed" ? (
        <EmptyState
          title={list.failure.title}
          body={list.failure.body}
          action={
            <button type="button" className="nd-button" onClick={() => void load()}>
              Try again
            </button>
          }
        />
      ) : null}

      {list.status === "ready" && assets.length === 0 ? (
        <EmptyState
          title="No assets registered"
          body="Drop an image on this page, or use Choose files. Files are stored under their sha256, so the same file registered twice is stored once."
        />
      ) : null}

      {list.status === "ready" && assets.length > 0 ? (
        <AssetGrid assets={assets} onOpen={setOpenIndex} />
      ) : null}

      {dragging ? (
        <div className="nd-assets__drop-overlay" aria-hidden="true">
          <p className="nd-assets__drop-label">Drop to register</p>
        </div>
      ) : null}

      {openIndex !== null && openAsset ? (
        <AssetLightbox
          assets={assets}
          index={openIndex}
          onClose={close}
          onNavigate={navigate}
          referencingNodes={references.get(openAsset.hash) ?? []}
          referencesLoading={referencesLoading}
        />
      ) : null}
    </div>
  );
}

/**
 * Resolve which nodes reference each asset, once, on first need.
 *
 * An asset-reference node carries the asset's sha256 in `props.asset_hash`
 * (`assets._resolve_hash`), and there is no server-side "who references this
 * hash" query, so the mapping is built client-side from one bounded node list.
 * It is an enhancement to the lightbox, not a requirement of it: if the call
 * fails the section simply reports nothing found.
 *
 * @param needed Whether the lightbox is open and the mapping is wanted.
 */
function useAssetReferences(needed: boolean) {
  const [references, setReferences] = useState<Map<string, NodeOut[]>>(new Map());
  const [referencesLoading, setLoading] = useState(false);
  /**
   * Whether the scan has *landed*, not whether one was ever started.
   *
   * Latching on the attempt wedges the feature: closing the lightbox aborts the
   * scan, so the spinner it left behind is never turned off, and the latch then
   * refuses to run another one — so every later open shows a permanent
   * "looking…" over a lookup that will never happen again.
   */
  const resolved = useRef(false);

  useEffect(() => {
    if (!needed || resolved.current) return;
    const controller = new AbortController();
    setLoading(true);
    listNodes({ limit: REFERENCE_SCAN_LIMIT }, controller.signal)
      .then((nodes) => {
        if (controller.signal.aborted) return;
        const byHash = new Map<string, NodeOut[]>();
        for (const node of nodes) {
          const hash = node.props["asset_hash"];
          if (typeof hash !== "string") continue;
          const bucket = byHash.get(hash);
          if (bucket) bucket.push(node);
          else byHash.set(hash, [node]);
        }
        resolved.current = true;
        setReferences(byHash);
      })
      .catch(() => {
        // Non-essential: an empty map renders as "no node references this".
      })
      .finally(() => {
        // An aborted scan leaves nothing on screen to spin for, but the flag
        // still has to come down: the next open re-runs the effect and would
        // otherwise inherit a `true` it never set.
        setLoading(false);
      });
    return () => controller.abort();
  }, [needed]);

  return { references, referencesLoading };
}

export { AssetsView };
