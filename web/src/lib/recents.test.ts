// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

async function freshStore() {
  vi.resetModules();
  return await import("./recents");
}

beforeEach(() => window.localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe("recent nodes", () => {
  it("exposes and records nothing before a verified identity establishes a scope", async () => {
    const store = await freshStore();
    store.recordRecentNode({ id: "alpha", title: "Private read" });

    expect(store.getRecentNodes()).toEqual([]);
    expect(Object.keys(window.localStorage)).toEqual([]);
  });

  it("keeps the newest successful read once and bounds each human's history", async () => {
    const store = await freshStore();
    store.setRecentNodesScope("human-owner");
    for (let index = 0; index < store.RECENT_NODE_LIMIT + 2; index += 1) {
      store.recordRecentNode({ id: `node-${index}`, title: `Node ${index}` });
    }
    store.recordRecentNode({ id: "node-2", title: "Renamed node 2" });

    expect(store.getRecentNodes()).toHaveLength(store.RECENT_NODE_LIMIT);
    expect(store.getRecentNodes()[0]).toEqual({ id: "node-2", title: "Renamed node 2" });
    expect(store.getRecentNodes().filter((node) => node.id === "node-2")).toHaveLength(1);
  });

  it("survives a reload only after the same verified identity restores its scope", async () => {
    const store = await freshStore();
    store.setRecentNodesScope("human-owner");
    store.recordRecentNode({ id: "alpha", title: "Alpha" });
    const reloaded = await freshStore();
    expect(reloaded.getRecentNodes()).toEqual([]);

    reloaded.setRecentNodesScope("human-owner");
    expect(reloaded.getRecentNodes()).toEqual([{ id: "alpha", title: "Alpha" }]);
  });

  it("changes scope rather than relying on cleanup when another human is verified", async () => {
    const store = await freshStore();
    store.setRecentNodesScope("human-owner");
    store.recordRecentNode({ id: "alpha", title: "Owner read" });

    store.setRecentNodesScope("human-second");
    expect(store.getRecentNodes()).toEqual([]);
    store.recordRecentNode({ id: "beta", title: "Second read" });

    expect(window.localStorage.getItem(store.recentNodesStorageKey("human-owner"))).toContain("Owner read");
    expect(window.localStorage.getItem(store.recentNodesStorageKey("human-second"))).toContain("Second read");
    store.setRecentNodesScope("human-owner");
    expect(store.getRecentNodes()).toEqual([{ id: "alpha", title: "Owner read" }]);
  });

  it("adopts cross-tab storage updates only for the verified scope", async () => {
    const store = await freshStore();
    store.setRecentNodesScope("human-owner");
    const ownerKey = store.recentNodesStorageKey("human-owner");
    const secondKey = store.recentNodesStorageKey("human-second");

    window.dispatchEvent(
      new StorageEvent("storage", {
        key: secondKey,
        newValue: JSON.stringify([{ id: "beta", title: "Second read" }]),
        storageArea: window.localStorage,
      }),
    );
    expect(store.getRecentNodes()).toEqual([]);

    window.dispatchEvent(
      new StorageEvent("storage", {
        key: ownerKey,
        newValue: JSON.stringify([{ id: "alpha", title: "Owner read" }]),
        storageArea: window.localStorage,
      }),
    );
    expect(store.getRecentNodes()).toEqual([{ id: "alpha", title: "Owner read" }]);
  });

  it("drops every active scope when another tab reports a session transition", async () => {
    const store = await freshStore();
    store.setRecentNodesScope("human-owner");
    store.recordRecentNode({ id: "alpha", title: "Owner read" });

    window.dispatchEvent(
      new StorageEvent("storage", {
        key: store.RECENT_NODES_INVALIDATION_STORAGE_KEY,
        newValue: "session-transition",
        storageArea: window.localStorage,
      }),
    );

    expect(store.getRecentNodesScope()).toBeNull();
    expect(store.getRecentNodes()).toEqual([]);
  });

  it("writes changing invalidation markers without secure-context crypto", async () => {
    vi.stubGlobal("crypto", {});
    const store = await freshStore();
    store.setRecentNodesScope("human-owner");

    store.invalidateRecentNodesScopes();
    expect(window.localStorage.getItem(store.RECENT_NODES_INVALIDATION_STORAGE_KEY)).toBe("1");

    store.invalidateRecentNodesScopes();
    expect(window.localStorage.getItem(store.RECENT_NODES_INVALIDATION_STORAGE_KEY)).toBe("2");
  });

  it("discards malformed stored data rather than presenting it as a readable node", async () => {
    const store = await freshStore();
    expect(store.readRecentNodes('[{"id":"ok","title":"OK"},{"id":4},null]')).toEqual([
      { id: "ok", title: "OK" },
    ]);
    expect(store.readRecentNodes("not json")).toEqual([]);
  });
});
