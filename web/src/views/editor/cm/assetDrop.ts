/**
 * Drag-and-drop of files onto the editor.
 *
 * The extension's whole job is to notice files and report *where* they landed;
 * uploading and deciding what Markdown to write is the view's business. Drops
 * that carry no files fall through untouched, so CodeMirror's own drag-to-move
 * of selected text keeps working.
 */

import { EditorView } from "@codemirror/view";

/** Callbacks the view supplies. */
export interface AssetDropHandlers {
  /**
   * Files were dropped on the editor.
   *
   * @param files The dropped files, in the order the browser reported them.
   * @param position Document offset under the pointer at the moment of the drop.
   */
  onFiles(files: File[], position: number): void;
  /** The pointer entered or left a drag carrying files. */
  onDragActive(active: boolean): void;
}

/**
 * Build the drag-and-drop extension.
 *
 * @param handlers Where to send dropped files and drag-state changes.
 */
export function assetDrop(handlers: AssetDropHandlers) {
  // dragenter/dragleave fire for every child element the pointer crosses, so
  // the depth counter is what keeps the highlight from flickering.
  let depth = 0;

  const reset = () => {
    depth = 0;
    handlers.onDragActive(false);
  };

  return EditorView.domEventHandlers({
    dragenter(event) {
      if (!carriesFiles(event)) return false;
      depth += 1;
      handlers.onDragActive(true);
      return false;
    },

    dragover(event) {
      if (!carriesFiles(event)) return false;
      // Without this the browser navigates to the dropped file.
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      return false;
    },

    dragleave(event) {
      if (!carriesFiles(event)) return false;
      depth = Math.max(0, depth - 1);
      if (depth === 0) handlers.onDragActive(false);
      return false;
    },

    drop(event, view) {
      const files = [...(event.dataTransfer?.files ?? [])];
      if (files.length === 0) {
        reset();
        return false;
      }
      event.preventDefault();
      reset();
      const position =
        view.posAtCoords({ x: event.clientX, y: event.clientY }) ?? view.state.selection.main.head;
      handlers.onFiles(files, position);
      return true;
    },
  });
}

/** Whether a drag is carrying files rather than text or an in-editor selection. */
function carriesFiles(event: DragEvent): boolean {
  const types = event.dataTransfer?.types;
  return types ? [...types].includes("Files") : false;
}
