/**
 * The `/spaces` screen's derivations, kept pure so the harness can cover them —
 * it renders no components, so everything worth asserting has to live here.
 *
 * Four responsibilities, and none of them is presentation:
 *
 * - **`main` and `meta` are structural, and the server does not know it.**
 *   `POST /api/spaces/main/archive` succeeds; every write that names no space
 *   still lands in `main` afterwards, because `resolve_space_id(None)` returns
 *   the id without reading the row's state — so the space would vanish from
 *   every picker while nodes kept arriving in it. Archiving `meta` retires the
 *   space that spaces themselves live in. Neither is reversible: the state
 *   machine has no `active ← archived` transition anywhere. So the refusal is
 *   here, with its reason attached, rather than a server error the human meets
 *   after the fact.
 * - **Archiving is not deleting**, and the confirm has to say so in the same
 *   breath as it asks. {@link archiveConsequences} is that copy, counted from
 *   the row rather than written in the abstract.
 * - **Two spaces must not end up sharing a name.** Every space reference on
 *   every nodum surface resolves as `id = ? OR title = ?`, and nothing in the
 *   schema stops two spaces carrying the same title — after which `--space
 *   research` means whichever row SQLite reached first. {@link validateSpaceName}
 *   is the only thing standing in front of that.
 * - **No failure may claim a space does not exist.** The server answers a space
 *   that is not there and a space the caller cannot read with the same words on
 *   purpose (Q13 review S3); copy that resolved the ambiguity would turn the
 *   refusal back into an existence oracle. {@link describeSpaceFailure} owns
 *   that wording, and everything that is *not* an unresolved space is handed
 *   straight to the shared classifier rather than re-derived here.
 */

import { ApiError, isUnknownSpace } from "../../api/client";
import type { SpaceOut } from "../../api/types";
import { describeFailure } from "../../lib";
import type { FailureDescription } from "../../lib";
// The pure half of the shared space vocabulary, imported by path rather than
// through `../../components` so this module's graph stays free of React.
import { resolveSpaceValue } from "../../components/spaceOptions";

/**
 * The spaces the schema creates and the system depends on
 * (`nodum/migrations.py`: `MAIN_SPACE_ID`, `META_SPACE_ID`).
 *
 * Matched by **id**, never by name: a rename changes the title and leaves the
 * id alone, which is exactly why a rename of one of these is survivable and an
 * archive is not.
 */
export const STRUCTURAL_SPACE_IDS: readonly string[] = ["main", "meta"];

/** Grant levels strongest first — the server's `GRANT_LEVEL_NAMES`, ranked. */
const LEVEL_RANK: Record<string, number> = { edit: 3, suggest: 2, read: 1 };

/** One agent's hold on a space, as the screen lists it. */
export interface GrantHolder {
  /** The agent's id, which is also its name (`service.create_agent`). */
  agent: string;
  /** `read`, `suggest`, or `edit`. */
  level: string;
}

/** One space, with everything the screen shows about it already derived. */
export interface SpaceRow {
  /** The row as the server sent it. */
  space: SpaceOut;
  /** The space's id — what every action addresses it by. */
  id: string;
  /** What the human reads: the title, or the id when it has none. */
  label: string;
  /** Live nodes: `active` + `proposed`, archived excluded. */
  nodeCount: number;
  /** The agents granted on it, strongest level first. */
  holders: GrantHolder[];
  /**
   * True when some agent holds `edit` here — the space governs itself: those
   * writes land `active` and never reach the review queue (design D4).
   */
  selfGoverning: boolean;
  /** True for `main` and `meta`: renameable, never archivable. */
  structural: boolean;
  /** True when this is the space new nodes currently land in (design D1). */
  writeTarget: boolean;
}

/**
 * Why a structural space cannot be archived, or null when it is an ordinary one.
 *
 * @param spaceId The space's id.
 * @returns One sentence naming the consequence, for the disabled action's title.
 */
