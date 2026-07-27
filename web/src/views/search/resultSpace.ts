/**
 * Whether a search result names its space, and what it is called.
 *
 * Search is the surface a human *scans*: a result list spans every space in
 * scope unless the filter narrowed it, and design decision D1 makes that
 * spanning the default. Without the space on the row, a `main` hit and a
 * `research` hit look identical until one is opened — the phase's own exit
 * criterion ("see which space anything they are reading lives in") failing on
 * the busiest surface it has.
 *
 * **A row states its space only when the filter did not already state it.**
 * With `?space=research` in force the server ANDs `n.space_id = ?` onto both
 * ranked lists *and* onto graph expansion, so every hit provably lives there;
 * repeating "research" on every row would be the filter read back a hundred
 * times rather than a fact about the row. Under *any space* — the default —
 * the space is genuinely unknown per row and is exactly what a scan needs.
 * This is the same rule `ResultRow.knownState` already follows for the state
 * filter, deliberately: a filtered dimension is implied, an unfiltered one is
 * rendered.
 *
 * **And it names the space rather than printing its id.** The first version of
 * this module ended in `spaceLabel`, whose fallback is the raw reference — so a
 * hit in a space archived since it was written rendered as
 * `in 4affabf6d856427886ad48570f5f6e20`, which is the sentence the exit
 * criterion exists to prevent. It goes through the shared `nameSpace` now, over
 * the active list *and* the lazy archived one, so an archived space is named and
 * marked instead.
 *
 * A hit whose `space_id` is null names nothing. The field is nullable because
 * the column is, and "space unknown" on a result row is a phrase with no
 * action behind it.
 */

import { nameSpace, spaceNameNote } from "../../components";
import type { SpaceName } from "../../components";
import type { NodeOut, SearchHit } from "../../api/types";

/**
 * The space a result row should name, or null when it should name none.
 *
 * @param hit One search hit, verbatim from the server.
 * @param active Active spaces, or null while `GET /api/spaces` is in flight —
 *   passed through as null so the row shows the id rather than asserting that
 *   no list names it.
 * @param archived Archived space nodes, from the lazy read; empty until one is
 *   needed.
 * @param spaceFilter The view's current space filter; `ANY_SPACE` (`""`) when
 *   the search spans every space in scope.
 * @returns The resolved name, or null when the row states no space.
 */
export function hitSpaceName(
  hit: SearchHit,
  active: readonly NodeOut[] | null,
  archived: readonly NodeOut[],
  spaceFilter: string,
): SpaceName | null {
  if (spaceFilter !== "") return null;
  if (hit.space_id === null) return null;
  return nameSpace(hit.space_id, active, archived);
}

/**
 * The hover text for a row's space chip.
 *
 * A live space gets the one thing a scanner might want next — the filter that
 * narrows to it. Anything else gets the shared note instead, because "narrow to
 * this one" is advice the picker cannot take for a space it does not offer.
 *
 * @param name The row's resolved space name.
 */
export function hitSpaceTitle(name: SpaceName): string {
  const note = spaceNameNote(name);
  return note === null
    ? `This node lives in the ${name.label} space. Narrow the space filter to read only that one.`
    : `This node lives in ${name.label}. ${note}`;
}
