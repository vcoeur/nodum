/**
 * The nodum look for CodeMirror.
 *
 * Everything here reads `styles/tokens.css` through custom properties rather
 * than repeating hex values, so the editor tracks the design system instead of
 * drifting from it. Only the *chrome* is themed — token colours come from
 * `oneDarkHighlightStyle`, which is a declared dependency and is tuned for a
 * dark background; writing our own would mean importing `@lezer/highlight`,
 * which this package does not depend on directly.
 */

import { EditorView } from "@codemirror/view";

/** Chrome, gutters, selection, and the completion palette. */
export const nodumEditorTheme = EditorView.theme(
  {
    "&": {
      height: "100%",
      color: "var(--nd-text)",
      backgroundColor: "var(--nd-bg)",
      fontSize: "var(--nd-text-md)",
    },

    ".cm-scroller": {
      fontFamily: "var(--nd-font-mono)",
      lineHeight: "var(--nd-leading-prose)",
      overflow: "auto",
    },

    ".cm-content": {
      padding: "var(--nd-space-12) var(--nd-space-16) 40vh",
      caretColor: "var(--nd-accent-bright)",
    },

    "&.cm-focused": { outline: "none" },

    ".cm-cursor, .cm-dropCursor": {
      borderLeft: "2px solid var(--nd-accent-bright)",
    },

    // CodeMirror draws its own selection layer; the native `::selection` rule
    // in base.css never applies inside the editor.
    "&.cm-focused .cm-selectionBackground, .cm-selectionBackground": {
      background: "var(--nd-accent-wash)",
    },
    ".cm-selectionMatch": { background: "var(--nd-state-active-wash)" },

    ".cm-activeLine": { backgroundColor: "rgb(255 255 255 / 3%)" },

    ".cm-matchingBracket, &.cm-focused .cm-matchingBracket": {
      backgroundColor: "var(--nd-accent-wash)",
      color: "var(--nd-accent-bright)",
      outline: "none",
    },
    ".cm-nonmatchingBracket, &.cm-focused .cm-nonmatchingBracket": {
      backgroundColor: "var(--nd-danger-wash)",
      color: "var(--nd-danger)",
    },

    ".cm-placeholder": { color: "var(--nd-text-faint)" },

    ".cm-specialChar": { color: "var(--nd-warn)" },

    /* --- Completion palette (slash commands and `[[` links) ---------- */

    ".cm-tooltip": {
      border: "1px solid var(--nd-border-strong)",
      borderRadius: "var(--nd-radius-md)",
      backgroundColor: "var(--nd-surface-overlay)",
      color: "var(--nd-text)",
      boxShadow: "var(--nd-shadow-overlay)",
    },

    ".cm-tooltip.cm-tooltip-autocomplete > ul": {
      fontFamily: "var(--nd-font-ui)",
      fontSize: "var(--nd-text-sm)",
      maxHeight: "16rem",
    },

    ".cm-tooltip.cm-tooltip-autocomplete > ul > li": {
      display: "flex",
      alignItems: "baseline",
      gap: "var(--nd-space-4)",
      padding: "var(--nd-space-3) var(--nd-space-6)",
      borderRadius: "var(--nd-radius-sm)",
    },

    ".cm-tooltip.cm-tooltip-autocomplete > ul > li[aria-selected]": {
      backgroundColor: "var(--nd-accent-wash)",
      color: "var(--nd-accent-bright)",
    },

    ".cm-completionLabel": { fontFamily: "var(--nd-font-mono)" },

    ".cm-completionMatchedText": {
      textDecoration: "none",
      color: "var(--nd-accent-bright)",
      fontWeight: "var(--nd-weight-semibold)",
    },

    ".cm-completionDetail": {
      marginLeft: "auto",
      paddingLeft: "var(--nd-space-8)",
      color: "var(--nd-text-faint)",
      fontSize: "var(--nd-text-2xs)",
      fontStyle: "normal",
      letterSpacing: "var(--nd-tracking-label)",
      textTransform: "uppercase",
    },

    ".cm-completionIcon": {
      width: "1.1em",
      color: "var(--nd-text-faint)",
      opacity: "1",
    },

    // The state ramp, on the one completion that carries a lifecycle: a `[[`
    // target that nobody has reviewed yet.
    ".cm-completionIcon-proposed": { color: "var(--nd-state-proposed)" },

    ".cm-tooltip.cm-completionInfo": {
      padding: "var(--nd-space-6) var(--nd-space-8)",
      maxWidth: "22rem",
      fontFamily: "var(--nd-font-ui)",
      fontSize: "var(--nd-text-xs)",
      lineHeight: "var(--nd-leading-normal)",
      color: "var(--nd-text-muted)",
      whiteSpace: "pre-wrap",
    },
  },
  { dark: true },
);