export function structuralReason(spaceId: string): string | null {
  if (spaceId === "main") {
    return (
      "main is where every write lands when nothing names a space, and the server resolves that " +
      "default by id whatever state the row is in — archiving it would hide the space while nodes " +
      "kept arriving in it."
    );
  }
  if (spaceId === "meta") {
    return (
      "meta is the space that spaces live in, along with the whole type vocabulary — archiving it " +
      "would retire the space holding this list."
    );
  }
  return null;
}

/**
 * Turn the server's space list into the screen's rows.
 *
 * Server order is kept (`list_spaces` returns creation order, so `main` and
 * `meta` lead), because a space list is a small stable vocabulary and
 * re-sorting it would move rows under the human between reloads.
 *
 * @param spaces `GET /api/spaces` — active spaces, each with its count and grants.
 * @param writeTarget The current write target, an id **or** a name.
 */
export function spaceRows(spaces: readonly SpaceOut[], writeTarget: string): SpaceRow[] {
  // The target is stored verbatim and may be a name; resolve it to the id the
  // rows carry, or a target set as `research` would never match `research`.
  const targetId = resolveSpaceValue(spaces, writeTarget);
  return spaces.map((space) => {
    const holders = space.grants
      .map((grant) => ({ agent: grant.agent_id, level: grant.level }))
      .sort((a, b) => {
        const rank = (LEVEL_RANK[b.level] ?? 0) - (LEVEL_RANK[a.level] ?? 0);
        return rank !== 0 ? rank : a.agent.localeCompare(b.agent);
      });
    return {
      space,
      id: space.id,
      label: space.title ?? space.id,
      nodeCount: space.node_count,
      holders,
      selfGoverning: holders.some((holder) => holder.level === "edit"),
      structural: STRUCTURAL_SPACE_IDS.includes(space.id),
      writeTarget: targetId === space.id,
    };
  });
}

/** `3 nodes` / `1 node`. */
function counted(total: number, noun: string): string {
  return `${total} ${noun}${total === 1 ? "" : "s"}`;
}

