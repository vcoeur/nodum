/**
 * Turn the server's unified diff into aligned side-by-side rows.
 *
 * `service.diff_versions` renders each version as `title:` / `props:` / blank
 * / content and runs `difflib.unified_diff` over the result, so what arrives is
 * a plain unified diff with `@@` hunk headers. Splitting it into two columns is
 * a presentation choice and belongs in the client — the wire format stays the
 * diff the CLI prints, so the two surfaces cannot disagree about what changed.
 */

/** One rendered line-pair in the side-by-side view. */
export interface DiffRow {
  /**
   * `replace` is a removal and an addition paired on the same row; `hunk` is a
   * separator carrying the `@@` header.
   */
  kind: "context" | "replace" | "add" | "remove" | "hunk";
  /** 1-based line number in the left (older) version, when there is one. */
  leftNumber: number | null;
  /** 1-based line number in the right (newer) version, when there is one. */
  rightNumber: number | null;
  /** Left-hand text; null on a row that exists only on the right. */
  left: string | null;
  /** Right-hand text; null on a row that exists only on the left. */
  right: string | null;
}

/** `@@ -12,7 +12,9 @@` — the start line on each side. */
const HUNK_HEADER = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/**
 * Parse a unified diff into side-by-side rows.
 *
 * Consecutive removals and additions are zipped: the n-th removed line sits
 * opposite the n-th added line, and the shorter side is padded, which is what
 * makes a reworded paragraph read as a rewrite rather than as a delete
 * followed by an unrelated insert.
 *
 * @param diff The unified diff exactly as the server produced it.
 * @returns Rows in file order. Empty when the diff is empty (identical inputs).
 */
export function parseUnifiedDiff(diff: string): DiffRow[] {
  const rows: DiffRow[] = [];
  if (diff.trim() === "") return rows;

  let leftNumber = 0;
  let rightNumber = 0;
  let removed: string[] = [];
  let added: string[] = [];

  const flush = () => {
    const pairs = Math.max(removed.length, added.length);
    for (let index = 0; index < pairs; index += 1) {
      const left = index < removed.length ? (removed[index] as string) : null;
      const right = index < added.length ? (added[index] as string) : null;
      rows.push({
        kind: left !== null && right !== null ? "replace" : left !== null ? "remove" : "add",
        leftNumber: left === null ? null : (leftNumber += 1),
        rightNumber: right === null ? null : (rightNumber += 1),
        left,
        right,
      });
    }
    removed = [];
    added = [];
  };

  for (const line of diff.split("\n")) {
    // difflib's file headers name the two version ids; the pane shows those in
    // its own column headings, so they are noise here.
    if (line.startsWith("---") || line.startsWith("+++")) continue;
    // "\ No newline at end of file" is a note about the input, not a change.
    if (line.startsWith("\\")) continue;

    const hunk = HUNK_HEADER.exec(line);
    if (hunk) {
      flush();
      leftNumber = Number(hunk[1]) - 1;
      rightNumber = Number(hunk[2]) - 1;
      rows.push({ kind: "hunk", leftNumber: null, rightNumber: null, left: line, right: null });
      continue;
    }

    if (line.startsWith("-")) {
      removed.push(line.slice(1));
      continue;
    }
    if (line.startsWith("+")) {
      added.push(line.slice(1));
      continue;
    }

    flush();
    const text = line.startsWith(" ") ? line.slice(1) : line;
    leftNumber += 1;
    rightNumber += 1;
    rows.push({ kind: "context", leftNumber, rightNumber, left: text, right: text });
  }

  flush();
  return rows;
}
