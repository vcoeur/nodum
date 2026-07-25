/**
 * The Cytoscape canvas and, more importantly, its lifecycle.
 *
 * One instance is created on mount and destroyed on unmount — never one per
 * render, and never one left behind by a navigation. Everything else in here
 * exists to keep that single instance in step with React state without
 * re-laying-out the graph for changes that do not need it:
 *
 * - **Elements** are replaced only when the element *signature* changes, and
 *   the positions of nodes that survive the change are carried over, so
 *   nudging the depth by one does not scramble the map you were reading.
 * - **Selection, hover, and path highlighting are class toggles.** They never
 *   touch the layout, which is what makes clicking around feel immediate on a
 *   graph at the 200-node cap.
 * - **No animation on a re-render.** The layout runs with `animate: false`;
 *   the only motion in the view is the user's own pan and zoom.
 */

import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef } from "react";
import cytoscape from "cytoscape";
import fcose from "cytoscape-fcose";
import type { BaseLayoutOptions, Core, ElementDefinition, Layouts, Position } from "cytoscape";
import { buildStylesheet, readGraphTokens } from "./graphStyle";

/** fcose's options, which `@types/cytoscape` only types down to `name`. */
interface FcoseLayoutOptions extends BaseLayoutOptions {
  name: "fcose";
  quality?: "draft" | "default" | "proof";
  randomize?: boolean;
  animate?: boolean;
  fit?: boolean;
  padding?: number;
  nodeSeparation?: number;
  idealEdgeLength?: number;
  nodeRepulsion?: number;
  numIter?: number;
  uniformNodeDimensions?: boolean;
  packComponents?: boolean;
}

// Registration is global and throws if repeated, which the dev server's hot
// reload would otherwise do on every edit to this file.
let layoutRegistered = false;
function registerLayout(): void {
  if (layoutRegistered) return;
  layoutRegistered = true;
  try {
    cytoscape.use(fcose);
  } catch {
    // Already registered by a previous module instance — nothing to do.
  }
}

/** Imperative controls the toolbar drives. */
export interface GraphCanvasHandle {
  /** Frame the whole graph. */
  fit(): void;
  /** Frame one node without changing the zoom. */
  center(nodeId: string): void;
  /** Re-run the layout from scratch. */
  relayout(): void;
}

interface GraphCanvasProps {
  /** The elements to render. */
  elements: ElementDefinition[];
  /** Identity of the element set; a change is what triggers a re-layout. */
  signature: string;
  /** The node whose detail panel is open. */
  selectedId: string | null;
  /** Path node ids that are present in this render. */
  pathNodeIds: readonly string[];
  /** Path edge ids that are present in this render. */
  pathEdgeIds: readonly string[];
  /** The two endpoints of the path, when present. */
  pathEndIds: readonly string[];
  /** True while a path is highlighted, which fades everything off it. */
  pathActive: boolean;
  /** Called with a node id, or null when the background is clicked. */
  onSelect: (nodeId: string | null) => void;
}

/**
 * Render the subgraph.
 *
 * @param props See {@link GraphCanvasProps}.
 * @param ref Imperative handle for fit / center / relayout.
 */
