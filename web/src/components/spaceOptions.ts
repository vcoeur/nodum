/**
 * The space vocabulary every space picker renders — kept pure so the harness
 * can cover it, since the harness does not render components.
 *
 * Two rules do all the work here.
 *
 * **A controlled `<select>` must always be able to represent its own value.**
 * A `value` matching none of the `<option>`s renders blank, and the next change
 * event silently rewrites the caller's state — so a selection the space list
 * does not contain (a space archived since the filter was set, a name a URL
 * carried in, the list still loading) gets an option of its own rather than
 * vanishing. Dropping it would show "Any space" while a filter was still
 * applied.
 *
 * **A space reference is an id *or* a name**, everywhere on every nodum
 * surface. Options key on the id, so a selection expressed as a name has to be
 * resolved to one before it can match — {@link resolveSpaceValue}. Without that
 * step, filtering by `research` and filtering by `research`'s id would render
 * as two different spaces in the same list.
 */

import type { NodeOut } from "../api/types";

/** The "no space filter" value: read every space in scope. The default. */
export const ANY_SPACE = "";

/** One entry in a space picker. */
export interface SpaceOption {
  /** The `<option value>`: a space id, or {@link ANY_SPACE}. */
  value: string;
  /** What the human reads. */
  label: string;
  /**
   * True when this option exists only because it is the current selection and
   * the space list does not contain it — a value the human can leave but not
   * come back to. The picker marks it rather than passing it off as ordinary.
   */
  unlisted?: boolean;
}

/**
 * Find a space by id or by name.
 *
 * @param spaces Every active space.
 * @param spaceRef A space id or name.
 */
function findSpace(spaces: readonly NodeOut[], spaceRef: string): NodeOut | undefined {
  return spaces.find(
    (candidate) => candidate.id === spaceRef || candidate.title === spaceRef,
  );
}

/**
 * How to name a space: its title, or the bare reference when it has none — or
 * when the reference is not in the list at all.
 *
 * A grant, a filter, or a write target can name a space that has since been
 * archived and so left `GET /api/spaces`, and it must still render as
 * something a human recognises rather than as nothing.
 *
 * @param spaces Every active space.
 * @param spaceRef A space id or name.
 */
export function spaceLabel(spaces: readonly NodeOut[], spaceRef: string): string {
  return findSpace(spaces, spaceRef)?.title ?? spaceRef;
}

/**
 * Normalise a space reference to the value a picker's options carry — its id.
 *
 * @param spaces Every active space.
 * @param spaceRef A space id or name, or {@link ANY_SPACE}.
 * @returns The matching space's id; the reference unchanged when nothing
 *   matches (it is then rendered as an unlisted option) or when it is
 *   {@link ANY_SPACE}.
 */
export function resolveSpaceValue(spaces: readonly NodeOut[], spaceRef: string): string {
  if (spaceRef === ANY_SPACE) return ANY_SPACE;
  return findSpace(spaces, spaceRef)?.id ?? spaceRef;
}

/**
 * The options a space picker renders, in the order it renders them.
 *
 * Spaces are sorted by label rather than left in server order, because the list
 * is a vocabulary the human scans and `GET /api/spaces` orders by id.
 *
 * @param spaces Every active space; an empty list yields the sentinel alone.
 * @param selected The current selection, by id or name, so it can be
 *   guaranteed representable.
 * @param anyLabel Label for the no-filter sentinel.
 */
export function spaceOptions(
  spaces: readonly NodeOut[],
  selected: string = ANY_SPACE,
  anyLabel = "Any space",
): SpaceOption[] {
  const listed: SpaceOption[] = spaces
    .map((space) => ({ value: space.id, label: space.title ?? space.id }))
    .sort((a, b) => a.label.localeCompare(b.label));

  const options: SpaceOption[] = [{ value: ANY_SPACE, label: anyLabel }, ...listed];

  const resolved = resolveSpaceValue(spaces, selected);
  if (resolved !== ANY_SPACE && !options.some((option) => option.value === resolved)) {
    options.push({ value: resolved, label: spaceLabel(spaces, resolved), unlisted: true });
  }

  return options;
}
