/**
 * `[[` autocomplete: a completion source backed by the title-prefix query.
 *
 * The source only ever fires inside an open `[[`, so it costs nothing while
 * ordinary prose is being typed. Between the keystroke and the request sits a
 * short delay the completion context can abort, which collapses a burst of
 * typing into one call rather than one call per character.
 *
 * `suggest_links` reads the `nodes` table directly rather than a projection, so
 * an empty list always means "no node has that title" and never "the index has
 * not been built". That guarantee is only worth anything if the client does not
 * quietly turn a failed request into an empty list, which is why a failure is
 * reported up to the view instead of being swallowed here.
 */

import type { CompletionContext, CompletionResult, CompletionSource } from "@codemirror/autocomplete";
import type { EditorView } from "@codemirror/view";
import { api } from "../../../api/client";
import type { NodeOut } from "../../../api/types";
import { describeError } from "../../../lib";

/** An open `[[` with the prefix typed so far, up to the cursor. */
const OPEN_WIKILINK = /\[\[[^\]\n]*$/;

/** How long a keystroke has to be followed by another before a request goes out. */
const DEBOUNCE_MS = 140;

/** Maximum candidates requested from the server. */
const LIMIT = 20;

/**
 * Build the `[[` completion source.
 *
 * @param reportFailure Called with a message when the suggestion query fails
 *   and with null when it next succeeds. The editor shows this in its status
 *   strip: an empty popup must read as "no node has that title", never as a
 *   silently broken feature.
 */
export function wikilinkCompletion(reportFailure: (message: string | null) => void): CompletionSource {
  return async (context: CompletionContext): Promise<CompletionResult | null> => {
    const open = context.matchBefore(OPEN_WIKILINK);
    if (!open) return null;

    const prefix = open.text.slice(2);
    await pause(DEBOUNCE_MS);
    if (context.aborted) return null;

    let candidates: NodeOut[];
    try {
      candidates = await api.suggestLinks(prefix, LIMIT);
      reportFailure(null);
    } catch (error) {
      reportFailure(describeError(error));
      return null;
    }
    if (context.aborted) return null;

    // A wikilink is addressed *by* title, so an untitled node is not a link
    // target however well it matches.
    const linkable = candidates.filter(
      (candidate): candidate is NodeOut & { title: string } =>
        typeof candidate.title === "string" && candidate.title.length > 0,
    );
    if (linkable.length === 0) return null;

    return {
      from: open.from + 2,
      options: linkable.map((candidate) => ({
        label: candidate.title,
        detail: candidate.state === "active" ? candidate.type : `${candidate.type} · ${candidate.state}`,
        // The second class carries the state ramp onto the completion icon, so
        // a link to structure nobody has reviewed reads as such while typing.
        type: candidate.state === "active" ? "variable" : `variable ${candidate.state}`,
        apply: applyWikilink(candidate.title),
      })),
      // Filtering the returned list locally is only correct when the list is
      // the complete answer for this prefix. At the limit there may be more,
      // so let a longer prefix go back to the server.
      ...(linkable.length < LIMIT ? { validFor: /^[^\]\n]*$/ } : {}),
    };
  };
}

/**
 * Insert `title`, closing the wikilink unless the user already typed `]]`.
 *
 * @param title The node title to link to.
 */
function applyWikilink(title: string) {
  return (view: EditorView, _completion: unknown, from: number, to: number): void => {
    const alreadyClosed = view.state.sliceDoc(to, to + 2) === "]]";
    const insert = alreadyClosed ? title : `${title}]]`;
    view.dispatch({
      changes: { from, to, insert },
      // Either way the cursor ends up just past the closing brackets.
      selection: { anchor: from + title.length + 2 },
      userEvent: "input.complete",
      scrollIntoView: true,
    });
  };
}

/** Resolve after `ms`, so an aborted completion never reaches the network. */
function pause(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}
