// @vitest-environment jsdom

/**
 * The preview's sanitising policy.
 *
 * This suite is the regression record for a confirmed stored-XSS hole. Node
 * content is written by agents and the preview renders it in the same origin
 * that holds the API's write credentials, so "the reviewer opened the draft"
 * must never mean "the draft ran".
 *
 * The hole: the previous sanitiser blocklisted elements by `tagName`, which the
 * DOM uppercases *only for HTML-namespaced elements*. Inside `<svg>` or
 * `<math>`, foreign-content elements keep their lowercase local name, so a set
 * holding `"SCRIPT"`, `"STYLE"` and friends matched none of them. Four payloads
 * were demonstrated to execute; each has a test below, named for what it did.
 *
 * The rest of the suite is the other half of the bargain — that closing the
 * hole did not take ordinary Markdown with it.
 *
 * Unlike every other suite in `web/`, this one needs a DOM: sanitising *is*
 * parsing, and a policy asserted against a string matcher would be asserting
 * against the wrong thing. The environment is set per file (the docblock above),
 * so the rest of the harness stays `node`.
 */

import { describe, expect, it } from "vitest";
import { renderMarkdown } from "./markdownRender";
import { sanitiseDiagramSvg } from "./mermaidRender";

/** Parse preview output the way `MarkdownPreview` does, and look at it as DOM. */
function preview(source: string): HTMLElement {
  const host = document.createElement("div");
  host.innerHTML = renderMarkdown(source).html;
  return host;
}

/** Every element in the subtree, by lowercase local name — namespace and all. */
function localNames(host: HTMLElement): string[] {
  return [...host.querySelectorAll("*")].map((element) => element.localName);
}

describe("the demonstrated payloads", () => {
  it("drops the <animate> that rewrites an anchor's href to javascript:", () => {
    // Payload 1. `<animate>` retargets the anchor *after* sanitising, so
    // checking static href/src/xlink:href never saw it. Clicking the text ran
    // arbitrary script.
    const host = preview(
      '<svg><a><animate attributeName="href" values="javascript:window.__PROOF=true"/>' +
        "<text>Expand full analysis</text></a></svg>",
    );

    expect(localNames(host)).not.toContain("animate");
    expect(localNames(host)).not.toContain("svg");
    expect(localNames(host)).not.toContain("a");
    expect(host.innerHTML).not.toContain("javascript:");
    expect(host.innerHTML).not.toContain("__PROOF");
  });

  it("drops a foreign-content <style>, and its declarations with it", () => {
    // Payload 2. Zero-click: an SVG-namespaced <style> restyled the whole app
    // the moment the preview rendered.
    const host = preview("<svg><style>body{background:#3a0000!important}</style></svg>");

    expect(localNames(host)).not.toContain("style");
    expect(host.querySelector("style")).toBeNull();
    // Removed whole, not unwrapped: hoisting the element's children would leave
    // the stylesheet text sitting in the prose.
    expect(host.textContent).not.toContain("#3a0000");
  });

  it("drops the style attribute that makes a full-viewport overlay", () => {
    // Payload 3. No script and no foreign content at all — an inline style is
    // enough for a fixed, viewport-filling, top-of-stack clickjacking surface.
    const host = preview(
      '<div style="position:fixed;inset:0;width:100vw;height:100vh;z-index:99999">over</div>',
    );

    const div = host.querySelector("div");
    expect(div).not.toBeNull();
    expect(div?.getAttribute("style")).toBeNull();
    expect(host.innerHTML).not.toContain("position:fixed");
  });

  it("drops an SVG <script>, which innerHTML leaves latent rather than running", () => {
    // Payload 4. `innerHTML` does not execute an inserted <script>, so this one
    // sat in the DOM waiting for anything that clones or re-inserts the subtree.
    const host = preview("<svg><script>window.__PROOF = true</script></svg>");

    expect(localNames(host)).not.toContain("script");
    expect(host.querySelector("script")).toBeNull();
    expect(host.textContent).not.toContain("__PROOF");
  });
});

