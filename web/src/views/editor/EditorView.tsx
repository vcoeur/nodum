/**
 * The Markdown editor — routes `/editor` (new node) and `/editor/:nodeId`.
 *
 * Two panes: a CodeMirror-6 Markdown *source* editor on the left and its
 * rendering on the right. The left pane holds `NodeOut.content` literally —
 * design §2.3.2 makes Markdown the canonical content, so there is no document
 * model between the keystroke and the database, and the right pane is a
 * one-way rendering that can never write back.
 *
 * What this component owns is the orchestration: the type catalog, the
 * document's load/save lifecycle (in {@link useNodeDocument}), the debounce
 * that keeps the preview off the typing path, asset uploads, and the states a
 * daily driver actually meets — loading, a blank new node, an id that does not
 * resolve, and a server that is not answering.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import type { KeyboardEvent } from "react";
import { api } from "../../api/client";
import type { TypeOut } from "../../api/types";
import {
  EmptyState,
  Spinner,
  unresolvedSpaceIds,
  useArchivedSpaces,
  useSpaces,
  useToast,
} from "../../components";
import { MarkdownEditor } from "./MarkdownEditor";
import type { MarkdownEditorHandle } from "./MarkdownEditor";
import { MarkdownPreview } from "./MarkdownPreview";
import { NodeMetaBar } from "./NodeMetaBar";
import { useNodeDocument } from "./useNodeDocument";
import type { SlashPaletteState } from "./cm/slashCommands";
import { describeError, useWriteTarget } from "../../lib";
import "./editor.css";

/** Quiet time before the preview re-renders. Long enough to stay off the typing path. */
const PREVIEW_DEBOUNCE_MS = 250;

/**
 * Node type a new document starts on when the catalog offers it.
 *
 * A preference among fetched types, not a hardcoded list: if no type has this
 * name the first type the server reports is used instead.
 */
const PREFERRED_TYPE_NAME = "note";

/** An upload in flight, or one that failed and is waiting to be acknowledged. */
interface Upload {
  id: number;
  name: string;
  status: "uploading" | "failed";
  message?: string;
}

