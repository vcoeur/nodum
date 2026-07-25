/**
 * Naming the cap that actually bit.
 *
 * `subgraph` enforces two caps — a node cap (`limit`) and an edge cap
 * (`limit * SUBGRAPH_EDGE_FACTOR`, because a node cap bounds nodes only and one
 * pair of nodes can carry hundreds of edges between them) — and reports both
 * through a single `truncated` boolean. The banner therefore has to *infer* the
 * cause, and getting it wrong is worse than staying silent: told "the node cap
 * stopped the walk" when the edge cap did, the reader raises the node cap, sees
 * an identical picture, and learns the banner cannot be trusted.
 *
 * The one signal available is the node count against the cap that was sent.
 */

import { describe, expect, it } from "vitest";
import { truncationCause } from "./TruncationNotice";
import { MAX_LIMIT } from "./filters";

describe("truncationCause", () => {
  it("blames the node cap when the walk filled it", () => {
    expect(truncationCause(200, 200)).toBe("node-cap");
    expect(truncationCause(1, 1)).toBe("node-cap");
  });

  it("blames the edge cap when the walk stopped with node budget left", () => {
    // The regression: two nodes, hundreds of edges between them. `truncated` is
    // true, the node count is nowhere near the cap, and the old banner still
    // said the node cap did it.
    expect(truncationCause(2, 200)).toBe("edge-cap");
    expect(truncationCause(199, 200)).toBe("edge-cap");
  });

  it("still blames the node cap when the server clamped the limit down", () => {
    // A hand-edited URL asking for more than `MAX_SUBGRAPH_LIMIT` is silently
    // clamped server-side, so the node count comes back below the limit that
    // was *sent* while the node cap is exactly what bit. Comparing against the
    // raw request would call that an edge cap — the same lie, reversed.
    expect(truncationCause(MAX_LIMIT, 99_999)).toBe("node-cap");
    expect(truncationCause(MAX_LIMIT, MAX_LIMIT)).toBe("node-cap");
  });

  it("never reports the edge cap for a graph that overshot the cap", () => {
    // Defensive: a count above the limit can only mean the node cap.
    expect(truncationCause(201, 200)).toBe("node-cap");
  });
});
