/**
 * Reading the reviewer context the server already computed.
 *
 * `list_proposals` attaches a `context` object per proposal, built by
 * `_node_context` / `_edge_context` / `_update_context` in `service.py`. Its
 * shape is fixed and small, and the view reads it rather than re-deriving
 * anything by chasing ids:
 *
 * - node   → `{"parent": <ref>}`, or `{}` for a root node
 * - edge   → `{"src": <ref>, "dst": <ref>}`
 * - update → `{"node": <ref>}`
 *
 * where a ref is `{"id", "title", "space_id"}` (`service._node_ref`). The space
 * is what the review queue groups by, so {@link ContextRef} carries it too.
 *
 * A referenced row that no longer exists comes back as `{"id"}` alone, with
 * neither a `title` nor a `space_id` — so every reader here tolerates both
 * being absent rather than assuming them.
 */

import type { ProposalOut, VersionOut } from "../../api/types";

/** The node fields a version snapshots — `service.VERSION_FIELDS`. */
export const VERSION_FIELDS = ["title", "content", "props"] as const;

/** One of the three snapshot fields. */
export type VersionField = (typeof VERSION_FIELDS)[number];

/** One referenced node out of a proposal's `context`. */
export interface ContextRef {
  id: string;
  title: string | null;
  /** The space it lives in; null only when the node no longer resolves. */
  spaceId: string | null;
}

/** Read one node ref out of a context object, tolerating every absent field. */
export function contextRef(context: unknown, key: string): ContextRef | null {
  if (context === null || typeof context !== "object") return null;
  const entry = (context as Record<string, unknown>)[key];
  if (entry === null || typeof entry !== "object") return null;
  const id = (entry as Record<string, unknown>).id;
  if (typeof id !== "string") return null;
  const title = (entry as Record<string, unknown>).title;
  const spaceId = (entry as Record<string, unknown>).space_id;
  return {
    id,
    title: typeof title === "string" ? title : null,
    spaceId: typeof spaceId === "string" ? spaceId : null,
  };
}

/** A context ref rendered for display: its title, or its id when untitled. */
export function refLabel(ref: ContextRef | null, fallback = "unknown"): string {
  if (ref === null) return fallback;
  return ref.title && ref.title.trim() !== "" ? ref.title : ref.id;
}

/**
 * Source and target labels for a proposal, whatever its kind.
 *
 * Used by the manifest, where every kind has to render as one comparable line.
 */
export function contextLabel(proposal: ProposalOut): { source: string; target: string } {
  if (proposal.kind === "edge") {
    return {
      source: refLabel(contextRef(proposal.context, "src")),
      target: refLabel(contextRef(proposal.context, "dst")),
    };
  }
  if (proposal.kind === "update") {
    return { source: "", target: refLabel(contextRef(proposal.context, "node")) };
  }
  const parent = contextRef(proposal.context, "parent");
  return { source: parent ? refLabel(parent) : "", target: proposal.node?.title ?? proposal.id };
}

/**
 * The fields a proposed update will actually write.
 *
 * `proposed_fields` names them. A `null` means the row predates migration 0008
 * and is read as naming all three — which is exactly `service._proposed_fields`
 * does, and exactly what such a proposal meant when it was staged. Unknown
 * names are dropped, as the service drops them.
 */
export function updateFields(version: VersionOut): VersionField[] {
  if (version.proposed_fields === null) return [...VERSION_FIELDS];
  return VERSION_FIELDS.filter((field) => version.proposed_fields?.includes(field));
}

/** The fields a snapshot carries that the proposal will *not* write. */
export function contextOnlyFields(version: VersionOut): VersionField[] {
  const applied = new Set<string>(updateFields(version));
  return VERSION_FIELDS.filter((field) => !applied.has(field));
}

/** One sentence naming what accepting this proposal changes. */
export function acceptConsequence(proposal: ProposalOut): string {
  if (proposal.kind === "node") {
    return (
      "Accept makes this node active. Any 'mentions' edges its wikilinks " +
      "materialised go live with it."
    );
  }
  if (proposal.kind === "edge") {
    return (
      "Accept makes this edge active — traversals, search expansion, and the " +
      "graph view start following it."
    );
  }
  if (proposal.kind === "update" && proposal.version) {
    const fields = updateFields(proposal.version);
    const list = fields.length === 0 ? "no fields" : fields.join(", ");
    return (
      `Accept writes ${list} to the node as it stands right now — nothing else ` +
      "in the snapshot below is applied."
    );
  }
  return "Accept moves this proposal to active.";
}

/** One sentence naming what rejecting this proposal does. */
export function rejectConsequence(proposal: ProposalOut): string {
  if (proposal.kind === "update") {
    return "Reject archives the proposed version. The node is untouched; your reason is logged.";
  }
  return `Reject archives this ${proposal.kind}. Nothing is deleted; your reason is logged.`;
}
