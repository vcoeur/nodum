import { beforeEach, describe, expect, it, vi } from "vitest";

async function freshState() {
  vi.resetModules();
  return await import("./modalState");
}

beforeEach(() => vi.resetModules());

describe("shared modal ownership", () => {
  it("remains open until the last modal releases ownership", async () => {
    const state = await freshState();
    state.modalOpened();
    state.modalOpened();
    state.modalClosed();
    expect(state.isModalOpen()).toBe(true);
    state.modalClosed();
    expect(state.isModalOpen()).toBe(false);
  });

  it("does not underflow when an already-closed dialog unmounts again", async () => {
    const state = await freshState();
    state.modalClosed();
    expect(state.isModalOpen()).toBe(false);
  });
});
