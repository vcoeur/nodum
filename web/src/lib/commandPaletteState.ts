/** One ownership signal for the app-wide command-palette shortcut. */

let open = false;
const listeners = new Set<() => void>();

/** Whether the palette currently owns Ctrl/Cmd-K. */
export function isCommandPaletteOpen(): boolean {
  return open;
}

/** Set the palette's ownership state and notify interested views. */
export function setCommandPaletteOpen(next: boolean): void {
  if (open === next) return;
  open = next;
  for (const listener of [...listeners]) listener();
}

/** Subscribe to palette ownership changes. */
export function onCommandPaletteChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
