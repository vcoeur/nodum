/**
 * Mermaid rendering for the preview pane: loaded once, initialised once, and
 * never allowed to throw into React.
 *
 * Three things this module exists to guarantee:
 *
 * 1. **One initialisation.** `mermaid.initialize` is global and re-running it
 *    resets configuration for every diagram on the page, so it happens exactly
 *    once, inside the module-level load promise.
 * 2. **Failure is a value, not an exception.** A half-typed diagram is the
 *    normal state of a diagram being written; {@link renderDiagram} returns a
 *    failure result instead of rejecting, so the preview renders "this diagram
 *    is broken" in place and the surrounding prose is untouched.
 * 3. **Typing stays cheap.** Results are memoised by source text, so editing a
 *    paragraph next to five diagrams re-renders none of them.
 *
 * Mermaid is ~500 kB of the bundle and only the editor's preview needs it, so
 * it is pulled in with a dynamic `import()` — the editor route opens without
 * paying for it, and the pane fills in when the first diagram appears.
 *
 * Rendered SVG goes through DOMPurify on the way out ({@link sanitiseDiagramSvg}).
 * Mermaid's own `securityLevel: "strict"` already sanitises the labels it draws,
 * but the preview inserts this markup with `innerHTML` into a page holding the
 * API's write credentials, and one library's strict mode should not be the only
 * thing standing between an agent's diagram source and script execution.
 */

import DOMPurify from "dompurify";
import type { Config as PurifyConfig, DOMPurify as Purifier } from "dompurify";

/** The mermaid API surface, without importing the module up front. */
type MermaidApi = (typeof import("mermaid"))["default"];

/** The outcome of rendering one diagram. */
export type DiagramResult =
  | { ok: true; svg: string }
  | { ok: false; message: string };

/**
 * Colours handed to mermaid.
 *
 * Literal values rather than `var(--nd-…)`: mermaid inlines these into the
 * generated SVG's own `<style>` block and also uses them for computed
 * contrast decisions, which a custom property cannot survive. They mirror
 * `styles/tokens.css` — keep them in step with it by hand.
 */
const THEME_VARIABLES = {
  darkMode: true,
  background: "#12151a",
  primaryColor: "#1d222a",
  primaryTextColor: "#dfe4ec",
  primaryBorderColor: "#39414f",
  secondaryColor: "#232935",
  tertiaryColor: "#171b21",
  lineColor: "#7a8394",
  textColor: "#dfe4ec",
  mainBkg: "#1d222a",
  nodeBorder: "#39414f",
  clusterBkg: "#171b21",
  clusterBorder: "#272d37",
  titleColor: "#c9a24a",
  edgeLabelBackground: "#171b21",
  fontFamily:
    'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
  fontSize: "14px",
};

/** How many rendered diagrams to remember. */
const CACHE_LIMIT = 64;

let loader: Promise<MermaidApi> | null = null;
const cache = new Map<string, DiagramResult>();
let sequence = 0;

/** Load and configure mermaid, once per page. */
function loadMermaid(): Promise<MermaidApi> {
  if (!loader) {
    loader = import("mermaid").then((module) => {
      const mermaid = module.default;
      mermaid.initialize({
        startOnLoad: false,
        // The preview renders content this browser did not author, so mermaid's
        // own label sanitising stays at its strictest setting.
        securityLevel: "strict",
        theme: "base",
        themeVariables: THEME_VARIABLES,
        flowchart: { htmlLabels: true, useMaxWidth: true },
        sequence: { useMaxWidth: true },
        gantt: { useMaxWidth: true },
      });
      return mermaid;
    });
  }
  return loader;
}

/**
 * Render one mermaid source to SVG.
 *
 * Never rejects: a syntax error, an unsupported diagram type, or a failure to
 * load mermaid at all all come back as `{ ok: false }` with a message worth
 * showing. Failures are cached alongside successes, so a diagram that is still
 * being typed is not re-attempted on every keystroke.
 *
 * @param source The text inside the ```mermaid fence.
 * @returns The SVG markup, or the reason it could not be produced.
 */
