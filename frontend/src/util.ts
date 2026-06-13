// Small display helpers shared across components.

/** First 8 characters of a UUID, for compact display. */
export const shortUuid = (uuid: string): string => String(uuid).slice(0, 8);

/** A node's display text is its `content`; fall back gracefully when empty. */
export const nodeText = (content: string | undefined): string =>
  content && content.trim() ? content : "(no content)";

/** Truncate a string to at most n characters, with an ellipsis. */
export const truncate = (text: string, n: number): string => {
  const value = String(text);
  return value.length > n ? `${value.slice(0, n - 1)}…` : value;
};

/** Read an Error-ish thrown value as a message string. */
export const errorMessage = (error: unknown, fallback: string): string =>
  error instanceof Error ? error.message : fallback;
