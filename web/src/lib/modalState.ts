/** Shared ownership count for every focus-owning overlay in the application. */

let openCount = 0;

/** Whether any registered focus-owning overlay currently owns focus. */
export function isModalOpen(): boolean {
  return openCount > 0;
}

/** Register a focus-owning overlay opening. */
export function modalOpened(): void {
  openCount += 1;
}

/** Register a focus-owning overlay closing without underflowing after a remount. */
export function modalClosed(): void {
  openCount = Math.max(0, openCount - 1);
}
