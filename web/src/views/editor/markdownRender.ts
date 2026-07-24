/**
 * Markdown → HTML for the preview pane, with ```mermaid fences lifted out.
 *
 * The preview is a *rendering* of the buffer, never a second copy of it: this
 * module is one-way by construction (there is no HTML → Markdown path here and
 * there must never be one), which is what keeps design §2.3.2 true — the text
 * in the editor is the stored `content`, byte for byte, and nothing round-trips
 * through an HTML or JSON intermediate.
 *
 * Mermaid fences are replaced with empty placeholder divs rather than rendered
 * inline, because rendering a diagram is asynchronous and can fail. The preview
 * component fills each placeholder afterwards, so one broken diagram cannot
 * take the surrounding prose with it.
 */

import { Marked } from "marked";

/** One rendered Markdown document plus the diagram sources lifted out of it. */
export interface RenderedMarkdown {
  /** Sanitised HTML. Each mermaid fence is an empty placeholder element. */
  html: string;
  /** The mermaid sources, in the order their placeholders appear. */
  diagrams: string[];
}

/** Class on the empty element standing in for a mermaid fence. */
export const DIAGRAM_PLACEHOLDER_CLASS = "nd-preview__diagram";

/**
 * Diagram sources collected during the current parse.
 *
 * Module-level because marked's renderer hooks take no user context. Safe
 * because `marked.parse` is synchronous here: a parse runs to completion before
 * the next one starts, so the two can never interleave.
 */
let collected: string[] = [];

const markdown = new Marked({
  gfm: true,
  breaks: false,
  renderer: {
    code({ text, lang }) {
      const language = (lang ?? "").trim().split(/\s+/)[0] ?? "";
      if (language.toLowerCase() === "mermaid") {
        const index = collected.push(text) - 1;
        return `<div class="${DIAGRAM_PLACEHOLDER_CLASS}" data-diagram="${index}"></div>`;
      }
      const attribute = language ? ` class="language-${escapeHtml(language)}"` : "";
      return `<pre><code${attribute}>${escapeHtml(text)}\n</code></pre>`;
    },
  },
});

/**
 * Render Markdown source to sanitised preview HTML.
 *
 * @param source The editor buffer, verbatim.
 * @returns The HTML and the mermaid sources its placeholders stand for.
 */
export function renderMarkdown(source: string): RenderedMarkdown {
  collected = [];
  const html = markdown.parse(source, { async: false });
  const diagrams = collected;
  collected = [];
  return { html: sanitiseHtml(html), diagrams };
}

/* ------------------------------------------------------------------ */
/* Sanitising                                                           */
/* ------------------------------------------------------------------ */

/**
 * Elements dropped wholesale from the preview.
 *
 * Markdown allows raw HTML, and node content is not always the reader's own
 * writing — agents propose content into this database, and the preview renders
 * it in the same origin that holds the API's write credentials. Stripping the
 * executable and network-fetching elements keeps "preview an agent's draft"
 * from meaning "run an agent's script".
 */
const FORBIDDEN_ELEMENTS = new Set([
  "SCRIPT",
  "STYLE",
  "IFRAME",
  "FRAME",
  "FRAMESET",
  "OBJECT",
  "EMBED",
  "LINK",
  "META",
  "BASE",
  "FORM",
  "INPUT",
  "BUTTON",
  "TEXTAREA",
  "SELECT",
  "TEMPLATE",
]);

/** Attributes whose value is a URL and therefore has to be checked. */
const URL_ATTRIBUTES = new Set(["href", "src", "xlink:href"]);

/** Attributes dropped unconditionally — URL carriers we do not need. */
const DROPPED_ATTRIBUTES = new Set(["srcset", "formaction", "ping", "background"]);

/**
 * URL schemes allowed through.
 *
 * Site-relative paths are the common case (`/api/assets/…/rendition`).
 * `data:` is allowed only for raster images — `data:image/svg+xml` is excluded
 * because an SVG document can carry script.
 */
const SAFE_URL = /^(?:https?:\/\/|mailto:|tel:|\/|\.{1,2}\/|#|data:image\/(?:png|jpeg|gif|webp);)/i;

/**
 * Strip scripting and network-fetching constructs out of rendered HTML.
 *
 * Parsed with `DOMParser` into an inert document, so nothing in the input runs
 * or loads while it is being inspected.
 *
 * @param html HTML from the Markdown renderer.
 * @returns The same HTML with unsafe elements and attributes removed.
 */
function sanitiseHtml(html: string): string {
  const parsed = new DOMParser().parseFromString(html, "text/html");
  const walker = parsed.createTreeWalker(parsed.body, NodeFilter.SHOW_ELEMENT);
  const doomed: Element[] = [];

  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const element = node as Element;
    if (FORBIDDEN_ELEMENTS.has(element.tagName)) {
      doomed.push(element);
      continue;
    }
    for (const attribute of [...element.attributes]) {
      const name = attribute.name.toLowerCase();
      if (name.startsWith("on") || DROPPED_ATTRIBUTES.has(name)) {
        element.removeAttribute(attribute.name);
        continue;
      }
      if (URL_ATTRIBUTES.has(name) && !SAFE_URL.test(attribute.value.trim())) {
        element.removeAttribute(attribute.name);
      }
    }
  }

  for (const element of doomed) element.remove();
  return parsed.body.innerHTML;
}

/** Escape the five characters that would otherwise open a tag or entity. */
function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