export const GraphCanvas = forwardRef<GraphCanvasHandle, GraphCanvasProps>(function GraphCanvas(
  {
    elements,
    signature,
    selectedId,
    pathNodeIds,
    pathEdgeIds,
    pathEndIds,
    pathActive,
    onSelect,
  },
  ref,
) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);
  const layoutRef = useRef<Layouts | null>(null);
  const renderedSignature = useRef<string>("");

  // Handlers are attached once, so they read the current callback off a ref
  // rather than being torn down and rebuilt whenever the parent re-renders.
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;

  /**
   * Frame the whole graph in the viewport.
   *
   * Resizes first: on the very first paint the container can still be settling
   * (the toolbar gains a row of filter chips, a banner appears), and framing
   * against a stale viewport leaves the graph pinned in a corner — which was
   * observed intermittently before this call was added.
   */
  const frameGraph = useCallback((cy: Core) => {
    cy.resize();
    if (cy.nodes().length === 0) return;
    cy.fit(undefined, 48);
    // A one- or two-node graph fits at an absurd zoom; cap it so a lone root
    // looks like a node rather than a wall.
    if (cy.zoom() > 1.5) {
      cy.zoom(1.5);
      cy.center();
    }
  }, []);

  const runLayout = useCallback(
    (cy: Core, randomize: boolean) => {
      layoutRef.current?.stop();
      cy.resize();
      if (cy.nodes().length <= 1) {
        frameGraph(cy);
        requestAnimationFrame(() => {
          if (!cy.destroyed()) frameGraph(cy);
        });
        return;
      }
      const options: FcoseLayoutOptions = {
        name: "fcose",
        // Beyond a few hundred nodes the higher-quality passes stop being worth
        // the wait; the cap keeps this from ever being a very large number.
        quality: cy.nodes().length > 300 ? "draft" : "default",
        randomize,
        animate: false,
        fit: true,
        padding: 48,
        nodeSeparation: 90,
        idealEdgeLength: 110,
        nodeRepulsion: 6000,
        uniformNodeDimensions: false,
        packComponents: true,
      };
      const layout = cy.layout(options);
      layoutRef.current = layout;
      layout.run();
      frameGraph(cy);
      // The layout's own fit happens against the viewport as it was when the
      // layout started; one more frame later the container has stopped moving.
      requestAnimationFrame(() => {
        if (!cy.destroyed()) frameGraph(cy);
      });
    },
    [frameGraph],
  );

  // --- Create and destroy the one instance ----------------------------------
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    registerLayout();

    const cy = cytoscape({
      container,
      elements: [],
      style: buildStylesheet(readGraphTokens()),
      layout: { name: "preset" },
      minZoom: 0.08,
      maxZoom: 3,
      boxSelectionEnabled: false,
      selectionType: "single",
      // Renders the graph to a texture while panning and zooming; the cap keeps
      // the element count low enough that nothing else is needed.
      textureOnViewport: true,
    });
    cyRef.current = cy;

    cy.on("tap", "node", (event) => onSelectRef.current(event.target.id() as string));
    cy.on("tap", (event) => {
      if (event.target === cy) onSelectRef.current(null);
    });
    cy.on("mouseover", "node, edge", (event) => event.target.addClass("hovered"));
    cy.on("mouseout", "node, edge", (event) => event.target.removeClass("hovered"));

    const observer = new ResizeObserver(() => cy.resize());
    observer.observe(container);

    return () => {
      observer.disconnect();
      layoutRef.current?.stop();
      layoutRef.current = null;
      // Destroying the instance takes its listeners, its canvases, and its
      // animation frames with it — nothing survives a route change.
      cy.destroy();
      cyRef.current = null;
      renderedSignature.current = "";
    };
  }, []);

  // --- Element sync, and only then a layout ---------------------------------
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    if (renderedSignature.current === signature) return;

    const hadElements = renderedSignature.current !== "";
    const positions = new Map<string, Position>();
    if (hadElements) {
      for (const node of cy.nodes()) positions.set(node.id(), { ...node.position() });
    }

    cy.batch(() => {
      cy.elements().remove();
      cy.add(elements);
    });

    let retained = 0;
    for (const node of cy.nodes()) {
      const previous = positions.get(node.id());
      if (previous) {
        node.position(previous);
        retained += 1;
      }
    }

    renderedSignature.current = signature;
    // Seeding from the retained positions keeps the map recognisable across a
    // filter change; a wholly new element set gets a fresh randomised start.
    runLayout(cy, retained === 0);
  }, [signature, elements, runLayout]);

  // --- Selection: a class toggle, never a layout ----------------------------
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      cy.elements().unselect();
      cy.edges().removeClass("incident");
      if (!selectedId) return;
      const node = cy.getElementById(selectedId);
      if (node.nonempty()) {
        node.select();
        // The type label of an incident edge is worth the pixels only while
        // its node is the one being read.
        node.connectedEdges().addClass("incident");
      }
    });
  }, [selectedId]);

  // --- Path highlight -------------------------------------------------------
  const pathNodeKey = pathNodeIds.join(",");
  const pathEdgeKey = pathEdgeIds.join(",");
  const pathEndKey = pathEndIds.join(",");
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      cy.elements().removeClass("path path-end faded");
      if (!pathActive) return;
      const onPathIds = new Set([
        ...(pathNodeKey ? pathNodeKey.split(",") : []),
        ...(pathEdgeKey ? pathEdgeKey.split(",") : []),
      ]);
      // Built by filtering rather than by merging singletons: Cytoscape's
      // `merge` returns a new collection instead of mutating the receiver, and
      // accumulating into the discarded result silently highlights nothing —
      // which reads on screen as a graph that vanished.
      const onPath = cy.elements().filter((element) => onPathIds.has(element.id()));
      onPath.addClass("path");
      for (const id of pathEndKey ? pathEndKey.split(",") : []) {
        cy.getElementById(id).addClass("path-end");
      }
      cy.elements().difference(onPath).addClass("faded");
    });
  }, [pathActive, pathNodeKey, pathEdgeKey, pathEndKey]);

  useImperativeHandle(
    ref,
    () => ({
      fit() {
        const cy = cyRef.current;
        if (cy) frameGraph(cy);
      },
      center(nodeId: string) {
        const cy = cyRef.current;
        if (!cy) return;
        const node = cy.getElementById(nodeId);
        if (node.nonempty()) cy.center(node);
      },
      relayout() {
        const cy = cyRef.current;
        if (cy) runLayout(cy, true);
      },
    }),
    [runLayout, frameGraph],
  );

  return <div className="nd-graph__cy" ref={containerRef} />;
});
