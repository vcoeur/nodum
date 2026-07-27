/**
 * Shared components — the ones every view is expected to reach for.
 *
 * Keep this barrel small. A component that only one view uses belongs in that
 * view's directory, not here; this file is a coordination surface between the
 * view slices, and everything added to it is something all of them inherit.
 *
 * Four non-components live here too, and all four are here because they are
 * parts of the shared space vocabulary rather than free-standing utilities:
 * `spaceOptions.ts` is what {@link SpaceFilter} renders, `useSpaces.ts` is the
 * read that fills it, `spaceNaming.ts` resolves a space reference for a surface
 * that *displays* one, and `useArchivedSpaces.ts` is the lazy read that lets it
 * name a space `GET /api/spaces` deliberately does not list. The filter is
 * controlled and presentational on purpose, so its data and its option rules
 * have to live somewhere every view can reach — and splitting them into
 * `src/lib/` would put one control's parts in two directories. The naming pair
 * followed them here rather than into `src/lib/` for the same reason: half of
 * it is a hook, and the two halves are useless apart.
 *
 * **`spaceLabel` is deliberately not re-exported.** It is the picker's own
 * fallback to the raw reference, correct inside a `<select>` and a bare 32-hex
 * id anywhere else — and it reached four surfaces that way before every one of
 * them was moved to `nameSpace`. Its only caller now lives in the same module,
 * so it is out of this barrel rather than sitting in the app-wide surface
 * waiting for a fifth. A view that wants to name a space wants `nameSpace`.
 */

export { EmptyState } from "./EmptyState";
export { ErrorBoundary } from "./ErrorBoundary";
export { Modal } from "./Modal";
export { NodeBadge } from "./NodeBadge";
export { SpaceFilter } from "./SpaceFilter";
export { findSpace, nameSpace, spaceNameNote, unresolvedSpaceIds } from "./spaceNaming";
export type { SpaceName, SpaceNameKind } from "./spaceNaming";
export { ANY_SPACE, resolveSpaceValue, spaceOptions, unlistedMark } from "./spaceOptions";
export type { SpaceOption } from "./spaceOptions";
export { Spinner } from "./Spinner";
export { useArchivedSpaces } from "./useArchivedSpaces";
export type { ArchivedSpaces } from "./useArchivedSpaces";
export { useSpaces } from "./useSpaces";
export type { SpaceList } from "./useSpaces";
export { ToastProvider, useToast } from "./Toast";
export type { Toast, ToastApi, ToastTone } from "./Toast";
