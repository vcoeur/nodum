/**
 * The two pieces of real logic in the API client: normalising a refused space
 * filter, and the two-request capability upload.
 *
 * Everything else in `client.ts` is a URL and a verb, which type-checking
 * already covers. Neither of these is: the two space-filtered reads answer an
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
 * The upload is here for the same reason: which address it redeems on, what it
 * declines to declare, and that its body is not sent as JSON are three
 * decisions no type expresses, and each of them is the difference between a
 * working ingestion and a silent wrong one.
 *
 * `fetch` is stubbed rather than run: the point is the client's own reaction to
 * a wire shape, and the wire shape is pinned on the Python side by
 * `test_spaces_reach_the_human_over_http` and the capability-URL tests.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  archiveSpace,
  createNode,
  createSpace,
  getCycle,
  ingestUpload,
  isRollbackConflict,
  isUnknownSpace,
  listCycles,
  listNodes,
  recordedUnknownSpace,
  redeemUploadGrant,
  renameSpace,
  rollbackCycle,
  runCycle,
  search,
  UnknownSpaceError,
  uploadRefusalPhase,
} from "./client";
import { describeFailure } from "../lib/failure";
import type { IngestOut, RollbackConflictOut, UploadGrantOut } from "./types";

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

describe("the write path and the lifecycle, not only the two reads", () => {
  // Every one of these used to throw a bare ApiError, and two views grew their
  // own copy of the message match to compensate. The discriminator has one
  // owner; these are the calls that make that true.

  it("normalises the write target a create could not resolve", async () => {
    stubFetch(404, "TypeNotFound", "unknown space: research");
    const error = await createNode({ type: "note", title: "x", space: "research" }).catch(
      (caught: unknown) => caught,
    );

    expect(isUnknownSpace(error)).toBe(true);
    expect((error as UnknownSpaceError).space).toBe("research");
  });

  it("leaves an unknown node type on the create path alone", async () => {
    // Same route, same status: the write path has its own way of producing a
    // 404 that has nothing to do with the space it named.
    stubFetch(404, "TypeNotFound", "unknown type: memo");
    const error = await createNode({ type: "memo", title: "x", space: "research" }).catch(
      (caught: unknown) => caught,
    );

    expect(isUnknownSpace(error)).toBe(false);
  });

  it("normalises nothing for a create that named no target", async () => {
    stubFetch(404, "TypeNotFound", "unknown space: ghost");
    const error = await createNode({ type: "note", title: "x" }).catch(
      (caught: unknown) => caught,
    );

    expect(isUnknownSpace(error)).toBe(false);
  });

  it("normalises a rename and an archive, naming the space each addressed", async () => {
    stubFetch(404, "TypeNotFound", "unknown space: research");
    const renamed = await renameSpace("research", "reference").catch(
      (caught: unknown) => caught,
    );

    stubFetch(404, "TypeNotFound", "unknown space: research");
    const archived = await archiveSpace("research").catch((caught: unknown) => caught);

    expect(isUnknownSpace(renamed)).toBe(true);
    expect(isUnknownSpace(archived)).toBe(true);
    expect((renamed as UnknownSpaceError).space).toBe("research");
    expect((archived as UnknownSpaceError).space).toBe("research");
  });

  it("reports a refused create-space against meta, the space it actually resolves", async () => {
    // `POST /api/spaces` names no space: it is `create_node(space="meta")`
    // underneath, so `meta` is what an unknown-space refusal there is about —
    // never the name being created, which does not exist yet by definition.
    stubFetch(404, "TypeNotFound", "unknown space: meta");
    const error = await createSpace("research").catch((caught: unknown) => caught);

    expect(isUnknownSpace(error)).toBe(true);
    expect((error as UnknownSpaceError).space).toBe("meta");
  });

  it("leaves a duplicate-name refusal from create-space alone", async () => {
    // A taken name is a 409 `SpaceNameTaken`, and it may be an *archived*
    // space that holds it — one no space listing returns. The message is the
    // only account of that, so it must reach the caller untouched.
    stubFetch(
      409,
      "SpaceNameTaken",
      "an archived space already answers to 'research' (id sp-1): archiving a space keeps its name reserved",
    );
    const error = await createSpace("research").catch((caught: unknown) => caught);

    expect(isUnknownSpace(error)).toBe(false);
    expect((error as ApiError).status).toBe(409);
    expect((error as ApiError).message).toContain("archived space already answers to");
  });
});

/**
 * The capability upload is the second piece of real logic in this file: two
 * requests, and three of the decisions behind them are invisible in the types.
 */
