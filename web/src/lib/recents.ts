/**
 * Successful reader loads, isolated by the verified human identity from `/api/me`.
 *
 * A recent is evidence of a completed read, not navigation intent. `NodeView`
 * records one only after its `getNode` request succeeds. The app shell is the
 * authority boundary: until it supplies a verified human id this store exposes
 * and records nothing, so a pending or failed identity check cannot read a
 * prior human's browser-local titles.
 */

import { useCallback, useSyncExternalStore } from "react";

/** Prefix for per-human localStorage keys containing successful node reads. */
export const RECENT_NODES_STORAGE_PREFIX = "nodum.recent-nodes.";

/** Cross-tab signal that a session transition invalidated every active recents scope. */
export const RECENT_NODES_INVALIDATION_STORAGE_KEY = "nodum.recent-nodes.invalidate";

/** Enough context to recognise a prior read without turning localStorage into a cache. */
export interface RecentNode {
  id: string;
  title: string | null;
}

/** Keep the palette useful without retaining an unbounded browsing history. */
export const RECENT_NODE_LIMIT = 12;

type RecentListener = () => void;
/** Shell listeners notified when another tab transitioned sessions. */
type ScopeInvalidationListener = () => void;

const listeners = new Set<RecentListener>();
const invalidationListeners = new Set<ScopeInvalidationListener>();
let currentScope: string | null = null;
let current: RecentNode[] = [];

/** Return the localStorage key reserved for one stable human identity. */
export function recentNodesStorageKey(humanId: string): string {
  return `${RECENT_NODES_STORAGE_PREFIX}${encodeURIComponent(humanId)}`;
}

/** Normalise one persisted value and discard malformed records. */
export function readRecentNodes(value: string | null): RecentNode[] {
  if (value === null) return [];
  try {
    const parsed: unknown = JSON.parse(value);
    if (!Array.isArray(parsed)) return [];
    const unique = new Set<string>();
    const recent: RecentNode[] = [];
    for (const item of parsed) {
      if (
        typeof item !== "object" ||
        item === null ||
        typeof item.id !== "string" ||
        !item.id.trim() ||
        (item.title !== null && typeof item.title !== "string") ||
        unique.has(item.id)
      ) {
        continue;
      }
      unique.add(item.id);
      recent.push({ id: item.id, title: item.title });
      if (recent.length === RECENT_NODE_LIMIT) break;
    }
    return recent;
  } catch {
    return [];
  }
}

/** Read one scope's localStorage value without making a blocked policy fatal to the UI. */
function readStored(humanId: string): RecentNode[] {
  try {
    return readRecentNodes(window.localStorage.getItem(recentNodesStorageKey(humanId)));
  } catch {
    return [];
  }
}

/** Persist a scope snapshot while retaining the in-memory fallback if storage is blocked. */
function writeStored(humanId: string, nodes: RecentNode[]): void {
  try {
    window.localStorage.setItem(recentNodesStorageKey(humanId), JSON.stringify(nodes));
  } catch {
    // The verified scope can still show its reads for this tab.
  }
}

/** Publish a stable snapshot to every subscriber. */
function publish(nodes: RecentNode[]): void {
  current = nodes;
  for (const listener of [...listeners]) listener();
}

/**
 * Set the only identity scope allowed to read or write recents in this tab.
 *
 * @param humanId Stable id returned by a successful `/api/me`, or null while
 * identity is pending, absent, or could not be verified.
 */
export function setRecentNodesScope(humanId: string | null): void {
  if (currentScope === humanId) return;
  currentScope = humanId;
  publish(humanId === null ? [] : readStored(humanId));
}

/**
 * Remove this and every same-origin tab's active scope after a session transition.
 *
 * This is an invalidation only: each tab may select a human record again solely
 * when its own shell verifies `/api/me`.
 */
export function invalidateRecentNodesScopes(): void {
  setRecentNodesScope(null);
  try {
    const previousMarker = Number(window.localStorage.getItem(RECENT_NODES_INVALIDATION_STORAGE_KEY));
    // A storage event only needs a changed value. Incrementing the persisted
    // marker avoids secure-context-only crypto while ensuring consecutive
    // transitions still notify other tabs.
    const nextMarker = Number.isFinite(previousMarker) ? previousMarker + 1 : 1;
    window.localStorage.setItem(RECENT_NODES_INVALIDATION_STORAGE_KEY, String(nextMarker));
  } catch {
    // This tab is already empty; blocked storage cannot notify another tab.
  }
}

/** Return the current verified scope, if one has been established. */
export function getRecentNodesScope(): string | null {
  return currentScope;
}

/** Return the recents visible to the current verified scope. */
export function getRecentNodes(): RecentNode[] {
  return current;
}

/** Record a completed reader load only for the current verified human. */
export function recordRecentNode(node: RecentNode): void {
  if (currentScope === null) return;
  const next = [node, ...current.filter((recent) => recent.id !== node.id)].slice(
    0,
    RECENT_NODE_LIMIT,
  );
  writeStored(currentScope, next);
  publish(next);
}

/** Clear the current verified scope's history as optional defense in depth. */
export function clearRecentNodes(): void {
  if (currentScope === null) return;
  current = [];
  try {
    window.localStorage.removeItem(recentNodesStorageKey(currentScope));
  } catch {
    // The in-memory reset still clears this verified scope in this tab.
  }
  for (const listener of [...listeners]) listener();
}

/** Subscribe to changes in the current verified scope's recents list. */
export function onRecentNodesChange(listener: RecentListener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Subscribe to another tab's session transition.
 *
 * The notification fires only on a `storage` event from another tab: this
 * tab's own invalidation calls never echo back. The shell re-verifies identity
 * in response, because the shared session cookie may have changed owners while
 * a request of its own was in flight.
 */
export function onRecentScopesInvalidated(listener: ScopeInvalidationListener): () => void {
  invalidationListeners.add(listener);
  return () => invalidationListeners.delete(listener);
}

/** Adopt another tab's update only when it belongs to this tab's verified scope. */
function adoptForeignChange(event: StorageEvent): void {
  if (event.key === RECENT_NODES_INVALIDATION_STORAGE_KEY) {
    setRecentNodesScope(null);
    // A session transition elsewhere may have replaced the session cookie this
    // tab verified. The shell must re-verify identity before any title may
    // render again: a still-pending identity response issued under the old
    // cookie is exactly the stale truth this notification exists to defeat.
    for (const listener of [...invalidationListeners]) listener();
    return;
  }
  if (currentScope === null || event.key !== recentNodesStorageKey(currentScope)) return;
  publish(readRecentNodes(event.newValue));
}

if (typeof window !== "undefined") window.addEventListener("storage", adoptForeignChange);

/** Subscribe a React surface to successful reads for the verified human only. */
export function useRecentNodes(): RecentNode[] {
  const subscribe = useCallback(
    (onStoreChange: () => void) => onRecentNodesChange(onStoreChange),
    [],
  );
  return useSyncExternalStore(subscribe, getRecentNodes, getRecentNodes);
}
