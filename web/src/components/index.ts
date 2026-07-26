/**
 * Shared components — the ones every view is expected to reach for.
 *
 * Keep this barrel small. A component that only one view uses belongs in that
 * view's directory, not here; this file is a coordination surface between the
 * view slices, and everything added to it is something all of them inherit.
 *
 * Two non-components live here too, and both are here because they are halves
 * of {@link SpaceFilter} rather than free-standing utilities: `spaceOptions.ts`
 * is the vocabulary it renders and `useSpaces.ts` is the read that fills it.
 * The filter is controlled and presentational on purpose, so its data and its
 * option rules have to live somewhere every view can reach — and splitting them
 * into `src/lib/` would put one control's three parts in two directories.
 */

export { EmptyState } from "./EmptyState";
export { ErrorBoundary } from "./ErrorBoundary";
export { Modal } from "./Modal";
export { NodeBadge } from "./NodeBadge";
export { SpaceFilter } from "./SpaceFilter";
export { ANY_SPACE, resolveSpaceValue, spaceLabel, spaceOptions } from "./spaceOptions";
export type { SpaceOption } from "./spaceOptions";
export { Spinner } from "./Spinner";
export { useSpaces } from "./useSpaces";
export type { SpaceList } from "./useSpaces";
export { ToastProvider, useToast } from "./Toast";
export type { Toast, ToastApi, ToastTone } from "./Toast";
