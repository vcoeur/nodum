/**
 * What the queue is allowed to say about a drop.
 *
 * Three semantics, and each one replaces something the panel used to get from
 * somewhere other than the server:
 *
 * - **the status is `created`**, not a comparison against hashes read before
 *   the batch. The old test answered "do these bytes exist?" while the row
 *   claimed to answer "is this document described?" — two different questions
 *   whenever the bytes were registered through the editor's drop;
 * - **the readout reports the subgraph**: the space it was filed in, how many
 *   per-page blocks landed, and whether any text came out — carrying the
 *   extraction's own `detail` when none did, because that is the only place
 *   *"install the `pdf` extra"* is ever said, and **only** on the branch where
 *   an extraction ran, because the other branch puts an idempotency note in
 *   that field;
 * - **no copy claims a space is missing, and none offers to resume.** The server
 *   answers a space that was never created and a space the caller holds no grant
 *   on with identical text on purpose, so the forbidden vocabulary is swept for
 *   here rather than left to a reviewer's eye — over every string the module can
 *   produce, not one branch of it.
 */

import { describe, expect, it } from "vitest";
import { ApiError, UnknownSpaceError, UnknownUploadSpaceError } from "../../api/client";
import {
  describeUploadFailure,
  readIngestion,
  uploadDetailLine,
  UPLOAD_STATUSES,
  type UploadOutcome,
} from "./uploadOutcome";
import { nameSpace } from "../../components/spaceNaming";
import type { IngestOut, NodeOut } from "../../api/types";

/** A node, with only the fields any of this reads set meaningfully. */
function node(id: string, overrides: Partial<NodeOut> = {}): NodeOut {
  return {
    id,
    space_id: "sp-research",
    type: "note",
    parent_id: null,
    position: null,
    title: id,
    content: "",
    props: {},
    state: "active",
    created_by: "human:alice",
    created_at: "2026-07-27 10:00:00",
    updated_at: "2026-07-27 10:00:00",
    ...overrides,
  };
}

/** A space node, as `GET /api/spaces` and the archived listing return them. */
function space(id: string, title: string): NodeOut {
  return node(id, { type: "space", space_id: "meta", title });
}

/** A per-page `block` child of the `source` node. */
function page(id: string): NodeOut {
  return node(id, { type: "block" });
}

/** An ingestion answer; every field the readout touches is overridable. */
function ingestion(overrides: Partial<IngestOut> = {}): IngestOut {
  return {
    asset: {
      hash: "a".repeat(64),
      mime: "application/pdf",
      size_bytes: 4096,
      original_name: "paper.pdf",
      extracted_text: "text",
      created_at: "2026-07-27 10:00:00",
    },
    asset_ref: node("ref-1"),
    source: node("src-1"),
    pages: [page("page-1"), page("page-2")],
    pages_truncated: false,
    edges: [],
    extraction: { handler: "pdf", chars: 4321, pages: 2, detail: null },
    created: true,
    event_seq: 12,
    ...overrides,
  };
}

describe("the status is the server's, not the client's", () => {
  it("reads created: true as ingested", () => {
    expect(readIngestion(ingestion({ created: true })).status).toBe("ingested");
  });

  it("reads created: false as already ingested", () => {
    // The same bytes and the same nodes: only `created` differs, which is the
    // whole point — nothing here may reach for the hash to decide this.
    expect(readIngestion(ingestion({ created: false })).status).toBe("already-ingested");
  });

  it("takes the space and the source node from the answer, not from the request", () => {
    const outcome = readIngestion(
      ingestion({
        asset_ref: node("ref-1", { space_id: "sp-main" }),
        source: node("src-9"),
      }),
    );

    expect(outcome.spaceId).toBe("sp-main");
    expect(outcome.sourceId).toBe("src-9");
  });
});

