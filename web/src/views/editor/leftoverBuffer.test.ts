/**
 * What a document switch writes on its way out.
 *
 * `/editor/:nodeId` → `/editor/:otherId` is a route-*parameter* change on a
 * mounted component. Neither `beforeunload` nor the unmount cleanup fires, so
 * the editor used to cancel the autosave debounce and overwrite the buffer with
 * the incoming document — typing into a note and clicking "New" inside the
 * 1200 ms window lost the edit outright, with no error anywhere.
 *
 * {@link flushLeftover} is that transition's write. Its whole reason to exist is
 * that it **carries** the buffer rather than reading refs: by the time it runs,
 * every ref describes a different document. These tests pin what it sends, and
 * pin the two silences it must not keep — an empty new document (which would
 * litter the graph) and a failed write (which is the loss the editor is built
 * around).
 *
 * They also pin what it hands *back*. A create's confirmation has to name the
 * space the server filed the node in (`describeLanding`), which only the
 * response knows — so this returns the created node rather than reducing it to
 * an id, and the caller cannot fall back to echoing the requested target.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushLeftover } from "./useNodeDocument";
import type { LeftoverBuffer } from "./useNodeDocument";
import { api } from "../../api/client";

vi.mock("../../api/client", () => ({
  api: {
    createNode: vi.fn(),
    updateNode: vi.fn(),
  },
}));

const createNode = vi.mocked(api.createNode);
const updateNode = vi.mocked(api.updateNode);

/** A stored document whose buffer has drifted from what the server holds. */
function leftover(overrides: Partial<LeftoverBuffer> = {}): LeftoverBuffer {
  return {
    id: "node-a",
    title: "Note A",
    content: "the text as typed",
    saved: { title: "Note A", content: "the text as loaded" },
    createType: "type-note",
    space: "main",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  createNode.mockResolvedValue({ id: "node-new", space_id: "sp-main" } as never);
  updateNode.mockResolvedValue({ id: "node-a" } as never);
});

describe("a document that was already saved", () => {
  it("writes the buffer as it stood, not whatever the editor holds now", async () => {
    await flushLeftover(leftover());
    expect(updateNode).toHaveBeenCalledWith("node-a", { content: "the text as typed" });
  });

  it("sends only the fields that changed", async () => {
    // An omitted key is left alone server-side and a present one is written, so
    // sending an unchanged title would re-stamp `updated_at` for nothing.
    await flushLeftover(leftover({ title: "Renamed" }));
    expect(updateNode).toHaveBeenCalledWith("node-a", {
      title: "Renamed",
      content: "the text as typed",
    });
  });

  it("sends a cleared title as null rather than as an empty string", async () => {
    await flushLeftover(
      leftover({ title: "   ", content: "the text as loaded" }),
    );
    expect(updateNode).toHaveBeenCalledWith("node-a", { title: null });
  });

  it("writes nothing at all when the buffer matches the server", async () => {
    await flushLeftover(
      leftover({ title: "Note A", content: "the text as loaded" }),
    );
    expect(updateNode).not.toHaveBeenCalled();
    expect(createNode).not.toHaveBeenCalled();
  });
});

describe("a document that was never saved", () => {
  it("creates it, under the type the editor had selected", async () => {
    const written = await flushLeftover(
      leftover({ id: null, title: "Fresh", content: "typed and left", saved: { title: "", content: "" } }),
    );
    expect(createNode).toHaveBeenCalledWith({
      type: "type-note",
      title: "Fresh",
      content: "typed and left",
      space: "main",
    });
    expect(written?.id).toBe("node-new");
  });

  it("hands back the server's node, so the toast can name where it really landed", () => {
    // The confirmation must name the landing space off the *response*, not off
    // the target that was requested — `describeLanding` is the enforcer and it
    // needs the node. Returning a bare id left the caller echoing the request
    // back, which reads plausibly right up to the day the two disagree.
    createNode.mockResolvedValue({ id: "node-new", space_id: "sp-research" } as never);
    return flushLeftover(
      leftover({ id: null, content: "typed and left", saved: { title: "", content: "" } }),
    ).then((written) => {
      expect(written?.created?.space_id).toBe("sp-research");
    });
  });

  it("reports an update as having created nothing, so it gets no landing toast", () => {
    // An update went to the space its node already lived in; a "created in …"
    // for one would be an outright lie about what just happened.
    return flushLeftover(leftover()).then((written) => {
      expect(written).toEqual({ id: "node-a", created: null });
    });
  });

  it("files it into the write target the editor was showing, not today's default", async () => {
    // The buffer carries the space for the same reason it carries the text: by
    // the time this runs the picker may already be describing another document's
    // session, and a leftover landing somewhere the human never chose is exactly
    // the D1a failure the visible target exists to prevent.
    await flushLeftover(
      leftover({
        id: null,
        title: "Fresh",
        content: "typed and left",
        saved: { title: "", content: "" },
        space: "research",
      }),
    );
    expect(createNode).toHaveBeenCalledWith(expect.objectContaining({ space: "research" }));
  });

  it("leaves an untouched blank alone", async () => {
    // Opening `/editor`, looking at it, and navigating away must not litter the
    // graph with empty nodes.
    const written = await flushLeftover(
      leftover({ id: null, title: "  ", content: "\n  \n", saved: { title: "", content: "" } }),
    );
    expect(createNode).not.toHaveBeenCalled();
    expect(written).toBeNull();
  });

  it("creates an untitled node rather than one titled with blanks", async () => {
    await flushLeftover(
      leftover({ id: null, title: "  ", content: "body only", saved: { title: "", content: "" } }),
    );
    expect(createNode).toHaveBeenCalledWith({
      type: "type-note",
      title: null,
      content: "body only",
      space: "main",
    });
  });

  it("refuses loudly when there is text but no type to create it under", async () => {
    // The alternative is dropping the text on the floor with nobody told, which
    // is the exact failure this whole path exists to prevent.
    await expect(
      flushLeftover(
        leftover({
          id: null,
          content: "work worth keeping",
          saved: { title: "", content: "" },
          createType: null,
        }),
      ),
    ).rejects.toThrow(/type catalog/);
    expect(createNode).not.toHaveBeenCalled();
  });
});

describe("when the write fails", () => {
  it("rejects rather than resolving, so the caller can report it", async () => {
    updateNode.mockRejectedValue(new Error("database is locked"));
    await expect(flushLeftover(leftover())).rejects.toThrow("database is locked");
  });
});
