/** Pure command-palette state and result modelling. */

import type { NodeOut } from "../api/types";
import type { RecentNode } from "./recents";

/** A non-destructive action offered by the global command palette. */
export interface PaletteItem {
  id: string;
  label: string;
  detail: string;
  kind: "view" | "new" | "cycle" | "node" | "recent";
  nodeId?: string;
}

/** The safe global destinations, kept separate from the shell's rendered nav. */
export const PALETTE_VIEWS: PaletteItem[] = [
  { id: "new", label: "New node", detail: "Open a blank node editor", kind: "new" },
  { id: "view:search", label: "Search", detail: "Jump to search", kind: "view" },
  { id: "view:review", label: "Review", detail: "Jump to review", kind: "view" },
  { id: "view:journal", label: "Journal", detail: "Jump to the dream journal", kind: "view" },
  { id: "view:graph", label: "Graph", detail: "Jump to graph", kind: "view" },
  { id: "view:assets", label: "Assets", detail: "Jump to assets", kind: "view" },
  { id: "view:spaces", label: "Spaces", detail: "Jump to spaces", kind: "view" },
  { id: "view:admin", label: "Admin", detail: "Jump to administration", kind: "view" },
  { id: "cycle", label: "Run cycle rehearsal", detail: "Run a dry run only", kind: "cycle" },
];

/** Build visible commands, filtering by query without losing the recents warning. */
export function paletteItems(
  query: string,
  recent: RecentNode[],
  matches: NodeOut[],
): PaletteItem[] {
  const normalized = query.trim().toLocaleLowerCase();
  const commands = PALETTE_VIEWS.filter((item) =>
    `${item.label} ${item.detail}`.toLocaleLowerCase().includes(normalized),
  );
  if (normalized) {
    return [
      ...matches.map((node) => ({
        id: `node:${node.id}`,
        label: node.title?.trim() || "Untitled node",
        detail: "Open node",
        kind: "node" as const,
        nodeId: node.id,
      })),
      ...commands,
    ];
  }
  return [
    ...recent.map((node) => ({
      id: `recent:${node.id}`,
      label: node.title?.trim() || "Untitled node",
      detail: "Previously opened; may no longer be available",
      kind: "recent" as const,
      nodeId: node.id,
    })),
    ...commands,
  ];
}

/** Keep the selected row within the displayed list. */
export function nextPaletteIndex(current: number, key: "ArrowDown" | "ArrowUp", count: number): number {
  if (count === 0) return -1;
  if (current < 0) return key === "ArrowDown" ? 0 : count - 1;
  return key === "ArrowDown" ? Math.min(current + 1, count - 1) : Math.max(current - 1, 0);
}

/**
 * Clamp a selection onto the displayed list.
 *
 * A `-1` chosen while the list was empty (ArrowDown on no results) must land
 * on the first row once results arrive: an unselected row is one Enter cannot
 * activate.
 */
export function clampPaletteIndex(current: number, count: number): number {
  if (count === 0) return -1;
  return Math.min(Math.max(current, 0), count - 1);
}