describe("the capability upload flow", () => {
  /** One recorded request. */
  interface Call {
    url: string;
    init: RequestInit;
  }

  /** Answer the requests in order, recording what was sent. */
  function stubSequence(...responses: { status?: number; body: unknown }[]): Call[] {
    const calls: Call[] = [];
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init: RequestInit = {}) => {
      calls.push({ url: String(input), init });
      const answer = responses[calls.length - 1] ?? { body: {} };
      return new Response(JSON.stringify(answer.body), {
        status: answer.status ?? 200,
        headers: { "content-type": "application/json" },
      });
    });
    return calls;
  }

  /** A grant whose `url` deliberately names somewhere this page is not. */
  function grant(token: string): UploadGrantOut {
    return {
      grant: {
        kind: "upload",
        token,
        url: `https://nodum.example.org/api/uploads/${token}`,
        asset_hash: null,
        expires_at: "2026-07-27 10:05:00",
        max_bytes: 12,
      },
      asset: null,
    };
  }

  /** Enough of an `IngestOut` to be parsed; the readout is tested elsewhere. */
  function ingested(): Partial<IngestOut> {
    return { created: true, event_seq: 3 };
  }

  const file = () => new File(["a document\n"], "paper.pdf", { type: "application/pdf" });

  it("declares name, mime, size and the write target — and no sha256", () => {
    // D4: a declared hash the graph already holds is answered with the asset and
    // no grant, and that shortcut proves the bytes exist rather than that
    // anything describes them. Declaring one would skip the ingestion asked for.
    const calls = stubSequence({ body: grant("tok-1") }, { body: ingested() });

    return ingestUpload(file(), { space: "research" }).then(() => {
      expect(calls[0]?.url).toBe("/api/uploads");
      const body = JSON.parse(String(calls[0]?.init.body)) as Record<string, unknown>;
      expect(body).toEqual({
        name: "paper.pdf",
        mime: "application/pdf",
        size: 11,
        space: "research",
      });
      expect(body).not.toHaveProperty("sha256");
    });
  });

  it("redeems on our own origin, never the address the grant carries", async () => {
    // D5: `grant.url` is built from `NODUM_PUBLIC_URL`, which exists for a
    // foreign host and may name another machine entirely. The client owns its
    // origin; the grant carries only the capability.
    const calls = stubSequence({ body: grant("tok-1") }, { body: ingested() });
    await ingestUpload(file(), { space: "research" });

    expect(calls[1]?.url).toBe("/api/uploads/tok-1");
    expect(calls[1]?.init.method).toBe("PUT");
  });

  it("sends the bytes raw, without claiming they are JSON", async () => {
    // Every other non-GET here carries `Content-Type: application/json` because
    // the server demands it. The two capability routes sit outside that gate
    // precisely so a raw upload need not lie about its body.
    const sent = file();
    const calls = stubSequence({ body: grant("tok-1") }, { body: ingested() });
    await ingestUpload(sent);

    expect(calls[1]?.init.body).toBe(sent);
    expect(calls[1]?.init.headers).not.toHaveProperty("Content-Type");
  });

  it("says so when a file's type is unknown rather than inventing one", async () => {
    const calls = stubSequence({ body: grant("tok-1") }, { body: ingested() });
    await ingestUpload(new File(["x"], "notes", { type: "" }));

    const body = JSON.parse(String(calls[0]?.init.body)) as Record<string, unknown>;
    expect(body["mime"]).toBe("application/octet-stream");
  });

  it("normalises a write target the mint refused", async () => {
    stubSequence({
      status: 404,
      body: { error: { type: "TypeNotFound", message: "unknown space: research" } },
    });
    const error = await ingestUpload(file(), { space: "research" }).catch(
      (caught: unknown) => caught,
    );

    expect(isUnknownSpace(error)).toBe(true);
    expect((error as UnknownSpaceError).space).toBe("research");
  });

  it("normalises the same refusal from the redemption, which resolves the space again", async () => {
    // A space archived between the mint and the PUT: the pipeline resolves the
    // token row's space on the far side, so the refusal arrives from the second
    // request with the same message and belongs to the same discriminator.
    stubSequence(
      { body: grant("tok-1") },
      {
        status: 404,
        body: { error: { type: "TypeNotFound", message: "unknown space: sp-old" } },
      },
    );
    const error = await ingestUpload(file(), { space: "sp-old" }).catch(
      (caught: unknown) => caught,
    );

    expect(isUnknownSpace(error)).toBe(true);
    expect((error as UnknownSpaceError).space).toBe("sp-old");
  });

  it("normalises a refused space on the exported redemption, with no mint in sight", async () => {
    // `redeemUploadGrant` is exported on the `api` barrel and its docstring
    // invites direct use, so it has to be safe on its own. Unnormalised, a
    // direct caller has no sanctioned way to recognise the refusal —
    // `isUnknownSpace` answers false — and the only thing left is
    // `describeFailure`, which renders any 404 as *"The server has no record of
    // …"* plus the server's own *"unknown space: sp-old"*: two forbidden
    // phrasings in one sentence. Normalising is what gives the caller the
    // discriminator that keeps it away from that path.
    stubSequence({
      status: 404,
      body: { error: { type: "TypeNotFound", message: "unknown space: sp-old" } },
    });
    const error = await redeemUploadGrant("tok-1", file()).catch((caught: unknown) => caught);

    expect(isUnknownSpace(error)).toBe(true);
    expect((error as UnknownSpaceError).type).toBe("UnknownSpace");
    // The token row's space is what the pipeline resolved, so the message names
    // the id; that is all this half can know, and it is not left bare.
    expect((error as UnknownSpaceError).space).toBe("sp-old");
    expect((error as UnknownSpaceError).wireStatus).toBe(404);
  });

  it("leaves a redemption refusal that is not about a space alone", async () => {
    stubSequence({
      status: 400,
      body: {
        error: { type: "UnsupportedUpload", message: "these bytes are not a type this route can act on" },
      },
    });
    const error = await redeemUploadGrant("tok-1", file()).catch((caught: unknown) => caught);

    expect(isUnknownSpace(error)).toBe(false);
    expect((error as ApiError).type).toBe("UnsupportedUpload");
  });

  it("says which request refused, because the two are not the same event", async () => {
    // A refused mint sent nothing; a refused redemption spent the grant and
    // streamed the whole file. Copy that cannot tell them apart claims "nothing
    // was uploaded" about a request that uploaded all of it.
    stubSequence({
      status: 404,
      body: { error: { type: "TypeNotFound", message: "unknown space: research" } },
    });
    const fromMint = await ingestUpload(file(), { space: "research" }).catch(
      (caught: unknown) => caught,
    );

    stubSequence(
      { body: grant("tok-1") },
      {
        status: 404,
        body: { error: { type: "TypeNotFound", message: "unknown space: sp-old" } },
      },
    );
    const fromRedemption = await ingestUpload(file(), { space: "reading" }).catch(
      (caught: unknown) => caught,
    );

    expect(uploadRefusalPhase(fromMint)).toBe("mint");
    expect(uploadRefusalPhase(fromRedemption)).toBe("redemption");
    // Still one discriminator for space-ness: the phase is extra information,
    // not a second way to ask whether a space was refused.
    expect(isUnknownSpace(fromMint)).toBe(true);
    expect(isUnknownSpace(fromRedemption)).toBe(true);
  });

  it("re-labels a redemption refusal with the reference the human typed", async () => {
    // The pipeline reports the *resolved* id, which is a 32-hex string nobody
    // typed; the caller asked for `reading`, and that is what the copy has to
    // resolve and name.
    stubSequence(
      { body: grant("tok-1") },
      {
        status: 404,
        body: {
          error: { type: "TypeNotFound", message: "unknown space: 18ee0caa66204b5284774855a9d5cb34" },
        },
      },
    );
    const error = await ingestUpload(file(), { space: "reading" }).catch(
      (caught: unknown) => caught,
    );

    expect((error as UnknownSpaceError).space).toBe("reading");
    expect(uploadRefusalPhase(error)).toBe("redemption");
  });

  it("has no phase on a bare unknown-space error, which no upload raises", () => {
    expect(uploadRefusalPhase(new UnknownSpaceError("sp-old", 404, "unknown space: sp-old"))).toBeNull();
    expect(uploadRefusalPhase(new ApiError(400, "TokenInvalid", "invalid or expired token"))).toBeNull();
  });

  it("does not crash on a grantless answer, and says nothing was ingested", async () => {
    // Unreachable while no sha256 is declared, so this is the shape being
    // handled honestly rather than dereferenced into a TypeError.
    const calls = stubSequence({
      body: { grant: null, asset: { hash: "a".repeat(64) } },
    });
    const error = await ingestUpload(file()).catch((caught: unknown) => caught);

    expect(calls).toHaveLength(1);
    expect((error as ApiError).type).toBe("MissingUploadGrant");
    expect((error as ApiError).message).toContain("not ingested");
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

describe("the consolidation cycle routes", () => {
  /** One captured request. */
  interface Call {
    url: string;
    init: RequestInit;
  }

  /** Answer every request with one body, capturing what was sent. */
  function capture(body: unknown, status = 200): Call[] {
    const calls: Call[] = [];
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init: RequestInit = {}) => {
      calls.push({ url: String(input), init });
      return new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      });
    });
    return calls;
  }

  /** The JSON body of a captured call. */
  function sentBody(call: Call | undefined): unknown {
    return JSON.parse(String(call?.init.body ?? "null"));
  }

  it("unwraps the journal's list envelope", async () => {
    const calls = capture({ cycles: [{ id: "cy-1" }], count: 1 });
    const cycles = await listCycles(100);

    expect(calls[0]?.url).toBe("/api/cycles?limit=100");
    expect(cycles).toHaveLength(1);
  });

  it("sends the event window on the detail read, so the notice can name it", async () => {
    const calls = capture({ cycle: { id: "cy-1" }, metrics: {}, events: [], events_truncated: false });
    await getCycle("cy-1", { limit: 500 });

    expect(calls[0]?.url).toBe("/api/cycles/cy-1?limit=500");
  });

  it("runs a cycle with dry_run as a real boolean, never a string", async () => {
    // The server refuses `"false"` rather than coercing it, which is the right
    // posture for a rehearsal flag — and the reason the client must not stringify.
    const calls = capture({ cycle: { id: "cy-2" }, report: {} });
    await runCycle({ dry_run: false });

    expect(calls[0]?.url).toBe("/api/cycles");
    expect(calls[0]?.init.method).toBe("POST");
    expect(sentBody(calls[0])).toEqual({ dry_run: false });
  });

  it("names a scope only when one was chosen, so the server's default applies", async () => {
    const scoped = capture({ cycle: { id: "cy-3" }, report: {} });
    await runCycle({ scope: "research", dry_run: true });
    expect(sentBody(scoped[0])).toEqual({ scope: "research", dry_run: true });

    vi.unstubAllGlobals();
    const unscoped = capture({ cycle: { id: "cy-4" }, report: {} });
    await runCycle();
    expect(sentBody(unscoped[0])).toEqual({});
  });

  it("normalises a scope the server would not resolve, like every other space call", async () => {
    // `open_cycle` resolves the scope through the ordinary space rule, so a
    // space archived since the picker was filled is refused here — and a bare
    // ApiError would leave the view with nothing but the message to match on.
    capture({ error: { type: "TypeNotFound", message: "unknown space: research" } }, 404);
    const error = await runCycle({ scope: "research" }).catch((caught: unknown) => caught);

    expect(isUnknownSpace(error)).toBe(true);
    expect((error as UnknownSpaceError).space).toBe("research");
  });

  it("leaves a refusal alone when the run named no scope", async () => {
    capture({ error: { type: "GrantNotPermitted", message: "open a cycle" } }, 403);
    const error = await runCycle().catch((caught: unknown) => caught);

    expect(isUnknownSpace(error)).toBe(false);
  });

  it("asks the rollback route to rehearse, which is what the confirm dialog needs", async () => {
    const calls = capture({ cycle_id: "cy-1", dry_run: true, conflicts: [] });
    await rollbackCycle("cy-1", { dryRun: true });

    expect(calls[0]?.url).toBe("/api/cycles/cy-1/rollback");
    expect(sentBody(calls[0])).toEqual({ dry_run: true });
  });

  it("keeps a refused rollback's conflicts, which live nowhere but the error body", async () => {
    // The one failure on this surface carrying more than type and message. A
    // generic ApiError would drop the rows, and "rollback failed" is exactly
    // the message decision C4 exists to avoid.
    const conflicts: RollbackConflictOut[] = [
      {
        kind: "edge",
        row_id: "e1",
        cycle_event_seq: 42,
        cycle_event_op: "edge.propose",
        conflicting_seq: 57,
        conflicting_op: "edge.accept",
        conflicting_actor: "human:alice",
        conflicting_cycle_id: null,
      },
    ];
    capture(
      { error: { type: "RollbackConflict", message: "the graph has moved on", conflicts } },
      409,
    );
    const error = await rollbackCycle("cy-1").catch((caught: unknown) => caught);

    expect(isRollbackConflict(error)).toBe(true);
    expect((error as ApiError).status).toBe(409);
    expect(isRollbackConflict(error) ? error.conflicts : []).toEqual(conflicts);
  });

  it("leaves every other failure an ordinary ApiError", async () => {
    capture({ error: { type: "InvalidTransition", message: "cycle cy-1 is already running" } }, 409);
    const error = await rollbackCycle("cy-1").catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(isRollbackConflict(error)).toBe(false);
  });
});