export async function renderDiagram(source: string): Promise<DiagramResult> {
  const cached = cache.get(source);
  if (cached) return cached;

  const id = `nd-mermaid-${++sequence}`;
  let result: DiagramResult;
  try {
    const mermaid = await loadMermaid();
    const { svg } = await mermaid.render(id, source);
    result = { ok: true, svg: sanitiseDiagramSvg(svg) };
  } catch (error) {
    result = { ok: false, message: describeDiagramError(error) };
  } finally {
    // A failed render leaves mermaid's scratch element behind in the body.
    removeStrayElement(id);
    removeStrayElement(`d${id}`);
  }

  remember(source, result);
  return result;
}

/**
 * The already-known result for a source, without starting a render.
 *
 * Lets the preview re-insert an unchanged diagram in the same tick it rebuilds
 * the surrounding HTML. Going through the promise would put the diagram back
 * one microtask later, which is one frame of collapsed-then-restored height on
 * every keystroke — the layout shift this editor is not allowed to have.
 *
 * @param source The text inside the ```mermaid fence.
 * @returns The cached result, or undefined if this diagram is new.
 */
export function peekDiagram(source: string): DiagramResult | undefined {
  return cache.get(source);
}

/* ------------------------------------------------------------------ */
/* Sanitising                                                           */
/* ------------------------------------------------------------------ */

/** Attributes whose value the browser will fetch or navigate to. */
const URL_ATTRIBUTES = ["href", "src", "xlink:href"];

/**
 * Characters a browser strips from a URL attribute before acting on it — the
 * same set DOMPurify normalises with, so a prefix test sees the same string.
 */
const ATTRIBUTE_WHITESPACE = /[\u0000-\u0020\u00a0\u1680\u180e\u2000-\u2029\u205f\u3000]/g;

/**
 * Animation and scripting elements, forbidden by name.
 *
 * `<animate>`, `<set>` and the `animate*` family can retarget another element's
 * attribute — `attributeName="href"` pointed at a `javascript:` value is the
 * canonical SVG XSS, and it lands *after* any static URL check has run. Mermaid
 * emits none of them at any security level, so forbidding them costs nothing
 * and removes the possibility that a future mermaid release, a directive, or a
 * parser bug hands one to `innerHTML`.
 *
 * `animatemotion`, `animatetransform` and `animatecolor` are in DOMPurify's
 * *allowed* SVG profile; the others it already refuses. Naming all of them
 * keeps the rule readable rather than dependent on which side of that line each
 * one happens to sit today.
 */
const FORBIDDEN_DIAGRAM_TAGS = [
  "animate",
  "animatecolor",
  "animatemotion",
  "animatetransform",
  "discard",
  "handler",
  "script",
  "set",
  "iframe",
  "object",
  "embed",
  "form",
  "input",
  "button",
  "textarea",
  "select",
  "base",
  "link",
  "meta",
];

/**
 * The policy rendered diagrams are reduced to.
 *
 * Deliberately looser than the Markdown preview's, because the input is
 * different: this is machine-generated markup from a library configured in
 * strict mode, not prose an agent typed.
 *
 * - **SVG profiles instead of an element list** — mermaid emits a different
 *   element vocabulary per diagram type (flowchart, sequence, gantt, class,
 *   state, ER, pie, git…). An allowlist enumerated by hand would silently break
 *   whichever diagram type nobody tested.
 * - **`foreignobject` added back, and made an HTML integration point** —
 *   `flowchart.htmlLabels` is on, so labels are HTML inside `<foreignObject>`.
 *   Without both settings every flowchart loses its label text.
 * - **`<style>` and the `style` attribute survive.** Mermaid inlines the whole
 *   theme as a `<style>` block scoped to the diagram's own id, plus per-element
 *   `style` attributes; dropping either renders every diagram unthemed. This is
 *   the one concession, and it is a real one: CSS reaching the page is the
 *   residual trust placed in mermaid's strict mode. It buys back the classes
 *   that matter — script, event handlers, animation retargeting, `javascript:`.
 * - **{@link FORBIDDEN_DIAGRAM_TAGS}** on top of the profile, and no `on*`
 *   attribute is ever on a DOMPurify allowlist.
 * - **DOMPurify's stock `ALLOWED_URI_REGEXP`**, unlike the Markdown preview's.
 *   That pattern is applied to *every* attribute value DOMPurify has not been
 *   told is inert, and an SVG's vocabulary is `viewBox="0 0 100 40"`,
 *   `d="M0,0 L8,4"`, `markerWidth="8"` — geometry, not URLs. Narrowing the
 *   pattern here would strip a diagram down to bare tags; enumerating the SVG
 *   attributes that are *not* URLs would be a second allowlist to keep in step
 *   with mermaid. The stock pattern already refuses `javascript:` and
 *   `data:`, which is what the URL check is for; {@link stripDataUrls} closes
 *   the one hole it leaves.
 */
