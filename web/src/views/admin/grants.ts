/**
 * Grant-display logic for the admin view, kept pure so the harness can cover it.
 *
 * The server orders grant rows by (agent, space); everything this view derives
 * — one agent's slice, the spaces still grantable to it — comes from the two
 * lists the view already loaded (grants, spaces), so the picker never needs a
 * per-agent round trip.
 *
 * Naming a space is *not* here: `components/spaceOptions.ts` owns `spaceLabel`
 * for every view. This module had its own id-only copy, which is a strict
 * subset — a space reference resolves by id **or** title everywhere in nodum,
 * and the server refuses a title that equals another space's id, so the shared
 * one is safe on a grant row's `space_id` and right everywhere else.
 */

import type { GrantLevel, GrantOut, NodeOut } from "../../api/types";

/** The levels a grant can carry, weakest to strongest (server: `GRANT_LEVEL_NAMES`). */
export const GRANT_LEVELS: readonly GrantLevel[] = ["read", "suggest", "edit"];

/** What a level lets the agent do, for the picker's option labels. */
export const LEVEL_SUMMARY: Record<GrantLevel, string> = {
  read: "read only",
  suggest: "propose changes (land proposed)",
  edit: "write directly (land active)",
};

/**
 * One agent's grants, ordered by space id.
 *
 * @param grants Every grant row, as `GET /api/grants` returns them.
 * @param agentId The agent whose slice is wanted.
 */
export function grantsForAgent(grants: readonly GrantOut[], agentId: string): GrantOut[] {
  return grants
    .filter((grant) => grant.agent_id === agentId)
    .sort((a, b) => (a.space_id < b.space_id ? -1 : a.space_id > b.space_id ? 1 : 0));
}

/**
 * The spaces an agent holds no grant on — the add-grant picker's options.
 *
 * Offering an already-granted space would silently re-level the grant
 * (`POST /api/grants` is upsert), which is a real action dressed as a no-op.
 *
 * @param spaces Every active space.
 * @param grants Every grant row.
 * @param agentId The agent being granted to.
 */
export function grantableSpaces(
  spaces: readonly NodeOut[],
  grants: readonly GrantOut[],
  agentId: string,
): NodeOut[] {
  const held = new Set(
    grants.filter((grant) => grant.agent_id === agentId).map((grant) => grant.space_id),
  );
  return spaces.filter((space) => !held.has(space.id));
}
