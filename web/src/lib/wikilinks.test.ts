// @vitest-environment jsdom

/**
 * The wikilink URL contract and its click interceptor.
 *
 * The href shape is a contract between the renderer (`markdownRender.ts`) and
 * the interceptor (editor preview, reading view) — and the sanitiser sits
 * between them, so the round-trip tests here run the *sanitised* anchor the
 * way a click actually would. The resolution mapping is the other half of the
 * same contract: only `resolved` navigates, and the failure copy is owned
 * here so the two surfaces cannot disagree about it.
 */

import { describe, expect, it, vi } from "vitest";
import type { TitleResolution } from "../api/types";
import {
  actionForResolution,
  attachWikilinkClicks,
  titleFromWikilinkHref,
  wikilinkHref,
  WIKILINK_TITLE_PATH,
} from "./wikilinks";

describe("the href contract", () => {
  it("builds and parses a site-relative href with the title encoded", () => {
    const href = wikilinkHref("Q13 principals design");
    expect(href).toBe("/node/title/Q13%20principals%20design");
    expect(titleFromWikilinkHref(href)).toBe("Q13 principals design");
  });

  it("keeps a title's punctuation out of the path", () => {
    const href = wikilinkHref("a/b#c?d&e='f");
    expect(href.startsWith(WIKILINK_TITLE_PATH)).toBe(true);
    // The title must not leak into the route or the query; `'` is the one
    // character encodeURIComponent leaves alone, and it is harmless inside
    // the double-quoted href attribute the renderer emits.
    const segment = href.slice(WIKILINK_TITLE_PATH.length);
    expect(segment).not.toMatch(/[/#?&"]/);
    expect(titleFromWikilinkHref(href)).toBe("a/b#c?d&e='f");
  });

  it("refuses a href that is not a wikilink's, including the path alone", () => {
    expect(titleFromWikilinkHref("/node/some-id")).toBeNull();
    expect(titleFromWikilinkHref("/node/title/")).toBeNull();
    expect(titleFromWikilinkHref("https://example.com/node/title/x")).toBeNull();
    expect(titleFromWikilinkHref("/node/title/%E0%A4%A")).toBeNull();
  });

  it("parses a percent-encoded non-ASCII title", () => {
    const href = wikilinkHref("École normale");
    expect(titleFromWikilinkHref(href)).toBe("École normale");
  });
});

describe("the resolution mapping", () => {
  function entry(overrides: Partial<TitleResolution>): TitleResolution {
    return {
      title: "the title",
      outcome: "not-found",
      node_id: null,
      space_id: null,
      ...overrides,
    };
  }

  it("navigates on resolved, with the server's id", () => {
    expect(actionForResolution(entry({ outcome: "resolved", node_id: "n1" }))).toEqual({
      kind: "navigate",
      nodeId: "n1",
    });
  });

  it("reports ambiguity instead of guessing", () => {
    const action = actionForResolution(entry({ outcome: "ambiguous" }));
    expect(action.kind).toBe("notice");
    if (action.kind === "notice") {
      expect(action.toastTitle).toContain('"the title"');
    }
  });

  it("names a missing title, and never claims a space is absent", () => {
    const action = actionForResolution(entry({ outcome: "not-found" }));
    expect(action.kind).toBe("notice");
    if (action.kind === "notice") {
      expect(action.toastTitle).toBe('No active node titled "the title"');
      expect(action.toastDetail).toBeUndefined();
      const copy = `${action.toastTitle} ${action.toastDetail ?? ""}`;
      expect(copy.toLowerCase()).not.toContain("does not exist");
      expect(copy.toLowerCase()).not.toContain("no such space");
    }
  });

  it("treats a contract-violating null id as missing, never navigating", () => {
    const action = actionForResolution(entry({ outcome: "resolved", node_id: null }));
    expect(action.kind).toBe("notice");
  });
});

describe("the click interceptor", () => {
  it("intercepts a plain click on a wikilink and reports its title", () => {
    const container = document.createElement("div");
    container.innerHTML =
      `<p>see <a class="nd-wikilink" href="${wikilinkHref("the target")}">label</a></p>` +
      '<p>and an <a href="https://example.com">ordinary link</a></p>';
    document.body.append(container);

    const onWikilink = vi.fn();
    const detach = attachWikilinkClicks(container, onWikilink);
    try {
      const anchor = container.querySelector<HTMLAnchorElement>("a.nd-wikilink");
      anchor?.dispatchEvent(new MouseEvent("click", { bubbles: true, button: 0 }));
      expect(onWikilink).toHaveBeenCalledWith("the target");

      // A click on a non-wikilink link is left alone.
      onWikilink.mockClear();
      container
        .querySelector<HTMLAnchorElement>('a:not(.nd-wikilink)')
        ?.dispatchEvent(new MouseEvent("click", { bubbles: true, button: 0 }));
      expect(onWikilink).not.toHaveBeenCalled();
    } finally {
      detach();
      container.remove();
    }
  });

  it("prevents default only for the plain left click it handles", () => {
    const container = document.createElement("div");
    container.innerHTML = `<a class="nd-wikilink" href="${wikilinkHref("x")}">x</a>`;
    document.body.append(container);

    const onWikilink = vi.fn();
    const detach = attachWikilinkClicks(container, onWikilink);
    try {
      const anchor = container.querySelector<HTMLAnchorElement>("a.nd-wikilink");
      const plain = new MouseEvent("click", { bubbles: true, button: 0, cancelable: true });
      anchor?.dispatchEvent(plain);
      expect(plain.defaultPrevented).toBe(true);
      expect(onWikilink).toHaveBeenCalledTimes(1);

      // A modified click is the browser's to open elsewhere: no intercept.
      onWikilink.mockClear();
      const modified = new MouseEvent("click", {
        bubbles: true,
        button: 0,
        cancelable: true,
        ctrlKey: true,
      });
      anchor?.dispatchEvent(modified);
      expect(modified.defaultPrevented).toBe(false);
      expect(onWikilink).not.toHaveBeenCalled();

      // Detaching stops the interception.
      detach();
      const after = new MouseEvent("click", { bubbles: true, button: 0, cancelable: true });
      anchor?.dispatchEvent(after);
      expect(after.defaultPrevented).toBe(false);
    } finally {
      container.remove();
    }
  });
});
