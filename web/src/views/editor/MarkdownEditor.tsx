/**
 * The CodeMirror-6 Markdown source surface.
 *
 * The text in this component *is* `NodeOut.content`. There is no document
 * model, no serializer, and no transform on load or save — design §2.3.2 makes
 * Markdown the canonical content, and the whole reason this slice runs on
 * CodeMirror rather than a rich-text editor is that a source editor has nothing
 * in between to lose fidelity in.
 *
 * ## Why the editor is uncontrolled
 *
 * The CodeMirror view is created once, in a mount effect, and never
 * reconfigured. Every dynamic input — the save callback, the type catalog the
 * slash palette offers, the drop handler — is read through a ref at the moment
 * it is needed, so a prop changing mid-session never rebuilds an extension or
 * re-measures the document. Switching to a different node changes this
 * component's React `key` instead, which is a clean remount rather than a
 * surgical document swap.
 */

import { useEffect, useImperativeHandle, useRef } from "react";
import type { Ref } from "react";
import { EditorState } from "@codemirror/state";
import {
  EditorView,
  drawSelection,
  dropCursor,
  highlightActiveLine,
  highlightSpecialChars,
  keymap,
  placeholder,
} from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { autocompletion } from "@codemirror/autocomplete";
import { bracketMatching, indentOnInput, syntaxHighlighting } from "@codemirror/language";
import { markdown, markdownLanguage } from "@codemirror/lang-markdown";
import { oneDarkHighlightStyle } from "@codemirror/theme-one-dark";
import { nodumEditorTheme } from "./cm/theme";
import { slashCommands } from "./cm/slashCommands";
import type { SlashPaletteState } from "./cm/slashCommands";
import { wikilinkCompletion } from "./cm/wikilinkComplete";
import { assetDrop } from "./cm/assetDrop";

/** What the view can ask the editor to do from the outside. */
export interface MarkdownEditorHandle {
  /** Put the caret back in the document. */
  focus(): void;
  /** The current buffer. */
  getContent(): string;
  /**
   * Insert a block of Markdown on its own line near `position`.
   *
   * @param position Document offset to insert at; clamped to the document.
   * @param text The Markdown to insert, without surrounding blank lines.
   */
  insertBlockAt(position: number, text: string): void;
}

interface MarkdownEditorProps {
  /** The document to open with. Read once, at mount. */
  initialDoc: string;
  /** The buffer changed. Called on every edit, so it must stay cheap. */
  onChange(content: string): void;
  /** The user asked for an immediate save (Mod-s). */
  onSave(): void;
  /** Current state for the slash palette, read when the palette opens. */
  slashState(): SlashPaletteState;
  /** The `[[` suggestion query failed, or (with null) recovered. */
  onLinkSuggestFailure(message: string | null): void;
  /** Files were dropped on the editor. */
  onFiles(files: File[], position: number): void;
  /** A file drag entered or left the editor. */
  onDragActive(active: boolean): void;
  ref?: Ref<MarkdownEditorHandle>;
}

/** The Markdown source editor. */
export function MarkdownEditor(props: MarkdownEditorProps) {
  const host = useRef<HTMLDivElement | null>(null);
  const view = useRef<EditorView | null>(null);

  // Every extension below reaches the current props through this ref, which is
  // what lets the view be built exactly once.
  const latest = useRef(props);
  latest.current = props;

  useEffect(() => {
    const parent = host.current;
    if (!parent) return;

    const state = EditorState.create({
      doc: latest.current.initialDoc,
      extensions: [
        history(),
        drawSelection(),
        dropCursor(),
        highlightSpecialChars(),
        highlightActiveLine(),
        indentOnInput(),
        bracketMatching(),
        EditorView.lineWrapping,

        markdown({ base: markdownLanguage, completeHTMLTags: false }),
        syntaxHighlighting(oneDarkHighlightStyle),
        nodumEditorTheme,

        autocompletion({
          override: [
            slashCommands(() => latest.current.slashState()),
            wikilinkCompletion((message) => latest.current.onLinkSuggestFailure(message)),
          ],
          closeOnBlur: true,
        }),

        placeholder("Markdown. Type / for commands, [[ to link a node."),

        keymap.of([
          {
            key: "Mod-s",
            preventDefault: true,
            run: () => {
              latest.current.onSave();
              return true;
            },
          },
          ...defaultKeymap,
          ...historyKeymap,
          indentWithTab,
        ]),

        assetDrop({
          onFiles: (files, position) => latest.current.onFiles(files, position),
          onDragActive: (active) => latest.current.onDragActive(active),
        }),

        EditorView.updateListener.of((update) => {
          if (update.docChanged) latest.current.onChange(update.state.doc.toString());
        }),

        EditorView.contentAttributes.of({
          "aria-label": "Markdown content",
          spellcheck: "true",
        }),
      ],
    });

    const created = new EditorView({ state, parent });
    view.current = created;
    created.focus();

    return () => {
      created.destroy();
      view.current = null;
    };
  }, []);

  useImperativeHandle(
    props.ref,
    (): MarkdownEditorHandle => ({
      focus: () => view.current?.focus(),
      getContent: () => view.current?.state.doc.toString() ?? "",
      insertBlockAt: (position, text) => {
        const current = view.current;
        if (!current) return;
        const clamped = Math.min(Math.max(position, 0), current.state.doc.length);
        const line = current.state.doc.lineAt(clamped);
        // Land on a line of its own: an image reference wedged into the middle
        // of a sentence renders as an inline image and reads as a mistake.
        const blank = line.text.trim().length === 0;
        const from = blank ? line.from : line.to;
        const insert = blank ? `${text}\n` : `\n${text}\n`;
        current.dispatch({
          changes: { from, insert },
          selection: { anchor: from + insert.length },
          scrollIntoView: true,
        });
        current.focus();
      },
    }),
    [],
  );

  return <div className="nd-editor__surface" ref={host} />;
}