describe("the shapes those payloads came in", () => {
  it("keeps no SVG or MathML element, whatever the case or nesting", () => {
    const host = preview(
      "<svg viewBox='0 0 1 1'><circle r='1'/></svg>" +
        "<SVG><FOREIGNOBJECT><div>x</div></FOREIGNOBJECT></SVG>" +
        "<math><mtext><style>body{display:none}</style></mtext></math>",
    );

    for (const name of ["svg", "circle", "foreignobject", "math", "mtext", "style"]) {
      expect(localNames(host)).not.toContain(name);
    }
  });

  it("drops every javascript: and data: URL, padded or not", () => {
    const host = preview(
      '<a href="javascript:alert(1)">a</a>' +
        '<a href="java\nscript:alert(1)">b</a>' +
        '<a href="JaVaScRiPt:alert(1)">c</a>' +
        '<img src="data:image/svg+xml,<svg onload=alert(1)>">' +
        '<img src="data:image/png;base64,iVBORw0KGgo=">' +
        '<a href="vbscript:msgbox(1)">d</a>',
    );

    for (const anchor of host.querySelectorAll("a")) {
      expect(anchor.getAttribute("href")).toBeNull();
    }
    for (const image of host.querySelectorAll("img")) {
      expect(image.getAttribute("src")).toBeNull();
    }
  });

  it("drops event handlers however they are spelled", () => {
    const host = preview(
      '<img src="/api/x" onerror="window.__PROOF=1">' +
        '<div ONMOUSEOVER="window.__PROOF=1">hover</div>' +
        "<p onclick='window.__PROOF=1'>click</p>",
    );

    expect(host.innerHTML).not.toContain("__PROOF");
    for (const element of host.querySelectorAll("*")) {
      for (const attribute of element.getAttributeNames()) {
        expect(attribute.startsWith("on")).toBe(false);
      }
    }
  });

  it("drops the executable and framing elements wholesale", () => {
    const host = preview(
      "<script>window.__PROOF=1</script>" +
        '<iframe src="/api"></iframe>' +
        '<object data="/x"></object>' +
        "<embed src='/x'>" +
        '<form action="/api/nodes"><input name="title"><button>go</button></form>' +
        '<base href="https://evil.example/">' +
        '<link rel="stylesheet" href="/x.css">' +
        "<template><img src=x onerror=alert(1)></template>",
    );

    for (const name of [
      "script",
      "iframe",
      "object",
      "embed",
      "form",
      "input",
      "button",
      "base",
      "link",
      "template",
    ]) {
      expect(localNames(host)).not.toContain(name);
    }
    expect(host.innerHTML).not.toContain("evil.example");
  });

  it("keeps no id or name, so nothing in the preview can clobber the app's DOM", () => {
    const host = preview('<div id="root" name="root">x</div>');
    const div = host.querySelector("div");

    expect(div?.getAttribute("id")).toBeNull();
    expect(div?.getAttribute("name")).toBeNull();
  });
});

