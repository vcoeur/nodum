/**
 * What the editor says about where a new node landed, and why a write did not.
 *
 * Design decision **D1a** is the reason this module exists rather than a string
 * template inside the save path: *the write target is never silent*. A sticky
 * space the human cannot see files work into the wrong territory, so the create
 * path shows the target before the write and this names the landing space after
 * it — "created in `research`", never a bare "created".
 *
 * Both answers are plain functions over plain data so the harness can hold the
 * two semantics that matter and a component cannot quietly drop them:
 *
 * - a landing notice **always names a space** the human can read, and says so
 *   loudly when the space the server filed the node in is not the one that was
 *   asked for;
 * - a refused write **never claims the space does not exist**. The server
 *   answers a nonexistent space and one the caller cannot read with the same
 *   words on purpose (Q13 review S3), and copy that resolved the ambiguity
 *   would turn the editor into an existence oracle over the whole file.
 */

import { ApiError, isUnknownSpace } from "../../api/client";
import { resolveSpaceValue, spaceLabel } from "../../components/spaceOptions";
import type { NodeOut } from "../../api/types";

/** A post-create confirmation: what happened, and where it landed. */
export interface LandingNotice {
  /** Headline for the toast. Names the landing space whenever there is one. */
  title: string;
  /** Second line: the sticky-target reminder, or the mismatch spelled out. */
  detail: string;
}

/** The literal text every space-resolving surface refuses with. */
const UNKNOWN_SPACE_MESSAGE = /^unknown space:/i;

/**
 * Whether a failed write was the write target refusing to resolve.
 *
 * `isUnknownSpace` is the shared answer and is tried first, but it only reaches
 * the two *read* calls the client normalises (`listNodes`, `search`) —
 * `createNode` throws the bare `ApiError` the wire carried. The write target is
 * the one space a create names, so the same message discriminator the client
 * documents is safe to apply here, and it is applied the same way: keyed on the
 * message, because the status alone is not specific enough (a 404 from
 * `POST /api/nodes` is equally an unknown node *type*).
 *
 * @param error The caught value.
 */
function isRefusedWriteTarget(error: unknown): boolean {
  if (isUnknownSpace(error)) return true;
  if (!(error instanceof ApiError)) return false;
  if (error.status !== 404 && error.status !== 400) return false;
  return UNKNOWN_SPACE_MESSAGE.test(error.message);
}

/**
 * Name the space a freshly created node landed in.
 *
 * The landing space is read off the **server's** answer rather than off the
 * requested target: the node is where the server says it is, and a confirmation
 * that echoed the request back would confirm nothing.
 *
 * @param created The node `POST /api/nodes` returned.
 * @param requested The write target the create asked for (id or name).
 * @param spaces Every active space, for turning an id into a name.
 * @returns Headline and detail for the post-create toast.
 */
export function describeLanding(
  created: NodeOut,
  requested: string,
  spaces: readonly NodeOut[],
): LandingNotice {
  const landed = created.space_id;
  if (landed === null) {
    // Defensive: every node has a space server-side. Saying "created" with no
    // space is still better than naming one the response did not carry.
    return {
      title: "Created",
      detail: "The server reported no space for this node.",
    };
  }

  const landedName = spaceLabel(spaces, landed);
  const asked = resolveSpaceValue(spaces, requested);
  if (asked === landed || requested === landed) {
    return {
      title: `Created in ${landedName}`,
      detail: `New nodes keep landing in ${landedName} until you change the space.`,
    };
  }

  return {
    title: `Created in ${landedName}`,
    detail:
      `The write target was ${spaceLabel(spaces, requested)}, but the server filed this node ` +
      `in ${landedName}.`,
  };
}

/**
 * Explain a create that the write target refused, or decline to.
 *
 * A target naming a space that has since been archived or renamed survives in
 * the store and fails here, deliberately — the shared store does not rewrite it
 * to `main`, because filing a node somewhere the human never chose is the
 * failure D1a exists to prevent. This is the sentence that makes that failure
 * legible.
 *
 * @param error The caught value.
 * @param requested The write target the create asked for (id or name).
 * @param spaces Every active space, for turning an id into a name.
 * @returns The sentence for the save-error panel, or null when the failure was
 *   something else and the caller's own error copy should stand.
 */
export function describeWriteFailure(
  error: unknown,
  requested: string,
  spaces: readonly NodeOut[],
): string | null {
  if (!isRefusedWriteTarget(error)) return null;
  const name = spaceLabel(spaces, requested);
  return (
    `The write target ${name} would not resolve — a space stops resolving once it is archived, ` +
    `and a renamed space no longer answers to its old name. Your text is still here: choose ` +
    `another space above and save again.`
  );
}
