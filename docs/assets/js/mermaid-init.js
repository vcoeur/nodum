// Local mermaid bootstrap — wired in via mkdocs.yml `extra_javascript`.
//
// mkdocs-material 9.7.7 checks for a `mermaid` global when it first observes a
// <pre class="mermaid"> block: if the global is missing it lazy-loads mermaid
// from a third-party CDN; if the global is present it uses it and owns
// initialization + rendering itself (mermaid.initialize({ startOnLoad: false,
// ... }) followed by mermaid.render() per block, with theme-aware CSS).
//
// The vendored mermaid.min.js above defines that global before the page's
// DOMContentLoaded handlers run, so the CDN fallback never fires and every
// block is rendered from the local script. startOnLoad is deliberately left
// off here: with material rendering each block itself, an auto-run would
// render every diagram twice.
if (typeof window.mermaid !== "undefined") {
  mermaid.initialize({ startOnLoad: false });
}
