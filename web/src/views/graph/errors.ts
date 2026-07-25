/**
 * The graph view's reading of a failure.
 *
 * Classification itself is shared (`src/lib/failure.ts`) — the "the API said no"
 * versus "nothing was listening" distinction is the same everywhere, and the
 * dev proxy's 502 has to land on the second one in every view or one of them
 * lies. What is local here is the *copy*: a 404 in this view means the root does
 * not exist, which has an obvious next action (pick another root) that no other
 * view shares, so it gets its own kind and its own sentence.
 */

import { describeFailure } from "../../lib";

/**
 * Which panel to show for a caught failure.
 *
 * Named for the view rather than `FailureKind`: that name belongs to the shared
 * classifier in `src/lib/failure.ts` and means the *kind of failure*, which is
 * the same everywhere. This is a choice of panel, which is not.
 */
export type GraphFailureKind = "root-missing" | "unreachable" | "rejected" | "unknown";

/** A caught failure, reduced to what the view renders. */
export interface Failure {
  kind: GraphFailureKind;
  /** Headline, in the interface's voice. */
  title: string;
  /** One line saying what to do about it. */
  detail: string;
}

/**
 * Classify a thrown value for one of the view's four panels.
 *
 * @param error The caught value.
 * @param rootId The root being rendered, named in the 404 message.
 * @returns What to tell the user.
 */
export function classifyFailure(error: unknown, rootId?: string): Failure {
  const described = describeFailure(error, rootId ? `the node ${rootId}` : "that node");
  switch (described.kind) {
    case "not-found":
      return {
        kind: "root-missing",
        title: "No such node",
        detail: rootId
          ? `Nothing in the graph has the id ${rootId}. Pick another root below.`
          : described.body,
      };
    case "unreachable":
      return {
        kind: "unreachable",
        title: "Cannot reach the nodum server",
        detail: "The API did not answer. Check that `nodum serve` is running, then retry.",
      };
    case "busy":
      return {
        kind: "rejected",
        title: described.title,
        detail: `${described.body} SQLite has one writer.`,
      };
    case "forbidden":
    case "refused":
      return { kind: "rejected", title: described.title, detail: described.body };
    default:
      return { kind: "unknown", title: described.title, detail: described.body };
  }
}
