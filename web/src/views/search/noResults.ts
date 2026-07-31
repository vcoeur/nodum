/**
 * What the search view says when a query matched nothing.
 *
 * The copy here used to be one sentence in the view: *"Every term has to match
 * — the query is ANDed. Try fewer words."* Both halves are now wrong, and the
 * second half is worse than wrong.
 *
 * The keyword matcher **ORs the query's terms and requires a quorum of their
 * inverse-document-frequency weight** (`nodum.search._compile_match`): a node
 * is a candidate when the terms it carries are worth at least half the query's
 * discriminating power. A term the index has never seen is dropped before the
 * quorum is computed, and so is one that appears in more than half the graph.
 * So a question keeps working when the graph does not hold every one of its
 * words — measured on a 312-node corpus, question-shaped queries went from
 * 85% returning nothing at all to 3%.
 *
 * **A query the graph knows no content word of still matches nothing**, and
 * that is why the advice below names a missing word rather than a missing
 * result. The drops relax in a fixed order — ubiquity first, because it is the
 * only one about cost rather than meaning — and function words are searched
 * only when the query has no content word at all. Otherwise *"What does
 * zarquon protect against?"* would answer with notes that share its phrasing
 * while `zarquon` alone correctly returns nothing, which is the empty result
 * turned into a confidently wrong one.
 *
 * Which makes *"try fewer words"* close to the opposite of the right advice.
 * Under the old rule every extra word was another way to match nothing, so
 * shortening was the only way through. Under the quorum a rare word is what
 * earns a match and a common one costs nothing, so the word a human deletes to
 * "try fewer words" is exactly the one that was going to find the note.
 *
 * It is a module with a test beside it, rather than JSX in the view, because
 * this copy carries a rule about the server that has now been wrong once —
 * `web/` has no DOM harness, so a sentence inside a component is a sentence
 * nothing can check.
 */

import { hasActiveFilters } from "./searchState";
import type { SearchState } from "./searchState";

/**
 * Queries at or below this many words are short enough that adding a
 * distinctive term is real advice rather than noise.
 *
 * The quorum weighs terms, so a one- or two-word query has almost nothing to
 * weigh: it either matches or it does not. A longer question already carries
 * several rare terms, and telling its author to add another would be advice
 * that changes nothing.
 */
const SHORT_QUERY_WORDS = 3;

/**
 * Sentences to show under "nothing matched", in the order they should read.
 *
 * @param state The search as it was run — the query and every filter on it.
 * @returns One or more sentences; never empty.
 */
export function describeNoResults(state: SearchState): string[] {
  const advice: string[] = [
    "Terms are matched individually and the rarest ones count for most, " +
      "so a longer question is fine — what is missing is a word this graph uses.",
  ];
  if (state.query.trim().split(/\s+/).filter(Boolean).length <= SHORT_QUERY_WORDS) {
    advice.push("Try another distinctive word from the note you are looking for.");
  }
  if (hasActiveFilters(state)) {
    advice.push("A filter may also be excluding it.");
  }
  if (!state.expand) {
    advice.push("Including neighbours of matches widens the result to what they link to.");
  }
  return advice;
}