describe("ordinary Markdown still renders", () => {
  const source = [
    "# Heading one",
    "",
    "Some **bold**, *em*, ~~struck~~ and `inline code`, plus a",
    "[relative link](/api/nodes/n1) and an [external one](https://example.com/x).",
    "",
    "![a rendition](/api/assets/a1/rendition/thumb)",
    "",
    "- first",
    "- second",
    "  - nested",
    "",
    "1. one",
    "2. two",
    "",
    "| Left | Center | Right |",
    "|:-----|:------:|------:|",
    "| a    | b      | c     |",
    "",
    "> a quotation",
    "",
    "```python",
    "print('hi')",
    "```",
    "",
    "---",
  ].join("\n");

  it("keeps headings, lists, tables, quotes and rules", () => {
    const host = preview(source);

    expect(host.querySelector("h1")?.textContent).toBe("Heading one");
    expect(host.querySelectorAll("ul li").length).toBe(3);
    expect(host.querySelectorAll("ol li").length).toBe(2);
    expect(host.querySelectorAll("table th").length).toBe(3);
    expect(host.querySelectorAll("table td").length).toBe(3);
    expect(host.querySelector("blockquote")?.textContent?.trim()).toBe("a quotation");
    expect(host.querySelector("hr")).not.toBeNull();
  });

  it("keeps inline formatting and fenced code with its language class", () => {
    const host = preview(source);

    expect(host.querySelector("strong")?.textContent).toBe("bold");
    expect(host.querySelector("em")?.textContent).toBe("em");
    expect(host.querySelector("del")?.textContent).toBe("struck");
    const block = host.querySelector("pre > code");
    expect(block?.className).toBe("language-python");
    expect(block?.textContent).toContain("print('hi')");
  });

  it("keeps the table alignment marked's renderer emits", () => {
    const host = preview(source);
    const alignments = [...host.querySelectorAll("th")].map((cell) => cell.getAttribute("align"));

    expect(alignments).toEqual(["left", "center", "right"]);
  });

  it("keeps links and images, and hardens only the external link", () => {
    const host = preview(source);
    const [relative, external] = [...host.querySelectorAll("a")];

    expect(relative?.getAttribute("href")).toBe("/api/nodes/n1");
    expect(relative?.getAttribute("rel")).toBeNull();
    expect(external?.getAttribute("href")).toBe("https://example.com/x");
    expect(external?.getAttribute("rel")).toBe("noopener noreferrer");
    expect(host.querySelector("img")?.getAttribute("src")).toBe(
      "/api/assets/a1/rendition/thumb",
    );
  });

  it("does not let author markup dictate its own rel", () => {
    const host = preview('<a href="https://example.com" rel="opener">x</a>');

    expect(host.querySelector("a")?.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("leaves the mermaid placeholder addressable by the preview", () => {
    const { html, diagrams } = renderMarkdown("```mermaid\ngraph TD; A-->B;\n```");
    const host = document.createElement("div");
    host.innerHTML = html;
    const placeholder = host.querySelector<HTMLElement>(".nd-preview__diagram");

    expect(diagrams).toEqual(["graph TD; A-->B;"]);
    expect(placeholder).not.toBeNull();
    expect(placeholder?.dataset["diagram"]).toBe("0");
  });

  it("escapes rather than renders HTML inside a code fence", () => {
    const host = preview("```html\n<img src=x onerror=alert(1)>\n```");

    expect(host.querySelector("img")).toBeNull();
    expect(host.querySelector("code")?.textContent).toContain("<img src=x onerror=alert(1)>");
  });
});

describe("the mermaid output policy", () => {
  it("keeps the structure and theming a rendered diagram needs", () => {
    const svg = sanitiseDiagramSvg(
      '<svg id="nd-mermaid-1" viewBox="0 0 100 40" role="graphics-document">' +
        "<style>#nd-mermaid-1 .node rect{fill:#1d222a}</style>" +
        '<defs><marker id="arrow" markerWidth="8" refX="4"><path d="M0,0 L8,4 L0,8"/></marker></defs>' +
        '<g class="node"><rect x="0" y="0" width="40" height="20" style="fill:#1d222a"/>' +
        '<foreignObject width="40" height="20"><div class="nodeLabel"><p>Label</p></div></foreignObject>' +
        "</g>" +
        '<path class="edge" d="M40,10 L100,10" marker-end="url(#arrow)"/></svg>',
    );
    const host = document.createElement("div");
    host.innerHTML = svg;

    expect(host.querySelector("svg")).not.toBeNull();
    expect(host.querySelector("style")?.textContent).toContain("fill:#1d222a");
    expect(host.querySelector("marker")?.getAttribute("markerWidth")).toBe("8");
    expect(host.querySelector("path.edge")?.getAttribute("marker-end")).toBe("url(#arrow)");
    expect(host.querySelector("path.edge")?.getAttribute("d")).toBe("M40,10 L100,10");
    expect(host.querySelector("rect")?.getAttribute("style")).toBe("fill:#1d222a");
    expect(host.querySelector("foreignObject p")?.textContent).toBe("Label");
    expect(host.querySelector("svg")?.getAttribute("viewBox")).toBe("0 0 100 40");
  });

  it("still drops script, animation retargeting and javascript: out of a diagram", () => {
    const svg = sanitiseDiagramSvg(
      "<svg>" +
        "<script>window.__PROOF = true</script>" +
        '<a href="javascript:window.__PROOF=true"><text>go</text></a>' +
        '<g onclick="window.__PROOF=true"><animate attributeName="href" ' +
        'values="javascript:window.__PROOF=true"/><set attributeName="href" ' +
        'to="javascript:window.__PROOF=true"/></g>' +
        "</svg>",
    );
    const host = document.createElement("div");
    host.innerHTML = svg;

    expect(host.querySelector("script")).toBeNull();
    expect([...host.querySelectorAll("*")].map((element) => element.localName)).not.toContain(
      "animate",
    );
    expect([...host.querySelectorAll("*")].map((element) => element.localName)).not.toContain(
      "set",
    );
    expect(svg).not.toContain("javascript:");
    expect(svg).not.toContain("onclick");
    // The diagram itself survives the removals.
    expect(host.querySelector("svg")).not.toBeNull();
  });
});
