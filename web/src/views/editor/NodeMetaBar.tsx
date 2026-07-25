/**
 * The bar above the editor: title, type, lifecycle metadata, and save state.
 *
 * Everything here has a reserved size. The save indicator is the only element
 * that changes several times a minute, so it is given a fixed width and a
 * right edge to grow towards — a status that re-flows the title field every
 * time it ticks over from "saving" to "saved" is the classic way a two-pane
 * editor ends up feeling unsettled.
 */

import { Link } from "react-router-dom";
import { NodeBadge, Spinner } from "../../components";
import { formatAbsolute } from "../../lib";
import type { NodeOut, TypeOut } from "../../api/types";
import type { SaveState } from "./useNodeDocument";

interface NodeMetaBarProps {
  /** The stored node, or null while the document has never been saved. */
  node: NodeOut | null;
  title: string;
  onTitleChange(title: string): void;
  /** The live node-type catalog, for a new node's type picker. */
  nodeTypes: readonly TypeOut[];
  /** Why the type catalog is unavailable, if it is. */
  typesError: string | null;
  /** Type the first save will use. Null until the catalog answers. */
  selectedType: string | null;
  onTypeChange(typeId: string): void;
  saveState: SaveState;
  saveError: string | null;
  savedAt: number | null;
  onSaveNow(): void;
  previewVisible: boolean;
  onTogglePreview(): void;
}

/** Title, type, metadata, and save status for the open document. */
export function NodeMetaBar({
  node,
  title,
  onTitleChange,
  nodeTypes,
  typesError,
  selectedType,
  onTypeChange,
  saveState,
  saveError,
  savedAt,
  onSaveNow,
  previewVisible,
  onTogglePreview,
}: NodeMetaBarProps) {
  return (
    <header className="nd-editor__meta">
      <div className="nd-editor__meta-line">
        <label className="nd-sr-only" htmlFor="nd-editor-title">
          Node title
        </label>
        <input
          id="nd-editor-title"
          className="nd-editor__title"
          type="text"
          value={title}
          placeholder="Untitled"
          autoComplete="off"
          spellCheck
          onChange={(event) => onTitleChange(event.target.value)}
        />

        <SaveIndicator state={saveState} savedAt={savedAt} onSaveNow={onSaveNow} />

        <button
          type="button"
          className="nd-button nd-button--small"
          onClick={onTogglePreview}
          aria-pressed={previewVisible}
          title="Show or hide the preview pane (Mod-\)"
        >
          {previewVisible ? "Hide preview" : "Show preview"}
        </button>

        <Link className="nd-button nd-button--small nd-button--ghost" to="/editor">
          New
        </Link>
      </div>

      <div className="nd-editor__meta-line nd-editor__meta-line--secondary">
        {node ? (
          <>
            <NodeBadge type={node.type} state={node.state} />
            <span className="nd-mono" title="Node id">
              {node.id}
            </span>
            <span className="nd-meta">
              created by <span className="nd-mono">{node.created_by}</span>
            </span>
            <Timestamp label="created" value={node.created_at} />
            <Timestamp label="updated" value={node.updated_at} />
            <Link className="nd-editor__history-link" to={`/history/${node.id}`}>
              Version history
            </Link>
          </>
        ) : (
          <NewNodeType
            nodeTypes={nodeTypes}
            typesError={typesError}
            selectedType={selectedType}
            onTypeChange={onTypeChange}
          />
        )}
      </div>

      {saveError ? (
        <p className="nd-editor__save-error" role="alert">
          <strong>Not saved.</strong> {saveError}{" "}
          <button type="button" className="nd-button nd-button--small" onClick={onSaveNow}>
            Retry now
          </button>
        </p>
      ) : null}
    </header>
  );
}

/** The type picker, shown only while the node can still be created with one. */
function NewNodeType({
  nodeTypes,
  typesError,
  selectedType,
  onTypeChange,
}: Pick<NodeMetaBarProps, "nodeTypes" | "typesError" | "selectedType" | "onTypeChange">) {
  if (typesError) {
    return (
      <span className="nd-editor__meta-warning" role="alert">
        Type catalog unavailable — {typesError} A new node cannot be created until it loads.
      </span>
    );
  }

  if (nodeTypes.length === 0) {
    return <span className="nd-meta">Loading node types…</span>;
  }

  return (
    <>
      <label className="nd-editor__type-field">
        <span className="nd-label">Type</span>
        <select
          className="nd-select nd-editor__type-select"
          value={selectedType ?? ""}
          onChange={(event) => onTypeChange(event.target.value)}
        >
          {nodeTypes.map((nodeType) => (
            <option key={nodeType.id} value={nodeType.id}>
              {nodeType.name}
            </option>
          ))}
        </select>
      </label>
      <span className="nd-meta">
        Not saved yet — the first keystroke creates the node. Type is fixed after that.
      </span>
    </>
  );
}

/** One labelled timestamp, with the raw stored value on hover. */
function Timestamp({ label, value }: { label: string; value: string }) {
  return (
    <span className="nd-meta" title={value}>
      {label} {formatAbsolute(value)}
    </span>
  );
}

/**
 * Where the buffer stands relative to the database, in a fixed-width slot.
 *
 * `failed` is a button: a save that did not land is the one status the user
 * needs to be able to act on without hunting for a control.
 */
function SaveIndicator({
  state,
  savedAt,
  onSaveNow,
}: {
  state: SaveState;
  savedAt: number | null;
  onSaveNow(): void;
}) {
  if (state === "failed") {
    return (
      <button
        type="button"
        className="nd-editor__save nd-editor__save--failed"
        onClick={onSaveNow}
        title="The last save failed. Click to try again."
      >
        Save failed — retry
      </button>
    );
  }

  if (state === "saving") {
    return (
      <span className="nd-editor__save" role="status">
        <Spinner label="Saving" />
        Saving…
      </span>
    );
  }

  if (state === "dirty") {
    return (
      <span className="nd-editor__save nd-editor__save--dirty" role="status" title="Mod-S saves now">
        Unsaved changes
      </span>
    );
  }

  if (state === "saved" && savedAt !== null) {
    return (
      <span className="nd-editor__save nd-editor__save--saved" role="status">
        Saved {new Date(savedAt).toLocaleTimeString()}
      </span>
    );
  }

  return <span className="nd-editor__save nd-editor__save--idle">No changes</span>;
}
