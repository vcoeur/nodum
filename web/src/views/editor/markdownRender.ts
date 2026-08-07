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
import type { TokenizerAndRendererExtension, Tokens } from "marked";
import DOMPurify from "dompurify";
import type { Config as PurifyConfig, DOMPurify as Purifier } from "dompurify";
import { wikilinkHref } from "../../lib/wikilinks";

/** One rendered Markdown document plus the diagram sources lifted out of it. */
export interface RenderedMarkdown {
  /** Sanitised HTML. Each mermaid fence is an empty placeholder element. */
  html: string;
  /** The mermaid sources, in the order their placeholders appear. */
  diagrams: string[];
}

/** Class on the empty element standing in for a mermaid fence. */
export const DIAGRAM_PLACEHOLDER_CLASS = "nd-preview__diagram";

/** Attribute carrying a placeholder's index into {@link RenderedMarkdown.diagrams}. */
const DIAGRAM_INDEX_ATTRIBUTE = "data-diagram";

/**
 * Diagram sources collected during the current parse.
 *
 * Module-level because marked's renderer hooks take no user context. Safe
 * because `marked.parse` is synchronous here: a parse runs to completion before
 * the next one starts, so the two can never interleave.
 */
let collected: string[] = [];

/**
 * One parsed `[[Title]]` (or `[[Title|label]]`) inline token.
 *
 * `label` is null when the wikilink has no `|label` half; the renderer falls
 * back to the title then, so `[[Title]]` and `[[Title|Title]]` render the
 * same anchor.
 */
interface WikilinkToken extends Tokens.Generic {
  type: "wikilink";
  title: string;
  label: string | null;
}

/**
 * `[[Title]]` → an anchor to the reading view.
 *
 * The grammar mirrors the write-side materialisation's
 * (`service.WIKILINK_RE` — no brackets, no newline), with the display-only
 * `|label` half layered on top: the first `|` splits title from label, and a
 * label never affects resolution. The target title travels in a site-relative
 * href (`/node/title/<Title>`) — not a `data-*` attribute, which the preview
 * policy strips by design — so the anchor survives {@link PREVIEW_POLICY}
 * unchanged and a click can resolve the title back out of the href. The
 * title is URL-encoded whole, so `#`, `?` and `/` in a title never leak into
 * the route or the query string.
 */
const WIKILINK_TOKEN = /^\[\[([^[\]\n]+?)(?:\|([^[\]\n]*))?\]\]/;

const wikilinkExtension: TokenizerAndRendererExtension = {
  name: "wikilink",
  level: "inline",
  start(src) {
    return src.indexOf("[[");
  },
  tokenizer(src): WikilinkToken | undefined {
    const match = WIKILINK_TOKEN.exec(src);
    if (match === null) return undefined;
    // The regex guarantees both groups; the `?? ""` fallbacks are for the
    // unchecked-index narrowing only.
    return {
      type: "wikilink",
      raw: match[0] ?? "",
      title: match[1] ?? "",
      label: match[2] ?? null,
    };
  },
  renderer(token) {
    const wikilink = token as WikilinkToken;
    return `<a href="${wikilinkHref(wikilink.title)}" class="nd-wikilink">${escapeHtml(
      wikilink.label || wikilink.title,
    )}</a>`;
  },
};

