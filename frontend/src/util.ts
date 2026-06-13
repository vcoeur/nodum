// Small display helpers shared across components.

import type { NodeData } from "./types";

/** First 8 characters of a UUID, for compact display. */
export const shortUuid = (uuid: string): string => String(uuid).slice(0, 8);

/** A node payload always carries a `text` field; fall back gracefully. */
export const nodeText = (data: NodeData | undefined): string =>
  data && typeof data.text === "string" ? data.text : "(no text)";

/** Truncate a string to at most n characters, with an ellipsis. */
export const truncate = (text: string, n: number): string => {
  const value = String(text);
  return value.length > n ? `${value.slice(0, n - 1)}…` : value;
};

/** Read an Error-ish thrown value as a message string. */
export const errorMessage = (error: unknown, fallback: string): string =>
  error instanceof Error ? error.message : fallback;
