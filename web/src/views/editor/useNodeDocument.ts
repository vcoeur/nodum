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
 *
 * ## Where a new node lands
 *
 * The write target arrives as an option rather than being read from
 * `lib/writeTarget.ts` here, and it is captured before the request like the
 * text is. Both follow from D1a: the space the human was *shown* is the space
 * the node must land in, and a value read at the moment the response comes back
 * could be a different one. Every create then says where it landed, and one
 * refused by the target says so in words rather than in the server's.
 *
 * ## Two documents at once
 *
 * `/editor/:nodeId` → `/editor/:otherId` is a *parameter* change, not a remount:
 * the component stays up and only this hook's effects re-run. So neither the
 * `beforeunload` warning nor the unmount flush fires, and both of the things
 * that keep a buffer safe have to be done explicitly on that transition:
 *
 * - the buffer is flushed through {@link flushLeftover} **before** it is
 *   overwritten, carrying its values rather than reading refs that are about to
 *   describe a different document;
 * - every save is tagged with the document it was issued for, so a response that
 *   lands after the switch cannot write the old document's text into the new
 *   one's baseline, flash the old node's metadata into the meta bar, or navigate
 *   away from the node the reader just opened.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import type { NodeOut, UpdateNodeBody } from "../../api/types";
import { unresolvedSpaceIds, useArchivedSpaces, useToast } from "../../components";
import { describeError, isNotFound } from "../../lib";
import {
  describeDetachedWriteFailure,
  describeLanding,
  describeWriteFailure,
} from "./createOutcome";

/** How the node itself loaded. */
export type LoadStatus = "loading" | "ready" | "missing" | "unavailable";

/** Where the buffer stands relative to the database. */
export type SaveState = "clean" | "dirty" | "saving" | "saved" | "failed";

/** Quiet time after the last edit before a save goes out. */
const AUTOSAVE_MS = 1200;

/** A document's buffer as it stood at the moment the editor let go of it. */
export interface LeftoverBuffer {
  /** The node it belongs to, or null when it was never saved. */
  id: string | null;
  title: string;
  content: string;
  /** What the server was known to hold, so the write stays minimal. */
  saved: { title: string; content: string };
  /** The type a create would use; null when the type catalog never answered. */
  createType: string | null;
  /**
   * The write target a create would use — the space id or name the editor was
   * *showing* when the buffer was let go of, carried for the same reason the
   * text is: reading the store here would file the leftover into whatever the
   * target has become since (design decision D1a).
   */
  space: string;
}

/** What a detached flush wrote. */
export interface FlushOutcome {
  /** The node id that was written. */
  id: string;
  /**
   * The node `POST /api/nodes` returned, when this flush *created* one.
   *
   * Carried rather than reduced to an id because the confirmation has to name
   * the space **the server filed it in**, which only the response knows: the
   * requested target is what was asked for, and `describeLanding` exists to
   * keep those two apart.
   */
  created: NodeOut | null;
}

/**
 * Write a buffer the editor has stopped holding.
 *
 * Opening another document replaces the live buffer wholesale, so a debounce
 * still counting down has nothing left to read by the time it fires. This
 * carries the values instead, and deliberately touches no React state: the view
 * is showing a *different* document by now, and a "saved" badge there would be
 * describing something the reader is not looking at.
 *
 * @param leftover The buffer, captured before it was replaced.
 * @returns What was written, or null when there was nothing to write.
 * @throws Error If a never-saved buffer has text but no type to create it under
 *   — silently dropping it would be the loss this hook exists to prevent.
 */
export async function flushLeftover(leftover: LeftoverBuffer): Promise<FlushOutcome | null> {
  const { id, title, content, saved, createType, space } = leftover;

  if (id === null) {
    // Same rule as the debounced path: opening `/editor` and walking away must
    // not litter the graph with empty nodes.
    if (content.trim() === "" && title.trim() === "") return null;
    if (createType === null) {
      throw new Error(
        "No node type available — the type catalog did not load, so this text could not be " +
          "written as a new node.",
      );
    }
    const created = await api.createNode({
      type: createType,
      title: title.trim() === "" ? null : title,
      content,
      space,
    });
    return { id: created.id, created };
  }

  const body: UpdateNodeBody = {};
  if (title !== saved.title) body.title = title.trim() === "" ? null : title;
  if (content !== saved.content) body.content = content;
  if (Object.keys(body).length === 0) return null;
  await api.updateNode(id, body);
  return { id, created: null };
}

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
  /**
   * The space a create lands in — the sticky write target, by id or name.
   *
   * Passed in rather than read from `lib/writeTarget.ts` here on purpose: D1a
   * requires the create surface to *show* the target, and taking the value the
   * view is rendering is the only arrangement in which the displayed space and
   * the written space cannot drift apart.
   */
  writeTarget: string;
  /**
   * Every active space, for naming one in the post-create confirmation and in
   * a refusal.
   *
   * **Null while `GET /api/spaces` has not answered, and passed on as null.**
   * That read is what decides whether the archived listing below is worth
   * making at all, so collapsing it to `[]` here would fire it on every mount
   * and let a refusal call a live space unnameable.
   */
  spaces: readonly NodeOut[] | null;
}