const markdown = new Marked({
  gfm: true,
  breaks: false,
  extensions: [wikilinkExtension],
  renderer: {
    code({ text, lang }) {
      const language = (lang ?? "").trim().split(/\s+/)[0] ?? "";
      if (language.toLowerCase() === "mermaid") {
        const index = collected.push(text) - 1;
        return `<div class="${DIAGRAM_PLACEHOLDER_CLASS}" ${DIAGRAM_INDEX_ATTRIBUTE}="${index}"></div>`;
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
  return { html: sanitisePreviewHtml(html), diagrams };
}

/* ------------------------------------------------------------------ */
/* Sanitising                                                           */
/* ------------------------------------------------------------------ */

/*
 * Why DOMPurify and not a hand-written pass.
 *
 * The previous version of this file blocklisted elements by `tagName`, which is
 * uppercased *only for HTML-namespaced elements*. Inside `<svg>` or `<math>` an
 * element keeps its lowercase local name, so `<svg><style>`, `<svg><script>`
 * and `<svg><animate>` walked straight through a set that only ever held
 * `"STYLE"`, `"SCRIPT"` and friends. That is not a typo to patch: HTML parsing
 * has foreign content, namespace confusion, mutation on re-serialisation and
 * attribute aliasing, and a blocklist has to be right about all of them
 * forever. An allowlist maintained by people who track those attacks is the
 * right shape; this module states the policy and DOMPurify enforces it.
 */

/**
 * Elements the preview renders.
 *
 * An allowlist, and deliberately close to what `marked` can emit from GitHub
 * Flavoured Markdown — headings, paragraphs, emphasis, code, quotes, lists,
 * tables, links, images — plus the few structural tags (`div`, `span`,
 * `details`, `figure`) that hand-written HTML in a note legitimately uses.
 * Everything else is dropped, including the whole of SVG and MathML (see
 * {@link PREVIEW_POLICY}).
 *
 * Not here on purpose: `input`. GFM task lists render as
 * `<input type="checkbox" disabled>`, which the previous sanitiser also
 * dropped, so keeping it out changes nothing a reader sees today and avoids
 * putting a form control — the one element family that carries `formaction`,
 * `autofocus` and form-association — into agent-authored content.
 */
const ALLOWED_TAGS = [
  "a",
  "abbr",
  "b",
  "blockquote",
  "br",
  "caption",
  "code",
  "col",
  "colgroup",
  "dd",
  "del",
  "details",
  "div",
  "dl",
  "dt",
  "em",
  "figcaption",
  "figure",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "hr",
  "i",
  "img",
  "ins",
  "kbd",
  "li",
  "mark",
  "ol",
  "p",
  "pre",
  "q",
  "s",
  "samp",
  "small",
  "span",
  "strong",
  "sub",
  "summary",
  "sup",
  "table",
  "tbody",
  "td",
  "tfoot",
  "th",
  "thead",
  "tr",
  "ul",
  "var",
];

/**
 * Attributes the preview keeps.
 *
 * `align`, `colspan` and `rowspan` are what marked's table renderer emits;
 * `class` carries `language-…` on code blocks and this module's own diagram
 * placeholder class; `data-diagram` is that placeholder's index.
 *
 * **`style` is not here.** An inline style on agent-authored content is enough
 * for a full-viewport `position:fixed` overlay — a clickjacking surface with no
 * script and no interaction required — and Markdown never needs one. `id` and
 * `name` are out for the same family of reasons (DOM clobbering, and colliding
 * with the app's own element ids). `rel` is out because this module *sets* it
 * (see {@link hardenPreviewElement}); an author-supplied `rel` could otherwise
 * undo that.
 */
const ALLOWED_ATTR = [
  "align",
  "alt",
  "class",
  "colspan",
  DIAGRAM_INDEX_ATTRIBUTE,
  "dir",
  "height",
  "href",
  "lang",
  "open",
  "reversed",
  "rowspan",
  "src",
  "start",
  "title",
  "width",
];

/** The one namespace the preview renders. Kills `<svg>` and `<math>` outright. */
const HTML_NAMESPACE = "http://www.w3.org/1999/xhtml";

/**
 * URL schemes allowed through, in `href` and `src`.
 *
 * Site-relative paths are the common case (`/api/assets/…/rendition`).
 * Anything without one of these prefixes — `javascript:`, `data:`, `vbscript:`,
 * a bare `foo.md`, a protocol-relative `//host` — loses the attribute.
 */
const SAFE_URL = /^(?:https?:\/\/|mailto:|tel:|\/(?!\/)|\.{1,2}\/|#)/i;

/** Attributes whose value the browser will fetch or navigate to. */
const URL_ATTRIBUTES = ["href", "src", "xlink:href"];

/**
 * The allowed attributes that are *not* URLs.
 *
 * DOMPurify runs `ALLOWED_URI_REGEXP` against every attribute value it has not
 * been told is inert — not just the URL-bearing ones. Its default pattern is
 * loose enough that `align="left"` and `colspan="2"` slip through by accident;
 * {@link SAFE_URL} is not, so without this list a strict URL policy would
 * quietly strip table alignment, column spans and the diagram placeholder's
 * index. Naming the inert attributes keeps the URL check aimed at
 * {@link URL_ATTRIBUTES} and nothing else.
 */
const INERT_ATTRIBUTES = ALLOWED_ATTR.filter((name) => !URL_ATTRIBUTES.includes(name));

/**
 * Characters a browser strips from a URL attribute before acting on it.
 *
 * Mirrors DOMPurify's own `ATTR_WHITESPACE`: `java\nscript:` and `data\t:` are
 * the same URL to the browser as the unpadded forms, so a prefix test has to
 * run against the stripped value or it tests the wrong string.
 */
// The control-character range is the point: it is exactly what a browser
// strips from a URL attribute before acting on it, so the check must match it.
// eslint-disable-next-line no-control-regex
const ATTRIBUTE_WHITESPACE = /[\u0000-\u0020\u00a0\u1680\u180e\u2000-\u2029\u205f\u3000]/g;

/** Absolute links get `rel`; site-relative and in-page ones are not external. */
const EXTERNAL_URL = /^https?:\/\//i;

/**
 * The preview's sanitising policy.
 *
 * What it allows, and why:
 *
 * - **{@link ALLOWED_TAGS} and {@link ALLOWED_ATTR}** — allowlists, so an
 *   element or attribute nobody thought about is absent rather than present.
 * - **`ALLOWED_NAMESPACES: [HTML_NAMESPACE]`** — *no SVG, no MathML.* Node
 *   content is authored by agents and a Markdown note has no legitimate need
 *   for raw SVG; keeping foreign content out removes the entire namespace-
 *   confusion class in one move, along with `<animate>`/`<set>` (which rewrite
 *   a sibling's `href` to `javascript:` *after* any static URL check has run)
 *   and foreign-content `<style>`/`<script>`. Diagrams are not affected: they
 *   come from ```mermaid fences and are rendered separately, by `mermaidRender`.
 * - **`ALLOWED_URI_REGEXP: SAFE_URL`, with {@link INERT_ATTRIBUTES} declared
 *   URI-safe** — stricter than DOMPurify's default, which also permits `ftp:`,
 *   `sms:`, `cid:`, `xmpp:` and scheme-less references. The second half is what
 *   keeps the first from also eating `align="left"`.
 * - **`ALLOW_DATA_ATTR: false`, `ALLOW_ARIA_ATTR: false`** — Markdown emits
 *   neither, so the only `data-` attribute that survives is the one this module
 *   puts there itself, named explicitly in {@link ALLOWED_ATTR}.
 * - **`ADD_FORBID_CONTENTS`** — DOMPurify keeps a removed element's children by
 *   default. For `<style>`, `<svg>`, `<math>` and `<template>` that would spill
 *   stylesheet or markup text into the prose; these are removed whole.
 *
 * Event handlers, `<script>`, `<iframe>`, `<object>` and the rest never come up
 * because they are not on the allowlists.
 */
const PREVIEW_POLICY: PurifyConfig = {
  ALLOWED_TAGS,
  ALLOWED_ATTR,
  ALLOWED_NAMESPACES: [HTML_NAMESPACE],
  ALLOWED_URI_REGEXP: SAFE_URL,
  ADD_URI_SAFE_ATTR: INERT_ATTRIBUTES,
  ALLOW_DATA_ATTR: false,
  ALLOW_ARIA_ATTR: false,
  ADD_FORBID_CONTENTS: ["style", "svg", "math", "template"],
};

/**
 * The instance the preview sanitises through.
 *
 * Its own instance rather than the shared default export, because it carries a
 * hook: hooks are per-instance global, and registering one on the default
 * export would silently apply to every other caller in the app.
 *
 * Created on first use, not at import time — the module is imported by the test
 * runner and by the bundle's entry graph, and only the browser has a `window`.
 */
let purifier: Purifier | null = null;

function previewPurifier(): Purifier {
  if (purifier === null) {
    purifier = DOMPurify(window);
    purifier.addHook("afterSanitizeAttributes", hardenPreviewElement);
  }
  return purifier;
}

/**
 * Strip scripting and network-fetching constructs out of rendered HTML.
 *
 * @param html HTML from the Markdown renderer.
 * @returns The same HTML reduced to {@link PREVIEW_POLICY}.
 */
function sanitisePreviewHtml(html: string): string {
  return previewPurifier().sanitize(html, PREVIEW_POLICY);
}

/**
 * Post-pass on every element DOMPurify keeps.
 *
 * Two jobs the allowlists cannot do on their own:
 *
 * 1. **No `data:` URLs at all.** DOMPurify permits a `data:` URL on media tags
 *    (`img` among them) regardless of `ALLOWED_URI_REGEXP`, which would let
 *    `data:image/svg+xml` back in through the one image tag the preview
 *    renders. Assets have a real endpoint; the preview needs no inline images.
 * 2. **External links carry `rel="noopener noreferrer"`.** `noreferrer` also
 *    keeps the node id in the current URL out of the `Referer` sent to a
 *    third-party site an agent linked to. Nothing here sets `target`, so
 *    `noopener` is defence rather than the load-bearing half.
 *
 * The element test is by namespace and local name on purpose: `tagName` is
 * uppercased only for HTML elements, and reading it as if it always were is
 * exactly the bug this rewrite exists to remove.
 */
function hardenPreviewElement(element: Element): void {
  for (const name of URL_ATTRIBUTES) {
    const value = element.getAttribute(name);
    if (value === null) continue;
    const normalised = value.replace(ATTRIBUTE_WHITESPACE, "").toLowerCase();
    if (normalised.startsWith("data:")) element.removeAttribute(name);
  }

  if (element.namespaceURI !== HTML_NAMESPACE || element.localName !== "a") return;
  const href = element.getAttribute("href");
  if (href !== null && EXTERNAL_URL.test(href.trim())) {
    element.setAttribute("rel", "noopener noreferrer");
  }
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
