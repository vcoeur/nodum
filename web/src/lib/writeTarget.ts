/**
 * The sticky write target: which space a newly created node lands in.
 *
 * Design decision D1 splits a space into two controls that never touch each
 * other — a per-view read **filter** defaulting to *any*, and this, a single
 * app-wide write **target** defaulting to `main` and persisted across sessions.
 * Reading `research` while still filing into `main` is the ordinary case, which
 * is why one switcher could not have served both.
 *
 * D1a is the other half of that decision, and it is a requirement rather than a
 * nicety: **a persisted target the human cannot see is a way to file work into
 * the wrong space.** So the store is built to be rendered, not just read —
 * {@link useWriteTarget} subscribes a component to it, and every surface that
 * creates a node is expected to show the current value at the moment of
 * writing. Anything that calls {@link getWriteTarget} without displaying the
 * answer is the failure mode this module exists to prevent.
 *
 * The value is a space id **or** name, kept verbatim: every space reference on
 * every nodum surface resolves either way, and the server is the only thing
 * that can say whether one still resolves. A target naming a space that has
 * since been archived or renamed therefore survives here and fails at the
 * write, which is the honest place for it to fail — silently rewriting it to
 * `main` would file the node somewhere the human never chose.
 *
 * Shaped after `session.ts`: a module-level value plus a subscriber set, so the
 * one piece of state has one owner and the shell, the editor and the `/spaces`
 * screen cannot drift apart holding three copies.
 */

import { useCallback, useSyncExternalStore } from "react";

/** Where nodes land until the human says otherwise (the server's own default). */
export const DEFAULT_WRITE_TARGET = "main";

/** The `localStorage` key the target persists under. */
export const WRITE_TARGET_STORAGE_KEY = "nodum.write-target";

/** Called with the new target whenever it changes. */
export type WriteTargetListener = (target: string) => void;

const listeners = new Set<WriteTargetListener>();

/** The live value; null until the first read pulls it out of storage. */
let current: string | null = null;

/**
 * Read the persisted target, tolerating storage being unavailable.
 *
 * `localStorage` throws rather than returning null when the browser blocks
 * site data, and a write target that cannot be read is not a reason for the
 * editor to fail to mount — the default is a correct answer.
 */
function readStored(): string {
  try {
    const stored = window.localStorage.getItem(WRITE_TARGET_STORAGE_KEY);
    return stored?.trim() ? stored.trim() : DEFAULT_WRITE_TARGET;
  } catch {
    return DEFAULT_WRITE_TARGET;
  }
}

/** Persist the target, tolerating storage being unavailable. */
function writeStored(target: string): void {
  try {
    window.localStorage.setItem(WRITE_TARGET_STORAGE_KEY, target);
  } catch {
    // Storage is blocked or full. The in-memory value still holds for this
    // session, which is strictly better than refusing the change.
  }
}

/**
 * The current write target.
 *
 * Stable between changes, so it is safe as a `useSyncExternalStore` snapshot.
 *
 * @returns The space id or name new nodes land in; `main` when nothing is set.
 */
export function getWriteTarget(): string {
  if (current === null) current = readStored();
  return current;
}

/**
 * Set the write target and tell every subscriber.
 *
 * A blank or whitespace-only value resets to {@link DEFAULT_WRITE_TARGET}
 * rather than persisting an empty target, because "" is not a space and the
 * server would answer it as `main` anyway — better to say so in the interface.
 * Setting the value it already holds notifies nobody.
 *
 * @param target A space id or name.
 */
export function setWriteTarget(target: string): void {
  const next = target.trim() || DEFAULT_WRITE_TARGET;
  if (next === getWriteTarget()) return;
  current = next;
  writeStored(next);
  for (const listener of [...listeners]) {
    try {
      listener(next);
    } catch {
      // A broken subscriber must not stop the others from re-rendering: a
      // stale target on screen is exactly what D1a forbids.
    }
  }
}

/**
 * Forget the stored target and fall back to `main`.
 *
 * The real caller is the `/spaces` screen: archiving the space you are
 * currently filing into leaves a target that no longer resolves, and resetting
 * it there is a visible act the human just performed rather than a silent
 * correction.
 */
export function clearWriteTarget(): void {
  setWriteTarget(DEFAULT_WRITE_TARGET);
  try {
    window.localStorage.removeItem(WRITE_TARGET_STORAGE_KEY);
  } catch {
    // See writeStored.
  }
}

/**
 * Subscribe to write-target changes.
 *
 * @param listener Called with the new target on every change.
 * @returns The unsubscribe function.
 */
export function onWriteTargetChange(listener: WriteTargetListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Subscribe a component to the write target — the D1a rendering hook.
 *
 * This lives beside the store rather than in a view because there is exactly
 * one write target and more than one surface has to show it; three views each
 * wiring their own `useSyncExternalStore` is three chances for one of them to
 * render a stale value.
 *
 * @returns The current target and a setter, in `useState` order.
 */
export function useWriteTarget(): [string, (target: string) => void] {
  const subscribe = useCallback(
    (onStoreChange: () => void) => onWriteTargetChange(() => onStoreChange()),
    [],
  );
  const target = useSyncExternalStore(subscribe, getWriteTarget, getWriteTarget);
  return [target, setWriteTarget];
}
