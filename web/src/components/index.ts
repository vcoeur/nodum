/**
 * Shared components — the ones every view is expected to reach for.
 *
 * Keep this barrel small. A component that only one view uses belongs in that
 * view's directory, not here; this file is a coordination surface between the
 * view slices, and everything added to it is something all of them inherit.
 */

export { EmptyState } from "./EmptyState";
export { ErrorBoundary } from "./ErrorBoundary";
export { Modal } from "./Modal";
export { NodeBadge } from "./NodeBadge";
export { SpaceFilter } from "./SpaceFilter";
export { ANY_SPACE, resolveSpaceValue, spaceLabel, spaceOptions } from "./spaceOptions";
export type { SpaceOption } from "./spaceOptions";
export { Spinner } from "./Spinner";
export { ToastProvider, useToast } from "./Toast";
export type { Toast, ToastApi, ToastTone } from "./Toast";
