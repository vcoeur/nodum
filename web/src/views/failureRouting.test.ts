/**
 * What each view does with a classified failure.
 *
 * `src/lib/failure.ts` decides *what kind of failure this was*; every view then
 * chooses a panel for it. Three modules used to export a type called
 * `FailureKind` for those two different jobs, with three different unions, and
 * the collision hid a real bug: the review view's mapping had no case for
 * `busy`, so a **503 — the retryable "the single SQLite writer is holding the
 * database" answer — fell through to its generic `api` branch** and the review
 * inbox told the reader its queue "could not be loaded".
 *
 * Review is the surface most likely to meet a 503: accepting a whole agent run
 * is a long write, and the poll runs against the same one writer. The graph view
 * had it right all along, which is what makes the disagreement the bug rather
 * than a style difference.
 *
 * These tests pin the routing per view, and pin that no view silently loses a
 * kind the shared classifier hands it.
 */

import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import { describeFailure } from "../lib";
import type { FailureKind } from "../lib";
import { classifyFailure as classifyForGraph } from "./graph/errors";
import { classifyFailure as classifyForReview } from "./review/useReviewQueue";

/** One caught value per kind the shared classifier can produce. */
const SAMPLES: Record<FailureKind, unknown> = {
  "not-found": new ApiError(404, "NodeNotFound", "no node abc"),
  unreachable: new TypeError("Failed to fetch"),
  forbidden: new ApiError(403, "HumanOnly", "review is human-only"),
  busy: new ApiError(503, "DatabaseBusy", "database is locked"),
  refused: new ApiError(422, "ValidationError", "bad rule"),
  unknown: "a string nobody wrapped",
};

describe("the shared classifier", () => {
  it("produces every kind the views have to route", () => {
    for (const [kind, error] of Object.entries(SAMPLES)) {
      expect(describeFailure(error).kind).toBe(kind);
    }
  });

  it("reads a 503 as retryable rather than as a refusal", () => {
    const described = describeFailure(new ApiError(503, "DatabaseBusy", "database is locked"));
    expect(described.kind).toBe("busy");
    expect(described.title).toBe("The database is busy");
  });
});

describe("the review view's banner", () => {
  it("keeps a 503 as its own retryable case", () => {
    // The regression. `api` renders as "The review queue could not be loaded",
    // which is a flat wall for a condition that clears by itself.
    expect(classifyForReview(SAMPLES.busy)).toBe("busy");
    expect(classifyForReview(SAMPLES.busy)).not.toBe("api");
  });

  it("still separates the two cases that are not the server saying no", () => {
    expect(classifyForReview(SAMPLES.forbidden)).toBe("forbidden");
    expect(classifyForReview(SAMPLES.unreachable)).toBe("unreachable");
    // A dev-proxy 502 is the other spelling of the same situation.
    expect(classifyForReview(new ApiError(502, "BadGateway", "Bad Gateway"))).toBe("unreachable");
  });

  it("routes what is left to the generic banner", () => {
    expect(classifyForReview(SAMPLES.refused)).toBe("api");
    expect(classifyForReview(SAMPLES["not-found"])).toBe("api");
    expect(classifyForReview(SAMPLES.unknown)).toBe("api");
  });
});

describe("the graph view's panel", () => {
  it("reads a 503 as retryable, and says why", () => {
    const failure = classifyForGraph(SAMPLES.busy);
    expect(failure.kind).toBe("rejected");
    expect(failure.title).toBe("The database is busy");
    expect(failure.detail).toContain("SQLite has one writer");
  });

  it("gives a missing root its own panel and its own next action", () => {
    const failure = classifyForGraph(SAMPLES["not-found"], "abc");
    expect(failure.kind).toBe("root-missing");
    expect(failure.detail).toContain("Pick another root");
  });

  it("collapses both spellings of an unreachable server", () => {
    expect(classifyForGraph(SAMPLES.unreachable).kind).toBe("unreachable");
    expect(classifyForGraph(new ApiError(504, "GatewayTimeout", "timeout")).kind).toBe(
      "unreachable",
    );
  });
});

describe("both views, over every kind", () => {
  it("route a busy database to a retryable panel and never to the generic one", () => {
    // The property that was violated: whatever each view calls the panel, a 503
    // must not land wherever "the server rejected this outright" lands.
    expect(classifyForReview(SAMPLES.busy)).not.toBe(classifyForReview(SAMPLES.refused));
    expect(classifyForGraph(SAMPLES.busy).title).not.toBe(
      classifyForGraph(SAMPLES.refused).title,
    );
  });

  it("answer for every kind, with no undefined branch", () => {
    for (const error of Object.values(SAMPLES)) {
      expect(classifyForReview(error)).toBeTypeOf("string");
      expect(classifyForGraph(error).kind).toBeTypeOf("string");
      expect(classifyForGraph(error).title).not.toBe("");
      expect(classifyForGraph(error).detail).not.toBe("");
    }
  });
});
