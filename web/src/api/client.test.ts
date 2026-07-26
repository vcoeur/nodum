/**
 * The API client's one piece of real logic: normalising a refused space filter.
 *
 * Everything else in `client.ts` is a URL and a verb, which type-checking
 * already covers. This does not: the two space-filtered reads answer an
 * unresolvable space with **different statuses** — 404 from `GET /api/nodes`
 * (the service's `TypeNotFound`), 400 from `GET /api/search` (a bare
 * `ValueError`, since `nodum.search` does not import the service's exception
 * vocabulary) — and the client's job is to make sure no view ever learns that.
 *
 * So what is asserted here is the *equivalence*: the same user-visible event
 * arrives as the same error, and `describeFailure` renders it the same way,
 * whichever endpoint refused. Plus the other half of that promise — refusals
 * that are **not** about a space keep their own identity, which a normalisation
 * keyed on the status alone would have swallowed (a 404 from the listing is
 * equally an unknown `type` filter).
 *
 * `fetch` is stubbed rather than run: the point is the client's own reaction to
 * a wire shape, and the wire shape is pinned on the Python side by
 * `test_spaces_reach_the_human_over_http`.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, isUnknownSpace, listNodes, search, UnknownSpaceError } from "./client";
import { describeFailure } from "../lib/failure";

/** Answer the next request with one status and one error envelope. */
function stubFetch(status: number, type: string, message: string) {
  const spy = vi.fn(
    async () =>
      new Response(JSON.stringify({ error: { type, message } }), {
        status,
        headers: { "content-type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", spy);
  return spy;
}

/**
 * Answer every request with a successful envelope.
 *
 * @returns The array the requested URLs accumulate in, in call order.
 */
function stubOk(body: unknown): string[] {
  const urls: string[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    urls.push(String(input));
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
  return urls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("an unresolvable space filter", () => {
  it("is the same failure from the node listing and from search", async () => {
    stubFetch(404, "TypeNotFound", "unknown space: nope");
    const fromListing = await listNodes({ space: "nope" }).catch((error: unknown) => error);

    stubFetch(400, "ValueError", "unknown space: nope");
    const fromSearch = await search("anything", { space: "nope" }).catch(
      (error: unknown) => error,
    );

    expect(isUnknownSpace(fromListing)).toBe(true);
    expect(isUnknownSpace(fromSearch)).toBe(true);
    expect((fromListing as UnknownSpaceError).type).toBe("UnknownSpace");
    expect((fromSearch as UnknownSpaceError).type).toBe("UnknownSpace");
    expect((fromListing as UnknownSpaceError).space).toBe("nope");
    expect((fromSearch as UnknownSpaceError).space).toBe("nope");
  });

  it("renders identically through describeFailure, which is the whole point", async () => {
    stubFetch(404, "TypeNotFound", "unknown space: nope");
    const fromListing = await listNodes({ space: "nope" }).catch((error: unknown) => error);

    stubFetch(400, "ValueError", "unknown space: nope");
    const fromSearch = await search("anything", { space: "nope" }).catch(
      (error: unknown) => error,
    );

    expect(describeFailure(fromSearch)).toEqual(describeFailure(fromListing));
    // Without the normalisation this would be "not-found" against the listing
    // and "refused" against search — one event, two panels.
    expect(describeFailure(fromListing).kind).toBe("not-found");
  });

  it("keeps what the server actually said, for anyone reading the network tab", async () => {
    stubFetch(404, "TypeNotFound", "unknown space: nope");
    const fromListing = (await listNodes({ space: "nope" }).catch(
      (error: unknown) => error,
    )) as UnknownSpaceError;

    stubFetch(400, "ValueError", "unknown space: nope");
    const fromSearch = (await search("q", { space: "nope" }).catch(
      (error: unknown) => error,
    )) as UnknownSpaceError;

    expect(fromListing.wireStatus).toBe(404);
    expect(fromSearch.wireStatus).toBe(400);
    expect(fromListing.status).toBe(404);
    expect(fromSearch.status).toBe(404);
  });

  it("stays an ApiError, so a view catching those still catches it", async () => {
    stubFetch(400, "ValueError", "unknown space: nope");
    const error = await search("q", { space: "nope" }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
  });
});

describe("refusals that are not about a space", () => {
  it("leaves an unknown type filter alone, even with a space also set", async () => {
    // Same endpoint, same status, same exception class — only the message
    // differs, which is exactly why the status cannot be the discriminator.
    stubFetch(404, "TypeNotFound", "unknown type: nope");
    const error = await listNodes({ space: "research", type: "nope" }).catch(
      (caught: unknown) => caught,
    );

    expect(isUnknownSpace(error)).toBe(false);
    expect((error as ApiError).type).toBe("TypeNotFound");
  });

  it("leaves any other 400 from search alone", async () => {
    stubFetch(400, "ValueError", "k must be positive");
    const error = await search("q", { space: "research", k: -1 }).catch(
      (caught: unknown) => caught,
    );

    expect(isUnknownSpace(error)).toBe(false);
    expect((error as ApiError).type).toBe("ValueError");
  });

  it("normalises nothing when the call named no space at all", async () => {
    stubFetch(404, "TypeNotFound", "unknown space: ghost");
    const error = await listNodes().catch((caught: unknown) => caught);

    expect(isUnknownSpace(error)).toBe(false);
  });

  it("leaves an unreachable server unreachable rather than blaming the space", async () => {
    // The dev proxy's 502 is an ApiError too. Reporting it as a bad space would
    // send the reader hunting for a space problem that does not exist.
    stubFetch(502, "HTTPError", "Bad Gateway");
    const error = await listNodes({ space: "research" }).catch((caught: unknown) => caught);

    expect(isUnknownSpace(error)).toBe(false);
    expect(describeFailure(error).kind).toBe("unreachable");
  });
});

describe("the space controls reach the wire", () => {
  it("sends space and include_meta on the node listing", async () => {
    const urls = stubOk({ nodes: [], count: 0 });
    await listNodes({ space: "research", include_meta: true });

    expect(urls[0]).toBe("/api/nodes?space=research&include_meta=true");
  });

  it("sends space and include_meta on search, alongside the query", async () => {
    const urls = stubOk({ query: "q", k: 10, hits: [] });
    await search("q", { space: "research", include_meta: false });

    expect(urls[0]).toBe("/api/search?q=q&space=research&include_meta=false");
  });

  it("omits both when they are not set, so the server's defaults apply", async () => {
    const urls = stubOk({ nodes: [], count: 0 });
    await listNodes({ type: "note" });

    expect(urls[0]).toBe("/api/nodes?type=note");
  });
});