const DIAGRAM_POLICY: PurifyConfig = {
  USE_PROFILES: { svg: true, svgFilters: true, html: true },
  ADD_TAGS: ["foreignobject"],
  HTML_INTEGRATION_POINTS: { foreignobject: true },
  FORBID_TAGS: FORBIDDEN_DIAGRAM_TAGS,
  FORBID_ATTR: ["formaction", "ping", "srcset", "background"],
};

/**
 * Its own DOMPurify instance, so the preview's link-hardening hook — registered
 * on that module's instance — cannot reach diagram markup, or vice versa.
 * Created on first use: only a browser has a `window`.
 */
let purifier: Purifier | null = null;

function diagramPurifier(): Purifier {
  if (purifier === null) {
    purifier = DOMPurify(window);
    purifier.addHook("afterSanitizeAttributes", stripDataUrls);
  }
  return purifier;
}

/**
 * Remove `data:` URLs, which DOMPurify permits on media elements — `<image>`
 * among them — whatever `ALLOWED_URI_REGEXP` says. A diagram has no use for an
 * inline document, and `data:image/svg+xml` is a document.
 */
function stripDataUrls(element: Element): void {
  for (const name of URL_ATTRIBUTES) {
    const value = element.getAttribute(name);
    if (value === null) continue;
    const normalised = value.replace(ATTRIBUTE_WHITESPACE, "").toLowerCase();
    if (normalised.startsWith("data:")) element.removeAttribute(name);
  }
}

/**
 * Reduce one rendered diagram to {@link DIAGRAM_POLICY}.
 *
 * Exported so the policy can be tested against a payload directly, without
 * loading mermaid.
 *
 * @param svg The SVG markup mermaid produced.
 * @returns The same markup with scripting and animation constructs removed.
 */
export function sanitiseDiagramSvg(svg: string): string {
  return diagramPurifier().sanitize(svg, DIAGRAM_POLICY);
}

/** Store a result, evicting the oldest entry once the cache is full. */
function remember(source: string, result: DiagramResult): void {
  if (cache.size >= CACHE_LIMIT) {
    const oldest = cache.keys().next();
    if (!oldest.done) cache.delete(oldest.value);
  }
  cache.set(source, result);
}

/** Drop the temporary node mermaid leaves in the document after a failure. */
function removeStrayElement(id: string): void {
  document.getElementById(id)?.remove();
}

/**
 * Get a readable message out of whatever mermaid threw.
 *
 * Mermaid's parse errors are plain objects carrying `str` rather than `Error`
 * instances, so `String(error)` alone yields `[object Object]`.
 */
function describeDiagramError(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "object" && error !== null) {
    const candidate = error as { str?: unknown; message?: unknown };
    if (typeof candidate.str === "string" && candidate.str) return candidate.str;
    if (typeof candidate.message === "string" && candidate.message) return candidate.message;
  }
  const rendered = String(error);
  return rendered === "[object Object]" ? "Mermaid could not render this diagram." : rendered;
}
