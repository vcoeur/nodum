/**
 * The Cytoscape stylesheet, derived from the app's design tokens.
 *
 * Cytoscape renders to a canvas and cannot read CSS custom properties, so the
 * palette is read out of `:root` once at mount and handed to the stylesheet as
 * literal colours. That keeps `styles/tokens.css` the single source of truth —
 * the alternative, a second hard-coded palette in here, is exactly how a graph
 * ends up disagreeing with the rest of an app about what `proposed` looks like.
 *
 * The two conventions from `styles/tokens.css` hold on the canvas too:
 * **state is the only thing colour encodes** (proposed violet, active
 * sea-green, archived recessive), and **the accent means "you can act on
 * this"** — it marks the root, the selection, and a highlighted path, never a
 * property of the data.
 *
 * Node *type* is therefore carried by shape rather than by a second colour
 * axis, and the legend under the canvas spells the mapping out.
 */

import type cytoscape from "cytoscape";
import type { EdgeSingular, NodeSingular, StylesheetStyle } from "cytoscape";

/** The palette and fonts the canvas needs. */
export interface GraphTokens {
  bg: string;
  surface: string;
  border: string;
  borderStrong: string;
  text: string;
  textMuted: string;
  textFaint: string;
  accent: string;
  accentBright: string;
  proposed: string;
  active: string;
  archived: string;
  fontUi: string;
  fontMono: string;
}

/** Used when a custom property is missing — a stylesheet must never be blank. */
const FALLBACK_TOKENS: GraphTokens = {
  bg: "#12151a",
  surface: "#171b21",
  border: "#272d37",
  borderStrong: "#39414f",
  text: "#dfe4ec",
  textMuted: "#9aa3b2",
  textFaint: "#7a8394",
  accent: "#c9a24a",
  accentBright: "#e0b95e",
  proposed: "#8b7fd4",
  active: "#5aa17f",
  archived: "#767f8f",
  fontUi: "system-ui, sans-serif",
  fontMono: "ui-monospace, monospace",
};

/** Custom-property name for each token. */
const TOKEN_VARIABLES: Record<keyof GraphTokens, string> = {
  bg: "--nd-bg",
  surface: "--nd-surface",
  border: "--nd-border",
  borderStrong: "--nd-border-strong",
  text: "--nd-text",
  textMuted: "--nd-text-muted",
  textFaint: "--nd-text-faint",
  accent: "--nd-accent",
  accentBright: "--nd-accent-bright",
  proposed: "--nd-state-proposed",
  active: "--nd-state-active",
  archived: "--nd-state-archived",
  fontUi: "--nd-font-ui",
  fontMono: "--nd-font-mono",
};

/**
 * Read the design tokens off the document root.
 *
 * @returns The resolved palette, with fallbacks for anything unset.
 */
export function readGraphTokens(): GraphTokens {
  const computed = getComputedStyle(document.documentElement);
  const tokens = { ...FALLBACK_TOKENS };
  for (const [name, variable] of Object.entries(TOKEN_VARIABLES) as [
    keyof GraphTokens,
    string,
  ][]) {
    const value = computed.getPropertyValue(variable).trim();
    if (value) tokens[name] = value;
  }
  return tokens;
}

/**
 * Shapes assigned to the built-in node types.
 *
 * Chosen so the distinctions that matter at a glance are the ones that differ
 * most: containers are rectangular, ideas are round, actors are angular.
 */
const BUILTIN_SHAPES: Record<string, cytoscape.Css.NodeShape> = {
  page: "round-rectangle",
  block: "rectangle",
  note: "round-rectangle",
  claim: "diamond",
  concept: "ellipse",
  person: "triangle",
  org: "pentagon",
  source: "barrel",
  asset_ref: "cut-rectangle",
  tag: "tag",
  daily: "round-diamond",
};

/** Shapes cycled through for user-defined types, in visual-distinctness order. */
const EXTRA_SHAPES: readonly cytoscape.Css.NodeShape[] = [
  "hexagon",
  "octagon",
  "star",
  "vee",
  "rhomboid",
  "heptagon",
  "concave-hexagon",
  "round-hexagon",
];

/**
 * Pick a shape for a node type.
 *
 * Built-ins get a fixed shape; anything user-defined gets a stable one derived
 * from the type id, so the same type is the same shape on every reload without
 * needing the type catalog to be reachable.
 *
 * @param typeId The node type id.
 * @returns A Cytoscape node shape.
 */
export function shapeForType(typeId: string): cytoscape.Css.NodeShape {
  const builtin = BUILTIN_SHAPES[typeId];
  if (builtin) return builtin;
  let hash = 0;
  for (let index = 0; index < typeId.length; index += 1) {
    hash = (hash * 31 + typeId.charCodeAt(index)) % 100_000;
  }
  return EXTRA_SHAPES[hash % EXTRA_SHAPES.length] ?? "hexagon";
}

