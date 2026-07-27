/**
 * The queue's bookkeeping, which used to live where nothing could test it.
 *
 * Two of these are load-bearing rather than cosmetic:
 *
 * - **a batch freezes the write target.** The target is app-wide, sticky and
 *   synchronised across tabs, so it can change mid-batch; a row that recorded
 *   the *current* value would have its refusal copy name a space that never
 *   refused anything;
 * - **the label map is total over the status union.** A status with no label
 *   renders `undefined` in a cell, and the union and the enumeration are one
 *   object here precisely so that cannot happen quietly.
 *
 * The other two are copy the human reads when something is refused or when a
 * screen reader is the only thing reading: both were unreachable from a test
 * while they sat inside the hook.
 */

import { describe, expect, it } from "vitest";
import { UPLOAD_STATUSES, type UploadStatus } from "./uploadOutcome";
import {
  batchSummary,
  describeRefusedDrop,
  nextBatch,
  statusLabel,
  type UploadItem,
} from "./uploadQueue";

/** A file with a name and a size, which is all the queue reads. */
function file(name: string, size = 3): File {
  return new File(["x".repeat(size)], name);
}

/** A settled row, for the summary. */
function row(id: number, status: UploadStatus): UploadItem {
  return {
    id,
    name: `file-${id}.pdf`,
    size: 10,
    status,
    requestedSpace: "research",
    outcome: null,
    error: null,
  };
}

describe("nextBatch", () => {
  it("carries the name and the size of every dropped file, in order", () => {
    const batch = nextBatch([file("a.pdf", 4), file("b.png", 9)], "research", 1);

    expect(batch.items.map((item) => item.name)).toEqual(["a.pdf", "b.png"]);
    expect(batch.items.map((item) => item.size)).toEqual([4, 9]);
  });

  it("queues every row, with nothing settled and nothing failed", () => {
    const batch = nextBatch([file("a.pdf"), file("b.pdf")], "research", 1);

    for (const item of batch.items) {
      expect(item.status).toBe("queued");
      expect(item.outcome).toBeNull();
      expect(item.error).toBeNull();
    }
  });

  it("files the whole batch against the target as it stood at the drop", () => {
    // One drop is one act. A `storage` event from another tab can move the
    // write target mid-batch, and a row that read the current value instead
    // would describe its refusal against a space that never refused it.
    const batch = nextBatch([file("a.pdf"), file("b.pdf"), file("c.pdf")], "sp-old", 1);

    expect(batch.items.map((item) => item.requestedSpace)).toEqual(["sp-old", "sp-old", "sp-old"]);
  });

  it("hands out ids from where the last batch stopped, since they are keys", () => {
    const first = nextBatch([file("a.pdf"), file("b.pdf")], "research", 1);
    const second = nextBatch([file("c.pdf")], "research", first.nextId);

    expect(first.items.map((item) => item.id)).toEqual([1, 2]);
    expect(first.nextId).toBe(3);
    expect(second.items.map((item) => item.id)).toEqual([3]);
    expect(second.nextId).toBe(4);
    // Reused across batches, a key collides with a row already on screen.
    const ids = [...first.items, ...second.items].map((item) => item.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("is empty for an empty drop, and consumes no id", () => {
    const batch = nextBatch([], "research", 7);

    expect(batch.items).toEqual([]);
    expect(batch.nextId).toBe(7);
  });
});

describe("statusLabel", () => {
  it("is total over the status union, so no cell can render undefined", () => {
    // The union is derived from `UPLOAD_STATUSES` and the map is a `Record` over
    // it, so a new status fails the build in the map; this walks the same array
    // to prove the map is complete at runtime too.
    expect(UPLOAD_STATUSES.length).toBeGreaterThan(0);
    for (const status of UPLOAD_STATUSES) {
      const label = statusLabel(status);
      expect(typeof label).toBe("string");
      expect(label.length).toBeGreaterThan(0);
    }
  });

  it("tells the two settled outcomes apart, which is the whole readout's premise", () => {
    expect(statusLabel("ingested")).toBe("ingested");
    expect(statusLabel("already-ingested")).toBe("already ingested");
    expect(statusLabel("ingested")).not.toBe(statusLabel("already-ingested"));
  });
});

describe("describeRefusedDrop", () => {
  it("says the files were not queued, rather than letting them vanish", () => {
    const message = describeRefusedDrop(3);

    expect(message).toContain("Those 3 files were not queued");
    expect(message).toContain("a batch is already running");
    expect(message).toContain("drop them again once this one finishes");
  });

  it("reads for a single file too", () => {
    expect(describeRefusedDrop(1)).toContain("That file was not queued");
  });

  it("never offers to resume, because there is nothing to resume", () => {
    for (const count of [1, 2, 10]) {
      expect(describeRefusedDrop(count).toLowerCase()).not.toMatch(/resume|pick up where/);
    }
  });
});

describe("batchSummary", () => {
  it("announces nothing about an empty queue", () => {
    expect(batchSummary([], false)).toBe("");
    expect(batchSummary([], true)).toBe("");
  });

  it("is one stable sentence while the batch runs, not one per row", () => {
    // A drop arriving during a batch is refused, so the count cannot change
    // mid-flight: this string is announced once and then stays put.
    const running = [row(1, "uploading"), row(2, "queued"), row(3, "queued")];
    const later = [row(1, "ingested"), row(2, "uploading"), row(3, "queued")];

    expect(batchSummary(running, true)).toBe("Ingesting 3 files.");
    expect(batchSummary(later, true)).toBe(batchSummary(running, true));
  });

  it("counts the three settled outcomes once the batch has finished", () => {
    const settled = [
      row(1, "ingested"),
      row(2, "ingested"),
      row(3, "already-ingested"),
      row(4, "failed"),
    ];

    expect(batchSummary(settled, false)).toBe(
      "Finished 4 files: 2 ingested, 1 already ingested, 1 failed.",
    );
  });

  it("leaves out the outcomes that did not happen", () => {
    expect(batchSummary([row(1, "ingested")], false)).toBe("Finished 1 file: 1 ingested.");
  });

  it("says only that the files are queued when the loop never ran them", () => {
    // An abort leaves rows behind with no verdict; claiming a finished batch
    // over them would report an outcome nothing produced.
    expect(batchSummary([row(1, "queued"), row(2, "queued")], false)).toBe("2 files queued.");
  });
});
