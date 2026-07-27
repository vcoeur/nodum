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
 *
 * Which of the two it was is usually knowable, and the panel says so when it is.
 * The filter refuses on exactly the reference `GET /api/spaces` stopped
 * carrying, so resolving it through `spaceLabel` alone meant the panel was
 * headed by a 32-hex id in the common case — the human archived `reading` and
 * followed their own bookmark. It goes through `nameSpace` over both lists now:
 * named, and told which thing happened when the archived listing knows.
 */

import { isUnknownSpace } from "../../api/client";
import { nameSpace } from "../../components/spaceNaming";
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
 * @param spaces Every active space, or null while that read has not answered —
 *   passed through as null rather than `?? []`, which would have the panel
 *   report a live space as one nothing names.
 * @param archived Archived space nodes from `useArchivedSpaces`, which is what
 *   names the filter in the case this panel most often renders for.
 * @returns The panel's copy, or null when the failure was something else and
 *   the view's ordinary error panel should render instead.
 */
export function describeSpaceFilterFailure(
  error: unknown,
  spaces: readonly NodeOut[] | null,
  archived: readonly NodeOut[],
): SpaceFilterFailure | null {
  if (!isUnknownSpace(error)) return null;

  // The active list will not hold it — that is why the server refused — so this
  // is the archived listing's answer or the bare reference.
  const name = nameSpace(error.space, spaces, archived);
  return {
    title: "That space filter could not be applied",
    detail:
      name.kind === "archived"
        ? `The server would not resolve ${name.label}, which has been archived. An archived ` +
          `space stops resolving, and a link from before it was archived lands here — what was ` +
          `written there is still readable. Search every space, or narrow to one from the list.`
        : `The server would not resolve ${name.label}. A space stops resolving once it is ` +
          `archived, and a renamed space no longer answers to its old name — a link from before ` +
          `either change lands here. Search every space, or narrow to one from the list.`,
    space: error.space,
  };
}