describe("the page-block count is the subgraph's, not the extraction's", () => {
  it("counts the block nodes that landed", () => {
    // Blank pages are skipped by the pipeline, so a 40-page scan can produce
    // two blocks. The row reports what is in the graph.
    const outcome = readIngestion(
      ingestion({
        pages: [page("page-1"), page("page-2")],
        extraction: { handler: "pdf", chars: 10, pages: 40, detail: null },
      }),
    );

    expect(outcome.pageBlocks).toBe(2);
  });

  it("counts only blocks, whatever else is handed back as a child", () => {
    // The server restricts this list to non-archived page blocks; the count is a
    // claim on screen, so anything else under `source` is not a page here either.
    const outcome = readIngestion(
      ingestion({ pages: [page("page-1"), node("note-1"), node("ref-2", { type: "asset_ref" })] }),
    );

    expect(outcome.pageBlocks).toBe(1);
  });

  it("carries the truncation flag through", () => {
    expect(readIngestion(ingestion({ pages_truncated: true })).pagesTruncated).toBe(true);
  });
});

describe("a malformed answer", () => {
  it("is a refusal, not a TypeError the classifier would call unreachable", () => {
    // Inside the queue's `try` a dereference of a missing field throws a
    // `TypeError`, and `describeFailure` reads a `TypeError` as *nothing was
    // listening* — so the row would blame a dead server for a request that
    // plainly arrived. Guarded the way the mint is guarded in `api/client.ts`.
    const malformed = ingestion();
    delete (malformed as Partial<IngestOut>).source;

    let caught: unknown;
    try {
      readIngestion(malformed);
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(ApiError);
    expect(caught).not.toBeInstanceOf(TypeError);
    expect((caught as ApiError).type).toBe("MalformedIngestResult");
  });

  it("still reads an answer that simply carried no describing space", () => {
    const malformed = ingestion();
    delete (malformed as Partial<IngestOut>).asset_ref;

    expect(readIngestion(malformed).spaceId).toBeNull();
  });
});

describe("the extraction detail", () => {
  it("is carried when no text came out, which is the only account of why", () => {
    const outcome = readIngestion(
      ingestion({
        extraction: {
          handler: "none",
          chars: 0,
          pages: 0,
          detail: "no handler for application/pdf: install the 'pdf' extra",
        },
      }),
    );

    expect(outcome.detail).toBe("no handler for application/pdf: install the 'pdf' extra");
    expect(uploadDetailLine(outcome, nameSpace("sp-research", [], []))).toContain(
      "install the 'pdf' extra",
    );
  });

  it("is dropped when text came out, where the character count says more", () => {
    const outcome = readIngestion(
      ingestion({
        extraction: { handler: "pdf", chars: 900, pages: 3, detail: "page 4 of 9 truncated" },
      }),
    );

    expect(outcome.detail).toBeNull();
    expect(uploadDetailLine(outcome, nameSpace("sp-research", [], []))).not.toContain("truncated");
  });

  it("never wears the idempotency note, which answers a different question", () => {
    // The second drop of an OCR-less PNG. The server skips extraction on this
    // branch and puts its own note in `detail`, so letting it through read as
    // *"no text extracted — already ingested into this space"* — a causal claim
    // that is false, in the slot where *"install the 'ocr' extra"* had been.
    const outcome = readIngestion(
      ingestion({
        created: false,
        extraction: {
          handler: "none",
          chars: 0,
          pages: 0,
          detail: "already ingested into this space; nothing re-extracted",
        },
      }),
    );

    expect(outcome.detail).toBeNull();
    const line = uploadDetailLine(outcome, nameSpace("sp-research", [], []));
    expect(line).not.toContain("already ingested into this space");
    expect(line).not.toContain("no text extracted —");
    // What is true instead: the record is empty and this drop extracted nothing.
    expect(line).toContain("no text on record, and nothing was re-extracted");
  });

  it("keeps the actionable sentence on the drop that did the extracting", () => {
    // The same file, first drop. This is the contrast the fix exists for.
    const first = readIngestion(
      ingestion({
        created: true,
        pages: [],
        extraction: {
          handler: "none",
          chars: 0,
          pages: 0,
          detail: "pytesseract is not installed (install the 'ocr' extra)",
        },
      }),
    );

    expect(uploadDetailLine(first, nameSpace("sp-research", [], []))).toContain(
      "no text extracted — pytesseract is not installed (install the 'ocr' extra)",
    );
  });
});

describe("the detail line", () => {
  const research = space("sp-research", "research");

  /** A settled outcome, with the counts each test cares about. */
  function outcome(overrides: Partial<UploadOutcome> = {}): UploadOutcome {
    return {
      status: "ingested",
      spaceId: "sp-research",
      sourceId: "src-1",
      pageBlocks: 2,
      pagesTruncated: false,
      chars: 4321,
      detail: null,
      ...overrides,
    };
  }

  it("always names the space the server filed into", () => {
    const line = uploadDetailLine(outcome(), nameSpace("sp-research", [research], []));

    expect(line).toContain("Filed in research");
    // The id it landed under is never the thing on screen.
    expect(line).not.toContain("sp-research");
  });

  it("does not report a filing on the drop where nothing was filed", () => {
    // The status cell one column to the left says "already ingested"; opening
    // with "Filed in research" had the two halves of one row contradict.
    const line = uploadDetailLine(
      outcome({ status: "already-ingested" }),
      nameSpace("sp-research", [research], []),
    );

    expect(line).toContain("Already described in research");
    expect(line).not.toContain("Filed in");
  });

  it("names an archived landing space rather than printing its id", () => {
    const retired = space("sp-old", "reading");
    const line = uploadDetailLine(
      outcome({ spaceId: "sp-old" }),
      nameSpace("sp-old", [research], [retired]),
    );

    expect(line).toContain("Filed in reading");
    expect(line).not.toContain("sp-old");
  });

  it("says so plainly when the response carried no space at all", () => {
    const line = uploadDetailLine(outcome({ spaceId: null }), null);

    expect(line).toContain("The server named no space");
  });

  it("counts page blocks, and says none rather than zero", () => {
    expect(uploadDetailLine(outcome({ pageBlocks: 0 }), nameSpace("sp-research", [research], [])))
      .toContain("no page blocks");
    expect(uploadDetailLine(outcome({ pageBlocks: 1 }), nameSpace("sp-research", [research], [])))
      .toContain("1 page block ");
    expect(uploadDetailLine(outcome({ pageBlocks: 7 }), nameSpace("sp-research", [research], [])))
      .toContain("7 page blocks");
  });

  it("reports a capped document instead of letting the count imply the whole of it", () => {
    const line = uploadDetailLine(
      outcome({ pageBlocks: 100, pagesTruncated: true }),
      nameSpace("sp-research", [research], []),
    );

    expect(line).toContain("100 page blocks");
    expect(line).toContain("later pages were not filed as blocks");
  });

  it("reports the character count, and 'no text extracted' for nothing", () => {
    expect(uploadDetailLine(outcome({ chars: 1 }), nameSpace("sp-research", [research], [])))
      .toContain("1 character of text");
    expect(uploadDetailLine(outcome({ chars: 4321 }), nameSpace("sp-research", [research], [])))
      .toContain("4321 characters of text");
    expect(uploadDetailLine(outcome({ chars: 0 }), nameSpace("sp-research", [research], [])))
      .toContain("no text extracted");
  });

  it("still names the space and the pages when nothing came out of the file", () => {
    // An image with no OCR handler is the ordinary case, and it is not a
    // failure: the bytes are described, they simply carry no text.
    const line = uploadDetailLine(
      outcome({ pageBlocks: 0, chars: 0, detail: "no handler for image/png" }),
      nameSpace("sp-research", [research], []),
    );

    expect(line).toBe(
      "Filed in research · no page blocks · no text extracted — no handler for image/png",
    );
  });
});

describe("a refused drop", () => {
  const research = space("sp-research", "research");
  const retired = space("sp-old", "reading");
  /** Every wording nothing user-facing may use about a space. */
  const FORBIDDEN = [
    "no such space",
    "does not exist",
    "unknown space",
    "missing space",
    "nonexistent space",
    "not found",
    "no record of",
  ];

  /** The refusal as the mint raises it: nothing has been sent. */
  function refusedMint(reference: string): UnknownUploadSpaceError {
    return new UnknownUploadSpaceError(reference, 404, `unknown space: ${reference}`, "mint");
  }

  /** The refusal as the redemption raises it: the whole file has been sent. */
  function refusedRedemption(reference: string): UnknownUploadSpaceError {
    return new UnknownUploadSpaceError(reference, 404, `unknown space: ${reference}`, "redemption");
  }

  it("says what changed about an archived write target, and names it", () => {
    const message = describeUploadFailure(refusedMint("sp-old"), "sp-old", [research], [retired]);

    expect(message).toContain("reading");
    expect(message).toContain("has been archived");
    expect(message).not.toContain("sp-old");
    for (const phrase of FORBIDDEN) expect(message.toLowerCase()).not.toContain(phrase);
  });

  it("claims nothing was sent only for the request that sent nothing", () => {
    // The mint checks the target before a byte moves, so this is the one phase
    // where "nothing was sent" is a fact.
    const message = describeUploadFailure(refusedMint("sp-old"), "sp-old", [research], [retired]);

    expect(message).toBe(
      "The write target reading has been archived — an archived space stops resolving, so " +
        "nothing new can be filed there. Nothing was sent. Choose another write target, then " +
        "drop the file again.",
    );
  });

  it("does not deny an upload the redemption had already taken in full", () => {
    // `urls.consume` spends the grant before the body is read, so by the time
    // the pipeline resolves the space the whole file has arrived. A row denying
    // it sat beside a grid that had just gained a tile.
    const message = describeUploadFailure(
      refusedRedemption("sp-old"),
      "sp-old",
      [research],
      [retired],
    );

    expect(message).toBe(
      "The write target reading has been archived — an archived space stops resolving, so " +
        "nothing new can be filed there. The file was sent, but nothing was stored and nothing " +
        "describes it. Choose another write target, then drop the file again.",
    );
    expect(message).not.toContain("Nothing was sent");
  });

  it("keeps the phase out of the space test, which stays isUnknownSpace alone", () => {
    // A bare `UnknownSpaceError` is still a refused space and still gets the
    // copy — it simply carries no phase, so the clause that depends on one is
    // dropped rather than guessed.
    const message = describeUploadFailure(
      new UnknownSpaceError("sp-old", 404, "unknown space: sp-old"),
      "sp-old",
      [research],
      [retired],
    );

    expect(message).toContain("has been archived");
    expect(message).not.toContain("was sent");
    expect(message).toContain("Choose another write target");
  });

  it("falls back to the disjunction when neither list names the target", () => {
    const message = describeUploadFailure(refusedMint("ghost"), "ghost", [research], []);

    expect(message).toContain("would not resolve");
    expect(message).toContain("renamed space no longer answers to its old name");
    for (const phrase of FORBIDDEN) expect(message.toLowerCase()).not.toContain(phrase);
  });

  it("says why the label is a reference while the space list is still in flight", () => {
    // Null in, null through. `?? []` at this call site turns `pending` into
    // `unknown`, and the two now read differently on screen — which is what
    // makes the rule testable through this function at all rather than only
    // through `unresolvedSpaceIds`.
    const pending = describeUploadFailure(refusedMint("sp-old"), "sp-old", null, [retired]);
    const asIfEmpty = describeUploadFailure(refusedMint("sp-old"), "sp-old", [], [retired]);

    expect(pending).toContain("sp-old");
    expect(pending).toContain("the space list has not answered yet");
    // With `?? []` the archived listing answers instead and the label becomes a
    // name, which is a different sentence about a different state of knowledge.
    expect(asIfEmpty).not.toContain("has not answered");
    expect(pending).not.toBe(asIfEmpty);
    for (const phrase of FORBIDDEN) expect(pending.toLowerCase()).not.toContain(phrase);
  });

  it("hands everything else to the shared classifier rather than guessing", () => {
    const refused = describeUploadFailure(
      new ApiError(400, "UnsupportedUpload", "these bytes are not a type this route can act on"),
      "research",
      [research],
      [],
    );

    expect(refused).toBe("UnsupportedUpload: these bytes are not a type this route can act on");
  });

  it("reports a dead server as unreachable rather than as a refusal", () => {
    // The dev proxy answers a stopped backend with a 502, which is an ApiError
    // too: re-deriving the meaning here is what would call it a refusal.
    const message = describeUploadFailure(
      new ApiError(502, "HTTPError", "Bad Gateway"),
      "research",
      [research],
      [],
    );

    expect(message).toContain("never reached the API");
  });

  it("describes a spent or expired grant through the shared classifier", () => {
    // `urls.consume` spends the token before the body is read, so this is what
    // a second attempt at one grant looks like: an ordinary refusal, described
    // by the one classifier rather than by a branch of its own here.
    const message = describeUploadFailure(
      new ApiError(400, "TokenInvalid", "invalid or expired token"),
      "research",
      [research],
      [],
    );

    expect(message).toBe("TokenInvalid: invalid or expired token");
  });
});

/**
 * The sweep, over **every** string this module can put on screen rather than one
 * branch that could never have produced the word.
 *
 * `urls.consume` spends the token before the body is read, so there is no
 * resumable state anywhere in this flow: a retry is a fresh mint, which is what
 * dropping the file again does. That is a rule about the copy as a whole, and it
 * only has teeth if the assertion covers the whole of it.
 */
describe("no copy here offers to continue something that cannot be continued", () => {
  const research = space("sp-research", "research");
  const retired = space("sp-old", "reading");

  /** Every sentence the module's exported functions can produce. */
  function everySentence(): string[] {
    const outcomes: UploadOutcome[] = UPLOAD_STATUSES.filter(
      (status): status is "ingested" | "already-ingested" =>
        status === "ingested" || status === "already-ingested",
    ).flatMap((status) =>
      [0, 1, 4321].flatMap((chars) =>
        [0, 1, 7].flatMap((pageBlocks) =>
          [false, true].flatMap((pagesTruncated) =>
            [null, "no handler for image/png"].map((detail) => ({
              status,
              spaceId: "sp-research",
              sourceId: "src-1",
              pageBlocks,
              pagesTruncated,
              chars,
              detail,
            })),
          ),
        ),
      ),
    );

    const lines = outcomes.flatMap((outcome) => [
      uploadDetailLine(outcome, nameSpace("sp-research", [research], [])),
      uploadDetailLine(outcome, nameSpace("sp-old", [research], [retired])),
      uploadDetailLine(outcome, null),
    ]);

    const refusals = [
      new UnknownUploadSpaceError("sp-old", 404, "unknown space: sp-old", "mint"),
      new UnknownUploadSpaceError("sp-old", 404, "unknown space: sp-old", "redemption"),
      new UnknownSpaceError("sp-old", 404, "unknown space: sp-old"),
      new ApiError(400, "TokenInvalid", "invalid or expired token"),
      new ApiError(413, "UploadTooLarge", "the declared size is above the ceiling"),
      new ApiError(502, "HTTPError", "Bad Gateway"),
      new ApiError(503, "DatabaseBusy", "database is locked"),
    ].flatMap((error) =>
      [
        [research] as readonly NodeOut[] | null,
        null,
      ].map((spaces) => describeUploadFailure(error, "sp-old", spaces, [retired])),
    );

    return [...lines, ...refusals];
  }

  it("never says resume, continue, or pick up where it left off", () => {
    const sentences = everySentence();

    // Guard the guard: an empty sweep would pass every assertion below it.
    expect(sentences.length).toBeGreaterThan(100);
    for (const sentence of sentences) {
      expect(sentence.toLowerCase()).not.toMatch(/resume|resumable|pick up where|continue/);
    }
  });

  it("never says a space does not exist, on any branch of any of them", () => {
    for (const sentence of everySentence()) {
      expect(sentence.toLowerCase()).not.toMatch(
        /no such space|does not exist|nonexistent|missing space|not found|no record of|unknown space/,
      );
    }
  });
});
