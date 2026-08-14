import { describe, expect, it } from "vitest";
import type { EdgeOut, NodeOut } from "../api/types";
import { edgeArchiveConsequences, edgeArchiveLabel, edgeArchiveRefusal } from "./edgeArchive";

const source = { id: "source", title: "Alpha", type: "note" } as NodeOut;
const destination = { id: "destination", title: "Beta" } as NodeOut;
const edge = { id: "edge", src_id: source.id, dst_id: destination.id, type: "supports" } as EdgeOut;

describe("edge archive copy", () => {
  it("offers retirement only for an active relationship", () => {
    expect(edgeArchiveRefusal(edge)).toBeNull();
    expect(edgeArchiveRefusal({ ...edge, state: "proposed" })).toBe(
      "Only an active relationship can be archived.",
    );
    expect(edgeArchiveRefusal({ ...edge, state: "archived" })).toBe("Already archived.");
  });

  it("identifies both endpoints and the directed relationship without node consequences", () => {
    const subject = { edge, source, destination };
    expect(edgeArchiveLabel(subject)).toBe("Alpha — supports → Beta");
    expect(edgeArchiveConsequences(subject)).toEqual([
      "Current active traversal will stop following this supports relationship from Alpha to Beta.",
      "Alpha and Beta do not change.",
      "The relationship stays in history.",
      "Archiving is one reversible event.",
    ]);
    expect(edgeArchiveConsequences(subject).join(" ")).not.toMatch(/deleted|incident|count/i);
  });
});
