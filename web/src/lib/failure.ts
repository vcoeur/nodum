/**
 * The one classifier for a caught failure.
 *
 * The distinction every view has to make is *the API said no* versus *nothing
 * was listening*, and it is not a single test:
 *
 * - in the packaged app the client is same-origin with the API, so an
 *   unreachable server rejects `fetch` with a `TypeError` and there is no
 *   status to read;
 * - behind the Vite dev proxy the same situation is a **502** (or 504) — a real
 *   HTTP response from the proxy, so it arrives as an {@link ApiError}.
 *
 * Reporting a gateway status as a refusal sends the reader hunting for a
 * permission problem that does not exist, so both spellings collapse to
 * `unreachable` here. Views map {@link FailureKind} onto their own panels; they
 * do not re-derive it.
 */

import { ApiError } from "../api/client";

/** What kind of failure this was, in the terms a view has to render. */
export type FailureKind =
  /** A 404 — the id did not resolve. */
  | "not-found"
  /** The request never reached the API: a dead server, or a dev-proxy gateway. */
  | "unreachable"
  /** A 403 — a human-only operation was refused. */
  | "forbidden"
  /** A 503 — the single SQLite writer is busy, so the call is retryable. */
  | "busy"
  /** Any other API error: the server saw the request and said no. */
  | "refused"
  /** Something that is not an API error and not a network failure. */
  | "unknown";

/** A caught failure reduced to what a view renders. */
export interface FailureDescription {
  kind: FailureKind;
  /** Headline, in the interface's voice. */
  title: string;
  /** One or two sentences: what happened and what to do about it. */
  body: string;
}

/** The "nothing was listening" description, shared by both ways of hitting it. */
function unreachable(): FailureDescription {
  return {
    kind: "unreachable",
    title: "No answer from the nodum server",
    body:
      "The request never reached the API. Check that `nodum serve` is running, and that the dev " +
      "server is proxying to its port.",
  };
}

/**
 * Turn a caught value into something a view can render without guessing.
 *
 * @param error The caught value.
 * @param subject What was being fetched, named in the 404 copy, e.g.
 *   `"this node"`.
 * @returns The kind, a headline, and a body.
 */
export function describeFailure(error: unknown, subject = "that"): FailureDescription {
  if (error instanceof ApiError) {
    if (error.status === 502 || error.status === 504) return unreachable();
    if (error.isNotFound) {
      return {
        kind: "not-found",
        title: "Not found",
        body: `The server has no record of ${subject}. ${error.message}`,
      };
    }
    if (error.isForbidden) {
      return { kind: "forbidden", title: "Not permitted", body: error.message };
    }
    if (error.isRetryable) {
      return {
        kind: "busy",
        title: "The database is busy",
        body: `${error.message} A single writer holds SQLite during a large write — try again.`,
      };
    }
    return {
      kind: "refused",
      title: "The server refused the request",
      body: `${error.type}: ${error.message}`,
    };
  }
  if (error instanceof TypeError) return unreachable();
  return {
    kind: "unknown",
    title: "Something went wrong",
    body: error instanceof Error ? error.message || error.name : String(error),
  };
}

/**
 * Whether a failure means the request never reached the API.
 *
 * @param error The caught value.
 */
export function isUnreachable(error: unknown): boolean {
  return describeFailure(error).kind === "unreachable";
}

/**
 * Whether a failure was the API reporting that an id does not resolve.
 *
 * @param error The caught value.
 */
export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.isNotFound;
}

/**
 * Describe a caught value in one line, for an inline status rather than a panel.
 *
 * @param error The caught value.
 * @returns A `type: message` pair for an API error, a sentence otherwise; never
 *   empty.
 */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 502 || error.status === 504) return "Cannot reach the nodum server.";
    return `${error.type}: ${error.message}`;
  }
  if (isUnreachable(error)) return "Cannot reach the nodum server.";
  if (error instanceof Error) return error.message || error.name;
  return String(error);
}
