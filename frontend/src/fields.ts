// Schema-driven field coercion, ported from the vanilla view: raw control values
// (strings, or booleans for checkboxes) are coerced to the declared FieldSpec
// type. Create mode omits empty optional fields; patch mode sends `null` for an
// emptied field so the server-side merge clears it.

import type { FieldSpec } from "./types";

export type RawValue = string | boolean;

/** Initial raw control value for a field, optionally from an existing typed value. */
export function initialRaw(spec: FieldSpec, value?: unknown): RawValue {
  if (spec.type === "bool") return Boolean(value);
  if (value === undefined || value === null) return "";
  if (spec.type === "list[str]" && Array.isArray(value)) return value.join(", ");
  return String(value);
}

/** Seed a raw-value record for a kind's fields from optional existing data. */
export function initialRawValues(
  fields: Record<string, FieldSpec>,
  data?: Record<string, unknown>,
): Record<string, RawValue> {
  const raw: Record<string, RawValue> = {};
  for (const [name, spec] of Object.entries(fields)) {
    raw[name] = initialRaw(spec, data ? data[name] : undefined);
  }
  return raw;
}

interface Coerced {
  value: unknown;
  empty: boolean;
}

/** Coerce a raw control value to its declared type; report whether it is empty. */
function coerce(spec: FieldSpec, raw: RawValue): Coerced {
  if (spec.type === "bool") {
    const value = Boolean(raw);
    return { value, empty: !value };
  }
  const text = String(raw).trim();
  if (spec.type === "int" || spec.type === "float") {
    if (text === "") return { value: null, empty: true };
    const num = spec.type === "int" ? parseInt(text, 10) : parseFloat(text);
    if (Number.isNaN(num)) return { value: null, empty: true };
    return { value: num, empty: false };
  }
  if (spec.type === "list[str]") {
    const items = text
      .split(",")
      .map((item) => item.trim())
      .filter((item) => item.length > 0);
    return { value: items, empty: items.length === 0 };
  }
  return { value: text, empty: text === "" }; // str, enum, unknown
}

/**
 * Build a `data` object from raw field values.
 *
 * @param fields The kind's field schema.
 * @param raw The current raw control values, keyed by field name.
 * @param clearEmpties When true (patch mode), send `null` for emptied fields so
 *   the merge clears them; otherwise (create mode) omit empty optionals.
 */
export function collectData(
  fields: Record<string, FieldSpec>,
  raw: Record<string, RawValue>,
  clearEmpties = false,
): Record<string, unknown> {
  const data: Record<string, unknown> = {};
  for (const [name, spec] of Object.entries(fields)) {
    const { value, empty } = coerce(spec, raw[name] ?? initialRaw(spec));
    if (empty) {
      if (clearEmpties) data[name] = null;
      else if (spec.required) data[name] = value;
    } else {
      data[name] = value;
    }
  }
  return data;
}
