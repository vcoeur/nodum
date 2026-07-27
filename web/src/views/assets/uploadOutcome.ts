/**
 * What one dropped document became, in the words the queue row shows.
 *
 * The assets drop-zone does not register bytes any more — it drives the
 * capability flow (`POST /api/uploads` → `PUT /api/uploads/{token}`), so every
 * drop produces a *subgraph*: an `asset_ref` describing the bytes in one space,
 * a `source` node carrying the extracted text, a `derived_from` edge, and one
 * `block` per page. This module is the reading of that answer, and it is a
 * plain module rather than logic inside the component for the usual reason —
 * the harness renders nothing, so a rule that matters has to be testable on its
 * own.
 *
 * Three semantics are pinned here, and each replaces something the old panel
 * guessed:
 *
 * 1. **The status comes from the server.** `created: true` is *ingested*,
 *    `created: false` is *already ingested* — ingestion is idempotent per
 *    `(hash, space)` and says so itself. The panel used to compare hashes
 *    against a list read before the batch, which answered a different question
 *    (do these bytes exist?) than the one on screen (is this document
 *    described?): a file registered earlier through the editor's drop is
 *    exactly the case where the bytes are present and nothing describes them.
 * 2. **The detail line reports what landed** — the space, the page-block count,
 *    and whether any text came out. When none did it carries the extraction's
 *    own `detail`, because that is where *"install the `pdf` extra"* and *"a
 *    scanned PDF needs OCR"* are already phrased, and a readout that dropped it
 *    would leave the human with "no text" and nothing to do about it. That slot
 *    belongs to the extraction and to nothing else: on the already-ingested
 *    branch the server puts its idempotency note there instead, which would
 *    have the row explain the absence of text by the presence of the subgraph
 *    and evict the one actionable sentence the first drop showed.
 * 3. **A refused write target is never described as a missing space.** The
 *    server answers a space that was never created and a space the caller holds
 *    no grant on with word-for-word identical text on purpose, so copy that
 *    resolved the ambiguity would be an existence oracle over the whole file —
 *    and handing an `UnknownSpaceError` to `describeFailure` prints *"The
 *    server has no record of …"*, which is that copy by accident. Say what
 *    *changed* instead. `isUnknownSpace` is the only test for space-ness; which
 *    of the two requests refused is a separate fact, carried by
 *    `uploadRefusalPhase`, because a refused mint sent nothing while a refused
 *    redemption sent the whole file.
 */

import { ApiError, isUnknownSpace, uploadRefusalPhase } from "../../api/client";
import type { UploadPhase } from "../../api/client";
import { describeFailure } from "../../lib";
import { nameSpace, writeTargetWouldNotResolve } from "../../components/spaceNaming";
import type { SpaceName } from "../../components/spaceNaming";
import type { ExtractionOut, IngestOut, NodeOut } from "../../api/types";

/**
 * Where one file is in the two-request flow, or where it stopped.
 *
 * Enumerated as an array with the union derived from it, rather than the other
 * way round: the queue's label map is a `Record` over this union, so a status
 * added here fails the build until it has a label, and a test can walk the same
 * array to prove the map is total. A union with the list written out separately
 * gives neither.
 *
 * - `queued` — waiting for the serial queue;
 * - `uploading` — minting the grant, or streaming the body into it;
 * - `ingested` — the pipeline wrote the subgraph;
 * - `already-ingested` — the target space already described these bytes and
 *   nothing was rewritten;
 * - `failed` — the mint or the redemption was refused.
 */
export const UPLOAD_STATUSES = [
  "queued",
  "uploading",
  "ingested",
  "already-ingested",
  "failed",
] as const;

/** One of {@link UPLOAD_STATUSES}. */
export type UploadStatus = (typeof UPLOAD_STATUSES)[number];