/** Everything the editor view needs to render and drive one document. */
export interface NodeDocument {
  status: LoadStatus;
  /** Why loading failed, when `status` is `missing` or `unavailable`. */
  loadError: string | null;
  /** The stored node, or null while a new document has never been saved. */
  node: NodeOut | null;
  /**
   * Archived space nodes, when this document holds a reference to one.
   *
   * Fetched here rather than in the view because the two references that can
   * need it are both this hook's: the open node's space, and the write target
   * a refusal has to name. One read serves the meta bar and the refusal copy,
   * and on a document in a live space it is never issued at all.
   */
  archivedSpaces: readonly NodeOut[];
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
export function useNodeDocument({
  nodeId,
  createType,
  writeTarget,
  spaces,
}: UseNodeDocumentOptions): NodeDocument {
  const navigate = useNavigate();
  const toast = useToast();

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

  /**
   * Which buffer the editor is bound to right now.
   *
   * Bumped whenever a *different* document takes the editor over. A save
   * captures it on the way out and re-checks it on the way back in: SQLite has
   * one writer and the debounce is over a second long, so a response landing
   * after the reader has moved on is ordinary, not exotic.
   */
  const docTokenRef = useRef(0);
  /** The save in flight, so a detached flush can queue behind it rather than race it. */
  const inFlightRef = useRef<Promise<void> | null>(null);
  /** What that save is writing, so a flush does not re-send bytes already on the wire. */
  const inFlightValuesRef = useRef<{ title: string; content: string } | null>(null);

  // A space this document names that `GET /api/spaces` does not: the open
  // node's, when it was written somewhere since retired, and the write target,
  // when the human archived the very space they were filing into. Both degrade
  // to a bare 32-hex id without this, on the bar and in every refusal.
  const unresolvedSpaces = useMemo(
    () => unresolvedSpaceIds([node?.space_id ?? "", writeTarget], spaces),
    [node?.space_id, writeTarget, spaces],
  );
  const archived = useArchivedSpaces(unresolvedSpaces.length > 0);

  const createTypeRef = useRef(createType);
  createTypeRef.current = createType;
  const writeTargetRef = useRef(writeTarget);
  writeTargetRef.current = writeTarget;
  const spacesRef = useRef(spaces);
  spacesRef.current = spaces;
  const archivedRef = useRef(archived.spaces);
  archivedRef.current = archived.spaces;
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;
  const toastRef = useRef(toast);
  toastRef.current = toast;

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

  /**
   * The debounced saver, read through a ref.
   *
   * `scheduleSave` is declared below `persist`, and the two form a cycle —
   * `persist`'s drain re-arms the debounce — so no dependency array can name
   * it. The ref is this file's own pattern for that (see `flushBufferRef`),
   * and reading it late is safe because `scheduleSave` is stable: both of its
   * dependencies (`cancelTimer`, `runPersist`) are []-dep callbacks.
   */
  const scheduleSaveRef = useRef<() => void>(() => {});

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
    // The document this save is *for*. Everything below the first `await` is
    // conditional on it: a response that outlives its document has to be dropped
    // rather than written into whatever the editor is showing by then.
    const token = docTokenRef.current;
    const stillCurrent = () => docTokenRef.current === token;

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

      // Captured before the await, like everything else this save is *for*: the
      // node must land in the space the editor was showing when it was written,
      // not in whatever the picker holds by the time the response comes back.
      const target = writeTargetRef.current;

      savingRef.current = true;
      inFlightValuesRef.current = { title: currentTitle, content };
      setSaveState("saving");
      setSaveError(null);
      try {
        // `space` is sent even when it is `main` — which the server would have
        // defaulted to anyway. The create body then carries literally what the
        // picker showed, so there is no unstated default between what the human
        // read and what was written (design decision D1a).
        const created = await api.createNode({
          type,
          title: currentTitle.trim() === "" ? null : currentTitle,
          content,
          space: target,
        });
        // D1a's other half: the landing space is confirmed out loud, from the
        // server's answer. Said before the currency check, because the node
        // exists wherever the reader has navigated to since.
        const landing = describeLanding(created, target, spacesRef.current);
        toastRef.current.show("success", landing.title, landing.detail);
        // The node exists either way — but if the reader has opened something
        // else since, adopting it here would rebind the editor to it and the
        // navigate below would yank them off the document they just opened.
        if (!stillCurrent()) return;
        ownedIdRef.current = created.id;
        savedRef.current = { title: currentTitle, content };
        setNode(created);
        setSavedAt(Date.now());
        settle();
        // Replace rather than push: the blank `/editor` entry is not a place
        // the back button should return to.
        navigateRef.current(`/editor/${created.id}`, { replace: true });
      } catch (error) {
        if (!stillCurrent()) {
          // Not `showError`: the shared classifier renders an `ApiError` as
          // `type: message`, which for a refused space is the literal
          // "UnknownSpace: unknown space: research" — the one wording nothing
          // user-facing may use. The in-place branch below has always routed
          // through `createOutcome`; this one is the same refusal after the
          // reader moved on, and it also owes them the sentence that says what
          // to do next, because their text is gone.
          const refused = describeDetachedWriteFailure(
            error,
            target,
            spacesRef.current,
            archivedRef.current,
          );
          if (refused === null) {
            toastRef.current.showError(error, "The new note could not be saved");
          } else {
            toastRef.current.show("error", "The new note could not be saved", refused);
          }
          return;
        }
        // A write target naming a space that has since been archived or renamed
        // fails here rather than being rewritten to `main` — deliberately, and
        // so it has to read as something the human can act on.
        setSaveState("failed");
        setSaveError(
          describeWriteFailure(error, target, spacesRef.current, archivedRef.current) ??
            describeError(error),
        );
      } finally {
        savingRef.current = false;
        inFlightValuesRef.current = null;
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
    inFlightValuesRef.current = { title: currentTitle, content };
    setSaveState("saving");
    setSaveError(null);
    try {
      // No abort signal, deliberately: cancelling a write in flight tells you
      // nothing about whether the server applied it. The response is *ignored*
      // when it belongs to a document that is no longer open, which is the same
      // protection without putting the write itself at risk.
      const updated = await api.updateNode(id, body);
      if (!stillCurrent()) return;
      savedRef.current = { title: currentTitle, content };
      setNode(updated);
      setSavedAt(Date.now());
      settle();
    } catch (error) {
      if (!stillCurrent()) {
        // The buffer it belonged to is gone from the screen, so there is no
        // panel that could carry this. An unreported failed write is the loss
        // this hook is built around, so it goes to the app-wide surface.
        toastRef.current.showError(error, "An edit to the previous note was not saved");
        return;
      }
      setSaveState("failed");
      setSaveError(
        isNotFound(error)
          ? `${describeError(error)} The text on screen is still yours — copy it before leaving.`
          : describeError(error),
      );
    } finally {
      savingRef.current = false;
      inFlightValuesRef.current = null;
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
      scheduleSaveRef.current();
    }
  }, [cancelTimer, hasUnsaved]);

