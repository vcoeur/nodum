/**
 * Cross-view helpers: things more than one view needs and none of them owns.
 *
 * `src/components/` holds shared *React* components; this directory holds the
 * plain functions — timestamp parsing and failure classification — that every
 * view has to get right the same way. Import from `../../lib`.
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

export { onUnauthorized, reportUnauthorized } from "./session";
export type { UnauthorizedListener } from "./session";
