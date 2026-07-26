/**
 * What the search view says when the server will not resolve the space filter.
 *
 * The client already normalises the wire's inconsistency — `GET /api/nodes`
 * refuses with a 404 and `GET /api/search` with a 400, for layering reasons
 * rather than by design — into one `UnknownSpaceError`. What is left is the
 * copy, and the copy carries a rule strong enough to be worth a module of its
 * own and a test beside it:
 *
 * **it must never say the space does not exist.** The server answers a space
 * that was never created and a space the caller holds no grant on with exactly
 * the same words (Q13 review S3), because a refusal that distinguished them
 * would be an existence oracle over the whole file. A panel that resolved the
 * ambiguity in the interface would put the leak back a layer up.
 *
 * The honest thing to say is what *changed*: a space stops resolving when it is
 * archived, and a renamed space stops answering to its old name — both of which
 * a bookmarked or shared `/search?space=…` link will meet.
 */

import { isUnknownSpace } from "../../api/client";
import { spaceLabel } from "../../components/spaceOptions";
import type { NodeOut } from "../../api/types";

/** A refused space filter, in the interface's voice. */
export interface SpaceFilterFailure {
  /** Panel headline. */
  title: string;
  /** The sentence under it: what happened, and what to do. */
  detail: string;
  /** The space the filter asked for, so the panel can offer to drop it. */
  space: string;
}

/**
 * Describe a failed search that was the space filter being refused.
 *
 * @param error The caught value.
 * @param spaces Every active space, for naming the filter's space.
 * @returns The panel's copy, or null when the failure was something else and
 *   the view's ordinary error panel should render instead.
 */
export function describeSpaceFilterFailure(
  error: unknown,
  spaces: readonly NodeOut[],
): SpaceFilterFailure | null {
  if (!isUnknownSpace(error)) return null;

  // Almost always falls back to the reference: a space that stopped resolving
  // is a space `GET /api/spaces` no longer lists.
  const name = spaceLabel(spaces, error.space);
  return {
    title: "That space filter could not be applied",
    detail:
      `The server would not resolve ${name}. A space stops resolving once it is archived, and a ` +
      `renamed space no longer answers to its old name — a link from before either change lands ` +
      `here. Search every space, or narrow to one from the list.`,
    space: error.space,
  };
}