/** The Markdown editor view. */
export default function EditorView() {
  const { nodeId } = useParams<{ nodeId: string }>();
  const toast = useToast();

  const { types, typesError } = useNodeTypes();
  const [selectedType, setSelectedType] = useState<string | null>(null);

  // The write target is app-wide state with one owner; this is the subscription
  // that makes it *visible* here, which is the whole of D1a. The same value is
  // handed to the document hook, so what the bar shows and what a create writes
  // are one variable rather than two reads of a store.
  const { spaces, failed: spacesFailed } = useSpaces();
  const [writeTarget, setWriteTarget] = useWriteTarget();

  const doc = useNodeDocument({
    nodeId,
    createType: selectedType,
    writeTarget,
    spaces: spaces ?? [],
  });

  // A node whose space has since been archived is a node the meta bar could
  // otherwise only describe as `in 4affabf6…`. One listing names it, fired only
  // when the open node actually reports a space `GET /api/spaces` does not
  // carry — which for an ordinary note is never.
  const unresolvedNodeSpace = useMemo(
    () => unresolvedSpaceIds([doc.node?.space_id ?? ""], spaces),
    [doc.node?.space_id, spaces],
  );
  const archivedSpaces = useArchivedSpaces(unresolvedNodeSpace.length > 0);

  const editorRef = useRef<MarkdownEditorHandle>(null);
  const [previewVisible, setPreviewVisible] = useState(true);
  const [previewSource, setPreviewSource] = useState("");
  const previewTimer = useRef<number | null>(null);

  const [uploads, setUploads] = useState<Upload[]>([]);
  const uploadSequence = useRef(0);
  const [dragActive, setDragActive] = useState(false);
  const [linkFailure, setLinkFailure] = useState<string | null>(null);

  /* --- Type catalog ------------------------------------------------ */

  useEffect(() => {
    if (selectedType !== null || types.length === 0) return;
    setSelectedType(preferredType(types));
  }, [types, selectedType]);

  /* --- Preview, off the typing path -------------------------------- */

  const schedulePreview = useCallback((text: string) => {
    if (previewTimer.current !== null) window.clearTimeout(previewTimer.current);
    previewTimer.current = window.setTimeout(() => {
      previewTimer.current = null;
      setPreviewSource(text);
    }, PREVIEW_DEBOUNCE_MS);
  }, []);

  // A freshly opened document renders at once: waiting out the debounce would
  // show an empty preview beside a full editor for a quarter of a second.
  useEffect(() => {
    if (previewTimer.current !== null) {
      window.clearTimeout(previewTimer.current);
      previewTimer.current = null;
    }
    setPreviewSource(doc.initialContent);
  }, [doc.docKey, doc.initialContent]);

  useEffect(
    () => () => {
      if (previewTimer.current !== null) window.clearTimeout(previewTimer.current);
    },
    [],
  );

  const handleContentChange = useCallback(
    (content: string) => {
      doc.handleContentChange(content);
      schedulePreview(content);
    },
    [doc, schedulePreview],
  );

  /* --- Slash palette ----------------------------------------------- */

  const slashState = useCallback(
    (): SlashPaletteState => ({
      nodeTypes: types,
      typeLocked: doc.node !== null,
      selectType: (typeId) => {
        setSelectedType(typeId);
        const chosen = types.find((candidate) => candidate.id === typeId);
        toast.show(
          "info",
          `Type set to ${chosen?.name ?? typeId}`,
          "Applied when this node is first saved.",
        );
      },
    }),
    [types, doc.node, toast],
  );

  /* --- Asset upload ------------------------------------------------- */

  const uploadImage = useCallback(
    async (file: File, position: number) => {
      const id = ++uploadSequence.current;
      setUploads((current) => [...current, { id, name: file.name, status: "uploading" }]);
      try {
        const asset = await api.uploadAsset(file);
        // Only now does anything reach the buffer: a reference is written after
        // the bytes are stored, never before, so a failed upload cannot leave a
        // link to an asset that does not exist.
        const reference = `![${altText(file.name)}](${api.renditionUrl(asset.hash, "preview")})`;
        editorRef.current?.insertBlockAt(position, reference);
        setUploads((current) => current.filter((entry) => entry.id !== id));
      } catch (error) {
        const message = describeError(error);
        setUploads((current) =>
          current.map((entry) =>
            entry.id === id ? { id, name: file.name, status: "failed", message } : entry,
          ),
        );
        toast.showError(error, `Could not upload ${file.name}`);
      }
    },
    [toast],
  );

  const handleFiles = useCallback(
    (files: File[], position: number) => {
      for (const file of files) {
        if (!file.type.startsWith("image/")) {
          toast.show(
            "error",
            "Only images can be dropped into the editor",
            `${file.name} is ${file.type || "of an unknown type"}. Register other files from the Assets view.`,
          );
          continue;
        }
        void uploadImage(file, position);
      }
    },
    [toast, uploadImage],
  );

  const dismissUpload = useCallback((id: number) => {
    setUploads((current) => current.filter((entry) => entry.id !== id));
  }, []);

  /* --- Keyboard ----------------------------------------------------- */

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      // The editor's own Mod-s binding already handled this one; acting again
      // would queue a second save behind the first.
      if (event.defaultPrevented) return;
      if (!event.metaKey && !event.ctrlKey) return;
      if (event.key === "s") {
        event.preventDefault();
        doc.saveNow();
      } else if (event.key === "\\") {
        event.preventDefault();
        setPreviewVisible((visible) => !visible);
      }
    },
    [doc],
  );

  /* --- The states before there is a document ------------------------ */

  if (doc.status === "loading") {
    return (
      <div className="nd-view">
        <div className="nd-empty">
          <Spinner large label="Loading node" />
        </div>
      </div>
    );
  }

  if (doc.status === "missing") {
    return (
      <div className="nd-view">
        <EmptyState
          title="No such node"
          body={
            <>
              Nothing in the graph has the id <span className="nd-mono">{nodeId}</span>. It may have
              been removed, or the link may be wrong.
            </>
          }
          action={
            <span className="nd-row">
              <Link className="nd-button nd-button--primary" to="/editor">
                Start a new node
              </Link>
              <Link className="nd-button" to="/search">
                Search the graph
              </Link>
            </span>
          }
        />
      </div>
    );
  }

  if (doc.status === "unavailable") {
    return (
      <div className="nd-view">
        <EmptyState
          title="Could not load that node"
          body={
            <>
              {doc.loadError} Nothing has been changed — the node is still exactly as it was.
            </>
          }
          action={
            <button type="button" className="nd-button nd-button--primary" onClick={doc.reload}>
              Try again
            </button>
          }
        />
      </div>
    );
  }

  /* --- The editor ---------------------------------------------------- */

  const showStrip = uploads.length > 0 || linkFailure !== null;

  return (
    <div className="nd-view nd-view--wide nd-editor" onKeyDown={handleKeyDown}>
      <NodeMetaBar
        node={doc.node}
        title={doc.title}
        onTitleChange={doc.setTitle}
        nodeTypes={types}
        typesError={typesError}
        selectedType={selectedType}
        onTypeChange={setSelectedType}
        spaces={spaces}
        archivedSpaces={archivedSpaces.spaces}
        spacesFailed={spacesFailed}
        writeTarget={writeTarget}
        onWriteTargetChange={setWriteTarget}
        saveState={doc.saveState}
        saveError={doc.saveError}
        savedAt={doc.savedAt}
        onSaveNow={doc.saveNow}
        previewVisible={previewVisible}
        onTogglePreview={() => setPreviewVisible((visible) => !visible)}
      />

      <div className="nd-editor__panes">
        <section
          className={
            dragActive
              ? "nd-editor__pane nd-editor__pane--source nd-editor__pane--drop"
              : "nd-editor__pane nd-editor__pane--source"
          }
          aria-label="Markdown source"
        >
          <MarkdownEditor
            key={doc.docKey}
            ref={editorRef}
            initialDoc={doc.initialContent}
            onChange={handleContentChange}
            onSave={doc.saveNow}
            slashState={slashState}
            onLinkSuggestFailure={setLinkFailure}
            onFiles={handleFiles}
            onDragActive={setDragActive}
          />

          {dragActive ? (
            <p className="nd-editor__drop-hint">Drop an image to upload and insert it</p>
          ) : null}

          {showStrip ? (
            <div className="nd-editor__strip">
              {uploads.map((upload) => (
                <span
                  key={upload.id}
                  className={
                    upload.status === "failed"
                      ? "nd-editor__strip-item nd-editor__strip-item--failed"
                      : "nd-editor__strip-item"
                  }
                >
                  {upload.status === "uploading" ? (
                    <>
                      <Spinner label={`Uploading ${upload.name}`} />
                      Uploading {upload.name}…
                    </>
                  ) : (
                    <>
                      {upload.name} failed to upload — {upload.message} Nothing was inserted.
                      <button
                        type="button"
                        className="nd-editor__strip-dismiss"
                        onClick={() => dismissUpload(upload.id)}
                        aria-label={`Dismiss the failed upload of ${upload.name}`}
                      >
                        ×
                      </button>
                    </>
                  )}
                </span>
              ))}

              {linkFailure ? (
                <span className="nd-editor__strip-item nd-editor__strip-item--failed">
                  Link suggestions unavailable — {linkFailure}
                </span>
              ) : null}
            </div>
          ) : null}
        </section>

        {previewVisible ? (
          <section className="nd-editor__pane nd-editor__pane--preview" aria-label="Preview">
            <MarkdownPreview source={previewSource} />
          </section>
        ) : null}
      </div>
    </div>
  );
}

/** The live node-type catalog, fetched once per mount. */
function useNodeTypes(): { types: TypeOut[]; typesError: string | null } {
  const [types, setTypes] = useState<TypeOut[]>([]);
  const [typesError, setTypesError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;

    void (async () => {
      try {
        const catalog = await api.getTypes(controller.signal);
        if (cancelled) return;
        setTypes(catalog.node_types);
        setTypesError(null);
      } catch (error) {
        if (cancelled || controller.signal.aborted) return;
        setTypesError(describeError(error));
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, []);

  return { types, typesError };
}

/** The type a new document starts on: the preferred name, else the first offered. */
function preferredType(types: readonly TypeOut[]): string | null {
  const preferred = types.find(
    (candidate) => candidate.name === PREFERRED_TYPE_NAME || candidate.id === PREFERRED_TYPE_NAME,
  );
  return preferred?.id ?? types[0]?.id ?? null;
}

/** Alt text for a dropped image: the file name, without extension or brackets. */
function altText(fileName: string): string {
  return fileName.replace(/\.[^.]+$/, "").replace(/[[\]]/g, "");
}

export { EditorView };
