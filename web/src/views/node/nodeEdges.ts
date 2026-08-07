/**
 * The reading view's edge rail: what `getNode(id, { depth: 1 })` means.
 *
 * The neighborhood read returns the root plus every active edge it walks,
 * and the rail is the part of that walk *incident to the root*: one row per
 * edge, oriented, with the far node attached. The walk is "both" directions,
 * so every incident edge of the root is present and nothing else is; the
 * logic here narrows the envelope to exactly that and derives the two facts
 * a row renders — direction and crossing — so the component stays a renderer.
 */

import type { EdgeOut, NodeOut, SubgraphOut } from "../../api/types";

/** One incident edge of the root, ready to render. */
export interface IncidentRow {
  edge: EdgeOut;
  /** `out` when the root is the edge's source, `in` when it is its target. */
  direction: "out" | "in";
  /** The far node, from the same walk; null only if the envelope lies. */
  far: NodeOut | null;
  /** True when the two endpoints live in different spaces (design D5). */
  crossing: boolean;
}

/**
 * The root's incident edges, outgoing first, in edge-creation order.
 *
 * @param subgraph The `depth: 1` neighborhood read.
 * @returns The rows, or an empty list when nothing is incident.
 */
export function incidentRows(subgraph: SubgraphOut): IncidentRow[] {
  const root = subgraph.nodes[0];
  if (root === undefined) return [];
  const byId = new Map(subgraph.nodes.map((node) => [node.id, node]));

  const rows: IncidentRow[] = [];
  for (const edge of subgraph.edges) {
    let direction: "out" | "in";
    let farId: string;
    if (edge.src_id === root.id) {
      direction = "out";
      farId = edge.dst_id;
    } else if (edge.dst_id === root.id) {
      direction = "in";
      farId = edge.src_id;
    } else {
      continue; // not incident — an envelope this read cannot produce
    }
    const far = byId.get(farId) ?? null;
    rows.push({
      edge,
      direction,
      far,
      crossing: far !== null && far.space_id !== null && far.space_id !== root.space_id,
    });
  }

  rows.sort((a, b) => {
    if (a.direction !== b.direction) return a.direction === "out" ? -1 : 1;
    return a.edge.created_at < b.edge.created_at ? -1 : 1;
  });
  return rows;
}
