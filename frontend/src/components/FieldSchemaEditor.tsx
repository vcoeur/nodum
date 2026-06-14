// A reusable editor for a kind's field schema — the `Record<string, FieldSpec>`
// that node kinds and edge kinds both carry. Each row is one field (name, type,
// required, enum choices, description); the parent owns the row list so it can
// seed it from an existing kind and reset it. `rowsToFields` assembles the wire
// object the API expects; `fieldsToRows` does the inverse for editing.

import type { FieldSpec, FieldType } from "../types";

/** The field types the metamodel accepts, in the order the picker lists them. */
export const FIELD_TYPES: FieldType[] = [
  "str",
  "int",
  "float",
  "bool",
  "enum",
  "list[str]",
  "date",
  "datetime",
];

/** One editable field row. `id` is a stable React key; `choices` is comma-separated. */
export interface FieldRow {
  id: number;
  name: string;
  type: FieldType;
  required: boolean;
  choices: string;
  description: string;
}

let rowCounter = 0;

/** A blank field row with a fresh id. */
export function emptyRow(): FieldRow {
  rowCounter += 1;
  return { id: rowCounter, name: "", type: "str", required: false, choices: "", description: "" };
}

/** Seed editable rows from an existing field schema (e.g. when editing a kind). */
export function fieldsToRows(fields: Record<string, FieldSpec>): FieldRow[] {
  return Object.entries(fields).map(([name, spec]) => {
    rowCounter += 1;
    return {
      id: rowCounter,
      name,
      type: spec.type,
      required: Boolean(spec.required),
      choices: (spec.choices ?? []).join(", "),
      description: spec.description ?? "",
    };
  });
}

/**
 * Assemble the wire field schema from editor rows.
 *
 * Blank-name rows (a freshly added row the user has not filled) are dropped.
 * `choices` is emitted only for `enum`; `description` only when non-empty. The
 * server is the source of truth for the rest (e.g. an enum needing choices).
 *
 * @throws Error on a duplicate field name.
 */
export function rowsToFields(rows: FieldRow[]): Record<string, FieldSpec> {
  const fields: Record<string, FieldSpec> = {};
  for (const row of rows) {
    const name = row.name.trim();
    if (!name) continue;
    if (name in fields) throw new Error(`duplicate field name '${name}'`);
    const spec: FieldSpec = { type: row.type, required: row.required };
    if (row.type === "enum") {
      spec.choices = row.choices
        .split(",")
        .map((choice) => choice.trim())
        .filter((choice) => choice.length > 0);
    }
    const description = row.description.trim();
    if (description) spec.description = description;
    fields[name] = spec;
  }
  return fields;
}

interface FieldSchemaEditorProps {
  rows: FieldRow[];
  onChange: (rows: FieldRow[]) => void;
}

/** Controlled editor for a kind's field rows. */
export function FieldSchemaEditor({ rows, onChange }: FieldSchemaEditorProps) {
  function update(index: number, patch: Partial<FieldRow>) {
    onChange(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }

  return (
    <div className="field-editor">
      <span className="field-editor-head">Fields</span>
      {rows.length === 0 && <p className="muted">No fields — add one if this kind needs typed metadata.</p>}
      {rows.map((row, index) => (
        <div className="field-row" key={row.id}>
          <input
            className="field-row-name"
            type="text"
            placeholder="field name"
            value={row.name}
            onChange={(event) => update(index, { name: event.target.value })}
          />
          <select
            value={row.type}
            onChange={(event) => update(index, { type: event.target.value as FieldType })}
          >
            {FIELD_TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
          <label className="inline">
            <input
              type="checkbox"
              checked={row.required}
              onChange={(event) => update(index, { required: event.target.checked })}
            />
            required
          </label>
          {row.type === "enum" && (
            <input
              type="text"
              placeholder="choices (comma, separated)"
              value={row.choices}
              onChange={(event) => update(index, { choices: event.target.value })}
            />
          )}
          <input
            type="text"
            placeholder="description"
            value={row.description}
            onChange={(event) => update(index, { description: event.target.value })}
          />
          <button
            type="button"
            className="small danger"
            aria-label="remove field"
            onClick={() => onChange(rows.filter((_, i) => i !== index))}
          >
            ×
          </button>
        </div>
      ))}
      <button type="button" className="small ghost" onClick={() => onChange([...rows, emptyRow()])}>
        + field
      </button>
    </div>
  );
}
