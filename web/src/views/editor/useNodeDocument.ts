/**
 * Loading a node into the editor and saving it back, on a debounce.
 *
 * The buffer is never transformed. What `getNode` returns as `content` is what
 * the editor opens with, and what the editor holds is what `createNode` /
 * `updateNode` are handed — no normalisation, no trimming, no round-trip
 * through another representation. That is design §2.3.2 stated as code.
 *
 * ## Why the live buffer lives in refs
 *
 * `contentRef` is written on every keystroke; the React state around it is
 * written only when something the *interface* shows actually changes. A buffer
 * kept in `useState` would re-render the whole view on every character, which
 * on a two-pane editor is exactly the layout jitter this slice is not allowed
 * to have.
 *
 * ## Losing work is the failure that matters
 *
 * A failed save leaves `saveState` at `failed` with the reason attached and the
 * buffer untouched — nothing is cleared, reset, or reloaded over the top of it.
 * Further typing re-arms the same debounce, so a save that failed because the
 * single SQLite writer was busy retries by itself, and one that failed for a
 * reason typing will not fix stays visible until the user acts on it.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import type { NodeOut, UpdateNodeBody } from "../../api/types";
import { describeError, isNotFound } from "../../lib";

/** How the node itself loaded. */
export type LoadStatus = "loading" | "ready" | "missing" | "unavailable";

/** Where the buffer stands relative to the database. */
export type SaveState = "clean" | "dirty" | "saving" | "saved" | "failed";

/** Quiet time after the last edit before a save goes out. */
const AUTOSAVE_MS = 1200;

interface UseNodeDocumentOptions {
  /** The `:nodeId` route parameter, or undefined on `/editor`. */
  nodeId: string | undefined;
  /**
   * Type id the first save creates the node with.
   *
   * Null while the type catalog has not answered — a new node cannot be created
   * without one, and this hook refuses rather than inventing a type id.
   */
  createType: string | null;
}

/** Everything the editor view needs to render and drive one document. */
export interface NodeDocument {
  status: LoadStatus;
  /** Why loading failed, when `status` is `missing` or `unavailable`. */
  loadError: string | null;
  /** The stored node, or null while a new document has never been saved. */
  node: NodeOut | null;
  /**
   * Identity of the buffer currently open.
   *
   * Used as the editor component's React key: it changes when a *different*
   * document is opened, and deliberately does not change when the open document
   * is saved for the first time — that would remount the editor mid-sentence.
   */
  docKey: string;
  /** The document the editor should open with. */
  initialContent: string;
  title: string;
  setTitle(next: string): void;
  saveState: SaveState;
  /** Why the last save failed, when `saveState` is `failed`. */
  saveError: string | null;
  /** When the last successful save landed, for the indicator. */
  savedAt: number | null;
  /** Report an edit. Cheap by design: called on every keystroke. */
  handleContentChange(content: string): void;
  /** Save immediately, skipping the debounce. */
  saveNow(): void;
  /** Re-fetch the node from the server. */
  reload(): void;
}

/**
 * Drive one node's buffer: load, track, and autosave it.
 *
 * @param options The route's node id and the type a create would use.
 */
