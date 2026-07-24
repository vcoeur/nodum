/**
 * Shared components — the ones every view is expected to reach for.
 *
 * Keep this barrel small. A component that only one view uses belongs in that
 * view's directory, not here; this file is a coordination surface between the
 * view slices, and everything added to it is something all of them inherit.
 */

export { EmptyState } from "./EmptyState";
export { ErrorBoundary } from "./ErrorBoundary";
export { NodeBadge } from "./NodeBadge";
export { Spinner } from "./Spinner";
export { ToastProvider, useToast } from "./Toast";
export type { Toast, ToastApi, ToastTone } from "./Toast";
