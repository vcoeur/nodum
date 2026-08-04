/**
 * Turning a `SubgraphOut` into Cytoscape elements.
 *
 * The service guarantees the result never names an edge endpoint it does not
 * also return, which is exactly the invariant a graph renderer needs. This
 * module still checks it: Cytoscape throws on an edge with a missing endpoint,
 * and a render that dies because the server changed shape is a worse failure
 * than one that quietly drops a dangling edge and says so in the status bar.
 *
 * It also marks **crossings** — edges whose two endpoints live in different
 * spaces (design decision D5). A crossing is a property of the data, not of the
 * space filter, so it is baked into the element set and is drawn whether or not
 * anything is narrowed: the boundaries in a file are worth seeing before anyone
 * goes looking for them. The filter's own effect is a class toggle applied over
 * the top and never touches these elements.
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
  /** The node's space id, or `""` when it carries none. */
  space: string;
}

/** What the stylesheet and the click handlers read off an edge. */
export interface GraphEdgeData extends Record<string, unknown> {
  id: string;
  source: string;
  target: string;
  label: string;
  type: string;
  state: string;
  /** True when the endpoints live in two different, known spaces. */
  crossing: boolean;
}

/** The element set plus what had to be dropped to build it. */
export interface GraphElements {
  elements: ElementDefinition[];
  /** Edges whose endpoints were not both present — expected to always be empty. */
  danglingEdges: number;
  /** Edges whose endpoints live in different spaces, drawn as crossings. */
  crossingEdges: number;
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

/** One row of the keyboard node list: the focusable twin of a canvas node. */
export interface NodeListItem {
  id: string;
  label: string;
}

/**
 * The rows of the keyboard node list: every rendered node, in response order.
 *
 * The canvas paints into a `<canvas>` whose only interaction is a pointer tap,
 * so the view gives each node a focusable twin in a plain list (review
 * 08-frontend, MAJOR 3); this is the pure mapping that twin renders from.
 *
 * @param nodes The rendered subgraph's nodes.
 * @returns One `{id, label}` row per node, labelled like the canvas node.
 */
export function nodeListItems(nodes: readonly NodeOut[]): NodeListItem[] {
  return nodes.map((node) => ({ id: node.id, label: nodeLabel(node) }));
}

/** The classes carrying a node's state and its root-ness. */
function nodeClasses(node: NodeOut, rootId: string): string {
  const classes = [`state-${node.state}`];
  if (node.id === rootId) classes.push("is-root");
  return classes.join(" ");
}

/**
 * Whether an edge joins two different spaces.
 *
 * Both endpoints have to be *known* for the answer to be yes. A node with no
 * `space_id` is unknown territory, not other territory, so it never produces a
 * crossing — the alternative marks a boundary the data does not actually claim.
 *
 * @param spaceByNode Space id per node id; a missing entry means unknown.
 * @param edge The edge to classify.
 */
function isCrossing(spaceByNode: ReadonlyMap<string, string>, edge: EdgeOut): boolean {
  const source = spaceByNode.get(edge.src_id);
  const target = spaceByNode.get(edge.dst_id);
  if (!source || !target) return false;
  return source !== target;
}

/**
 * Build the Cytoscape element set for a subgraph.
 *
 * @param subgraph The server's bounded result.
 * @returns Elements, the counts of dropped and crossing edges, and a set
 *   signature.
 */
export function toElements(subgraph: SubgraphOut): GraphElements {
  const nodeIds = new Set(subgraph.nodes.map((node) => node.id));
  const spaceByNode = new Map<string, string>();
  for (const node of subgraph.nodes) {
    if (node.space_id) spaceByNode.set(node.id, node.space_id);
  }

  const elements: ElementDefinition[] = subgraph.nodes.map((node) => ({
    group: "nodes",
    data: {
      id: node.id,
      label: nodeLabel(node),
      type: node.type,
      state: node.state,
      space: node.space_id ?? "",
    } satisfies GraphNodeData,
    classes: nodeClasses(node, subgraph.root),
  }));

  let danglingEdges = 0;
  let crossingEdges = 0;
  for (const edge of subgraph.edges) {
    if (!nodeIds.has(edge.src_id) || !nodeIds.has(edge.dst_id)) {
      danglingEdges += 1;
      continue;
    }
    const crossing = isCrossing(spaceByNode, edge);
    if (crossing) crossingEdges += 1;
    elements.push({
      group: "edges",
      data: {
        id: edge.id,
        source: edge.src_id,
        target: edge.dst_id,
        label: edge.type,
        type: edge.type,
        state: edge.state,
        crossing,
      } satisfies GraphEdgeData,
      classes: crossing ? `state-${edge.state} crossing` : `state-${edge.state}`,
    });
  }

  // A node's space is in the signature because it decides the crossing classes:
  // a node that moved space changes how its edges are drawn, and the canvas
  // only replaces elements when this string changes.
  const signature = `${subgraph.root}|${subgraph.nodes
    .map((node) => `${node.id}:${node.state}:${node.type}:${node.space_id ?? ""}`)
    .join(",")}|${subgraph.edges.map((edge) => `${edge.id}:${edge.state}`).join(",")}`;

  return { elements, danglingEdges, crossingEdges, signature };
}

/** One incident edge, resolved against the node at the other end. */
export interface IncidentEdge {
  edge: EdgeOut;
  /** "out" when the selected node is the source. */
  direction: "out" | "in";
  other: NodeOut | null;
  /**
   * True when this edge leaves the selected node's space.
   *
   * The panel is where a human lands after clicking a dimmed far endpoint, so
   * it is where the crossing has to be named rather than only drawn.
   */
  crossing: boolean;
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
  const here = byId.get(nodeId)?.space_id ?? null;
  /** Unknown on either side is not a crossing — same rule as {@link isCrossing}. */
  const crosses = (other: NodeOut | null): boolean =>
    here !== null && other?.space_id != null && other.space_id !== here;

  const incident: IncidentEdge[] = [];
  for (const edge of subgraph.edges) {
    if (edge.src_id === nodeId) {
      const other = byId.get(edge.dst_id) ?? null;
      incident.push({ edge, direction: "out", other, crossing: crosses(other) });
    } else if (edge.dst_id === nodeId) {
      const other = byId.get(edge.src_id) ?? null;
      incident.push({ edge, direction: "in", other, crossing: crosses(other) });
    }
  }
  return incident.sort((a, b) => a.direction.localeCompare(b.direction));
}

/** The distinct values of one field across the loaded subgraph. */
export function distinctValues(values: readonly string[]): string[] {
  return [...new Set(values)].sort();
}
