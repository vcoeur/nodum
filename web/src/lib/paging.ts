/**
 * Which slice of a long list to render, and what the pager says about it.
 *
 * Promoted out of the journal when the review queue needed the same thing. Both
 * screens meet the same shape: a server-side cap that works, a presentation with
 * none, and a list long enough that rendering it whole is not a page a human can
 * read. A 500-event cycle came to 12 066 DOM nodes and 79 055 px of scroll; a
 * cycle that files hundreds of proposals in one run puts the queue in the same
 * place. Two copies of this arithmetic is how the two screens drift apart, so
 * there is one.
 *
 * Paging rather than collapsing, because on both screens what is rendered is
 * also what is **fetched**: the journal looks up an endpoint title per node on
 * the page, and the review queue's cards carry per-proposal reads of their own.
 */

/** One page of a list, and what to call it. */
export interface PageWindow {
  /** Zero-based page index, clamped into range. */
  page: number;
  /** How many pages there are; at least 1, even for an empty list. */
  pages: number;
  /** Slice bounds into the list — `[from, to)`. */
  from: number;
  to: number;
  /** `Events 1–25 of 500`, one-based and inclusive, for a human. */
  label: string;
}

/** Capitalise a noun for the start of a label. */
function opening(word: string): string {
  return word === "" ? "" : `${word[0]?.toUpperCase() ?? ""}${word.slice(1)}`;
}

/**
 * Which slice of a list to render, and what the pager says.
 *
 * The page is **clamped rather than trusted**: a list that shrinks under a pager
 * — the journal's events reload after a rollback, the review queue's shrink on
 * every accept — would otherwise leave the reader on an empty page with no way
 * back that is not the browser's.
 *
 * @param total How many items there are.
 * @param page The page asked for, zero-based; out of range is clamped.
 * @param size Items per page; anything below 1 is read as 1, so a caller that
 *   passes a mis-derived 0 gets a slow page rather than a division by zero.
 * @param noun What one item is called, for the label — `"event"`, `"proposal"`.
 * @param nounPlural Its plural, when appending `s` is wrong.
 */
export function pageWindow(
  total: number,
  page: number,
  size: number,
  noun: string,
  nounPlural?: string,
): PageWindow {
  const plural = nounPlural ?? `${noun}s`;
  const perPage = Math.max(1, Math.floor(size));
  const pages = Math.max(1, Math.ceil(total / perPage));
  const current = Math.min(Math.max(0, Math.floor(page)), pages - 1);
  const from = current * perPage;
  const to = Math.min(total, from + perPage);
  const label =
    total === 0 ? `No ${plural}` : `${opening(plural)} ${from + 1}–${to} of ${total}`;
  return { page: current, pages, from, to, label };
}
