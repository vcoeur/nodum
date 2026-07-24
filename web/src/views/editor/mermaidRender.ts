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
 */

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
    result = { ok: true, svg };
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
