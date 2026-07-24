/**
 * Turning a `SubgraphOut` into Cytoscape elements.
 *
 * The service guarantees the result never names an edge endpoint it does not
 * also return, which is exactly the invariant a graph renderer needs. This
 * module still checks it: Cytoscape throws on an edge with a missing endpoint,
 * and a render that dies because the server changed shape is a worse failure
 * than one that quietly drops a dangling edge and says so in the status bar.
 */

import type { ElementDefinition } from "cytoscape";
import type { EdgeOut, NodeOut, SubgraphOut } from "../../api/types";

/** What the stylesheet and the click handlers read off a node. */
export interface GraphNodeData extends Record<string, unknown> {
  id: string;
  /** Truncated title, or the id when a node has none. */
  label: string;
  type: string;
  state: string;
}

/** What the stylesheet and the click handlers read off an edge. */
export interface GraphEdgeData extends Record<string, unknown> {
  id: string;
  source: string;
  target: string;
  label: string;
  type: string;
  state: string;
}

/** The element set plus what had to be dropped to build it. */
export interface GraphElements {
  elements: ElementDefinition[];
  /** Edges whose endpoints were not both present — expected to always be empty. */
  danglingEdges: number;
  /** Identity of the element set, for deciding whether a re-layout is needed. */
  signature: string;
}

/** How much of a title the resting label shows; hover reveals the rest. */
const LABEL_LIMIT = 42;

/** A node's on-canvas label: its title, or a short form of its id. */
export function nodeLabel(node: NodeOut): string {
  const title = node.title?.trim();
  if (!title) return `⟨${node.id.slice(0, 8)}⟩`;
  return title.length > LABEL_LIMIT ? `${title.slice(0, LABEL_LIMIT - 1)}…` : title;
}

/** The classes carrying a node's state and its root-ness. */
function nodeClasses(node: NodeOut, rootId: string): string {
  const classes = [`state-${node.state}`];
  if (node.id === rootId) classes.push("is-root");
  return classes.join(" ");
}

/**
 * Build the Cytoscape element set for a subgraph.
 *
 * @param subgraph The server's bounded result.
 * @returns Elements, the count of dropped edges, and a set signature.
 */
export function toElements(subgraph: SubgraphOut): GraphElements {
  const nodeIds = new Set(subgraph.nodes.map((node) => node.id));
  const elements: ElementDefinition[] = subgraph.nodes.map((node) => ({
    group: "nodes",
    data: {
      id: node.id,
      label: nodeLabel(node),
      type: node.type,
      state: node.state,
    } satisfies GraphNodeData,
    classes: nodeClasses(node, subgraph.root),
  }));

  let danglingEdges = 0;
  for (const edge of subgraph.edges) {
    if (!nodeIds.has(edge.src_id) || !nodeIds.has(edge.dst_id)) {
      danglingEdges += 1;
      continue;
    }
    elements.push({
      group: "edges",
      data: {
        id: edge.id,
        source: edge.src_id,
        target: edge.dst_id,
        label: edge.type,
        type: edge.type,
        state: edge.state,
      } satisfies GraphEdgeData,
      classes: `state-${edge.state}`,
    });
  }

  const signature = `${subgraph.root}|${subgraph.nodes
    .map((node) => `${node.id}:${node.state}:${node.type}`)
    .join(",")}|${subgraph.edges.map((edge) => `${edge.id}:${edge.state}`).join(",")}`;

  return { elements, danglingEdges, signature };
}

/** One incident edge, resolved against the node at the other end. */
export interface IncidentEdge {
  edge: EdgeOut;
  /** "out" when the selected node is the source. */
  direction: "out" | "in";
  other: NodeOut | null;
}

/**
 * Collect the edges incident to one node within the loaded subgraph.
 *
 * Scoped to what is on screen on purpose: the detail panel describes the
 * rendered graph, not the whole database, and pretending otherwise would make
 * a filtered view look complete.
 *
 * @param subgraph The rendered subgraph.
 * @param nodeId The selected node.
 * @returns Incident edges, outgoing first, each with its far node.
 */
export function incidentEdges(subgraph: SubgraphOut, nodeId: string): IncidentEdge[] {
  const byId = new Map(subgraph.nodes.map((node) => [node.id, node]));
  const incident: IncidentEdge[] = [];
  for (const edge of subgraph.edges) {
    if (edge.src_id === nodeId) {
      incident.push({ edge, direction: "out", other: byId.get(edge.dst_id) ?? null });
    } else if (edge.dst_id === nodeId) {
      incident.push({ edge, direction: "in", other: byId.get(edge.src_id) ?? null });
    }
  }
  return incident.sort((a, b) => a.direction.localeCompare(b.direction));
}

/** The distinct values of one field across the loaded subgraph. */
export function distinctValues(values: readonly string[]): string[] {
  return [...new Set(values)].sort();
}
