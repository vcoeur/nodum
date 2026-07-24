/**
 * Formatting helpers local to the assets and history views.
 *
 * Timestamp parsing and failure classification used to live here too; both were
 * needed by every view and now live in `src/lib/`. What is left is asset-shaped:
 * byte sizes, sha256 abbreviation, and the mime test that decides whether a
 * rendition can exist at all.
 */

/** Binary units, largest first, so the loop can stop at the first that fits. */
const BYTE_UNITS = ["GB", "MB", "kB"] as const;
const BYTE_STEPS = [1024 ** 3, 1024 ** 2, 1024] as const;

/**
 * Render a byte count in the largest unit that keeps the number under 1024.
 *
 * @param bytes Size in bytes.
 * @returns e.g. `"1.4 MB"`, or `"872 B"` below a kilobyte.
 */
export function formatBytes(bytes: number): string {
  for (let index = 0; index < BYTE_STEPS.length; index += 1) {
    const step = BYTE_STEPS[index] as number;
    if (bytes >= step) {
      const value = bytes / step;
      return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${BYTE_UNITS[index]}`;
    }
  }
  return `${bytes} B`;
}

/**
 * Shorten a sha256 to something recognisable at a glance.
 *
 * The full hash is always available in the metadata panel; this is for a
 * caption where 64 hex characters would be noise.
 */
export function shortHash(hash: string): string {
  return hash.length > 14 ? `${hash.slice(0, 12)}…` : hash;
}

/**
 * Whether an asset's mime type is one the rendition pipeline can rasterise.
 *
 * Renditions are image-only by design (§5.7): a non-image asset gets a typed
 * entry and no `<img>` is ever pointed at it, so the UI never asks the server
 * for a rendition it will refuse.
 */
export function isImageMime(mime: string): boolean {
  return mime.startsWith("image/");
}
