// The node detail view: a node, its payload, its incident edges (with inline
// edit/delete), and the subgraph explorer. Re-fetches whenever the open node or
// the reload token changes.

import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { apiGet, apiSend } from "../api";
import { collectData, initialRawValues } from "../fields";
import type { RawValue } from "../fields";
import { useSchema } from "../schema";
import type { Deleted, EdgeOut, NodeOut, NodeWithEdges } from "../types";
import { errorMessage, nodeText, shortUuid } from "../util";
import { FieldSet } from "./Field";
import { GraphView } from "./GraphView";

type EndpointLabels = Record<string, { text: string; kind: string }>;
type Editing = { type: "node" } | { type: "edge"; edge: EdgeOut } | null;

interface NodeDetailProps {
  uuid: string;
  reloadToken: number;
  onOpen: (uuid: string) => void;
  onReload: () => void;
  onDeleted: () => void;
}

export function NodeDetail({ uuid, reloadToken, onOpen, onReload, onDeleted }: NodeDetailProps) {
  const [data, setData] = useState<NodeWithEdges | null>(null);
  const [labels, setLabels] = useState<EndpointLabels>({});
  const [status, setStatus] = useState("Loading node…");
  const [editing, setEditing] = useState<Editing>(null);

  useEffect(() => {
    let cancelled = false;
    setEditing(null);
    setData(null);
    setStatus("Loading node…");
    (async () => {
      try {
        const result = await apiGet<NodeWithEdges>(`/nodes/${encodeURIComponent(uuid)}`);
        if (cancelled) return;
        setData(result);
        setStatus("");
        const others = result.edges.map((edge) =>
          edge.from_uuid === uuid ? edge.to_uuid : edge.from_uuid,
        );
        const resolved: EndpointLabels = {
          [result.node.uuid]: { text: nodeText(result.node.data), kind: result.node.kind },
        };
        await Promise.allSettled(
          [...new Set(others)].map(async (other) => {
            try {
              const neighbour = await apiGet<NodeWithEdges>(`/nodes/${encodeURIComponent(other)}`);
              resolved[other] = { text: nodeText(neighbour.node.data), kind: neighbour.node.kind };
            } catch {
              resolved[other] = { text: shortUuid(other), kind: "?" };
            }
          }),
        );
        if (!cancelled) setLabels(resolved);
      } catch (error) {
        if (!cancelled) setStatus(errorMessage(error, "Could not load node."));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [uuid, reloadToken]);

  async function deleteNode() {
    if (!data) return;
    const message =
      `Delete this ${data.node.kind} node and its ${data.edges.length} incident edge(s)? ` +
      "This cannot be undone.";
    if (!window.confirm(message)) return;
    try {
      const result = await apiSend<Deleted>("DELETE", `/nodes/${encodeURIComponent(uuid)}`);
      setStatus(`Deleted ${result.deleted} row(s) (node + cascaded edges).`);
      onDeleted();
    } catch (error) {
      setStatus(errorMessage(error, "Delete failed."));
    }
  }

  async function deleteEdge(edge: EdgeOut) {
    if (!window.confirm(`Delete this ${edge.kind} edge?`)) return;
    try {
      await apiSend<Deleted>("DELETE", `/edges/${encodeURIComponent(edge.uuid)}`);
      onReload();
    } catch (error) {
      setStatus(errorMessage(error, "Delete failed."));
    }
  }

  if (!data) {
    return (
      <section>
        <h2>Node</h2>
        <div className="status" aria-live="polite">
          {status}
        </div>
      </section>
    );
  }

  const { node, edges } = data;

  return (
    <section>
      <h2>Node</h2>
      <div className="status" aria-live="polite">
        {status}
      </div>
      <div className="actions">
        <button type="button" onClick={() => setEditing({ type: "node" })}>
          Edit
        </button>
        <button type="button" className="danger" onClick={deleteNode}>
          Delete
        </button>
      </div>

      {editing?.type === "node" && (
        <EditNodeForm
          node={node}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            onReload();
          }}
        />
      )}
      {editing?.type === "edge" && (
        <EditEdgeForm
          edge={editing.edge}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            onReload();
          }}
        />
      )}

      <p className="node-text">{nodeText(node.data)}</p>
      <p className="node-ids">
        <span className="tag">{node.kind}</span>
        <span className="uuid">{node.uuid}</span>
      </p>
      <h3>Payload</h3>
      <pre className="payload">{JSON.stringify(node.data, null, 2)}</pre>
      <h3>Edges ({edges.length})</h3>
      <EdgeTable
        edges={edges}
        selfUuid={node.uuid}
        labels={labels}
        onOpen={onOpen}
        onEditEdge={(edge) => setEditing({ type: "edge", edge })}
        onDeleteEdge={deleteEdge}
      />

      <GraphView seedUuid={node.uuid} onOpen={onOpen} />
    </section>
  );
}