describe("the same refusal, recorded rather than caught", () => {
  /**
   * What `nodum.consolidate` stores for a failure — `f"{type(failure).__name__}:
   * {failure}"` — for the refusal a scoped cycle actually meets first.
   */
  const RECORDED = "TypeNotFound: unknown space: f73944650d5c4255a0aa5421308f62b0";

  it("recognises an unresolvable space in a cycle report's own error string", () => {
    // The journal renders failures that were caught hours ago on the server, so
    // there is no response left for `isUnknownSpace` to test — and the copy rule
    // applies to that text exactly as it does to a live refusal. This is the
    // same regex, not a second copy of it.
    expect(recordedUnknownSpace(RECORDED)).toBe("f73944650d5c4255a0aa5421308f62b0");
  });

  it("reads the message with no exception prefix in front of it too", () => {
    expect(recordedUnknownSpace("unknown space: research")).toBe("research");
  });

  it("answers null for a recorded failure that is about something else", () => {
    expect(recordedUnknownSpace("GrantNotPermitted: open a consolidation cycle")).toBeNull();
    expect(recordedUnknownSpace("boom")).toBeNull();
    expect(recordedUnknownSpace("")).toBeNull();
  });

  it("does not eat a colon that is part of the message", () => {
    // The prefix strip is identifier-shaped and anchored, so a message that
    // merely contains a colon keeps all of itself.
    expect(recordedUnknownSpace("unknown type: note")).toBeNull();
  });

  it("agrees with the live discriminator on the same wire message", () => {
    // The property that matters: whichever way the refusal reaches a view, the
    // answer to "was this a space that would not resolve" is one answer.
    const live = new UnknownSpaceError("research", 404, "unknown space: research");
    expect(isUnknownSpace(live)).toBe(true);
    expect(recordedUnknownSpace(`TypeNotFound: ${live.message}`)).toBe("research");
  });
});
