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
 *
 * The refusal comes in two shapes because the editor writes in two: an
 * in-place save, whose text is still on screen, and a **detached** one issued
 * after the reader has already opened something else. Both must obey the copy
 * rule and they cannot share a last sentence — "your text is still here"
 * is false once the buffer that held it has been replaced. Missing the second
 * of these is how `UnknownSpace: unknown space: research` reached a toast:
 * `describeError` renders an `ApiError` as `type: message`, so any path that
 * hands one an unresolved space prints the forbidden words verbatim.
 *
 * The sentence that says *what changed about the target* is not written here
 * any more: the assets drop-zone became its second user and the two copies had
 * already drifted, so it lives in `components/spaceNaming.ts` as
 * `writeTargetWouldNotResolve`, beside `spaceNameNote`. What stays local is the
 * last sentence of each shape, which is the part that genuinely differs.
 *
 * Both refusals resolve the target through `nameSpace` over **two** lists. The
 * space a write target stops resolving for is, overwhelmingly, one the human
 * just archived — so the active list alone can only render the id, and both
 * sentences read `The write target 18ee0caa66204b5284774855a9d5cb34 would not
 * resolve` at the person who retired `reading` a minute ago. Naming it also
 * lets the copy say the *specific* true thing (archived) instead of the
 * disjunction (archived or renamed), which stays for the case where nothing
 * named it.
 */

import { isUnknownSpace } from "../../api/client";
import { findSpace, nameSpace, writeTargetWouldNotResolve } from "../../components/spaceNaming";
import type { NodeOut } from "../../api/types";

/**
 * The archived list a landing notice never needs.
 *
 * A node cannot land in an archived space — the server refuses the write, which
 * is the whole of {@link describeWriteFailure}'s existence — so a confirmation
 * resolves against the active list alone. Named rather than inlined so the
 * reason is stated once instead of read off an empty array literal.
 */
const NO_ARCHIVED_SPACES: readonly NodeOut[] = [];

/** A post-create confirmation: what happened, and where it landed. */
export interface LandingNotice {
  /** Headline for the toast. Names the landing space whenever there is one. */
  title: string;
  /** Second line: the sticky-target reminder, or the mismatch spelled out. */
  detail: string;
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
 * @param spaces Every active space, for turning an id into a name; null while
 *   `GET /api/spaces` has not answered, which the notice degrades to the
 *   reference for rather than going quiet.
 * @returns Headline and detail for the post-create toast.
 */
export function describeLanding(
  created: NodeOut,
  requested: string,
  spaces: readonly NodeOut[] | null,
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

  const landedName = nameSpace(landed, spaces, NO_ARCHIVED_SPACES).label;
  // A reference is an id *or* a name, so the target has to be matched both ways
  // before it can be called a mismatch — picking `research` from the list and
  // the server answering with its id is the ordinary case, not a divergence.
  const asked = spaces === null ? undefined : findSpace(spaces, requested);
  if (asked?.id === landed || requested === landed) {
    return {
      title: `Created in ${landedName}`,
      detail: `New nodes keep landing in ${landedName} until you change the space.`,
    };
  }

  return {
    title: `Created in ${landedName}`,
    detail:
      `The write target was ${nameSpace(requested, spaces, NO_ARCHIVED_SPACES).label}, but the ` +
      `server filed this node in ${landedName}.`,
  };
}

/**
 * Explain a create that the write target refused, or decline to.
 *
 * A target naming a space that has since been archived or renamed survives in
 * the store and fails here, deliberately — the shared store does not rewrite it
 * to `main`, because filing a node somewhere the human never chose is the
 * failure D1a exists to prevent. This is the sentence that makes that failure
 * legible **while the text is still on screen**; a write the editor has already
 * let go of gets {@link describeDetachedWriteFailure} instead.
 *
 * `isUnknownSpace` is the only test performed: `api/client.ts` normalises the
 * refusal on **every** call that names a space, `createNode` included, and a
 * second copy of that message match here would be a discriminator with two
 * owners.
 *
 * @param error The caught value.
 * @param requested The write target the create asked for (id or name).
 * @param spaces Every active space, or null while that read has not answered —
 *   passed through as null, because a list still in flight has ruled nothing
 *   out and `?? []` here would report a live space as unnameable.
 * @param archived Archived space nodes from `useArchivedSpaces`, which is what
 *   turns the usual case of this failure from an id into a name.
 * @returns The sentence for the save-error panel, or null when the failure was
 *   something else and the caller's own error copy should stand.
 */
export function describeWriteFailure(
  error: unknown,
  requested: string,
  spaces: readonly NodeOut[] | null,
  archived: readonly NodeOut[],
): string | null {
  if (!isUnknownSpace(error)) return null;
  return (
    `${writeTargetWouldNotResolve(nameSpace(requested, spaces, archived))} Your text is still here: ` +
    "choose another space above and save again."
  );
}

/**
 * The same refusal, for a write the editor is no longer holding the buffer for.
 *
 * Opening another document flushes the previous buffer detached — it reports
 * through the toast surface, because by the time it answers the editor is
 * showing something else. Two of those paths went straight to
 * `toast.showError`, which renders an `ApiError` as `type: message` and so put
 * **"UnknownSpace: unknown space: research"** on screen: the exact wording
 * nothing user-facing may use, on the one path where the human has also just
 * lost the text.
 *
 * It cannot share {@link describeWriteFailure}'s last sentence. "Your text is
 * still here" is true of the save panel and false here — the buffer it
 * described was replaced by the document the reader opened, which is why this
 * write was detached in the first place.
 *
 * @param error The caught value.
 * @param requested The write target the create asked for (id or name).
 * @param spaces Every active space, or null while that read has not answered.
 * @param archived Archived space nodes from `useArchivedSpaces`.
 * @returns The toast's detail line, or null when the failure was something else
 *   and the shared classifier should describe it.
 */
export function describeDetachedWriteFailure(
  error: unknown,
  requested: string,
  spaces: readonly NodeOut[] | null,
  archived: readonly NodeOut[],
): string | null {
  if (!isUnknownSpace(error)) return null;
  return (
    `${writeTargetWouldNotResolve(nameSpace(requested, spaces, archived))} That note is no longer ` +
    "open, so its text could not be kept — pick another space before writing it again."
  );
}
