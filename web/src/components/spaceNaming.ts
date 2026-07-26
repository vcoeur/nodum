/**
 * What to call a space, and how much is actually known about the name.
 *
 * `GET /api/spaces` is **active-only** and stays that way — it is the read
 * behind {@link useSpaces} and therefore behind every picker in the app, and
 * leaking retired spaces into all of them was rejected when archived titles
 * became reserved for good. But a space id outlives the listing: archive
 * `research` and every node, proposal, grant and search hit already filed there
 * goes on reporting that id. `spaceLabel` can only render it as the raw 32-hex
 * string it is — honest, and useless, because the human is looking at the space
 * they retired an hour ago with nothing on screen saying so.
 *
 * This module is the answer, and it is deliberately **shared**: the review
 * queue built it first, and search, the editor's meta bar, the graph inspector
 * and `/admin`'s grant table all had the same bare-id rendering. A second copy
 * of one idea is how the copies drift, so there is one — resolving a reference
 * against **two** lists (the shared active one, and the archived space nodes
 * from {@link useArchivedSpaces}) and keeping the answers apart rather than
 * collapsing them into a string.
 *
 * Four answers, because the screen says something different about each:
 *
 * - `active` — named plainly; nothing more to say;
 * - `archived` — named *and* marked. Its content is still there and still
 *   readable; the space is simply out of every picker;
 * - `unknown` — reported as the id it is, with no guess layered on top;
 * - `pending` — the active list has not answered yet, so **nothing** has been
 *   ruled out. This is a separate answer rather than a shade of `unknown`
 *   because the difference is load-bearing twice over: a screen must not claim
 *   "nothing names this space" while the request that would name it is in
 *   flight, and {@link unresolvedSpaceIds} must not report every space on the
 *   screen as unresolved and fire the archived read on a perfectly healthy one.
 */

import type { NodeOut } from "../api/types";

/** How a space reference resolved. */
export type SpaceNameKind =
  /** Listed by `GET /api/spaces`. */
  | "active"
  /** Not listed there, but found among the archived space nodes in meta. */
  | "archived"
  /** Both lists have answered and neither holds it — all that is known is the id. */
  | "unknown"
  /** The active list has not answered yet; nothing is ruled out. */
  | "pending";

/** A space reference, resolved for display. */
export interface SpaceName {
  /** What to show: the space's title, or the bare reference when nothing names it. */
  label: string;
  kind: SpaceNameKind;
}

/**
 * Find a space by id **or** by name — the one place that match lives.
 *
 * A space reference is an id or a title everywhere on every nodum surface, so
 * every lookup in the app resolves both ways or none. `spaceOptions.ts` builds
 * its picker vocabulary on this, and so does everything below.
 *
 * @param spaces The list to search.
 * @param spaceRef A space id or name.
 * @returns The matching space node, or undefined.
 */
export function findSpace(
  spaces: readonly NodeOut[],
  spaceRef: string,
): NodeOut | undefined {
  return spaces.find((space) => space.id === spaceRef || space.title === spaceRef);
}

/**
 * Resolve a space reference for display.
 *
 * @param spaceRef The space id (or name) a row reported.
 * @param active Active spaces from the shared {@link useSpaces} hook, or null
 *   while that read has not answered (and after it failed).
 * @param archived Archived space nodes from {@link useArchivedSpaces}; empty
 *   while that read has not run, failed, or found none.
 * @returns The label to render and how it was resolved.
 */
export function nameSpace(
  spaceRef: string,
  active: readonly NodeOut[] | null,
  archived: readonly NodeOut[],
): SpaceName {
  // Nothing has been ruled out yet: reporting `unknown` here would have every
  // surface assert "no list names this space" against a request still in flight.
  if (active === null) return { label: spaceRef, kind: "pending" };

  const live = findSpace(active, spaceRef);
  if (live) return { label: live.title ?? live.id, kind: "active" };
  const retired = findSpace(archived, spaceRef);
  if (retired) return { label: retired.title ?? retired.id, kind: "archived" };
  return { label: spaceRef, kind: "unknown" };
}

/**
 * What a resolution says beyond the name, or null when the name says it all.
 *
 * One owner for a sentence four surfaces need, and one place to keep it inside
 * the copy rule: none of these may claim a space does not exist, because the
 * server answers a space that was never created and one the caller holds no
 * grant on with word-for-word identical text on purpose.
 *
 * @param name A resolved space name.
 * @returns The sentence to show beside it, or null for an ordinary live space.
 */
export function spaceNameNote(name: SpaceName): string | null {
  if (name.kind === "archived") {
    return (
      "This space has been archived. Archiving is not deletion — what was written there is " +
      "still here and still readable — but the space is out of every picker, nothing new can " +
      "be filed in it, and every grant on it is inert."
    );
  }
  if (name.kind === "unknown") {
    return (
      "Neither the active space list nor the archived one names this space, so the id it " +
      "reports is all there is to go on."
    );
  }
  if (name.kind === "pending") {
    return "The space list has not answered yet, so this is the id rather than the name.";
  }
  return null;
}

/**
 * The space ids in this set that the active list does not name.
 *
 * Drives the archived-space read on every surface that has one: it runs only
 * when the screen actually holds a reference to a space no picker lists, which
 * on a healthy file is never.
 *
 * **Null in means empty out**, and that is the point rather than a convenience.
 * With the active list still in flight, treating it as `[]` reports *every*
 * space on the screen as unresolved and fires the lazy read the moment any
 * surface mounts — the opposite of lazy, and racing on fetch order at that.
 *
 * @param spaceIds Every space id the screen carries (blank ids — a row that
 *   reported no space — are not space ids and are skipped).
 * @param active Active spaces from the shared hook, or null while it is loading.
 * @returns Each unresolved id once, in first-seen order; empty while loading.
 */
export function unresolvedSpaceIds(
  spaceIds: readonly string[],
  active: readonly NodeOut[] | null,
): string[] {
  if (active === null) return [];
  const unresolved = spaceIds.filter((spaceId) => spaceId !== "" && !findSpace(active, spaceId));
  return [...new Set(unresolved)];
}
