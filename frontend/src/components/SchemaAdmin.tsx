// The schema-administration view: full CRUD over the runtime-evolvable schema
// (the `/node-kinds` and `/edge-kinds` API routes). Lists the live node + edge
// kinds, each with edit/delete, and a create form per side. Every mutation calls
// `reload()` from the schema context so the create/edit pickers elsewhere see the
// change immediately. Deleting an in-use kind returns 409; the delete control
// then offers an `into` reassignment target, matching the CLI's `--into`.

import { useState } from "react";
import type { FormEvent } from "react";

import { ApiError, apiSend } from "../api";
import { useSchema } from "../schema";
import type { EdgeKind, FieldSpec, NodeKind } from "../types";
import { errorMessage } from "../util";
import { FieldSchemaEditor, fieldsToRows, rowsToFields } from "./FieldSchemaEditor";
import type { FieldRow } from "./FieldSchemaEditor";

export function SchemaAdmin() {
  const { schema, reload } = useSchema();
  const [creating, setCreating] = useState<"node" | "edge" | null>(null);
  const [editingNode, setEditingNode] = useState<string | null>(null);
  const [editingEdge, setEditingEdge] = useState<string | null>(null);
  const [status, setStatus] = useState("");

  const nodeKindNames = schema.node_kinds.map((each) => each.name);

  function closeForms() {
    setCreating(null);
    setEditingNode(null);
    setEditingEdge(null);
  }

  async function afterMutation(message: string) {
    closeForms();
    setStatus(message);
    await reload();
  }

  return (
    <>
      <section className="kind-admin">
        <h2>Node kinds ({schema.node_kinds.length})</h2>
        <p className="lede">
          A kind defines a node's field schema and what its <code>content</code> means. Editing
          replaces the whole field set; deleting is refused while the kind is in use unless you
          reassign its nodes into another kind first.
        </p>
        <ul className="kind-list">
          {schema.node_kinds.map((nodeKind) => (
            <li key={nodeKind.name} className="kind-item" data-group={nodeKind.group}>
              <div className="kind-head">
                {nodeKind.group && <span className="kind-group">{nodeKind.group}</span>}
                <span className="kind-name">{nodeKind.name}</span>
                <div className="kind-actions">
                  <button
                    type="button"
                    className="small ghost"
                    onClick={() => {
                      closeForms();
                      setEditingNode(nodeKind.name);
                    }}
                  >
                    edit
                  </button>
                  <DeleteKindControl
                    scope="node"
                    name={nodeKind.name}
                    candidates={nodeKindNames.filter((name) => name !== nodeKind.name)}
                    onDeleted={() => afterMutation(`Deleted node kind ${nodeKind.name}.`)}
                  />
                </div>
              </div>
              <p className="kind-meta">
                content → <b>{nodeKind.content_label}</b>
              </p>
              <FieldChips fields={nodeKind.fields} />
              {editingNode === nodeKind.name && (
                <NodeKindForm
                  mode="edit"
                  initial={nodeKind}
                  onCancel={() => setEditingNode(null)}
                  onSaved={() => afterMutation(`Updated node kind ${nodeKind.name}.`)}
                />
              )}
            </li>
          ))}
        </ul>
        {creating === "node" ? (
          <NodeKindForm
            mode="create"
            onCancel={() => setCreating(null)}
            onSaved={(name) => afterMutation(`Created node kind ${name}.`)}
          />
        ) : (
          <button
            type="button"
            onClick={() => {
              closeForms();
              setCreating("node");
            }}
          >
            New node kind
          </button>
        )}
      </section>

      <section className="kind-admin">
        <h2>Edge kinds ({schema.edge_kinds.length})</h2>
        <p className="lede">
          An edge kind constrains which node kinds an edge may join (its <code>from → to</code>{" "}
          signature) and carries its own optional fields. Deleting is refused while edges use it
          unless you reassign them into another edge kind first.
        </p>
        <ul className="kind-list">
          {schema.edge_kinds.map((edgeKind) => (
            <li key={edgeKind.name} className="kind-item edge">
              <div className="kind-head">
                <span className="kind-name">{edgeKind.name}</span>
                <div className="kind-actions">
                  <button
                    type="button"
                    className="small ghost"
                    onClick={() => {
                      closeForms();
                      setEditingEdge(edgeKind.name);
                    }}
                  >
                    edit
                  </button>
                  <DeleteKindControl
                    scope="edge"
                    name={edgeKind.name}
                    candidates={schema.edge_kinds
                      .map((each) => each.name)
                      .filter((name) => name !== edgeKind.name)}
                    onDeleted={() => afterMutation(`Deleted edge kind ${edgeKind.name}.`)}
                  />
                </div>
              </div>
              <div className="sig">
                <span className="sig-set">
                  {edgeKind.from.map((endpoint) => (
                    <span key={endpoint} className="sig-node">
                      {endpoint}
                    </span>
                  ))}
                </span>
                <span className="arrow">→</span>
                <span className="sig-set">
                  {edgeKind.to.map((endpoint) => (
                    <span key={endpoint} className="sig-node">
                      {endpoint}
                    </span>
                  ))}
                </span>
                {edgeKind.symmetric && <span className="sig-sym">symmetric</span>}
              </div>
              <FieldChips fields={edgeKind.fields} />
              {editingEdge === edgeKind.name && (
                <EdgeKindForm
                  mode="edit"
                  initial={edgeKind}
                  nodeKindNames={nodeKindNames}
                  onCancel={() => setEditingEdge(null)}
                  onSaved={() => afterMutation(`Updated edge kind ${edgeKind.name}.`)}
                />
              )}
            </li>
          ))}
        </ul>
        {creating === "edge" ? (
          <EdgeKindForm
            mode="create"
            nodeKindNames={nodeKindNames}
            onCancel={() => setCreating(null)}
            onSaved={(name) => afterMutation(`Created edge kind ${name}.`)}
          />
        ) : (
          <button
            type="button"
            onClick={() => {
              closeForms();
              setCreating("edge");
            }}
          >
            New edge kind
          </button>
        )}
        <div className="status" aria-live="polite">
          {status}
        </div>
      </section>
    </>
  );
}

