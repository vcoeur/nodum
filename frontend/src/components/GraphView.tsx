// The subgraph explorer: expands a seed node and draws the result as a
// circular-layout SVG node-link diagram (dependency-free). Nodes are clickable
// to navigate; colours come from each kind's display group.

import { useState } from "react";
import type { KeyboardEvent } from "react";

import { apiGet } from "../api";
import { useSchema } from "../schema";
import type { NodeOut, Subgraph } from "../types";
import { errorMessage, nodeText, truncate } from "../util";

const GROUP_COLORS: Record<string, string> = {
  entity: "#dbe7f3",
  literature: "#dfe6d4",
  note: "#f6e6c4",
};

interface GraphViewProps {
  seedUuid: string;
  onOpen: (uuid: string) => void;
}

export function GraphView({ seedUuid, onOpen }: GraphViewProps) {
  const { groupOf } = useSchema();
  const [depth, setDepth] = useState("1");
  const [status, setStatus] = useState("");
  const [subgraph, setSubgraph] = useState<Subgraph | null>(null);

  async function run() {
    setStatus("Expanding…");
    try {
      const response = await apiGet<Subgraph>(
        `/expand?seed=${encodeURIComponent(seedUuid)}&depth=${encodeURIComponent(depth)}`,
      );
      setSubgraph(response);
      setStatus(
        `depth ${response.depth}: ${response.nodes.length} node(s), ${response.edges.length} edge(s)`,
      );
    } catch (error) {
      setStatus(errorMessage(error, "Expand failed."));
      setSubgraph(null);
    }
  }

  return (
    <div className="graph-controls">
      <h3>Subgraph</h3>
      <div className="graph-controls-row">
        <label className="inline">
          depth
          <input
            type="number"
            min={1}
            max={6}
            value={depth}
            onChange={(event) => setDepth(event.target.value)}
          />
        </label>
        <button type="button" onClick={run}>
          Show graph
        </button>
        <span className="status inline-status" aria-live="polite">
          {status}
        </span>
      </div>
      {subgraph && (
        <GraphSvg subgraph={subgraph} groupColor={(kind) => GROUP_COLORS[groupOf(kind) ?? ""] ?? "#e8e8e6"} onOpen={onOpen} />
      )}
    </div>
  );
}

interface GraphSvgProps {
  subgraph: Subgraph;
  groupColor: (kind: string) => string;
  onOpen: (uuid: string) => void;
}

function GraphSvg({ subgraph, groupColor, onOpen }: GraphSvgProps) {
  const width = 640;
  const height = 420;
  const cx = width / 2;
  const cy = height / 2;
  const radius = 160;
  const r = 22;

  const count = subgraph.nodes.length;
  const positions = new Map<string, { x: number; y: number; node: NodeOut }>();
  subgraph.nodes.forEach((node, index) => {
    let x = cx;
    let y = cy;
    if (count > 1) {
      const angle = (2 * Math.PI * index) / count - Math.PI / 2;
      x = cx + radius * Math.cos(angle);
      y = cy + radius * Math.sin(angle);
    }
    positions.set(node.uuid, { x, y, node });
  });

  function onKeyDown(event: KeyboardEvent, uuid: string) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onOpen(uuid);
    }
  }

  return (
    <div className="graph-panel">
      <svg viewBox={`0 0 ${width} ${height}`} className="graph-svg" role="img" aria-label="subgraph diagram">
        <defs>
          <marker
            id="arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="7"
            markerHeight="7"
            orient="auto-start-reverse"
          >
            <path d="M0,0 L10,5 L0,10 z" fill="#7a7a7a" />
          </marker>
        </defs>
        <g className="edge-layer">
          {subgraph.edges.map((edge) => {
            const from = positions.get(edge.from_uuid);
            const to = positions.get(edge.to_uuid);
            if (!from || !to || from === to) return null;
            const dx = to.x - from.x;
            const dy = to.y - from.y;
            const len = Math.hypot(dx, dy) || 1;
            const ux = dx / len;
            const uy = dy / len;
            const sx = from.x + ux * r;
            const sy = from.y + uy * r;
            const ex = to.x - ux * r;
            const ey = to.y - uy * r;
            return (
              <g key={edge.uuid}>
                <line
                  x1={sx}
                  y1={sy}
                  x2={ex}
                  y2={ey}
                  className="graph-edge"
                  markerEnd="url(#arrow)"
                />
                <text
                  x={(sx + ex) / 2}
                  y={(sy + ey) / 2 - 2}
                  className="graph-edge-label"
                  textAnchor="middle"
                >
                  {edge.kind}
                </text>
              </g>
            );
          })}
        </g>
        <g className="node-layer">
          {[...positions.values()].map(({ x, y, node }) => (
            <g
              key={node.uuid}
              className="graph-node"
              tabIndex={0}
              role="button"
              onClick={() => onOpen(node.uuid)}
              onKeyDown={(event) => onKeyDown(event, node.uuid)}
            >
              <circle cx={x} cy={y} r={r} fill={groupColor(node.kind)} stroke="#5a5a5a" strokeWidth={1} />
              <title>{`${node.kind}: ${nodeText(node.data)}`}</title>
              <text x={x} y={y + 4} className="graph-node-label" textAnchor="middle">
                {truncate(nodeText(node.data), 18)}
              </text>
            </g>
          ))}
        </g>
      </svg>
    </div>
  );
}
