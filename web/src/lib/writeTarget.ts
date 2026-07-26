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
 * screen cannot drift apart holding three copies. **One owner means one owner
 * per browser, not per tab**, which is why the module also listens for
 * `storage`: two tabs that disagreed about the target would reproduce D1a's
 * failure one level out — the human sets `research` in one tab, writes from
 * another that still says `main`, and the node lands somewhere they did not
 * choose. `localStorage` is what makes the target survive a session and is also
 * the only channel a second tab has, so adopting its change is the same
 * mechanism, read instead of written.
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
 * Adopt a new value and tell every subscriber. Persistence is the caller's.
 *
 * Split out from {@link setWriteTarget} because a target arriving from another
 * tab is already in storage: writing it back would be a redundant write, and in
 * the `clear()` case it would resurrect the key the other tab just removed.
 *
 * @param next The new target, already normalised.
 */
function publish(next: string): void {
  if (next === getWriteTarget()) return;
  current = next;
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
  // Storage first, so a subscriber that re-reads it during the broadcast does
  // not see the value this call is replacing.
  writeStored(next);
  publish(next);
}

/**
 * Adopt a write target another tab set.
 *
 * The `storage` event fires only in the *other* documents on this origin, which
 * is exactly the case D1a's on-screen value cannot cover on its own: a tab that
 * never re-read storage would keep showing — and writing to — the target it
 * held when it loaded.
 *
 * A `null` key is `localStorage.clear()`, which took our key with everything
 * else; a `null` or blank value is the key removed or emptied. All three mean
 * the same thing the module means at startup: the default.
 *
 * @param event The `storage` event.
 */
function adoptForeignChange(event: StorageEvent): void {
  if (event.key !== null && event.key !== WRITE_TARGET_STORAGE_KEY) return;
  publish(event.newValue?.trim() ? event.newValue.trim() : DEFAULT_WRITE_TARGET);
}

// Registered once, at import, for the same reason the store itself is a module
// singleton: there is one write target, and a per-component subscription would
// mean tabs agreeing only while some particular view happened to be mounted.
// Guarded because this module is also imported by the `node`-environment tests,
// where there is no window to listen on.
if (typeof window !== "undefined") {
  window.addEventListener("storage", adoptForeignChange);
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