/** A kind's typed fields shown as chips (required fields are gold-accented). */
function FieldChips({ fields }: { fields: Record<string, FieldSpec> }) {
  const entries = Object.entries(fields);
  if (entries.length === 0) {
    return (
      <div className="chips">
        <span className="chip empty">no fields</span>
      </div>
    );
  }
  return (
    <div className="chips">
      {entries.map(([name, spec]) => (
        <span key={name} className={spec.required ? "chip req" : "chip"}>
          {name}
          <span className="chip-type">
            {spec.type}
            {spec.required ? " · req" : ""}
          </span>
        </span>
      ))}
    </div>
  );
}

interface DeleteKindControlProps {
  scope: "node" | "edge";
  name: string;
  candidates: string[];
  onDeleted: () => void;
}

/** A delete button that, on a 409 (kind in use), offers an `into` reassignment. */
function DeleteKindControl({ scope, name, candidates, onDeleted }: DeleteKindControlProps) {
  const [phase, setPhase] = useState<"idle" | "reassign" | "busy">("idle");
  const [into, setInto] = useState(candidates[0] ?? "");
  const [error, setError] = useState("");
  const base = scope === "node" ? "/node-kinds" : "/edge-kinds";

  async function attempt(target?: string) {
    setPhase("busy");
    setError("");
    const url = `${base}/${encodeURIComponent(name)}${
      target ? `?into=${encodeURIComponent(target)}` : ""
    }`;
    try {
      await apiSend("DELETE", url);
      onDeleted();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        setPhase("reassign");
        setError(caught.message);
      } else {
        setPhase("idle");
        setError(errorMessage(caught, "Delete failed."));
      }
    }
  }

  if (phase === "reassign") {
    return (
      <div className="reassign">
        <span className="status error inline-status">{error}</span>
        {candidates.length === 0 ? (
          <span className="muted">No other {scope} kind to reassign into.</span>
        ) : (
          <>
            <label className="inline">
              into
              <select value={into} onChange={(event) => setInto(event.target.value)}>
                {candidates.map((candidate) => (
                  <option key={candidate} value={candidate}>
                    {candidate}
                  </option>
                ))}
              </select>
            </label>
            <button type="button" className="small danger" onClick={() => attempt(into)}>
              reassign &amp; delete
            </button>
          </>
        )}
        <button type="button" className="small ghost" onClick={() => setPhase("idle")}>
          cancel
        </button>
      </div>
    );
  }

  return (
    <>
      <button
        type="button"
        className="small danger"
        disabled={phase === "busy"}
        onClick={() => {
          if (window.confirm(`Delete ${scope} kind "${name}"?`)) attempt();
        }}
      >
        delete
      </button>
      {error && <span className="status error inline-status">{error}</span>}
    </>
  );
}

interface NodeKindFormProps {
  mode: "create" | "edit";
  initial?: NodeKind;
  onCancel: () => void;
  onSaved: (name: string) => void;
}