/** What ingestion reported, reduced to what a row renders. */
export interface UploadOutcome {
  /** The server's own verdict: `created` decides which of the two this is. */
  status: "ingested" | "already-ingested";
  /** The space the server actually filed the subgraph in, by id. */
  spaceId: string | null;
  /** The `source` node, which is what the row links to in the editor. */
  sourceId: string;
  /** Per-page `block` children under `source`. */
  pageBlocks: number;
  /** True when the document had more pages than the block cap allowed. */
  pagesTruncated: boolean;
  /** Characters of text extraction produced. */
  chars: number;
  /**
   * The extraction's own account of why no text came out, or null.
   *
   * Carried **only when an extraction ran here and produced nothing**. With
   * text present the note is about the extraction rather than about the outcome
   * (a cap that bit, a page that truncated) and the character count already
   * says the useful thing; on the already-ingested branch no extraction ran at
   * all, and what the server puts in this field there is its idempotency note —
   * true about the drop, and an answer to a different question than "why is
   * there no text". Letting it through had the second drop of an OCR-less PNG
   * read *"no text extracted — already ingested into this space"*, which
   * asserts a causal link that does not exist and evicts the *"install the
   * 'ocr' extra"* the first drop got right.
   */
  detail: string | null;
}

/**
 * Read one ingestion's answer.
 *
 * Guarded the way the mint is guarded in `api/client.ts`, and for a sharper
 * reason: a body that does not match `IngestOut` would dereference into a
 * `TypeError` inside the queue's `try`, and `describeFailure` reads a
 * `TypeError` as *nothing was listening* — so the row would tell the human to
 * check that `nodum serve` is running about a request that plainly reached it.
 *
 * @param result The `IngestOut` the redemption returned.
 * @returns The status and the facts the row's detail line is built from.
 * @throws ApiError When the answer is not the shape this client can read.
 */
export function readIngestion(result: IngestOut): UploadOutcome {
  const source: NodeOut | undefined = result.source;
  const assetRef: NodeOut | undefined = result.asset_ref;
  const extraction: ExtractionOut | undefined = result.extraction;
  if (
    typeof source?.id !== "string" ||
    !Array.isArray(result.pages) ||
    typeof extraction?.chars !== "number"
  ) {
    throw new ApiError(
      500,
      "MalformedIngestResult",
      "the server's answer did not describe an ingestion, so there is nothing to report about " +
        "these bytes",
    );
  }

  const chars = extraction.chars;
  const created = result.created === true;
  return {
    status: created ? "ingested" : "already-ingested",
    // The `asset_ref` is the node the `(hash, space)` uniqueness is keyed on,
    // so its space is the filing decision itself; `source` carries the same one.
    spaceId: assetRef?.space_id ?? null,
    sourceId: source.id,
    // Belt as well as braces. The server restricts this list to non-archived
    // page blocks, and the count is a claim on screen — so anything else that
    // ever appears under `source` is not counted as a page here either.
    pageBlocks: result.pages.filter((page) => page?.type === "block").length,
    pagesTruncated: result.pages_truncated === true,
    chars,
    // Only the branch where an extraction actually ran may fill this slot.
    detail: created && chars === 0 ? extraction.detail : null,
  };
}

/** How many per-page blocks landed, and whether the cap cut the document. */
function pageBlocksPhrase(outcome: UploadOutcome): string {
  if (outcome.pageBlocks === 0) return "no page blocks";
  const counted = `${outcome.pageBlocks} ${outcome.pageBlocks === 1 ? "page block" : "page blocks"}`;
  return outcome.pagesTruncated ? `${counted} (later pages were not filed as blocks)` : counted;
}

/**
 * Whether any text came out, and how much.
 *
 * The already-ingested branch reports the text **on record** rather than the
 * text this drop produced, because this drop produced none: extraction is
 * skipped when the space already describes the bytes. So an empty count there is
 * a fact about the stored asset and comes with no account of why — the run that
 * would have carried one happened on an earlier drop and is not reported back.
 * Saying that plainly is the alternative to borrowing the idempotency note and
 * letting it read as the reason.
 */
function textPhrase(outcome: UploadOutcome): string {
  if (outcome.chars > 0) {
    return `${outcome.chars} ${outcome.chars === 1 ? "character" : "characters"} of text`;
  }
  if (outcome.status === "already-ingested") {
    return "no text on record, and nothing was re-extracted";
  }
  return outcome.detail === null ? "no text extracted" : `no text extracted — ${outcome.detail}`;
}

