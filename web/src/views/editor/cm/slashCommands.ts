/**
 * The slash command palette.
 *
 * Typing `/` at the start of a line opens a filterable list of the node types
 * the server reports plus a few Markdown scaffolds. It is built as a CodeMirror
 * completion source rather than a React overlay on purpose: the completion
 * tooltip already has keyboard navigation, prefix filtering, an info panel, and
 * Escape-to-dismiss, it is positioned at the caret without measuring anything,
 * and — the reason that matters most here — it renders *outside* the document
 * flow, so opening it cannot shift a single line of text.
 *
 * ## Why choosing a type inserts nothing
 *
 * A node type's schema constrains `props`, which is a separate column from
 * `content`. Writing its required fields into the Markdown as a scaffold would
 * put structured data into the canonical content where nothing will ever read
 * it back — exactly the "the editor's idea of the document is not the truth"
 * failure design §2.3.2 forbids. So a type command sets the type and removes
 * the `/…` text, and the schema's requirements are shown in the completion's
 * info panel instead of being pasted into the buffer.
 */

import type {
  Completion,
  CompletionContext,
  CompletionResult,
  CompletionSource,
} from "@codemirror/autocomplete";
import type { EditorView } from "@codemirror/view";
import type { TypeOut } from "../../../api/types";

/** What the palette needs to know at the moment it opens. */
export interface SlashPaletteState {
  /** The live node-type catalog. Empty until `getTypes` answers. */
  nodeTypes: readonly TypeOut[];
  /**
   * True once the node exists in the database.
   *
   * `PATCH /api/nodes/{id}` carries title, content, and props — there is no
   * field for type, and `service.update_node` has no parameter for one. Rather
   * than offer a command that silently does nothing, the palette drops the type
   * options entirely once the node is saved.
   */
  typeLocked: boolean;
  /** Set the type the first save will create the node with. */
  selectType(typeId: string): void;
}

/** A `/` command at the very start of a line, with the letters typed so far. */
const SLASH_COMMAND = /^\/[\w-]*$/;

/** A Markdown block a `/` command can insert. */
interface Snippet {
  /** Command name, without the slash. */
  name: string;
  /** Right-hand label in the palette. */
  detail: string;
  /** What the info panel explains. */
  info: string;
  /** The text inserted in place of the `/…` command. */
  body: string;
  /** Where the caret lands, counted from the start of `body`. */
  caret: number;
}

const MERMAID_BODY = "```mermaid\nflowchart TD\n  A[Start] --> B[End]\n```\n";
const CODE_BODY = "```\n\n```\n";
const TABLE_BODY = "| Column | Column |\n| --- | --- |\n|  |  |\n";

/**
 * Markdown scaffolds.
 *
 * Kept to the three blocks that are genuinely awkward to type by hand — and
 * `mermaid` first, because a diagram fence is the one block whose payoff (the
 * live preview beside it) is specific to this editor.
 */
const SNIPPETS: readonly Snippet[] = [
  {
    name: "mermaid",
    detail: "diagram",
    info: "Insert a mermaid fence. The preview pane renders it as you type.",
    body: MERMAID_BODY,
    caret: "```mermaid\n".length,
  },
  {
    name: "code",
    detail: "code block",
    info: "Insert a fenced code block.",
    body: CODE_BODY,
    caret: "```\n".length,
  },
  {
    name: "table",
    detail: "table",
    info: "Insert a two-column GitHub-flavoured Markdown table.",
    body: TABLE_BODY,
    caret: TABLE_BODY.length,
  },
];

/**
 * Build the slash-command completion source.
 *
 * @param read Reads the current palette state. A function rather than a value
 *   so the source can be installed once, at mount, and still see a type catalog
 *   that arrives later — reconfiguring the editor mid-session would cost a
 *   re-measure and a visible reflow.
 */
export function slashCommands(read: () => SlashPaletteState): CompletionSource {
  return (context: CompletionContext): CompletionResult | null => {
    const line = context.state.doc.lineAt(context.pos);
    const typed = context.state.sliceDoc(line.from, context.pos);
    if (!SLASH_COMMAND.test(typed)) return null;

    const state = read();
    const options: Completion[] = [];

    if (!state.typeLocked) {
      for (const nodeType of state.nodeTypes) {
        options.push({
          label: `/${nodeType.name}`,
          detail: "node type",
          type: "class",
          boost: 1,
          info: describeType(nodeType),
          apply: applyType(nodeType.id, state.selectType),
        });
      }
    }

    for (const snippet of SNIPPETS) {
      options.push({
        label: `/${snippet.name}`,
        detail: snippet.detail,
        type: "keyword",
        info: snippet.info,
        apply: applySnippet(snippet),
      });
    }

    if (options.length === 0) return null;
    return { from: line.from, options, validFor: SLASH_COMMAND };
  };
}

/** Set the node's type and remove the command text. */
function applyType(typeId: string, selectType: (typeId: string) => void) {
  return (view: EditorView, _completion: Completion, from: number, to: number): void => {
    selectType(typeId);
    view.dispatch({ changes: { from, to, insert: "" }, userEvent: "input.complete" });
  };
}

/** Replace the command text with the snippet and place the caret inside it. */
function applySnippet(snippet: Snippet) {
  return (view: EditorView, _completion: Completion, from: number, to: number): void => {
    view.dispatch({
      changes: { from, to, insert: snippet.body },
      selection: { anchor: from + snippet.caret },
      userEvent: "input.complete",
      scrollIntoView: true,
    });
  };
}

/**
 * One paragraph on what a type is for, for the completion's info panel.
 *
 * Prefers the schema's own description, then names the properties the schema
 * requires so the reader knows what the type expects before committing to it.
 */
function describeType(nodeType: TypeOut): string {
  const lines: string[] = [];
  const description = nodeType.json_schema["description"];
  if (typeof description === "string" && description.trim()) lines.push(description.trim());

  const required = nodeType.json_schema["required"];
  if (Array.isArray(required) && required.length > 0) {
    const names = required.filter((name): name is string => typeof name === "string");
    if (names.length > 0) lines.push(`Required props: ${names.join(", ")}`);
  }

  if (!nodeType.is_builtin) lines.push("User-defined type.");
  lines.push("Type is fixed once the node is created.");
  return lines.join("\n\n");
}
