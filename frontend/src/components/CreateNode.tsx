// Create a typed node: the kind picker drives a schema-rendered field set; on
// success the new node opens in the detail view.

import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { apiSend } from "../api";
import { collectData, initialRawValues } from "../fields";
import type { RawValue } from "../fields";
import { useSchema } from "../schema";
import type { NodeOut } from "../types";
import { errorMessage, shortUuid } from "../util";
import { FieldSet } from "./Field";

interface CreateNodeProps {
  onCreated: (uuid: string) => void;
}

export function CreateNode({ onCreated }: CreateNodeProps) {
  const { schema, nodeKind } = useSchema();
  const [kindName, setKindName] = useState(schema.node_kinds[0]?.name ?? "");
  const [text, setText] = useState("");
  const [raw, setRaw] = useState<Record<string, RawValue>>({});
  const [status, setStatus] = useState("");

  const kind = nodeKind(kindName);

  useEffect(() => {
    setRaw(kind ? initialRawValues(kind.fields) : {});
    // Reset the field set whenever the selected kind changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kindName]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const body = text.trim();
    if (!body) {
      setStatus("Text is required.");
      return;
    }
    const data = collectData(kind?.fields ?? {}, raw);
    setStatus("Creating…");
    try {
      const node = await apiSend<NodeOut>("POST", "/nodes", { kind: kindName, text: body, data });
      setStatus(`Created ${node.kind} ${shortUuid(node.uuid)}.`);
      setText("");
      setRaw(kind ? initialRawValues(kind.fields) : {});
      onCreated(node.uuid);
    } catch (error) {
      setStatus(errorMessage(error, "Create failed."));
    }
  }

  return (
    <section>
      <h2>Create node</h2>
      <form onSubmit={submit}>
        <label className="block">
          kind
          <select value={kindName} onChange={(event) => setKindName(event.target.value)}>
            {schema.node_kinds.map((each) => (
              <option key={each.name} value={each.name}>
                {each.name}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          {kind?.text_label ?? "text"}
          <textarea
            rows={2}
            placeholder="The node's text…"
            value={text}
            onChange={(event) => setText(event.target.value)}
          />
        </label>
        {kind && (
          <FieldSet
            fields={kind.fields}
            raw={raw}
            onChange={(name, value) => setRaw((current) => ({ ...current, [name]: value }))}
          />
        )}
        <button type="submit">Create node</button>
      </form>
      <div className="status" aria-live="polite">
        {status}
      </div>
    </section>
  );
}
