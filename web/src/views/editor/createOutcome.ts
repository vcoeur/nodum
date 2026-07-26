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
 */

import { isUnknownSpace } from "../../api/client";
import { resolveSpaceValue, spaceLabel } from "../../components/spaceOptions";
import type { NodeOut } from "../../api/types";

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
 * Why a write target stopped resolving, said without claiming it is not there.
 *
 * The one sentence both refusal shapes are built on, so the copy rule is obeyed
 * in one place rather than in each caller. It states what *changed* — the only
 * honest thing available, since the server's refusal is word-for-word identical
 * for a space that was never created and one the caller holds no grant on.
 *
 * @param name The write target, already resolved to something readable.
 */
function targetWouldNotResolve(name: string): string {
  return (
    `The write target ${name} would not resolve — a space stops resolving once it is archived, ` +
    "and a renamed space no longer answers to its old name."
  );
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
 * @param spaces Every active space, for turning an id into a name.
 * @returns The sentence for the save-error panel, or null when the failure was
 *   something else and the caller's own error copy should stand.
 */
export function describeWriteFailure(
  error: unknown,
  requested: string,
  spaces: readonly NodeOut[],
): string | null {
  if (!isUnknownSpace(error)) return null;
  return (
    `${targetWouldNotResolve(spaceLabel(spaces, requested))} Your text is still here: choose ` +
    "another space above and save again."
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
 * @param spaces Every active space, for turning an id into a name.
 * @returns The toast's detail line, or null when the failure was something else
 *   and the shared classifier should describe it.
 */
export function describeDetachedWriteFailure(
  error: unknown,
  requested: string,
  spaces: readonly NodeOut[],
): string | null {
  if (!isUnknownSpace(error)) return null;
  return (
    `${targetWouldNotResolve(spaceLabel(spaces, requested))} That note is no longer open, so ` +
    "its text could not be kept — pick another space before writing it again."
  );
}
