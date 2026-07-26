/**
 * The bar above the editor: title, type, space, lifecycle metadata, save state.
 *
 * Everything here has a reserved size. The save indicator is the only element
 * that changes several times a minute, so it is given a fixed width and a
 * right edge to grow towards — a status that re-flows the title field every
 * time it ticks over from "saving" to "saved" is the classic way a two-pane
 * editor ends up feeling unsettled.
 *
 * A space appears here in **two different jobs**, which design decision D1
 * keeps apart everywhere else too and which this bar must not blur:
 *
 * - on a saved node, the space it *lives in* — a fact, not a control. A node's
 *   space is fixed at creation like its type, so offering a picker would be the
 *   frontend contract's "do not render a control for something the service
 *   cannot do";
 * - on a new document, the **write target** — the sticky, app-wide space the
 *   next create lands in. It is shown at the moment of writing because D1a says
 *   a persisted target the human cannot see is a way to file work into the
 *   wrong territory.
 */

import { Link } from "react-router-dom";
import {
  ANY_SPACE,
  NodeBadge,
  Spinner,
  nameSpace,
  resolveSpaceValue,
  spaceLabel,
  spaceNameNote,
  spaceOptions,
} from "../../components";
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
  /** Active spaces from `GET /api/spaces`; null while loading or after a failure. */
  spaces: readonly NodeOut[] | null;
  /**
   * Archived space nodes, for naming the space of a node written into one that
   * has since been retired. Empty until the lazy read is needed.
   */
  archivedSpaces: readonly NodeOut[];
  /** True once the space list request failed — the picker says so. */
  spacesFailed: boolean;
  /** The sticky write target: the space the first save will land in. */
  writeTarget: string;
  onWriteTargetChange(target: string): void;
  saveState: SaveState;
  saveError: string | null;
  savedAt: number | null;
  onSaveNow(): void;
  previewVisible: boolean;
  onTogglePreview(): void;
}

/** Title, type, space, metadata, and save status for the open document. */
export function NodeMetaBar({
  node,
  title,
  onTitleChange,
  nodeTypes,
  typesError,
  selectedType,
  onTypeChange,
  spaces,
  archivedSpaces,
  spacesFailed,
  writeTarget,
  onWriteTargetChange,
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
            <NodeSpace node={node} spaces={spaces} archivedSpaces={archivedSpaces} />
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
          <>
            <NewNodeType
              nodeTypes={nodeTypes}
              typesError={typesError}
              selectedType={selectedType}
              onTypeChange={onTypeChange}
            />
            <WriteTargetPicker
              spaces={spaces}
              spacesFailed={spacesFailed}
              writeTarget={writeTarget}
              onWriteTargetChange={onWriteTargetChange}
            />
            {typesError === null && nodeTypes.length > 0 ? (
              <span className="nd-meta">
                Not saved yet — the first keystroke creates the node in that space. Type and space
                are fixed after that.
              </span>
            ) : null}
          </>
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
    <label className="nd-editor__type-field">
      <span className="nd-label">Type</span>
      <select
        name="node-type"
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
  );
}

/**
 * The space a saved node lives in — a fact on the bar, not a control.
 *
 * `space_id` reaches the frontend on every node and used to be rendered
 * nowhere, which left the human unable to answer "where is this?" without the
 * CLI. It is deliberately read-only: a node's space is fixed at creation, like
 * its type, so a picker here would offer something `PATCH /api/nodes/{id}`
 * cannot do.
 *
 * It resolves through the shared `nameSpace` rather than `spaceLabel`, because
 * a node whose space has since been archived is exactly the case a human opens
 * the editor to understand — and `spaceLabel`'s picker fallback rendered it as
 * `in 4affabf6d856427886ad48570f5f6e20`.
 */
function NodeSpace({
  node,
  spaces,
  archivedSpaces,
}: {
  node: NodeOut;
  spaces: readonly NodeOut[] | null;
  archivedSpaces: readonly NodeOut[];
}) {
  if (node.space_id === null) {
    return (
      <span className="nd-meta" title="The server reported no space for this node.">
        space unknown
      </span>
    );
  }

  const name = nameSpace(node.space_id, spaces, archivedSpaces);
  const note = spaceNameNote(name);
  return (
    <span
      className="nd-meta nd-editor__space"
      title={
        note === null
          ? `This node lives in the ${name.label} space. A node's space is fixed at creation, like its type.`
          : `This node lives in ${name.label}. ${note}`
      }
    >
      in <span className="nd-mono">{name.label}</span>
      {name.kind === "archived" ? (
        <span className="nd-badge nd-badge--archived nd-editor__space-mark">
          <span className="nd-badge__dot" aria-hidden="true" />
          archived
        </span>
      ) : null}
    </span>
  );
}

/**
 * The sticky write target, on the surface that is about to use it (D1a).
 *
 * Not the shared `SpaceFilter`: that control is the *read* half of D1 and
 * offers an "any space" sentinel, which a write has no meaning for — a node
 * lands in exactly one space. The picker is built from the same shared
 * vocabulary so a target the list does not carry stays representable rather
 * than rendering blank and being silently rewritten by the next change event.
 */
function WriteTargetPicker({
  spaces,
  spacesFailed,
  writeTarget,
  onWriteTargetChange,
}: Pick<NodeMetaBarProps, "spaces" | "spacesFailed" | "writeTarget" | "onWriteTargetChange">) {
  const known = spaces ?? [];
  const listKnown = spaces !== null && !spacesFailed;
  const options = spaceOptions(known, writeTarget).filter((option) => option.value !== ANY_SPACE);
  const selected = resolveSpaceValue(known, writeTarget);
  const unlisted = options.find((option) => option.value === selected)?.unlisted === true;

  return (
    <>
      <label className="nd-editor__space-field">
        <span className="nd-label">Space</span>
        <select
          name="write-target"
          className="nd-select nd-editor__space-select"
          value={selected}
          disabled={spaces === null && !spacesFailed}
          title={
            spacesFailed
              ? "The space list could not be loaded — a new node will still be written to this target."
              : "Where the next new node lands. It sticks across sessions and is separate from any space filter you read with."
          }
          onChange={(event) => onWriteTargetChange(event.target.value)}
        >
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.unlisted && listKnown ? `${option.label} (unavailable)` : option.label}
            </option>
          ))}
        </select>
      </label>

      {unlisted && listKnown ? (
        <span className="nd-editor__meta-warning" role="alert">
          {spaceLabel(known, writeTarget)} is not in the space list — it may have been archived or
          renamed. Saving will be refused until another space is chosen.
        </span>
      ) : null}
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