/** `a`, `a and b`, `a, b and c`. */
function joined(names: readonly string[]): string {
  if (names.length <= 1) return names[0] ?? "";
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/**
 * What archiving this space will and will not do, one sentence per line.
 *
 * The screen shows these at the moment of the confirm, because the one thing a
 * human reliably assumes about "archive" is that the contents go with it — and
 * here they do not. The counts come off the row so the sentences are about this
 * space rather than about archiving in general.
 *
 * @param row The space being archived.
 */
export function archiveConsequences(row: SpaceRow): string[] {
  const lines: string[] = [];

  lines.push(
    row.nodeCount === 0
      ? `${row.label} holds no live nodes.`
      : `${row.label} holds ${counted(row.nodeCount, "live node")} — active and proposed both.`,
  );
  lines.push(
    row.nodeCount === 0
      ? "Archiving deletes nothing: a node written there later would keep its space_id and stay as readable as it is now."
      : "Archiving deletes none of them: every one keeps its space_id and stays exactly as readable as it is now.",
  );

  if (row.holders.length === 0) {
    lines.push("No agent holds a grant on it.");
  } else if (row.holders.length === 1) {
    lines.push(
      `The grant held by ${row.holders[0]?.agent} goes inert — the row stays on record and reaches nothing here.`,
    );
  } else {
    lines.push(
      `The ${counted(row.holders.length, "grant")} on it go inert — ` +
        `${joined(row.holders.map((holder) => holder.agent))} keep their rows and reach nothing here.`,
    );
  }

  lines.push(
    "The space stops resolving, so nothing new can be written into it or granted on it.",
  );
  lines.push("There is no way back — nodum has no un-archive.");

  if (row.writeTarget) {
    lines.push("It is your current write target, so archiving it resets that to main.");
  }

  return lines;
}

/**
 * What a rename leaves behind, for the toast that confirms it.
 *
 * A space resolves by id **or** by title, and a rename touches only the title —
 * so the id keeps working and the old name stops. Except when they were the
 * same string, which is the case for every space the schema seeded (`main`'s id
 * *is* `main`) and so is exactly the case a flat "the old name no longer
 * resolves" sentence would get wrong.
 *
 * @param row The space, before the rename.
 * @param name Its new name.
 */
export function renameConsequence(row: SpaceRow, name: string): string {
  if (row.label === row.id) {
    return (
      `${row.label} is now ${name}. Its id is still "${row.id}" — the same string as the old ` +
      "name — so anything that named it that keeps resolving."
    );
  }
  return (
    `${row.label} is now ${name}. Its id ("${row.id}") is unchanged, so anything referring to it ` +
    `by id still resolves; anything referring to it as "${row.label}" no longer does.`
  );
}

/**
 * Check a proposed space name, or null when it is fine.
 *
 * Two spaces sharing a name is the failure this guards: a space reference
 * resolves as `id = ? OR title = ?` on every nodum surface and no index stops
 * the collision, so a second `research` would make `--space research`,
 * `grant … research`, and the space filter all mean whichever row the query
 * reached first. The check is exact rather than case-insensitive because exact
 * is what the server compares — refusing `Research` beside `research` would be
 * refusing a name that genuinely resolves.
 *
 * @param name The name as typed.
 * @param spaces Every active space.
 * @param options `renaming` names the space being renamed, so it does not clash
 *   with itself.
 * @returns The reason to refuse, or null.
 */
export function validateSpaceName(
  name: string,
  spaces: readonly SpaceOut[],
  options: { renaming?: string } = {},
): string | null {
  const trimmed = name.trim();
  if (trimmed === "") {
    return "A space needs a name — it is what --space, a grant, and every space filter refer to it by.";
  }

  const renaming = options.renaming;
  if (renaming !== undefined) {
    const current = spaces.find((space) => space.id === renaming);
    if (current && (current.title ?? current.id) === trimmed) {
      return "That is already its name.";
    }
  }

  const clash = spaces.some(
    (space) => space.id !== renaming && (space.id === trimmed || space.title === trimmed),
  );
  if (clash) {
    return `Another space already answers to "${trimmed}" — a space resolves by id or by name, so the two could not be told apart.`;
  }

  return null;
}

/**
 * Whether a caught failure is the server declining to resolve a space.
 *
 * {@link isUnknownSpace} is the sanctioned discriminator and is checked first,
 * but `api/client.ts` normalises only the two space-*filtered reads*
 * (`GET /api/nodes`, `GET /api/search`) — the lifecycle routes this screen
 * calls are not wrapped, so their refusal still arrives as a plain
 * {@link ApiError}. The literal message is the same one the client keys on
 * (`service.py`'s `unknown space: <ref>`), which is why matching it here is a
 * stand-in for the normalisation and not a second rule.
 */
function isSpaceUnresolved(error: unknown): boolean {
  if (isUnknownSpace(error)) return true;
  return error instanceof ApiError && /^unknown space:/i.test(error.message);
}

/**
 * Describe a failure from a space call, in words that never claim the space is gone.
 *
 * The server answers "a space that does not exist" and "a space you cannot
 * read" identically and deliberately, so copy that picked one would be
 * inventing a fact — and the shared classifier's own 404 body ("The server has
 * no record of …") is exactly that claim. Everything else is handed to
 * {@link describeFailure} unchanged: this module does not re-derive what kind
 * of failure something was.
 *
 * @param error The caught value.
 * @param spaceRef The space the call named, for the copy.
 */
export function describeSpaceFailure(error: unknown, spaceRef: string): FailureDescription {
  if (isSpaceUnresolved(error)) {
    return {
      kind: "not-found",
      title: "That space did not resolve",
      body:
        `nodum would not resolve "${spaceRef}". A space stops resolving once it is archived or ` +
        "renamed, so this list may be out of date — reload the screen to see what is there now.",
    };
  }
  return describeFailure(error, "that space");
}