/** Colour for a lifecycle state, falling back to the neutral one. */
function stateColour(tokens: GraphTokens, state: string): string {
  if (state === "proposed") return tokens.proposed;
  if (state === "archived") return tokens.archived;
  if (state === "active") return tokens.active;
  return tokens.textFaint;
}

/**
 * Build the stylesheet.
 *
 * @param tokens The resolved palette.
 * @returns A Cytoscape stylesheet.
 */
export function buildStylesheet(tokens: GraphTokens): StylesheetStyle[] {
  return [
    {
      selector: "node",
      style: {
        width: 20,
        height: 20,
        shape: (node: NodeSingular) => shapeForType(String(node.data("type") ?? "")),
        "background-color": (node: NodeSingular) =>
          stateColour(tokens, String(node.data("state") ?? "")),
        "background-opacity": 0.85,
        "border-width": 1.5,
        "border-color": (node: NodeSingular) =>
          stateColour(tokens, String(node.data("state") ?? "")),
        "border-opacity": 1,
        label: "data(label)",
        color: tokens.textMuted,
        "font-family": tokens.fontUi,
        "font-size": 9,
        "font-weight": 500,
        "text-valign": "bottom",
        "text-halign": "center",
        "text-margin-y": 5,
        "text-wrap": "ellipsis",
        "text-max-width": "96px",
        "text-background-color": tokens.bg,
        "text-background-opacity": 0.7,
        "text-background-padding": "2px",
        "text-background-shape": "roundrectangle",
        "min-zoomed-font-size": 7,
        "overlay-opacity": 0,
      },
    },
    {
      // Archived nodes recede: the lowest-contrast thing on the canvas, by design.
      selector: "node.state-archived",
      style: { "background-opacity": 0.4, "border-opacity": 0.6, color: tokens.textFaint },
    },
    {
      // Proposed structure is inert, not real — hollow rather than filled.
      selector: "node.state-proposed",
      style: { "background-opacity": 0.28, "border-width": 2 },
    },
    {
      // The root is what was asked for, so it carries the accent.
      selector: "node.is-root",
      style: {
        width: 32,
        height: 32,
        "border-width": 3,
        "border-color": tokens.accent,
        "font-size": 11,
        color: tokens.text,
        "text-max-width": "150px",
        "z-index": 20,
      },
    },
    {
      selector: "node:selected",
      style: {
        "overlay-color": tokens.accentBright,
        "overlay-opacity": 0.22,
        "overlay-padding": 8,
        color: tokens.text,
        "text-background-opacity": 0.95,
        "z-index": 30,
      },
    },
    {
      // Hover reveals the full title, which the resting label truncates.
      selector: "node.hovered",
      style: {
        color: tokens.text,
        "text-max-width": "280px",
        "text-background-opacity": 0.96,
        "text-background-color": tokens.surface,
        "z-index": 40,
      },
    },
    {
      selector: "edge",
      style: {
        width: 1.2,
        "curve-style": "bezier",
        "line-color": (edge: EdgeSingular) =>
          stateColour(tokens, String(edge.data("state") ?? "")),
        "target-arrow-color": (edge: EdgeSingular) =>
          stateColour(tokens, String(edge.data("state") ?? "")),
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.65,
        opacity: 0.5,
        "font-family": tokens.fontMono,
        "font-size": 8,
        color: tokens.textMuted,
        "text-background-color": tokens.bg,
        "text-background-opacity": 0.9,
        "text-background-padding": "2px",
        "text-rotation": "autorotate",
        "min-zoomed-font-size": 7,
      },
    },
    {
      // Proposed edges are dashed and violet: visibly not part of the live
      // graph, never mixed in unmarked with what is.
      selector: "edge.state-proposed",
      style: { "line-style": "dashed", "line-dash-pattern": [5, 4], opacity: 0.8, width: 1.4 },
    },
    {
      selector: "edge.state-archived",
      style: { "line-style": "dotted", opacity: 0.28 },
    },
    {
      // The edge type only earns screen space when the edge is under attention.
      selector: "edge.hovered, edge:selected, edge.incident",
      style: { label: "data(label)", width: 2, opacity: 1, "z-index": 25 },
    },
    {
      selector: "edge.path",
      style: {
        label: "data(label)",
        width: 3,
        opacity: 1,
        "line-color": tokens.accentBright,
        "target-arrow-color": tokens.accentBright,
        "line-style": "solid",
        color: tokens.accentBright,
        "z-index": 35,
      },
    },
    {
      selector: "node.path",
      style: {
        "border-color": tokens.accentBright,
        "border-width": 3,
        color: tokens.text,
        "z-index": 35,
      },
    },
    {
      selector: "node.path-end",
      style: { "border-width": 5, width: 28, height: 28 },
    },
    {
      // Everything off the highlighted path steps back rather than disappearing:
      // the surrounding graph is the context that makes a path mean something.
      selector: ".faded",
      style: { opacity: 0.22, "text-opacity": 0 },
    },
  ];
}
