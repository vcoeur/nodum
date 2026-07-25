import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { renditionUrl } from "../../api/client";
import { Spinner } from "../../components";
import type { AssetOut, NodeOut } from "../../api/types";
import { ExportJsonButton } from "./ExportJsonButton";
import { formatTimestamp, formatTimestampLong } from "../../lib";
import { formatBytes, isImageMime } from "./formatting";

/**
 * The asset lightbox: one asset, full metadata, keyboard-driven.
 *
 * Design §5.7 is the whole shape of this component. **Originals are never
 * served**, so there is no "view original", no download link, and no URL in
 * here that could resolve to the stored bytes — the only image the lightbox
 * can point at is the derived `preview` rendition, and a non-image asset gets
 * a typed panel instead of an `<img>` that would 400.
 *
 * Renditions are generated lazily on first request, so the first open of an
 * asset can take seconds while Pillow works. That is a loading state, not a
 * hang, and it is shown as one.
 *
 * ## Keyboard behaviour follows `views/review/Modal.tsx`
 *
 * That component is the reference for what a dialog in this app does, and three
 * of its decisions are load-bearing here for reasons the lightbox makes worse
 * rather than better:
 *
 * - **Escape is bound on `document`, not on the dialog element.** A React
 *   `onKeyDown` only fires while focus is inside the subtree, so the moment
 *   anything drops focus out of it the dialog becomes unclosable by keyboard.
 *   This one has an everyday way to do exactly that: the step buttons are
 *   `disabled` at the ends of the list, so arrowing to the last asset disables
 *   the button the pointer just used and the browser hands focus to `<body>`.
 * - **Focus is taken back when it leaves.** Same cause; the trap cannot help,
 *   because a trap only sees keys that reach it.
 * - **Focus is restored only to an element still in the document.** Registering
 *   an upload reloads the grid, so the tile that opened the lightbox is usually
 *   detached by the time it closes, and focusing a detached node silently drops
 *   focus to `<body>` — worse than leaving it where it is.
 */

/** Everything inside the dialog that a Tab can land on. */
const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "select:not([disabled])",
  "input:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

interface AssetLightboxProps {
  /** The full list, so the lightbox can walk it without refetching. */
  assets: AssetOut[];
  /** Index into {@link assets} of the asset on screen. */
  index: number;
  /** Close and return focus to whatever opened the lightbox. */
  onClose: () => void;
  /** Move by `delta` assets; the caller clamps. */
  onNavigate: (delta: number) => void;
  /** Nodes carrying this asset's hash in their props, if any are known yet. */
  referencingNodes: NodeOut[];
  /** True while the referencing-node lookup is in flight. */
  referencesLoading: boolean;
}

/**
 * Render the lightbox for one asset.
 *
 * Keyboard contract: Escape closes, Left/Right move between assets, Tab is
 * trapped inside the dialog, and focus returns to the opener on close.
 */
