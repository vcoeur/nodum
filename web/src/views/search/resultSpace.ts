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
 * A hit whose `space_id` is null names nothing. The field is nullable because
 * the column is, and "space unknown" on a result row is a phrase with no
 * action behind it.
 */

import { spaceLabel } from "../../components";
import type { NodeOut, SearchHit } from "../../api/types";

/**
 * The space label a result row should show, or null when it should show none.
 *
 * @param hit One search hit, verbatim from the server.
 * @param spaces Active spaces, for resolving the id to a name; an empty list
 *   (or one still loading) falls back to the raw reference rather than
 *   rendering blank.
 * @param spaceFilter The view's current space filter; `ANY_SPACE` (`""`) when
 *   the search spans every space in scope.
 * @returns The label to render, or null when the row states no space.
 */
export function hitSpaceLabel(
  hit: SearchHit,
  spaces: readonly NodeOut[],
  spaceFilter: string,
): string | null {
  if (spaceFilter !== "") return null;
  if (hit.space_id === null) return null;
  return spaceLabel(spaces, hit.space_id);
}
