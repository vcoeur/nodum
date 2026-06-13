// Create a typed edge: the edge-kind picker shows the endpoint signature and
// drives two kind-filtered node pickers plus a schema-rendered field set.

import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { apiSend } from "../api";
import { collectData, initialRawValues } from "../fields";
import type { RawValue } from "../fields";
import { useSchema } from "../schema";
import type { EdgeOut, SearchHit } from "../types";
import { errorMessage, shortUuid } from "../util";
import { FieldSet } from "./Field";
import { NodePicker } from "./NodePicker";

interface CreateEdgeProps {
  onCreated: (fromUuid: string) => void;
}

export function CreateEdge({ onCreated }: CreateEdgeProps) {
  const { schema, edgeKind } = useSchema();
  const [kindName, setKindName] = useState(schema.edge_kinds[0]?.name ?? "");
  const [fromHit, setFromHit] = useState<SearchHit | null>(null);
  const [toHit, setToHit] = useState<SearchHit | null>(null);
  const [raw, setRaw] = useState<Record<string, RawValue>>({});
  const [status, setStatus] = useState("");

  const kind = edgeKind(kindName);

  useEffect(() => {
    setFromHit(null);
    setToHit(null);
    setRaw(kind ? initialRawValues(kind.fields) : {});
    // Reset endpoints + fields whenever the selected edge kind changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kindName]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!fromHit || !toHit) {
      setStatus("Pick both a 'from' and a 'to' node.");
      return;
    }
    const data = collectData(kind?.fields ?? {}, raw);
    setStatus("Creating…");
    try {
      const edge = await apiSend<EdgeOut>("POST", "/edges", {
        kind: kindName,
        from_uuid: fromHit.uuid,
        to_uuid: toHit.uuid,
        data,
      });
      setStatus(`Created ${edge.kind} ${shortUuid(edge.uuid)}.`);
      const openFrom = fromHit.uuid;
      setFromHit(null);
      setToHit(null);
      setRaw(kind ? initialRawValues(kind.fields) : {});
      onCreated(openFrom);
    } catch (error) {
      setStatus(errorMessage(error, "Create failed."));
    }
  }

  return (
    <section>
      <h2>Create edge</h2>
      <form onSubmit={submit}>
        <label className="block">
          edge kind
          <select value={kindName} onChange={(event) => setKindName(event.target.value)}>
            {schema.edge_kinds.map((each) => (
              <option key={each.name} value={each.name}>
                {each.name}
              </option>
            ))}
          </select>
        </label>
        {kind && (
          <div className="signature">
            <span className="sig-side">from: {kind.from.join(", ")}</span>
            <span className="sig-arrow"> → </span>
            <span className="sig-side">to: {kind.to.join(", ")}</span>
          </div>
        )}
        <div className="picker-row">
          <div className="picker-col">
            <span className="picker-label">from</span>
            {kind && (
              <NodePicker
                key={`from-${kindName}`}
                allowedKinds={kind.from}
                selected={fromHit}
                onSelect={setFromHit}
              />
            )}
          </div>
          <div className="picker-col">
            <span className="picker-label">to</span>
            {kind && (
              <NodePicker
                key={`to-${kindName}`}
                allowedKinds={kind.to}
                selected={toHit}
                onSelect={setToHit}
              />
            )}
          </div>
        </div>
        {kind && (
          <FieldSet
            fields={kind.fields}
            raw={raw}
            onChange={(name, value) => setRaw((current) => ({ ...current, [name]: value }))}
          />
        )}
        <button type="submit">Create edge</button>
      </form>
      <div className="status" aria-live="polite">
        {status}
      </div>
    </section>
  );
}