export function AssetLightbox({
  assets,
  index,
  onClose,
  onNavigate,
  referencingNodes,
  referencesLoading,
}: AssetLightboxProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const asset = assets[index];
  // `assets` can empty out under an open lightbox — a Refresh landing on a
  // library whose last asset is gone. The component then renders null, so every
  // effect below has to be gated on the same condition rather than locking the
  // page's scroll for a dialog that is not on screen.
  const open = asset !== undefined;

  /** Where focus was before the dialog took it, so close can hand it back. */
  const restoreTo = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    // In an effect, not during render: render can run without committing, and
    // React may run it more than once, either of which captures the wrong
    // element — usually the dialog itself on the second pass.
    restoreTo.current = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();

    // The page behind a modal must not scroll under it.
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    return () => {
      document.body.style.overflow = previousOverflow;
      // Only if it still exists: focusing a detached node drops focus onto
      // `<body>`, which is worse than leaving it for the view to place.
      if (restoreTo.current?.isConnected) restoreTo.current.focus();
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        onNavigate(-1);
        return;
      }
      if (event.key === "ArrowRight") {
        event.preventDefault();
        onNavigate(1);
        return;
      }
      if (event.key !== "Tab") return;

      // Focus trap: wrap at both ends rather than letting Tab escape to the
      // page behind, which a screen-reader user cannot see is still there.
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = [...dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)].filter(
        (element) => element.offsetParent !== null || element === document.activeElement,
      );
      const active = document.activeElement;
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0] as HTMLElement;
      const last = focusable[focusable.length - 1] as HTMLElement;
      // Focus already outside the portal — a disabled step button, or a stray
      // click on the page behind. Tab is the moment to pull it back in.
      if (active === null || !dialog.contains(active)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
        return;
      }
      if (event.shiftKey && (active === first || active === dialog)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, onClose, onNavigate]);

  useEffect(() => {
    if (!open) return;
    // Stepping to either end disables the button that was just activated, and
    // the browser answers by focusing `<body>`. `aria-modal="true"` promises
    // that cannot happen, so the dialog takes focus back.
    const dialog = dialogRef.current;
    if (dialog && !dialog.contains(document.activeElement)) dialog.focus();
  }, [open, index]);

  if (!asset) return null;

  const label = asset.original_name ?? asset.hash;

  return createPortal(
    <div
      className="nd-modal-backdrop nd-lightbox-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        className="nd-lightbox"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <header className="nd-lightbox__header">
          <div className="nd-lightbox__heading">
            <h2 id={titleId} className="nd-lightbox__title nd-truncate" title={label}>
              {label}
            </h2>
            <p className="nd-meta">
              {index + 1} of {assets.length} · {asset.mime}
            </p>
          </div>
          <button
            type="button"
            className="nd-button nd-button--ghost nd-button--small"
            onClick={onClose}
            aria-label="Close the lightbox"
          >
            Close
          </button>
        </header>

        <div className="nd-lightbox__body">
          <div className="nd-lightbox__stage">
            <button
              type="button"
              className="nd-lightbox__step"
              onClick={() => onNavigate(-1)}
              disabled={index === 0}
              aria-label="Previous asset"
            >
              ‹
            </button>
            <AssetPreview key={asset.hash} asset={asset} />
            <button
              type="button"
              className="nd-lightbox__step"
              onClick={() => onNavigate(1)}
              disabled={index === assets.length - 1}
              aria-label="Next asset"
            >
              ›
            </button>
          </div>

          <aside className="nd-lightbox__meta" aria-label="Asset metadata">
            <AssetMetadata asset={asset} />
            <ReferencingNodes
              nodes={referencingNodes}
              loading={referencesLoading}
              assetHash={asset.hash}
            />
          </aside>
        </div>

        <footer className="nd-lightbox__footer">
          <p className="nd-meta">
            <kbd>←</kbd> <kbd>→</kbd> move · <kbd>Esc</kbd> closes
          </p>
        </footer>
      </div>
    </div>,
    document.body,
  );
}

/** Whether the preview rendition has arrived, and how big it turned out. */
type PreviewState =
  | { status: "loading" }
  | { status: "ready"; width: number; height: number }
  | { status: "failed" };

/**
 * The preview rendition, or a typed panel when there can never be one.
 *
 * The dimensions reported here are the *rendition's* (≤1024px), never the
 * original's — the UI has no way to learn the original's size and should not
 * pretend otherwise.
 */
