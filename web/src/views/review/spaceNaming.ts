/**
 * What to call a space the review queue reports.
 *
 * The queue names spaces the shared vocabulary cannot. `GET /api/spaces` lists
 * **active** spaces only, deliberately — it is the read behind `useSpaces()`
 * and therefore behind every picker in the app, and leaking retired spaces
 * into all of them was rejected when archived titles became reserved for good.
 * But a proposal outlives the space it was filed in: archive `research` while
 * an agent's suggestions are still waiting and every one of them keeps
 * reporting that space id, which `spaceLabel` can then only render as the raw
 * 32-hex string. Honest, and useless — the human is looking at the space they
 * retired an hour ago with nothing to say so.
 *
 * So this module resolves a space against **two** lists: the shared active one,
 * and a review-local list of archived space nodes read straight out of meta
 * (see `useArchivedSpaces`). Nothing about the shared endpoint changes.
 *
 * The three answers are kept apart rather than collapsed into a string,
 * because the screen says something different about each: an active space is
 * named plainly, an archived one is named *and* marked (its proposals are
 * still reviewable — archiving is not deletion), and one that resolves to
 * neither is reported as the id it is, with no guess layered on top.
 */

import type { NodeOut } from "../../api/types";

/** How a space id resolved. */
export type SpaceNameKind =
  /** Listed by `GET /api/spaces`. */
  | "active"
  /** Not listed there, but found among the archived space nodes in meta. */
  | "archived"
  /** Neither list holds it — all that is known is the id. */
  | "unknown";

/** A space id, resolved for display. */
export interface SpaceName {
  /** What to show: the space's title, or the bare id when nothing names it. */
  label: string;
  kind: SpaceNameKind;
}

/** Find a space node by id or title, the way every space reference resolves. */
function find(spaces: readonly NodeOut[], spaceRef: string): NodeOut | undefined {
  return spaces.find((space) => space.id === spaceRef || space.title === spaceRef);
}

/**
 * Resolve a space id for display.
 *
 * @param spaceRef The space id (or name) a proposal reported.
 * @param active Active spaces, from the shared `useSpaces()` hook.
 * @param archived Archived space nodes, from the review-local read; empty
 *   while that read has not run, failed, or found none.
 * @returns The label to render and how it was resolved.
 */
export function nameSpace(
  spaceRef: string,
  active: readonly NodeOut[],
  archived: readonly NodeOut[],
): SpaceName {
  const live = find(active, spaceRef);
  if (live) return { label: live.title ?? live.id, kind: "active" };
  const retired = find(archived, spaceRef);
  if (retired) return { label: retired.title ?? retired.id, kind: "archived" };
  return { label: spaceRef, kind: "unknown" };
}

/**
 * The space ids in these sections that the active list does not name.
 *
 * Drives the archived-space read: it runs only when the queue actually holds a
 * proposal in a space no picker lists, which on a healthy graph is never.
 *
 * @param spaceIds Every space id the queue's sections carry (blank ids — the
 *   *space not reported* bucket — are not space ids and are skipped).
 * @param active Active spaces, from the shared hook.
 */
export function unresolvedSpaceIds(
  spaceIds: readonly string[],
  active: readonly NodeOut[],
): string[] {
  const unresolved = spaceIds.filter((spaceId) => spaceId !== "" && !find(active, spaceId));
  return [...new Set(unresolved)];
}
