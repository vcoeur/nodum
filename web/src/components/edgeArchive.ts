import type { EdgeOut, NodeOut } from "../api/types";

/** The named endpoints and relationship used by the edge archive UI. */
export interface EdgeArchiveSubject {
  edge: EdgeOut;
  source: NodeOut;
  destination: NodeOut;
}

/** Name a node for consequence copy without inventing missing metadata. */
export function edgeEndpointLabel(node: NodeOut): string {
  return node.title?.trim() ? node.title : node.id;
}

/** Name the exact relationship, including direction and both endpoints. */
export function edgeArchiveLabel(subject: EdgeArchiveSubject): string {
  return `${edgeEndpointLabel(subject.source)} — ${subject.edge.type} → ${edgeEndpointLabel(subject.destination)}`;
}

/** Explain why this relationship cannot take the active-to-archived transition. */
export function edgeArchiveRefusal(edge: EdgeOut): string | null {
  if (edge.state === "archived") return "Already archived.";
  if (edge.state === "proposed") return "Only an active relationship can be archived.";
  return null;
}

/** State only consequences the edge transition actually provides. */
export function edgeArchiveConsequences(subject: EdgeArchiveSubject): string[] {
  const source = edgeEndpointLabel(subject.source);
  const destination = edgeEndpointLabel(subject.destination);
  return [
    `Current active traversal will stop following this ${subject.edge.type} relationship from ${source} to ${destination}.`,
    `${source} and ${destination} do not change.`,
    "The relationship stays in history.",
    "Archiving is one reversible event.",
  ];
}