/** Create or edit a node kind (name is fixed in edit mode). */
function NodeKindForm({ mode, initial, onCancel, onSaved }: NodeKindFormProps) {
  const [name, setName] = useState(initial?.name ?? "");
  const [group, setGroup] = useState(initial?.group ?? "");
  const [contentLabel, setContentLabel] = useState(initial?.content_label ?? "text");
  const [rows, setRows] = useState<FieldRow[]>(() => fieldsToRows(initial?.fields ?? {}));
  const [status, setStatus] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    const trimmedName = name.trim();
    if (mode === "create" && !trimmedName) {
      setStatus("Name is required.");
      return;
    }
    if (!contentLabel.trim()) {
      setStatus("Content label is required.");
      return;
    }
    let fields: Record<string, unknown>;
    try {
      fields = rowsToFields(rows);
    } catch (caught) {
      setStatus(errorMessage(caught, "Invalid fields."));
      return;
    }
    setStatus("Saving…");
    try {
      if (mode === "create") {
        await apiSend("POST", "/node-kinds", {
          name: trimmedName,
          group,
          content_label: contentLabel,
          fields,
        });
        onSaved(trimmedName);
      } else {
        await apiSend("PATCH", `/node-kinds/${encodeURIComponent(initial!.name)}`, {
          group,
          content_label: contentLabel,
          fields,
        });
        onSaved(initial!.name);
      }
    } catch (caught) {
      setStatus(errorMessage(caught, "Save failed."));
    }
  }

  return (
    <form className="edit-form" onSubmit={submit}>
      <h3>{mode === "create" ? "New node kind" : `Edit ${initial!.name}`}</h3>
      {mode === "create" && (
        <label className="block">
          name
          <input
            type="text"
            placeholder="e.g. Dataset"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
      )}
      <label className="block">
        group
        <input
          type="text"
          placeholder="e.g. entity / note (optional)"
          value={group}
          onChange={(event) => setGroup(event.target.value)}
        />
      </label>
      <label className="block">
        content label
        <input
          type="text"
          placeholder="what this kind's content means, e.g. name"
          value={contentLabel}
          onChange={(event) => setContentLabel(event.target.value)}
        />
      </label>
      <FieldSchemaEditor rows={rows} onChange={setRows} />
      <div className="actions">
        <button type="submit">Save</button>
        <button type="button" className="ghost" onClick={onCancel}>
          Cancel
        </button>
      </div>
      <div className="status" aria-live="polite">
        {status}
      </div>
    </form>
  );
}

interface EdgeKindFormProps {
  mode: "create" | "edit";
  initial?: EdgeKind;
  nodeKindNames: string[];
  onCancel: () => void;
  onSaved: (name: string) => void;
}

/** Create or edit an edge kind: its endpoint signature, symmetric flag, fields. */
function EdgeKindForm({ mode, initial, nodeKindNames, onCancel, onSaved }: EdgeKindFormProps) {
  const [name, setName] = useState(initial?.name ?? "");
  const [from, setFrom] = useState<string[]>(initial?.from ?? []);
  const [to, setTo] = useState<string[]>(initial?.to ?? []);
  const [symmetric, setSymmetric] = useState(initial?.symmetric ?? false);
  const [rows, setRows] = useState<FieldRow[]>(() => fieldsToRows(initial?.fields ?? {}));
  const [status, setStatus] = useState("");

  function toggle(selected: string[], setSelected: (next: string[]) => void, value: string) {
    setSelected(
      selected.includes(value) ? selected.filter((each) => each !== value) : [...selected, value],
    );
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const trimmedName = name.trim();
    if (mode === "create" && !trimmedName) {
      setStatus("Name is required.");
      return;
    }
    if (from.length === 0 || to.length === 0) {
      setStatus("Pick at least one 'from' and one 'to' node kind.");
      return;
    }
    let fields: Record<string, unknown>;
    try {
      fields = rowsToFields(rows);
    } catch (caught) {
      setStatus(errorMessage(caught, "Invalid fields."));
      return;
    }
    setStatus("Saving…");
    try {
      if (mode === "create") {
        await apiSend("POST", "/edge-kinds", { name: trimmedName, from, to, symmetric, fields });
        onSaved(trimmedName);
      } else {
        await apiSend("PATCH", `/edge-kinds/${encodeURIComponent(initial!.name)}`, {
          from,
          to,
          symmetric,
          fields,
        });
        onSaved(initial!.name);
      }
    } catch (caught) {
      setStatus(errorMessage(caught, "Save failed."));
    }
  }

  return (
    <form className="edit-form" onSubmit={submit}>
      <h3>{mode === "create" ? "New edge kind" : `Edit ${initial!.name}`}</h3>
      {mode === "create" && (
        <label className="block">
          name
          <input
            type="text"
            placeholder="e.g. DerivedFrom"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
      )}
      <fieldset className="endpoint-set">
        <legend>from (allowed source kinds)</legend>
        <div className="checkbox-group">
          {nodeKindNames.map((kindName) => (
            <label key={kindName} className="inline">
              <input
                type="checkbox"
                checked={from.includes(kindName)}
                onChange={() => toggle(from, setFrom, kindName)}
              />
              {kindName}
            </label>
          ))}
        </div>
      </fieldset>
      <fieldset className="endpoint-set">
        <legend>to (allowed target kinds)</legend>
        <div className="checkbox-group">
          {nodeKindNames.map((kindName) => (
            <label key={kindName} className="inline">
              <input
                type="checkbox"
                checked={to.includes(kindName)}
                onChange={() => toggle(to, setTo, kindName)}
              />
              {kindName}
            </label>
          ))}
        </div>
      </fieldset>
      <label className="inline">
        <input
          type="checkbox"
          checked={symmetric}
          onChange={(event) => setSymmetric(event.target.checked)}
        />
        symmetric
      </label>
      <FieldSchemaEditor rows={rows} onChange={setRows} />
      <div className="actions">
        <button type="submit">Save</button>
        <button type="button" className="ghost" onClick={onCancel}>
          Cancel
        </button>
      </div>
      <div className="status" aria-live="polite">
        {status}
      </div>
    </form>
  );
}
