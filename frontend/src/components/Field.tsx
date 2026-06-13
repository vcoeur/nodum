// Schema-driven form controls: one labelled input per FieldSpec, and a FieldSet
// that lays out every field of a kind. Raw values are owned by the parent.

import type { FieldSpec } from "../types";
import type { RawValue } from "../fields";

interface FieldProps {
  name: string;
  spec: FieldSpec;
  value: RawValue;
  onChange: (value: RawValue) => void;
}

function Control({ spec, value, onChange }: Omit<FieldProps, "name">) {
  switch (spec.type) {
    case "bool":
      return (
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(event) => onChange(event.target.checked)}
        />
      );
    case "int":
    case "float":
      return (
        <input
          type="number"
          step={spec.type === "int" ? "1" : "any"}
          value={String(value)}
          onChange={(event) => onChange(event.target.value)}
        />
      );
    case "enum":
      return (
        <select value={String(value)} onChange={(event) => onChange(event.target.value)}>
          <option value="">(none)</option>
          {(spec.choices ?? []).map((choice) => (
            <option key={choice} value={choice}>
              {choice}
            </option>
          ))}
        </select>
      );
    case "list[str]":
      return (
        <input
          type="text"
          placeholder="comma, separated"
          value={String(value)}
          onChange={(event) => onChange(event.target.value)}
        />
      );
    default:
      return (
        <input
          type="text"
          value={String(value)}
          onChange={(event) => onChange(event.target.value)}
        />
      );
  }
}

/** A single labelled, schema-typed control. */
export function Field({ name, spec, value, onChange }: FieldProps) {
  return (
    <label className="field">
      <span className="field-name">{spec.required ? `${name} *` : name}</span>
      <Control spec={spec} value={value} onChange={onChange} />
      {spec.description && <span className="field-desc">{spec.description}</span>}
    </label>
  );
}

interface FieldSetProps {
  fields: Record<string, FieldSpec>;
  raw: Record<string, RawValue>;
  onChange: (name: string, value: RawValue) => void;
}

/** Render every field of a kind; returns null when the kind has no fields. */
export function FieldSet({ fields, raw, onChange }: FieldSetProps) {
  const entries = Object.entries(fields);
  if (entries.length === 0) return null;
  return (
    <div className="fields">
      {entries.map(([name, spec]) => (
        <Field
          key={name}
          name={name}
          spec={spec}
          value={raw[name] ?? (spec.type === "bool" ? false : "")}
          onChange={(value) => onChange(name, value)}
        />
      ))}
    </div>
  );
}