  const persistRef = useRef(persist);
  persistRef.current = persist;

  /** Run a save and keep a handle on it, so a flush can wait its turn. */
  const runPersist = useCallback(() => {
    const run = persistRef.current();
    inFlightRef.current = run;
    void run.finally(() => {
      if (inFlightRef.current === run) inFlightRef.current = null;
    });
    return run;
  }, []);

  /**
   * Whether the buffer holds anything not already saved or already on the wire.
   *
   * Distinct from {@link hasUnsaved}, which compares against the last *landed*
   * save: during a request `savedRef` still describes the state before it, so a
   * flush keyed on `hasUnsaved` would re-send bytes already in flight and cut a
   * second version row for nothing.
   */
  const hasUnflushed = useCallback(() => {
    const target = inFlightValuesRef.current ?? savedRef.current;
    return contentRef.current !== target.content || titleRef.current !== target.title;
  }, []);

  /**
   * Send the live buffer on its way before something overwrites it.
   *
   * Called on every transition that swaps the open document without unmounting
   * the view. The write is detached — it carries the buffer's values and reports
   * through the toast surface rather than through this document's save state,
   * because by the time it answers the editor is showing something else.
   */
  const flushBuffer = useCallback(() => {
    cancelTimer();
    // Whatever `pendingRef` was going to re-arm is being taken here instead;
    // leaving it set would re-read a buffer that by then holds another document.
    pendingRef.current = false;
    if (!hasUnflushed()) return;

    const leftover: LeftoverBuffer = {
      id: ownedIdRef.current,
      title: titleRef.current,
      content: contentRef.current,
      saved: savedRef.current,
      createType: createTypeRef.current,
      space: writeTargetRef.current,
    };
    const issue = () => {
      void flushLeftover(leftover).then(
        (written) => {
          // A create the reader has already navigated away from is still a node
          // filed into a space, and D1a allows no silent ones. Only a create
          // gets this: an update went to the space its node already lived in.
          if (written === null || written.created === null) return;
          // Through `describeLanding`, like the in-place create: the
          // confirmation names the space **the server** filed the node in, off
          // the response, rather than echoing back the target that was asked
          // for. Naming the request would confirm nothing, and would go on
          // reading plausibly the day the two stop agreeing.
          const landing = describeLanding(written.created, leftover.space, spacesRef.current);
          toastRef.current.show(
            "success",
            landing.title,
            `Written from the note that was open a moment ago. ${landing.detail}`,
          );
        },
        (error: unknown) => {
          // Same reason as the detached create above: an unresolved space
          // handed to the shared classifier prints the forbidden wording.
          const refused = describeDetachedWriteFailure(
            error,
            leftover.space,
            spacesRef.current,
            archivedRef.current,
          );
          if (refused === null) {
            toastRef.current.showError(error, "Unsaved changes to the previous note were lost");
          } else {
            toastRef.current.show(
              "error",
              "Unsaved changes to the previous note were lost",
              refused,
            );
          }
        },
      );
    };

    // Two writes to the same node racing each other can land in either order,
    // and the loser would win.
    const inFlight = inFlightRef.current;
    if (inFlight === null) issue();
    else void inFlight.then(issue, issue);
  }, [cancelTimer, hasUnflushed]);

