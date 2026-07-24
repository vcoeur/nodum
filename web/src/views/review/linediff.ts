/**
 * A small line-level diff, for the side-by-side panes of a proposed update.
 *
 * The server's `diff_versions` returns a *unified* diff over a rendering that
 * concatenates title, props, and content into one text. That is the right thing
 * for an audit trail and the wrong thing for a reviewer deciding field by field
 * what an accept will write, so the side-by-side panes are computed here, per
 * field, and the server's unified diff is shown alongside as the authoritative
 * second opinion.
 *
 * Duplication note: a generic diff renderer would serve the version-history
 * view in slice 6 too. Hoist it to `src/components/` when that slice needs it.
 */

/** What one row of a side-by-side diff represents. */
export type DiffRowKind = "equal" | "removed" | "added" | "changed";

/** One aligned row: a left line, a right line, or both. */
export interface DiffRow {
  kind: DiffRowKind;
  left: string | null;
  right: string | null;
  leftNumber: number | null;
  rightNumber: number | null;
}

/** Above this, the quadratic LCS is not worth it and panes render unaligned. */
export const DIFF_LINE_LIMIT = 1500;

/** Split into lines, treating the empty string as a single empty line. */
function toLines(text: string): string[] {
  return text.length === 0 ? [""] : text.split("\n");
}

/**
 * Longest-common-subsequence table over two line arrays.
 *
 * Plain O(n·m) dynamic programming: the inputs are one Markdown node's content,
 * and {@link DIFF_LINE_LIMIT} keeps the pathological case off the main thread.
 */
function lcsTable(left: string[], right: string[]): number[][] {
  const table: number[][] = Array.from({ length: left.length + 1 }, () =>
    new Array<number>(right.length + 1).fill(0),
  );
  for (let i = left.length - 1; i >= 0; i -= 1) {
    const row = table[i];
    const next = table[i + 1];
    if (!row || !next) continue;
    for (let j = right.length - 1; j >= 0; j -= 1) {
      row[j] =
        left[i] === right[j] ? (next[j + 1] ?? 0) + 1 : Math.max(next[j] ?? 0, row[j + 1] ?? 0);
    }
  }
  return table;
}

/**
 * Pair a run of removals with a run of additions into `changed` rows.
 *
 * Side by side, "this line became that line" is what a reviewer reads; leaving
 * them as separate removed/added rows doubles the height and hides the pairing.
 */
function emitRun(
  rows: DiffRow[],
  removed: { text: string; number: number }[],
  added: { text: string; number: number }[],
): void {
  const paired = Math.min(removed.length, added.length);
  for (let index = 0; index < paired; index += 1) {
    const from = removed[index];
    const to = added[index];
    if (!from || !to) continue;
    rows.push({
      kind: "changed",
      left: from.text,
      right: to.text,
      leftNumber: from.number,
      rightNumber: to.number,
    });
  }
  for (let index = paired; index < removed.length; index += 1) {
    const from = removed[index];
    if (!from) continue;
    rows.push({
      kind: "removed",
      left: from.text,
      right: null,
      leftNumber: from.number,
      rightNumber: null,
    });
  }
  for (let index = paired; index < added.length; index += 1) {
    const to = added[index];
    if (!to) continue;
    rows.push({
      kind: "added",
      left: null,
      right: to.text,
      leftNumber: null,
      rightNumber: to.number,
    });
  }
  removed.length = 0;
  added.length = 0;
}

/**
 * Diff two texts into aligned side-by-side rows.
 *
 * @param before The current live text.
 * @param after The proposed text.
 * @returns Aligned rows, or null when either side exceeds
 *   {@link DIFF_LINE_LIMIT} — the caller then renders unaligned panes and says
 *   so rather than freezing.
 */
export function diffLines(before: string, after: string): DiffRow[] | null {
  const left = toLines(before);
  const right = toLines(after);
  if (left.length > DIFF_LINE_LIMIT || right.length > DIFF_LINE_LIMIT) return null;

  const table = lcsTable(left, right);
  const rows: DiffRow[] = [];
  const pendingRemoved: { text: string; number: number }[] = [];
  const pendingAdded: { text: string; number: number }[] = [];

  let i = 0;
  let j = 0;
  while (i < left.length && j < right.length) {
    if (left[i] === right[j]) {
      emitRun(rows, pendingRemoved, pendingAdded);
      rows.push({
        kind: "equal",
        left: left[i] ?? "",
        right: right[j] ?? "",
        leftNumber: i + 1,
        rightNumber: j + 1,
      });
      i += 1;
      j += 1;
      continue;
    }
    const down = table[i + 1]?.[j] ?? 0;
    const across = table[i]?.[j + 1] ?? 0;
    if (down >= across) {
      pendingRemoved.push({ text: left[i] ?? "", number: i + 1 });
      i += 1;
    } else {
      pendingAdded.push({ text: right[j] ?? "", number: j + 1 });
      j += 1;
    }
  }
  while (i < left.length) {
    pendingRemoved.push({ text: left[i] ?? "", number: i + 1 });
    i += 1;
  }
  while (j < right.length) {
    pendingAdded.push({ text: right[j] ?? "", number: j + 1 });
    j += 1;
  }
  emitRun(rows, pendingRemoved, pendingAdded);
  return rows;
}

/**
 * Collapse long runs of unchanged rows, keeping `context` rows either side.
 *
 * @returns The rows to render, with a `null` standing for "n lines unchanged".
 */
export function collapseEqual(rows: DiffRow[], context = 3): (DiffRow | number)[] {
  const output: (DiffRow | number)[] = [];
  let run: DiffRow[] = [];

  const flush = (atEnd: boolean) => {
    if (run.length === 0) return;
    const atStart = output.length === 0;
    const head = atStart ? 0 : context;
    const tail = atEnd ? 0 : context;
    if (run.length <= head + tail + 1) {
      output.push(...run);
    } else {
      output.push(...run.slice(0, head));
      output.push(run.length - head - tail);
      output.push(...run.slice(run.length - tail));
    }
    run = [];
  };

  for (const row of rows) {
    if (row.kind === "equal") {
      run.push(row);
      continue;
    }
    flush(false);
    output.push(row);
  }
  flush(true);
  return output;
}
