/**
 * The query terms worth highlighting in a search snippet.
 *
 * Split out of `snippet.tsx` so the pure string logic lives where the unit
 * harness can test it without a DOM: `queryTerms` is a plain function over
 * plain data, exactly the scope `vitest.config.ts` documents for the `.ts`
 * side of the tree.
 */

/** Beyond this, term highlighting stops being a reading aid and starts being noise. */
const MAX_TERMS = 8;

/**
 * Split the query into the terms worth highlighting.
 *
 * Mirrors `search._match_query`'s tokenisation (whitespace-separated, all
 * required), minus the FTS quoting, which is a server concern.
 *
 * @param query The raw query text.
 * @returns Distinct non-empty terms, capped.
 */
export function queryTerms(query: string): string[] {
  const seen = new Set<string>();
  for (const token of query.split(/\s+/)) {
    const term = token.trim();
    if (term.length === 0) continue;
    seen.add(term.toLowerCase());
    if (seen.size >= MAX_TERMS) break;
  }
  return [...seen];
}