  // Read through a ref by the unmount effect, which must run exactly once and
  // so cannot list `flushBuffer` as a dependency.
  const flushBufferRef = useRef(flushBuffer);
  flushBufferRef.current = flushBuffer;

  const scheduleSave = useCallback(() => {
    cancelTimer();
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      void runPersist();
    }, AUTOSAVE_MS);
  }, [cancelTimer, runPersist]);

  scheduleSaveRef.current = scheduleSave;

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
    void runPersist();
  }, [runPersist]);

  const reload = useCallback(() => {
    // Before `ownedIdRef` is cleared: a flush after it would read a null id and
    // create a second node out of the document being re-fetched.
    flushBuffer();
    ownedIdRef.current = null;
    startedRef.current = false;
    setReloadToken((token) => token + 1);
  }, [flushBuffer]);

  /* ---------------------------------------------------------------- */
  /* Loading                                                           */
  /* ---------------------------------------------------------------- */

  const openBlank = useCallback(() => {
    // The "New" link is a route-parameter change on a mounted component, so
    // nothing else would have sent the buffer anywhere before it is cleared.
    flushBuffer();
    docTokenRef.current += 1;
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
    // `flushBuffer`, not `cancelTimer`: this callback calls the first and never
    // the second. Both happen to be permanently stable today, so the wrong
    // dependency is inert — and there is no ESLint here to notice if one of
    // them ever stops being, at which point `openBlank` would go on flushing
    // through a closure over a buffer that has already moved on.
  }, [flushBuffer]);

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

    // Order matters. The buffer goes out first, carrying the document it still
    // belongs to; only then is the editor re-pointed, which orphans any save
    // already on the wire so its answer cannot land on the incoming document.
    flushBuffer();
    docTokenRef.current += 1;
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
  }, [nodeId, reloadToken, flushBuffer, openBlank]);

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
      // Still the same document on screen, so this one reports through the save
      // indicator like any other save.
      if (document.visibilityState === "hidden" && hasUnsaved()) void runPersist();
    };
    window.addEventListener("beforeunload", warn);
    document.addEventListener("visibilitychange", flushOnHide);
    return () => {
      window.removeEventListener("beforeunload", warn);
      document.removeEventListener("visibilitychange", flushOnHide);
    };
  }, [hasUnsaved, runPersist]);

  useEffect(
    () => () => {
      // Leaving the view: fire the pending save rather than dropping it. The
      // request outlives the component, which is the point — and it goes out
      // detached, because there is no longer a save indicator to answer to.
      flushBufferRef.current();
    },
    [],
  );

  return {
    status,
    loadError,
    node,
    archivedSpaces: archived.spaces,
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
