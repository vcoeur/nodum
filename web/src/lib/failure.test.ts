/**
 * The regression guard for "the API refused this" versus "nothing was listening".
 *
 * That distinction is not one test, and getting it wrong is what
 * `review/useReviewQueue.ts` did before the classifier was hoisted: it treated
 * anything that was an `ApiError` as a refusal, so a dead backend behind the
 * Vite dev proxy — which arrives as a **502**, a real HTTP response — told the
 * reviewer "the server refused the request" for a server that never saw it.
 *
 * The two spellings of the same situation are the pair these tests exist for:
 * same-origin (the packaged app) it is a `fetch` `TypeError` with no status at
 * all; behind the proxy it is a 502 or 504. Both must collapse to
 * `unreachable`, and no other status may.
 */

import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import { describeError, describeFailure, isNotFound, isUnreachable } from "./failure";

describe("the unreachable/refused split", () => {
  it("reads a dev-proxy gateway status as unreachable, not as a refusal", () => {
    // The exact bug the hoist fixed. A 502 is an `ApiError`, so the naive test
    // ("is it an ApiError? then the API answered") classifies it as `refused`.
    for (const status of [502, 504]) {
      const described = describeFailure(new ApiError(status, "BadGateway", "Bad Gateway"));
      expect(described.kind).toBe("unreachable");
      expect(described.title).toBe("No answer from the nodum server");
    }
  });

  it("reads a same-origin fetch rejection as the same kind", () => {
    // In the packaged app there is no proxy and no status — `fetch` rejects.
    // The reader must get the identical diagnosis either way.
    const sameOrigin = describeFailure(new TypeError("Failed to fetch"));
    const behindProxy = describeFailure(new ApiError(502, "BadGateway", "Bad Gateway"));
    expect(sameOrigin.kind).toBe("unreachable");
    expect(sameOrigin).toEqual(behindProxy);
  });

  it("names `nodum serve` and the proxy, because those are the two things to check", () => {
    expect(describeFailure(new TypeError("Failed to fetch")).body).toContain("nodum serve");
    expect(describeFailure(new TypeError("Failed to fetch")).body).toContain("proxy");
  });

  it("does not sweep a neighbouring 5xx into unreachable", () => {
    // 500 is the server answering. Only the gateway statuses mean "never
    // arrived", and widening that would hide real server errors.
    expect(describeFailure(new ApiError(500, "RuntimeError", "boom")).kind).toBe("refused");
    expect(describeFailure(new ApiError(501, "NotImplemented", "no")).kind).toBe("refused");
  });
});

describe("describeFailure", () => {
  it("maps 404 to not-found and names the subject the caller was after", () => {
    const described = describeFailure(new ApiError(404, "NodeNotFound", "no such node"), "this node");
    expect(described.kind).toBe("not-found");
    expect(described.body).toContain("this node");
    expect(described.body).toContain("no such node");
  });

  it("falls back to a neutral subject when the caller names none", () => {
    expect(describeFailure(new ApiError(404, "NodeNotFound", "gone")).body).toContain("that");
  });

  it("maps 403 to forbidden and shows the server's own words", () => {
    // The 403 here is the human tier refusing an agent actor; the service's
    // message is the explanation, so it is surfaced verbatim rather than
    // replaced with UI copy.
    const described = describeFailure(new ApiError(403, "NotPermitted", "only the 'human' actor may accept"));
    expect(described.kind).toBe("forbidden");
    expect(described.body).toBe("only the 'human' actor may accept");
  });

  it("maps 503 to busy and says it is worth retrying", () => {
    const described = describeFailure(new ApiError(503, "OperationalError", "database is locked"));
    expect(described.kind).toBe("busy");
    expect(described.body).toContain("database is locked");
    expect(described.body).toContain("try again");
  });

  it("carries the server's error type into a refusal, so the taxonomy survives", () => {
    const described = describeFailure(new ApiError(409, "InvalidTransition", "already accepted"));
    expect(described.kind).toBe("refused");
    expect(described.body).toBe("InvalidTransition: already accepted");
  });

  it("classifies anything that is neither as unknown, without throwing", () => {
    expect(describeFailure(new RangeError("out of range")).kind).toBe("unknown");
    expect(describeFailure("just a string")).toEqual({
      kind: "unknown",
      title: "Something went wrong",
      body: "just a string",
    });
    expect(describeFailure(undefined).body).toBe("undefined");
  });

  it("falls back to an Error's name when it carries no message", () => {
    // An empty body would render an unexplained panel; the class name is at
    // least a lead.
    expect(describeFailure(new RangeError()).body).toBe("RangeError");
  });

  it("always produces a non-empty title and body", () => {
    const cases: unknown[] = [
      new ApiError(404, "X", "x"),
      new ApiError(403, "X", "x"),
      new ApiError(503, "X", "x"),
      new ApiError(502, "X", "x"),
      new ApiError(400, "X", "x"),
      new TypeError("x"),
      new Error(""),
      null,
    ];
    for (const value of cases) {
      const described = describeFailure(value);
      expect(described.title.length).toBeGreaterThan(0);
      expect(described.body.length).toBeGreaterThan(0);
    }
  });
});

describe("isUnreachable", () => {
  it("is true for both spellings and false for a real answer", () => {
    expect(isUnreachable(new TypeError("Failed to fetch"))).toBe(true);
    expect(isUnreachable(new ApiError(502, "BadGateway", "x"))).toBe(true);
    expect(isUnreachable(new ApiError(504, "GatewayTimeout", "x"))).toBe(true);
    expect(isUnreachable(new ApiError(404, "NodeNotFound", "x"))).toBe(false);
    expect(isUnreachable(new ApiError(500, "RuntimeError", "x"))).toBe(false);
    expect(isUnreachable(new Error("something else"))).toBe(false);
  });
});

describe("isNotFound", () => {
  it("is an API 404 and nothing else", () => {
    expect(isNotFound(new ApiError(404, "NodeNotFound", "x"))).toBe(true);
    expect(isNotFound(new ApiError(403, "NotPermitted", "x"))).toBe(false);
    // A dead server is not evidence that the id is wrong — the view that
    // conflates these tells the user to fix a URL that is fine.
    expect(isNotFound(new TypeError("Failed to fetch"))).toBe(false);
  });
});

describe("describeError", () => {
  it("collapses a gateway status to one plain line", () => {
    expect(describeError(new ApiError(502, "BadGateway", "Bad Gateway"))).toBe(
      "Cannot reach the nodum server.",
    );
    expect(describeError(new TypeError("Failed to fetch"))).toBe("Cannot reach the nodum server.");
  });

  it("keeps `type: message` for anything the server actually answered", () => {
    expect(describeError(new ApiError(409, "InvalidTransition", "already accepted"))).toBe(
      "InvalidTransition: already accepted",
    );
  });

  it("is never empty", () => {
    expect(describeError(new Error(""))).toBe("Error");
    expect(describeError(null)).toBe("null");
    expect(describeError({ weird: true })).toBe("[object Object]");
  });
});
