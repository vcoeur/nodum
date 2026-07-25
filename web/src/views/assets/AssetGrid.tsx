import { useState } from "react";
import { renditionUrl } from "../../api/client";
import { Spinner } from "../../components";
import type { AssetOut } from "../../api/types";
import { formatBytes, isImageMime, shortHash } from "./formatting";

/**
 * The rendition grid.
 *
 * Every tile reserves its frame with a fixed aspect ratio *before* the image
 * arrives, so the grid never reflows as thumbnails land — the whole point of
 * lazily generated renditions is that the first pass is slow, and a grid that
 * jumps under the cursor while it fills is unusable.
 *
 * Thumbnails are `<img src={renditionUrl(...)}>`, never a fetch: the browser
 * gets to cache them, and the derived WebP is the only representation of an
 * asset the server will ever hand out (design §5.7).
 */

interface AssetGridProps {
  assets: AssetOut[];
  /** Open the lightbox at this index. */
  onOpen: (index: number) => void;
}

/** Render every asset as a keyboard-reachable tile. */
export function AssetGrid({ assets, onOpen }: AssetGridProps) {
  return (
    <ul className="nd-asset-grid">
      {assets.map((asset, index) => (
        <li key={asset.hash}>
          <AssetTile asset={asset} onOpen={() => onOpen(index)} />
        </li>
      ))}
    </ul>
  );
}

/** Whether a tile's thumbnail has arrived. */
type ThumbState = "loading" | "ready" | "failed";

/** One asset: its thumb rendition, name, and size. */
function AssetTile({ asset, onOpen }: { asset: AssetOut; onOpen: () => void }) {
  const [state, setState] = useState<ThumbState>("loading");
  const image = isImageMime(asset.mime);
  const label = asset.original_name ?? shortHash(asset.hash);

  return (
    <button
      type="button"
      className="nd-asset-tile"
      onClick={onOpen}
      aria-label={`Open ${label} — ${asset.mime}, ${formatBytes(asset.size_bytes)}`}
    >
      <span className="nd-asset-tile__frame">
        {!image ? (
          <span className="nd-asset-tile__typed">
            <span className="nd-asset-tile__glyph" aria-hidden="true">
              ▤
            </span>
            <span className="nd-asset-tile__typed-label nd-mono">{asset.mime}</span>
          </span>
        ) : (
          <>
            {state === "loading" ? (
              <span className="nd-asset-tile__pending">
                <Spinner label="Loading thumbnail" />
              </span>
            ) : null}
            {state === "failed" ? (
              <span className="nd-asset-tile__typed">
                <span className="nd-asset-tile__glyph" aria-hidden="true">
                  ⃠
                </span>
                <span className="nd-asset-tile__typed-label nd-mono">no rendition</span>
              </span>
            ) : null}
            <img
              className="nd-asset-tile__image"
              src={renditionUrl(asset.hash, "thumb")}
              alt=""
              loading="lazy"
              decoding="async"
              data-status={state}
              onLoad={() => setState("ready")}
              onError={() => setState("failed")}
            />
          </>
        )}
      </span>
      <span className="nd-asset-tile__name nd-truncate">{label}</span>
      <span className="nd-asset-tile__meta nd-meta">
        {formatBytes(asset.size_bytes)} · {asset.mime}
      </span>
    </button>
  );
}

export default AssetGrid;
