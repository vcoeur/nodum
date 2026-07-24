/**
 * The live preview beside the source.
 *
 * Read-only by construction: nothing in this pane can edit the buffer, so the
 * preview can never become a second, competing copy of the document.
 *
 * The HTML is written imperatively rather than through `dangerouslySetInnerHTML`
 * because the diagrams land asynchronously — React would either have to own DOM
 * it did not create or re-render the whole tree once per diagram. Since React
 * renders no children into the target element, mutating it directly is safe.
 */

import { useEffect, useRef } from "react";
import { DIAGRAM_PLACEHOLDER_CLASS, renderMarkdown } from "./markdownRender";
import { peekDiagram, renderDiagram } from "./mermaidRender";
import type { DiagramResult } from "./mermaidRender";

interface MarkdownPreviewProps {
  /** Markdown to render. The caller debounces this. */
  source: string;
}

/** The rendered-Markdown pane, with mermaid fences drawn as diagrams. */
export function MarkdownPreview({ source }: MarkdownPreviewProps) {
  const scroller = useRef<HTMLDivElement | null>(null);
  const target = useRef<HTMLDivElement | null>(null);
  /** Bumped per render pass, so a slow diagram cannot land in a newer document. */
  const generation = useRef(0);

  useEffect(() => {
    const container = target.current;
    const scrollHost = scroller.current;
    if (!container || !scrollHost) return;

    const pass = ++generation.current;
    const { html, diagrams } = renderMarkdown(source);

    const scrollTop = scrollHost.scrollTop;
    container.innerHTML = html;

    for (const placeholder of container.querySelectorAll<HTMLElement>(
      `.${DIAGRAM_PLACEHOLDER_CLASS}`,
    )) {
      const index = Number(placeholder.dataset["diagram"]);
      const diagram = diagrams[index];
      if (diagram === undefined) continue;

      const known = peekDiagram(diagram);
      if (known) {
        fill(placeholder, known);
        continue;
      }

      markPending(placeholder);
      void renderDiagram(diagram).then((result) => {
        if (generation.current !== pass || !placeholder.isConnected) return;
        fill(placeholder, result);
      });
    }

    // Restored last: re-inserting cached diagrams above has already restored
    // the document's height, so the offset still means what it did.
    scrollHost.scrollTop = scrollTop;
  }, [source]);

  useEffect(() => {
    return () => {
      generation.current += 1;
    };
  }, []);

  return (
    <div className="nd-editor__preview" ref={scroller}>
      {source.trim().length === 0 ? (
        <p className="nd-editor__preview-idle">Nothing to preview yet.</p>
      ) : null}
      <div className="nd-preview" ref={target} />
    </div>
  );
}

/** Show that a diagram is being drawn, reserving roughly its eventual space. */
function markPending(placeholder: HTMLElement): void {
  placeholder.classList.add("nd-preview__diagram--pending");
  placeholder.textContent = "Rendering diagram…";
}

/** Put a finished diagram — or an honest failure — into its placeholder. */
function fill(placeholder: HTMLElement, result: DiagramResult): void {
  placeholder.classList.remove("nd-preview__diagram--pending");

  if (result.ok) {
    placeholder.classList.remove("nd-preview__diagram--failed");
    placeholder.innerHTML = result.svg;
    return;
  }

  // Built as nodes, not markup: mermaid's messages quote the offending source,
  // which regularly contains angle brackets.
  placeholder.classList.add("nd-preview__diagram--failed");
  placeholder.replaceChildren();
  placeholder.setAttribute("role", "note");

  const heading = document.createElement("p");
  heading.className = "nd-preview__diagram-title";
  heading.textContent = "Diagram failed to render";

  const detail = document.createElement("pre");
  detail.className = "nd-preview__diagram-message";
  detail.textContent = result.message;

  placeholder.append(heading, detail);
}
