import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DragEvent as ReactDragEvent } from "react";
import { listAssets, listNodes } from "../../api/client";
import { EmptyState, Spinner, unresolvedSpaceIds, useArchivedSpaces, useSpaces } from "../../components";
import type { AssetOut, NodeOut } from "../../api/types";
import { AssetGrid } from "./AssetGrid";
import { AssetLightbox } from "./AssetLightbox";
import { AssetUploader, useAssetUploads } from "./AssetUploader";
import { describeFailure, useWriteTarget, type FailureDescription } from "../../lib";
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
 *
 * The drop-zone is the other half of the page, and it **creates nodes** — a
 * drop here runs the ingestion pipeline rather than registering bytes. That is
 * why this view reads the space vocabulary at all: decision D1a requires the
 * write target to be on screen wherever it is used, and a target or a landing
 * space the active listing cannot name has to be named rather than printed as a
 * 32-hex id.
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

  const { spaces } = useSpaces();
  const [writeTarget] = useWriteTarget();

  const uploads = useAssetUploads({
    writeTarget,
    onIngested: useCallback(() => void load(), [load]),
  });

  // Everything this screen names, not only what a row happens to show: the
  // write target is the reference that most often points at a space the active
  // listing does not carry, and gating on the queue alone would leave the one
  // control that has to be legible unable to name it. A refused row names the
  // target *it* was minted against, which is not necessarily the current one.
  const namedSpaces = useMemo(
    () =>
      [
        writeTarget,
        ...uploads.items.flatMap((item) => [item.requestedSpace, item.outcome?.spaceId ?? ""]),
      ].filter((reference) => reference !== ""),
    [writeTarget, uploads.items],
  );
  const archivedSpaces = useArchivedSpaces(unresolvedSpaceIds(namedSpaces, spaces).length > 0);

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

  // No `busy` check here: the queue refuses a second concurrent batch itself,
  // which is the only place that can answer synchronously and the only place
  // both entry points (this drop and the panel's file input) go through. A copy
  // of the guard here would be a second owner of the rule, and this one reads a
  // `busy` that React may not have re-rendered yet.
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

      <AssetUploader
        queue={uploads}
        writeTarget={writeTarget}
        spaces={spaces}
        archivedSpaces={archivedSpaces.spaces}
      />

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
          body="Drop a document on this page, or use Choose files. Each one is ingested into the space shown above: the bytes are stored under their sha256 and the graph gets nodes describing them and whatever text came out."
        />
      ) : null}

      {list.status === "ready" && assets.length > 0 ? (
        <AssetGrid assets={assets} onOpen={setOpenIndex} />
      ) : null}

      {dragging ? (
        <div className="nd-assets__drop-overlay" aria-hidden="true">
          <p className="nd-assets__drop-label">Drop to ingest</p>
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
