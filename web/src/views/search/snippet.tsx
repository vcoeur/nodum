import type { ReactNode } from "react";

/**
 * Rendering a server-supplied snippet with the matched terms marked.
 *
 * **Nothing here ever produces HTML.** The snippet is node content the server
 * echoed back, so it is turned into React text nodes and `<mark>` elements —
 * React escapes text children, and `dangerouslySetInnerHTML` is deliberately
 * absent from this file. Marking is a display affordance; it must not become an
 * injection surface.
 *
 * Two sources of marking, in order:
 *
 * 1. **The server's own markers.** `search._SNIPPET_PRE/_POST` are both `**`,
 *    so the FTS5 `snippet()` output wraps matched terms in Markdown bold. Those
 *    pairs are the authoritative match positions.
 * 2. **The typed terms.** A hit found by the vector signal has a plain chunk as
 *    its snippet with no markers at all, so the query's own terms are matched
 *    case-insensitively in the unmarked spans. This is a reading aid, not a
 *    claim about what the retrieval matched.
 *
 * Known cosmetic limitation: content is canonical Markdown, so genuine `**bold**`
 * in a node body is indistinguishable from a match marker and will render as a
 * mark. Preferring a false positive here to parsing Markdown for a 200-character
 * preview.
 */

/** Matched pairs of `**…**`, non-greedy so adjacent marks do not merge. */
const MARKER_PATTERN = /\*\*([\s\S]+?)\*\*/g;

/** Escape a query term for literal use inside a RegExp. */
function escapeForRegExp(term: string): string {
  return term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Build the alternation that matches any query term, or null when there is none. */
function termPattern(terms: string[]): RegExp | null {
  const usable = terms.filter((term) => term.length > 0);
  if (usable.length === 0) return null;
  // Longest first, so "graphql" wins over "graph" on an overlapping span.
  const alternation = [...usable]
    .sort((left, right) => right.length - left.length)
    .map(escapeForRegExp)
    .join("|");
  return new RegExp(`(${alternation})`, "gi");
}

/** Emit one unmarked span, highlighting any query terms inside it. */
function pushPlain(out: ReactNode[], text: string, pattern: RegExp | null, keyBase: string): void {
  if (text.length === 0) return;
  if (pattern === null) {
    out.push(text);
    return;
  }
  pattern.lastIndex = 0;
  let cursor = 0;
  let match = pattern.exec(text);
  let index = 0;
  while (match !== null) {
    if (match.index > cursor) out.push(text.slice(cursor, match.index));
    out.push(
      <mark className="nd-search-hit__mark nd-search-hit__mark--term" key={`${keyBase}-t${index}`}>
        {match[0]}
      </mark>,
    );
    cursor = match.index + match[0].length;
    index += 1;
    // A zero-length match would loop forever; terms are never empty, but the
    // regex is built from user input, so step defensively.
    if (match[0].length === 0) pattern.lastIndex += 1;
    match = pattern.exec(text);
  }
  if (cursor < text.length) out.push(text.slice(cursor));
}

/**
 * Render a snippet as escaped React nodes with matches marked.
 *
 * @param snippet The `SearchHit.snippet` string, verbatim from the server.
 * @param terms Query terms from {@link queryTerms}.
 * @returns Text nodes and `<mark>` elements — never raw HTML.
 */
export function renderSnippet(snippet: string, terms: string[]): ReactNode {
  // Collapse whitespace so a multi-line FTS snippet still occupies two lines and
  // every row in the list keeps the same height.
  const text = snippet.replace(/\s+/g, " ").trim();
  if (text.length === 0) return null;

  const pattern = termPattern(terms);
  const out: ReactNode[] = [];
  let cursor = 0;
  let index = 0;

  MARKER_PATTERN.lastIndex = 0;
  let match = MARKER_PATTERN.exec(text);
  while (match !== null) {
    pushPlain(out, text.slice(cursor, match.index), pattern, `p${index}`);
    out.push(
      <mark className="nd-search-hit__mark" key={`m${index}`}>
        {match[1]}
      </mark>,
    );
    cursor = match.index + match[0].length;
    index += 1;
    match = MARKER_PATTERN.exec(text);
  }
  pushPlain(out, text.slice(cursor), pattern, `p${index}`);

  return out;
}
