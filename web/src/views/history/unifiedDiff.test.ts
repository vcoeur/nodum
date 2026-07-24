/**
 * Turning the server's unified diff into aligned side-by-side rows.
 *
 * The wire format is whatever `difflib.unified_diff` produced server-side, so
 * the fixtures here are real difflib output rather than hand-written diffs —
 * `service.diff_versions` joins with `"\n"` and `lineterm=""`, which is exactly
 * what these strings are.
 *
 * The property worth protecting is the **zip**: consecutive removals and
 * additions are paired positionally so a reworded paragraph reads as a rewrite
 * rather than as a delete followed by an unrelated insert. Everything else here
 * guards the line numbering, which is the part a reader uses to find the change
 * in the actual document.
 */

import { describe, expect, it } from "vitest";
import { parseUnifiedDiff } from "./unifiedDiff";

/** Real `difflib.unified_diff` output over two rendered versions. */
const SERVER_DIFF = [
  "--- version v1",
  "+++ version v2",
  "@@ -1,6 +1,7 @@",
  "-title: Old title",
  "+title: New title",
  " props: {}",
  " ",
  " First paragraph.",
  "-Second paragraph.",
  "+Second paragraph, reworded.",
  "+An inserted line.",
  " Third paragraph.",
].join("\n");

describe("parseUnifiedDiff", () => {
  it("returns nothing for identical versions", () => {
    // `diff_versions` on two equal renderings produces an empty string, and an
    // empty diff must render as "no change", not as one blank row.
    expect(parseUnifiedDiff("")).toEqual([]);
    expect(parseUnifiedDiff("   \n  ")).toEqual([]);
  });

  it("drops the file headers, which the pane shows in its own columns", () => {
    const rows = parseUnifiedDiff(SERVER_DIFF);
    const text = rows.flatMap((row) => [row.left, row.right]);
    expect(text).not.toContain("--- version v1");
    expect(text).not.toContain("+++ version v2");
  });

  it("keeps the hunk header as its own separator row", () => {
    const rows = parseUnifiedDiff(SERVER_DIFF);
    expect(rows[0]).toEqual({
      kind: "hunk",
      leftNumber: null,
      rightNumber: null,
      left: "@@ -1,6 +1,7 @@",
      right: null,
    });
  });

  it("pairs a rewrite on one row instead of splitting it into a delete and an insert", () => {
    const rows = parseUnifiedDiff(SERVER_DIFF);
    const replaced = rows.filter((row) => row.kind === "replace");
    expect(replaced).toHaveLength(2);
    expect(replaced[0]).toMatchObject({
      left: "title: Old title",
      right: "title: New title",
    });
    expect(replaced[1]).toMatchObject({
      left: "Second paragraph.",
      right: "Second paragraph, reworded.",
    });
  });

  it("pads the shorter side of an uneven run rather than mis-pairing it", () => {
    // Two additions against one removal: the first pairs, the second is an
    // addition with no counterpart. Zipping it against the *next* removal
    // would claim a rewrite that did not happen.
    const rows = parseUnifiedDiff(SERVER_DIFF);
    const added = rows.filter((row) => row.kind === "add");
    expect(added).toHaveLength(1);
    expect(added[0]).toMatchObject({ left: null, right: "An inserted line." });
  });

  it("strips exactly one marker character from every line", () => {
    const rows = parseUnifiedDiff(SERVER_DIFF);
    const context = rows.filter((row) => row.kind === "context");
    expect(context.map((row) => row.left)).toEqual([
      "props: {}",
      "",
      "First paragraph.",
      "Third paragraph.",
    ]);
    // A context line is the same text on both sides, by definition.
    for (const row of context) expect(row.left).toBe(row.right);
  });

  it("numbers both sides from the hunk header, so the numbers match the document", () => {
    const rows = parseUnifiedDiff(SERVER_DIFF);
    expect(rows.map((row) => [row.kind, row.leftNumber, row.rightNumber])).toEqual([
      ["hunk", null, null],
      ["replace", 1, 1],
      ["context", 2, 2],
      ["context", 3, 3],
      ["context", 4, 4],
      ["replace", 5, 5],
      ["add", null, 6],
      ["context", 6, 7],
    ]);
  });

  it("keeps the two sides' numbering independent once they diverge", () => {
    // After a pure insertion the right side is one ahead for the rest of the
    // hunk; sharing a counter would mislabel every following line.
    const rows = parseUnifiedDiff(SERVER_DIFF);
    const last = rows[rows.length - 1]!;
    expect(last.leftNumber).toBe(6);
    expect(last.rightNumber).toBe(7);
  });

  it("restarts numbering at each hunk header", () => {
    const diff = [
      "@@ -1,2 +1,2 @@",
      " alpha",
      "-beta",
      "+BETA",
      "@@ -40,2 +40,2 @@",
      " omega",
      "-psi",
      "+PSI",
    ].join("\n");
    const rows = parseUnifiedDiff(diff);
    expect(rows.map((row) => [row.kind, row.leftNumber, row.rightNumber])).toEqual([
      ["hunk", null, null],
      ["context", 1, 1],
      ["replace", 2, 2],
      ["hunk", null, null],
      ["context", 40, 40],
      ["replace", 41, 41],
    ]);
  });

  it("reads a single-line hunk header, which difflib emits without a count", () => {
    const rows = parseUnifiedDiff(["@@ -7 +9 @@", "-only", "+ONLY"].join("\n"));
    expect(rows[1]).toMatchObject({ kind: "replace", leftNumber: 7, rightNumber: 9 });
  });

  it("flushes a pending run at the end of the diff", () => {
    // The last thing in a diff is often a change with no trailing context; a
    // parser that only flushes on a context line would drop it.
    const rows = parseUnifiedDiff(["@@ -1,1 +1,1 @@", "-gone", "+here"].join("\n"));
    expect(rows).toHaveLength(2);
    expect(rows[1]).toMatchObject({ kind: "replace", left: "gone", right: "here" });
  });

  it("renders a pure deletion as remove rows with no right-hand side", () => {
    const rows = parseUnifiedDiff(["@@ -1,3 +1,1 @@", " keep", "-one", "-two"].join("\n"));
    expect(rows.slice(2)).toEqual([
      { kind: "remove", leftNumber: 2, rightNumber: null, left: "one", right: null },
      { kind: "remove", leftNumber: 3, rightNumber: null, left: "two", right: null },
    ]);
  });

  it("keeps a removed or added blank line rather than losing it to the marker strip", () => {
    const rows = parseUnifiedDiff(["@@ -1,1 +1,1 @@", "-", "+"].join("\n"));
    expect(rows[1]).toMatchObject({ kind: "replace", left: "", right: "" });
  });

  it("ignores the no-newline-at-end-of-file note, which is not a change", () => {
    const rows = parseUnifiedDiff(
      ["@@ -1,1 +1,1 @@", "-old", "\\ No newline at end of file", "+new"].join("\n"),
    );
    expect(rows).toHaveLength(2);
    expect(rows[1]).toMatchObject({ kind: "replace", left: "old", right: "new" });
  });

  it("produces no trailing blank row for a server-shaped diff", () => {
    // `service.diff_versions` joins with "\n" and `lineterm=""`, so there is no
    // trailing newline; a parser that assumed one would add a phantom row.
    const rows = parseUnifiedDiff(SERVER_DIFF);
    expect(rows[rows.length - 1]).toMatchObject({ kind: "context", left: "Third paragraph." });
  });
});