function AssetPreview({ asset }: { asset: AssetOut }) {
  const [state, setState] = useState<PreviewState>({ status: "loading" });

  if (!isImageMime(asset.mime)) {
    return (
      <div className="nd-lightbox__frame nd-lightbox__frame--typed">
        <p className="nd-lightbox__glyph" aria-hidden="true">
          ▤
        </p>
        <p className="nd-lightbox__typed-title">No preview for {asset.mime}</p>
        <p className="nd-meta nd-lightbox__typed-body">
          nodum only serves derived image renditions, and this asset is not a raster image. The
          original bytes stay in the database and are never served.
        </p>
      </div>
    );
  }

  return (
    <div className="nd-lightbox__frame">
      {state.status === "loading" ? (
        <div className="nd-lightbox__pending">
          <Spinner large label="Loading the preview rendition" />
          <p className="nd-meta">
            Building the preview — renditions are generated on first view, so this one may take a
            moment.
          </p>
        </div>
      ) : null}

      {state.status === "failed" ? (
        <div className="nd-lightbox__pending">
          <p className="nd-lightbox__typed-title">Preview unavailable</p>
          <p className="nd-meta nd-lightbox__typed-body">
            The server could not produce a <code>preview</code> rendition for this asset. Renditions
            are fully regenerable, so this is safe to retry — the original is untouched, and is
            never served either way.
          </p>
        </div>
      ) : null}

      <img
        className="nd-lightbox__image"
        src={renditionUrl(asset.hash, "preview")}
        alt={asset.original_name ?? `Asset ${asset.hash}`}
        data-status={state.status}
        onLoad={(event) =>
          setState({
            status: "ready",
            width: event.currentTarget.naturalWidth,
            height: event.currentTarget.naturalHeight,
          })
        }
        onError={() => setState({ status: "failed" })}
      />

      {state.status === "ready" ? (
        <p className="nd-lightbox__dimensions nd-meta">
          preview rendition · {state.width} × {state.height}
        </p>
      ) : null}
    </div>
  );
}

/** The metadata panel: everything the server will tell us about the asset. */
function AssetMetadata({ asset }: { asset: AssetOut }) {
  return (
    <dl className="nd-lightbox__dl">
      <dt>sha256</dt>
      <dd className="nd-mono nd-lightbox__hash">{asset.hash}</dd>

      <dt>mime</dt>
      <dd className="nd-mono">{asset.mime}</dd>

      <dt>size</dt>
      <dd>
        {formatBytes(asset.size_bytes)}{" "}
        <span className="nd-meta">({asset.size_bytes.toLocaleString()} bytes)</span>
      </dd>

      <dt>original name</dt>
      <dd className="nd-truncate" title={asset.original_name ?? undefined}>
        {asset.original_name ?? <span className="nd-meta">none recorded</span>}
      </dd>

      <dt>registered</dt>
      <dd title={formatTimestampLong(asset.created_at)}>{formatTimestamp(asset.created_at)}</dd>

      <dt>extracted text</dt>
      <dd className="nd-meta">
        {asset.extracted_text ? `${asset.extracted_text.length} characters` : "none — Phase 4"}
      </dd>
    </dl>
  );
}

/**
 * Nodes that reference this asset, and the export action for each.
 *
 * This is the "node context" the thin JSON export hangs off in the asset view:
 * an asset is not a node and cannot be exported, but the note that embeds it
 * can be.
 */
function ReferencingNodes({
  nodes,
  loading,
  assetHash,
}: {
  nodes: NodeOut[];
  loading: boolean;
  assetHash: string;
}) {
  return (
    <section className="nd-lightbox__refs">
      <h3 className="nd-label">Referenced by</h3>
      {loading ? (
        <p className="nd-meta">
          <Spinner label="Looking for referencing nodes" /> looking…
        </p>
      ) : nodes.length === 0 ? (
        <p className="nd-meta">
          No node carries this asset&rsquo;s hash in its props yet. Drop it into a note in the
          editor to attach it.
        </p>
      ) : (
        <ul className="nd-lightbox__ref-list">
          {nodes.map((node) => (
            <li key={node.id} className="nd-lightbox__ref">
              <div className="nd-lightbox__ref-head">
                <Link to={`/editor/${encodeURIComponent(node.id)}`} className="nd-truncate">
                  {node.title ?? node.id}
                </Link>
                <Link
                  to={`/history/${encodeURIComponent(node.id)}`}
                  className="nd-lightbox__ref-history"
                >
                  history
                </Link>
              </div>
              <ExportJsonButton nodeId={node.id} compact />
            </li>
          ))}
        </ul>
      )}
      <p className="nd-meta nd-lightbox__refs-note">
        Matched on <code className="nd-mono">props.asset_hash</code> ={" "}
        <code className="nd-mono">{assetHash.slice(0, 12)}…</code>
      </p>
    </section>
  );
}

export default AssetLightbox;