/**
 * Where the subgraph is, in the tense the outcome actually warrants.
 *
 * "Filed in `research`" is a report of something this drop did, and on the
 * already-ingested branch nothing was filed — the row's own status label says
 * so one cell to the left, so opening with the past tense had the two halves of
 * one line contradict each other.
 */
function landingPhrase(outcome: UploadOutcome, space: SpaceName | null): string {
  if (space === null) {
    // Defensive: every node has a space server-side. Saying so beats naming a
    // space the response did not carry.
    return "The server named no space";
  }
  return outcome.status === "already-ingested"
    ? `Already described in ${space.label}`
    : `Filed in ${space.label}`;
}

/**
 * The settled row's detail line: where it landed and what came out of it.
 *
 * The space is **always** named, and it is named from the server's answer
 * rather than from the target that was asked for — decision D1a's other half:
 * the confirmation of a write says where the write actually went.
 *
 * @param outcome The reading from {@link readIngestion}.
 * @param space The landing space resolved through `nameSpace`, or null when the
 *   response carried none.
 * @returns One line, ` · `-separated.
 */
export function uploadDetailLine(outcome: UploadOutcome, space: SpaceName | null): string {
  return [landingPhrase(outcome, space), pageBlocksPhrase(outcome), textPhrase(outcome)].join(" · ");
}

/**
 * What a refused write target left behind, which depends on which request said no.
 *
 * The client deliberately collapses both requests' space refusals onto one
 * discriminator — `isUnknownSpace` is the only test for space-ness, and a second
 * one is how two copies drift — so the *phase* is carried alongside rather than
 * re-derived here. It has to be, because the two are different events:
 *
 * - a refused **mint** never sent anything. `POST /api/uploads` checks the
 *   target before a byte moves, so there is nothing stored and nothing to undo;
 * - a refused **redemption** sent the whole file. `urls.consume` spent the grant
 *   before the body was read, and the pipeline resolves the space before it
 *   registers the bytes — so nothing is stored, but "nothing was uploaded" is
 *   false about a request that uploaded all of it, and a row claiming it sat
 *   beside a grid that had just gained a tile.
 *
 * With no phase to go on — a bare `UnknownSpaceError`, which this flow does not
 * produce — the clause is dropped rather than guessed.
 */
function refusedTargetAftermath(phase: UploadPhase | null): string {
  if (phase === "mint") return "Nothing was sent.";
  if (phase === "redemption") {
    return "The file was sent, but nothing was stored and nothing describes it.";
  }
  return "";
}

/**
 * Describe a drop that did not land.
 *
 * Two flows can fail and they fail for different reasons: the mint (a file
 * above the server's ceiling, a target space this session cannot write) and the
 * redemption (the shared type policy refusing the bytes). Neither is a state to
 * resume — `urls.consume` spends the token *before* the body is read, so a
 * refused upload has spent its grant and a retry is a fresh mint, which is what
 * dropping the file again does.
 *
 * Everything but the refused space goes to `describeFailure`, which is the one
 * place that tells *the API said no* from *nothing was listening*; this adds no
 * discriminator of its own beyond `isUnknownSpace`, the sanctioned one. The
 * remedy is named without naming a screen, because the panel above the queue is
 * where it is actually reachable and it carries a link rather than a direction.
 *
 * @param error The caught value.
 * @param requested The write target this upload asked for (id or name).
 * @param spaces Active spaces, or null while that read has not answered —
 *   passed through as null, since a list in flight has ruled nothing out.
 * @param archived Archived space nodes, which is what turns the usual case of
 *   this failure from a 32-hex id into a name.
 * @returns The sentence for the row's detail cell.
 */
export function describeUploadFailure(
  error: unknown,
  requested: string,
  spaces: readonly NodeOut[] | null,
  archived: readonly NodeOut[],
): string {
  if (isUnknownSpace(error)) {
    const aftermath = refusedTargetAftermath(uploadRefusalPhase(error));
    return [
      writeTargetWouldNotResolve(nameSpace(requested, spaces, archived)),
      aftermath,
      "Choose another write target, then drop the file again.",
    ]
      .filter((sentence) => sentence !== "")
      .join(" ");
  }
  return describeFailure(error, "this upload").body;
}
