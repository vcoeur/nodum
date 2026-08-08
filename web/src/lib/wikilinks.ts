/**
 * The wikilink contract between the renderer and the reader.
 *
 * A rendered `[[Title]]` is an `<a class="nd-wikilink" href="/node/title/…">`.
 * This module owns that href shape — the tokenizer that writes it
 * (`views/editor/markdownRender.ts`) and the interceptor that reads it back
 * (the editor preview and the reading view) must agree on it, and a second
 * copy of one idea is how the two drift apart.
 *
 * The title travels **in the href**, never in a `data-*` attribute: the
 * preview's sanitiser strips data attributes by design
 * (`ALLOW_DATA_ATTR: false`), while a site-relative `/…` href passes its URL
 * policy untouched. Clicking a wikilink resolves the title to a node id
 * before navigating, so a click can never land on a guess — except the
 * id-form `[[<id>]]`, which names its node directly and navigates to it
 * without a resolution round trip (the resolve read is title-only and would
 * miss an id). The plain left click is the only one intercepted, so a
 * middle-click or a modified click falls through to the browser and lands on
 * the same target through the `/node/title/:title` route.
 */

import type { TitleResolution } from "../api/types";

/** The client route a wikilink title resolves through. */
export const WIKILINK_TITLE_PATH = "/node/title/";

/**
 * The characters a title cannot carry inside `[[…]]`.
 *
 * The preview tokenizer splits the first `|` off as its display-only label
 * half, and both the preview's and the server's (`service.WIKILINK_RE`)
 * grammars exclude brackets — a title holding any of the three would render
 * one target and materialise a `mentions` edge to another.
 */
const UNLINKABLE_TITLE = /[[|\]]/;

/**
 * Build the wikilink text to insert for a chosen target.
 *
 * A title containing `|`, `[`, or `]` cannot travel inside `[[…]]` verbatim
 * (see {@link UNLINKABLE_TITLE}); the node id is safe in both grammars, and
 * the server resolves an exact id before any title
 * (`service._resolve_wikilink`), so such a title links by id. The reader's
 * click path recognises the id form by shape and navigates directly
 * ({@link wikilinkTargetId}), and the mention edge points at the same node.
 *
 * @param title The chosen target's title.
 * @param nodeId The chosen target's node id.
 * @returns The `[[…]]` text to insert at the caret.
 */
export function wikilinkInsertion(title: string, nodeId: string): string {
  return UNLINKABLE_TITLE.test(title) ? `[[${nodeId}]]` : `[[${title}]]`;
}

/**
 * Build the href a rendered wikilink carries.
 *
 * @param title The wikilink target, as written in `[[…]]`.
 */
export function wikilinkHref(title: string): string {
  return `${WIKILINK_TITLE_PATH}${encodeURIComponent(title)}`;
}

/**
 * Read the title back out of a wikilink href.
 *
 * @param href The anchor's href, as the sanitiser left it.
 * @returns The target title, or null when the href is not a wikilink's (or
 *   its percent-encoding is malformed, which nothing this app writes can
 *   produce).
 */
export function titleFromWikilinkHref(href: string): string | null {
  if (!href.startsWith(WIKILINK_TITLE_PATH)) return null;
  try {
    const title = decodeURIComponent(href.slice(WIKILINK_TITLE_PATH.length));
    return title === "" ? null : title;
  } catch {
    return null;
  }
}

/**
 * The shape of a generated node id.
 *
 * Every generated id in the store is `uuid4().hex` — 32 lowercase hex digits
 * (`migrations.py`, `service.py`). The server's own wikilink resolution
 * checks an exact id before any title, so an id-shaped target is
 * unambiguous: it is the id form, not a title that happens to look like one.
 */
const NODE_ID = /^[0-9a-f]{32}$/;

/**
 * The node id an id-form wikilink target names, or null.
 *
 * `[[<id>]]` is the editor's fallback insertion for a title the wikilink
 * grammar cannot carry ({@link wikilinkInsertion}). The click path needs this
 * to navigate an id-form wikilink directly: the title-resolution read is
 * title-only and would answer "No active node titled …" for an id.
 *
 * @param target The wikilink target, as written in `[[…]]`.
 * @returns The target itself when it matches the id shape, else null.
 */
export function wikilinkTargetId(target: string): string | null {
  return NODE_ID.test(target) ? target : null;
}

/** What following a resolved wikilink should do. */
export type WikilinkAction =
  | { kind: "navigate"; nodeId: string }
  | { kind: "notice"; toastTitle: string; toastDetail?: string };

/**
 * Decide what a resolved title means for the reader.
 *
 * Only `resolved` navigates. The two failure outcomes become toasts that say
 * what is known — an ambiguous title is reported, never guessed, and a
 * missing one is named — so a wikilink click never travels somewhere
 * arbitrary. The copy lives here rather than in a view because both rendered
 * surfaces (the editor preview and the reading view) show it.
 *
 * @param resolution One title's server-side outcome.
 */
export function actionForResolution(resolution: TitleResolution): WikilinkAction {
  if (resolution.outcome === "resolved" && resolution.node_id !== null) {
    return { kind: "navigate", nodeId: resolution.node_id };
  }
  if (resolution.outcome === "ambiguous") {
    return {
      kind: "notice",
      toastTitle: `"${resolution.title}" is ambiguous`,
      toastDetail: "Several nodes share that title — pick one from search.",
    };
  }
  return { kind: "notice", toastTitle: `No active node titled "${resolution.title}"` };
}

/**
 * Intercept plain clicks on `a.nd-wikilink` inside a rendered-Markdown container.
 *
 * @param container The element the rendered HTML lives in.
 * @param onWikilink Called with the target title when a wikilink is clicked,
 *   and the node id when the target is the id-form (`[[<id>]]`) — the
 *   consumer navigates directly then, skipping the title-resolution round
 *   trip a title target implies.
 * @returns A function that removes the listener.
 */
export function attachWikilinkClicks(
  container: HTMLElement,
  onWikilink: (title: string, nodeId: string | null) => void,
): () => void {
  const onClick = (event: MouseEvent) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    const target = event.target;
    if (!(target instanceof Element)) return;
    const anchor = target.closest("a.nd-wikilink");
    if (anchor === null) return;
    const title = titleFromWikilinkHref(anchor.getAttribute("href") ?? "");
    if (title === null) return;
    event.preventDefault();
    onWikilink(title, wikilinkTargetId(title));
  };
  container.addEventListener("click", onClick);
  return () => container.removeEventListener("click", onClick);
}