export function useNodeDocument({ nodeId, createType }: UseNodeDocumentOptions): NodeDocument {
  const navigate = useNavigate();

  const [status, setStatus] = useState<LoadStatus>("loading");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [node, setNode] = useState<NodeOut | null>(null);
  const [docKey, setDocKey] = useState("new:0");
  const [initialContent, setInitialContent] = useState("");
  const [title, setTitleState] = useState("");
  const [saveState, setSaveState] = useState<SaveState>("clean");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  /** The live buffer — authoritative between renders. */
  const contentRef = useRef("");
  const titleRef = useRef("");
  /** What the server is known to hold, to diff against and to send minimally. */
  const savedRef = useRef({ title: "", content: "" });
  /** The node this editor is bound to; null until the first save creates one. */
  const ownedIdRef = useRef<string | null>(null);
  const startedRef = useRef(false);
  const savingRef = useRef(false);
  const pendingRef = useRef(false);
  const timerRef = useRef<number | null>(null);
  const blankCountRef = useRef(0);

  const createTypeRef = useRef(createType);
  createTypeRef.current = createType;
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;

  const cancelTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  /**
   * Whether the buffer differs from what the server is known to hold.
   *
   * A never-saved blank document compares equal to the empty baseline, so
   * opening `/editor` and leaving raises no warning and writes nothing.
   */
  const hasUnsaved = useCallback(() => {
    const saved = savedRef.current;
    return contentRef.current !== saved.content || titleRef.current !== saved.title;
  }, []);

  /* ---------------------------------------------------------------- */
  /* Saving                                                            */
  /* ---------------------------------------------------------------- */

  const persist = useCallback(async (): Promise<void> => {
    cancelTimer();
    if (savingRef.current) {
      pendingRef.current = true;
      return;
    }

    const content = contentRef.current;
    const currentTitle = titleRef.current;
    const saved = savedRef.current;
    const id = ownedIdRef.current;

    if (content === saved.content && currentTitle === saved.title) {
      setSaveState((current) => (current === "dirty" ? (id === null ? "clean" : "saved") : current));
      return;
    }

    if (id === null) {
      // Nothing typed yet: opening `/editor` and walking away must not litter
      // the graph with empty nodes.
      if (content.trim() === "" && currentTitle.trim() === "") {
        setSaveState("clean");
        return;
      }
      const type = createTypeRef.current;
      if (type === null) {
        setSaveState("failed");
        setSaveError(
          "No node type available — the type catalog did not load, so a new node cannot be created. Your text is still here.",
        );
        return;
      }

      savingRef.current = true;
      setSaveState("saving");
      setSaveError(null);
      try {
        const created = await api.createNode({
          type,
          title: currentTitle.trim() === "" ? null : currentTitle,
          content,
        });
        ownedIdRef.current = created.id;
        savedRef.current = { title: currentTitle, content };
        setNode(created);
        setSavedAt(Date.now());
        settle();
        // Replace rather than push: the blank `/editor` entry is not a place
        // the back button should return to.
        navigateRef.current(`/editor/${created.id}`, { replace: true });
      } catch (error) {
        setSaveState("failed");
        setSaveError(describeError(error));
      } finally {
        savingRef.current = false;
        drainPending();
      }
      return;
    }

    // Send only what changed: an omitted key is left alone server-side, while
    // a present one is written — so a no-op field is never re-stamped.
    const body: UpdateNodeBody = {};
    if (currentTitle !== saved.title) body.title = currentTitle.trim() === "" ? null : currentTitle;
    if (content !== saved.content) body.content = content;

    savingRef.current = true;
    setSaveState("saving");
    setSaveError(null);
    try {
      const updated = await api.updateNode(id, body);
      savedRef.current = { title: currentTitle, content };
      setNode(updated);
      setSavedAt(Date.now());
      settle();
    } catch (error) {
      setSaveState("failed");
      setSaveError(
        isNotFound(error)
          ? `${describeError(error)} The text on screen is still yours — copy it before leaving.`
          : describeError(error),
      );
    } finally {
      savingRef.current = false;
      drainPending();
    }

    /** After a save, say `saved` only if nothing has been typed since. */
    function settle(): void {
      setSaveState(hasUnsaved() ? "dirty" : "saved");
    }

    /** Run the save that arrived while this one was in flight. */
    function drainPending(): void {
      if (!pendingRef.current) return;
      pendingRef.current = false;
      scheduleSave();
    }
  }, [cancelTimer, hasUnsaved]);

  const persistRef = useRef(persist);
  persistRef.current = persist;

  const scheduleSave = useCallback(() => {
    cancelTimer();
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      void persistRef.current();
    }, AUTOSAVE_MS);
  }, [cancelTimer]);

  const handleContentChange = useCallback(
    (content: string) => {
      contentRef.current = content;
      // Identity-preserving update: React bails out, so a burst of typing costs
      // one render at the start of the burst and none after it.
      setSaveState((current) => (current === "dirty" || current === "saving" ? current : "dirty"));
      scheduleSave();
    },
    [scheduleSave],
  );

  const setTitle = useCallback(
    (next: string) => {
      titleRef.current = next;
      setTitleState(next);
      setSaveState((current) => (current === "dirty" || current === "saving" ? current : "dirty"));
      scheduleSave();
    },
    [scheduleSave],
  );

  const saveNow = useCallback(() => {
    void persistRef.current();
  }, []);

  const reload = useCallback(() => {
    ownedIdRef.current = null;
    startedRef.current = false;
    setReloadToken((token) => token + 1);
  }, []);

  /* ---------------------------------------------------------------- */
  /* Loading                                                           */
  /* ---------------------------------------------------------------- */

  const openBlank = useCallback(() => {
    cancelTimer();
    ownedIdRef.current = null;
    contentRef.current = "";
    titleRef.current = "";
    savedRef.current = { title: "", content: "" };
    startedRef.current = true;
    blankCountRef.current += 1;
    setNode(null);
    setTitleState("");
    setInitialContent("");
    setDocKey(`new:${blankCountRef.current}`);
    setStatus("ready");
    setLoadError(null);
    setSaveState("clean");
    setSaveError(null);
    setSavedAt(null);
  }, [cancelTimer]);

  useEffect(() => {
    if (nodeId === undefined) {
      // Already sitting on an untouched blank document — leave it alone rather
      // than resetting a buffer the user may have started typing into.
      if (startedRef.current && ownedIdRef.current === null) return;
      openBlank();
      return;
    }

    // The node this editor just created: the URL changed, the document did not.
    if (startedRef.current && nodeId === ownedIdRef.current) return;

    const controller = new AbortController();
    let cancelled = false;

    cancelTimer();
    setStatus("loading");
    setLoadError(null);

    void (async () => {
      try {
        const loaded = await api.getNode(nodeId, controller.signal);
        if (cancelled) return;
        ownedIdRef.current = loaded.id;
        contentRef.current = loaded.content;
        titleRef.current = loaded.title ?? "";
        savedRef.current = { title: loaded.title ?? "", content: loaded.content };
        startedRef.current = true;
        setNode(loaded);
        setTitleState(loaded.title ?? "");
        setInitialContent(loaded.content);
        setDocKey(`node:${loaded.id}`);
        setSaveState("clean");
        setSaveError(null);
        setSavedAt(null);
        setStatus("ready");
      } catch (error) {
        if (cancelled || controller.signal.aborted) return;
        setLoadError(describeError(error));
        setStatus(isNotFound(error) ? "missing" : "unavailable");
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [nodeId, reloadToken, cancelTimer, openBlank]);

  /* ---------------------------------------------------------------- */
  /* Not losing the buffer                                             */
  /* ---------------------------------------------------------------- */

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (!hasUnsaved()) return;
      event.preventDefault();
      event.returnValue = "";
    };
    const flushOnHide = () => {
      if (document.visibilityState === "hidden" && hasUnsaved()) void persistRef.current();
    };
    window.addEventListener("beforeunload", warn);
    document.addEventListener("visibilitychange", flushOnHide);
    return () => {
      window.removeEventListener("beforeunload", warn);
      document.removeEventListener("visibilitychange", flushOnHide);
    };
  }, [hasUnsaved]);

  useEffect(
    () => () => {
      // Leaving the view: fire the pending save rather than dropping it. The
      // request outlives the component, which is the point.
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      if (hasUnsaved()) void persistRef.current();
    },
    [hasUnsaved],
  );

  return {
    status,
    loadError,
    node,
    docKey,
    initialContent,
    title,
    setTitle,
    saveState,
    saveError,
    savedAt,
    handleContentChange,
    saveNow,
    reload,
  };
}
