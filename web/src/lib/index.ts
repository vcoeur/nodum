/**
 * Cross-view helpers: things more than one view needs and none of them owns.
 *
 * `src/components/` holds shared *React* components; this directory holds the
 * plain functions — timestamp parsing and failure classification — that every
 * view has to get right the same way. Import from `../../lib`.
 *
 * `writeTarget.ts` is the one module here that also exports a hook. It owns a
 * single piece of app-wide state that decision D1a requires every node-create
 * surface to *display*, and the store plus the one line that renders it belong
 * together: three views each wiring their own `useSyncExternalStore` is three
 * chances for one of them to show a stale target.
 */

export {
  formatAbsolute,
  formatRelative,
  formatTimestamp,
  formatTimestampLong,
  parseTimestamp,
  timestampMs,
} from "./time";

export {
  describeError,
  describeFailure,
  isNotFound,
  isUnreachable,
} from "./failure";
export type { FailureDescription, FailureKind } from "./failure";

export { pageWindow } from "./paging";
export type { PageWindow } from "./paging";

export {
  actionForResolution,
  attachWikilinkClicks,
  titleFromWikilinkHref,
  wikilinkHref,
  WIKILINK_TITLE_PATH,
} from "./wikilinks";
export type { WikilinkAction } from "./wikilinks";

export { onUnauthorized, reportUnauthorized } from "./session";
export type { UnauthorizedListener } from "./session";

export {
  clearWriteTarget,
  DEFAULT_WRITE_TARGET,
  getWriteTarget,
  onWriteTargetChange,
  setWriteTarget,
  useWriteTarget,
  WRITE_TARGET_STORAGE_KEY,
} from "./writeTarget";
export type { WriteTargetListener } from "./writeTarget";
