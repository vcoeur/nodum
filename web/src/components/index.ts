/**
 * Shared components — the ones every view is expected to reach for.
 *
 * Keep this barrel small. A component that only one view uses belongs in that
 * view's directory, not here; this file is a coordination surface between the
 * view slices, and everything added to it is something all of them inherit.
 *
 * Six non-components live here too, and each is here because it is part of a
 * shared *vocabulary* rather than a free-standing utility. Four belong to the
 * space vocabulary: `spaceOptions.ts` is what {@link SpaceFilter} renders,
 * `useSpaces.ts` is the read that fills it, `spaceNaming.ts` resolves a space
 * reference for a surface that *displays* one, and `useArchivedSpaces.ts` is
 * the lazy read that lets it name a space `GET /api/spaces` deliberately does
 * not list. The filter is controlled and presentational on purpose, so its data
 * and its option rules have to live somewhere every view can reach — and
 * splitting them into `src/lib/` would put one control's parts in two
 * directories. The naming pair followed them here rather than into `src/lib/`
 * for the same reason: half of it is a hook, and the two halves are useless
 * apart.
 *
 * The other two are the retirement vocabulary, and they split the same way:
 * `nodeArchive.ts` is what {@link ArchiveNodeDialog} says, and
 * `useNodeArchive.ts` is the write plus the undo it promises. The undo has to
 * outlive the dialog — it is offered after the dialog closes — so the flow
 * belongs to whatever mounts it, not to the dialog.
 *
 * **`spaceLabel` is deliberately not re-exported.** It is the picker's own
 * fallback to the raw reference, correct inside a `<select>` and a bare 32-hex
 * id anywhere else — and it reached four surfaces that way before every one of
 * them was moved to `nameSpace`. Its only caller now lives in the same module,
 * so it is out of this barrel rather than sitting in the app-wide surface
 * waiting for a fifth. A view that wants to name a space wants `nameSpace`.
 */

export { ArchiveNodeDialog } from "./ArchiveNodeDialog";
export { ContextMenu, MenuButton, useContextMenu } from "./ContextMenu";
export type { ContextMenuController, MenuAction } from "./ContextMenu";
export { EmptyState } from "./EmptyState";
export { ErrorBoundary } from "./ErrorBoundary";
export { LinkDialog } from "./LinkDialog";
export { Modal } from "./Modal";
export { archiveConsequences, archiveRefusal } from "./nodeArchive";
export { ArchiveEdgeDialog } from "./ArchiveEdgeDialog";
export type { EdgeArchiveSubject } from "./edgeArchive";
export { edgeArchiveRefusal } from "./edgeArchive";
export { useEdgeArchive } from "./useEdgeArchive";
export { NodeBadge } from "./NodeBadge";
export { NodePeek, NodePeekScope } from "./NodePeek";
export { SpaceFilter } from "./SpaceFilter";
export {
  findSpace,
  nameSpace,
  spaceNameNote,
  unresolvedSpaceIds,
  writeTargetWouldNotResolve,
} from "./spaceNaming";
export type { SpaceName, SpaceNameKind } from "./spaceNaming";
export { ANY_SPACE, resolveSpaceValue, spaceOptions, unlistedMark } from "./spaceOptions";
export type { SpaceOption } from "./spaceOptions";
export { Spinner } from "./Spinner";
export { useArchivedSpaces } from "./useArchivedSpaces";
export type { ArchivedSpaces } from "./useArchivedSpaces";
export { useNodeArchive } from "./useNodeArchive";
export type { NodeArchiveApi } from "./useNodeArchive";
export { useNodeTypes } from "./useNodeTypes";
export type { NodeTypeList } from "./useNodeTypes";
export { useSpaces } from "./useSpaces";
export type { SpaceList } from "./useSpaces";
export { ToastProvider, useToast } from "./Toast";
export type { Toast, ToastAction, ToastApi, ToastTone } from "./Toast";