interface EdgeTableProps {
  edges: EdgeOut[];
  selfUuid: string;
  labels: EndpointLabels;
  onOpen: (uuid: string) => void;
  onEditEdge: (edge: EdgeOut) => void;
  onDeleteEdge: (edge: EdgeOut) => void;
}

function EdgeTable({ edges, selfUuid, labels, onOpen, onEditEdge, onDeleteEdge }: EdgeTableProps) {
  const { edgeKind } = useSchema();
  if (edges.length === 0) return <p className="muted">No incident edges.</p>;
  return (
    <table className="edges">
      <thead>
        <tr>
          <th>dir</th>
          <th>kind</th>
          <th>other endpoint</th>
          <th aria-label="actions" />
        </tr>
      </thead>
      <tbody>
        {edges.map((edge) => {
          const outgoing = edge.from_uuid === selfUuid;
          const otherUuid = outgoing ? edge.to_uuid : edge.from_uuid;
          const info = labels[otherUuid] ?? { text: shortUuid(otherUuid), kind: "?" };
          const kind = edgeKind(edge.kind);
          const hasFields = kind ? Object.keys(kind.fields).length > 0 : false;
          return (
            <tr key={edge.uuid}>
              <td className="dir">{outgoing ? "out →" : "← in"}</td>
              <td className="tag-cell">{edge.kind}</td>
              <td>
                <div className="other-cell">
                  <span className="tag">{info.kind}</span>
                  <button type="button" className="endpoint clickable" onClick={() => onOpen(otherUuid)}>
                    {info.text}
                  </button>
                </div>
              </td>
              <td>
                <div className="edge-actions">
                  {hasFields && (
                    <button type="button" className="small" onClick={() => onEditEdge(edge)}>
                      edit
                    </button>
                  )}
                  <button type="button" className="small danger" onClick={() => onDeleteEdge(edge)}>
                    delete
                  </button>
                </div>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

interface EditNodeFormProps {
  node: NodeOut;
  onCancel: () => void;
  onSaved: () => void;
}

function EditNodeForm({ node, onCancel, onSaved }: EditNodeFormProps) {
  const { nodeKind } = useSchema();
  const kind = nodeKind(node.kind);
  const [text, setText] = useState(nodeText(node.data));
  const [raw, setRaw] = useState<Record<string, RawValue>>(() =>
    initialRawValues(kind?.fields ?? {}, node.data),
  );
  const [status, setStatus] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    const body = text.trim();
    if (!body) {
      setStatus("Text is required.");
      return;
    }
    setStatus("Saving…");
    try {
      await apiSend<NodeOut>("PATCH", `/nodes/${encodeURIComponent(node.uuid)}`, {
        text: body,
        data: collectData(kind?.fields ?? {}, raw, true),
      });
      onSaved();
    } catch (error) {
      setStatus(errorMessage(error, "Save failed."));
    }
  }

  return (
    <form className="edit-form" onSubmit={submit}>
      <h3>Edit {node.kind}</h3>
      <label className="block">
        {kind?.text_label ?? "text"}
        <textarea rows={2} value={text} onChange={(event) => setText(event.target.value)} />
      </label>
      {kind && (
        <FieldSet
          fields={kind.fields}
          raw={raw}
          onChange={(name, value) => setRaw((current) => ({ ...current, [name]: value }))}
        />
      )}
      <div className="actions">
        <button type="submit">Save</button>
        <button type="button" className="ghost" onClick={onCancel}>
          Cancel
        </button>
      </div>
      <div className="status">{status}</div>
    </form>
  );
}

interface EditEdgeFormProps {
  edge: EdgeOut;
  onCancel: () => void;
  onSaved: () => void;
}

function EditEdgeForm({ edge, onCancel, onSaved }: EditEdgeFormProps) {
  const { edgeKind } = useSchema();
  const kind = edgeKind(edge.kind);
  const [raw, setRaw] = useState<Record<string, RawValue>>(() =>
    initialRawValues(kind?.fields ?? {}, edge.data),
  );
  const [status, setStatus] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setStatus("Saving…");
    try {
      await apiSend<EdgeOut>("PATCH", `/edges/${encodeURIComponent(edge.uuid)}`, {
        data: collectData(kind?.fields ?? {}, raw, true),
      });
      onSaved();
    } catch (error) {
      setStatus(errorMessage(error, "Save failed."));
    }
  }

  return (
    <form className="edit-form" onSubmit={submit}>
      <h3>Edit {edge.kind} edge</h3>
      {kind && (
        <FieldSet
          fields={kind.fields}
          raw={raw}
          onChange={(name, value) => setRaw((current) => ({ ...current, [name]: value }))}
        />
      )}
      <div className="actions">
        <button type="submit">Save</button>
        <button type="button" className="ghost" onClick={onCancel}>
          Cancel
        </button>
      </div>
      <div className="status">{status}</div>
    </form>
  );
}
