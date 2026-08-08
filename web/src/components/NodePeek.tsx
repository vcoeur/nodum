/**
 * The shared quick-preview peek card — a hover/focus popover showing what a
 * node contains, without opening it.
 *
 * One component, three trigger sites: search result titles, the graph panel's
 * title, and the `a.nd-wikilink` anchors inside rendered Markdown (the editor
 * preview and the reading view). The card is a portal positioned near the
 * trigger, so no ancestor's `overflow` can clip it.
 *
 * Two surfaces hang off the same controller:
 *
 * - `NodePeek` wraps a trigger element directly (hover/focus on it shows the
 *   card) — the search title and the graph panel title;
 * - `NodePeekScope` owns a rendered-Markdown container and delegates
 *   `mouseover`/`focusin`/`mouseout`/`focusout` on it, the same way
 *   `lib/wikilinks.ts`'s click interceptor does — the anchors inside sanitised
 *   `innerHTML` cannot host React props, so the container can. It also watches
 *   the container for mutations, so a preview re-render that detaches the
 *   armed anchor dismisses the card instead of leaving it pinned to a dead
 *   element.
 *
 * Everything testable about the card lives in `lib/peek.ts`; this file is the
 * wiring: the intent timers, the fetch through a per-session cache, the
 * title→id resolution wikilinks need, and the positioning. The excerpt is
 * plain text from `peekExcerpt` — a peek never runs the sanitiser or mermaid.
 */

import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";
import { forwardRef } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { getNode, resolveTitles } from "../api/client";
import { NodeBadge } from "./NodeBadge";
import { nameSpace, spaceNameNote, unresolvedSpaceIds } from "./spaceNaming";
import { useArchivedSpaces } from "./useArchivedSpaces";
import { useSpaces } from "./useSpaces";
import { formatTimestamp } from "../lib";
import {
  createPeekCache,
  edgeCounts,
  peekExcerpt,
  peekReducer,
  PEEK_DELAY_MS,
  PEEK_IDLE,
  PEEK_LEAVE_GRACE_MS,
  type PeekData,
} from "../lib/peek";
import { titleFromWikilinkHref } from "../lib/wikilinks";
import "./NodePeek.css";

/* ------------------------------------------------------------------ */
/* The per-session data path                                           */
/* ------------------------------------------------------------------ */

/**
 * The shared peek cache: one request pair per node per session.
 *
 * Both reads (`getNode` and its depth-1 neighbourhood) already exist on the
 * client; the counts are derived from the same walk the reading view's rail
 * narrows, so a peek and the view agree about how many edges a node has.
 */
const peekCache = createPeekCache(async (nodeId) => {
  const [node, subgraph] = await Promise.all([getNode(nodeId), getNode(nodeId, { depth: 1 })]);
  return { node, inCount: edgeCounts(subgraph).in, outCount: edgeCounts(subgraph).out };
});

/**
 * Title → node id for wikilink peeks, per session.
 *
 * A wikilink href carries a **title**, and the peek needs an id to fetch.
 * Resolving once per title (and caching the failure too, so a broken link
 * stops asking) keeps a hover at one resolution plus the cached node reads.
 */
const titleToId = new Map<string, string | null>();
const titleResolutions = new Map<string, Promise<string | null>>();

/**
 * The cache key for a title resolution: the space preference counts, because
 * `resolveTitles` tie-breaks on it and the surfaces differ (the reading view
 * passes the node's own space, the editor preview none).
 */
function titleCacheKey(title: string, space: string | undefined): string {
  return `${space ?? ""}\u0000${title}`;
}

/**
 * Resolve a wikilink title to a node id, per session, with the click path's
 * space preference.
 *
 * @param title The wikilink target, as written in `[[…]]`.
 * @param space The read-side tie-break, like `resolveTitles` takes.
 * @returns The node id, or null when the title does not resolve.
 */
function resolveTitleId(title: string, space: string | undefined): Promise<string | null> {
  const key = titleCacheKey(title, space);
  const cached = titleToId.get(key);
  if (cached !== undefined) return Promise.resolve(cached);
  const pending = titleResolutions.get(key);
  if (pending !== undefined) return pending;
  const started = resolveTitles([title], space === undefined ? undefined : { space })
    .then(([resolution]) => {
      const nodeId =
        resolution !== undefined && resolution.outcome === "resolved" && resolution.node_id !== null
          ? resolution.node_id
          : null;
      titleToId.set(key, nodeId);
      return nodeId;
    })
    .finally(() => {
      titleResolutions.delete(key);
    });
  titleResolutions.set(key, started);
  return started;
}

/* ------------------------------------------------------------------ */
/* Positioning                                                         */
/* ------------------------------------------------------------------ */

/** The gap between a trigger and its card, in px. */
const PEEK_GAP = 6;
/** The minimum distance a card keeps from the viewport edge, in px. */
const PEEK_MARGIN = 8;

/**
 * Place a fixed-position card next to its anchor without overflowing.
 *
 * The card prefers to open below the trigger and flips above when it would
 * not fit; horizontally it shifts left rather than going off-screen.
 *
 * @param anchor The trigger element, for its viewport rect.
 * @param card The card element; its `left`/`top` are set.
 */
function positionCard(anchor: Element, card: HTMLElement): void {
  const anchorRect = anchor.getBoundingClientRect();
  const cardRect = card.getBoundingClientRect();
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;

  let left = anchorRect.left;
  let top = anchorRect.bottom + PEEK_GAP;
  const flips = top + cardRect.height > viewportHeight - PEEK_MARGIN;
  if (flips && anchorRect.top - cardRect.height - PEEK_GAP >= PEEK_MARGIN) {
    top = anchorRect.top - cardRect.height - PEEK_GAP;
  }
  if (left + cardRect.width > viewportWidth - PEEK_MARGIN) {
    left = Math.max(PEEK_MARGIN, viewportWidth - cardRect.width - PEEK_MARGIN);
  }
  card.style.left = `${Math.round(left)}px`;
  card.style.top = `${Math.round(top)}px`;
}

/* ------------------------------------------------------------------ */
/* The intent controller                                              */
/* ------------------------------------------------------------------ */

/** How the card was confirmed, which decides what hides it (see blur rules). */
type ShownBy = "hover" | "focus";

/**
 * The controller shared by the trigger wrapper and the delegated scope.
 *
 * Owns the intent state machine (`lib/peek.ts`), the two timers (intent and
 * the leave-grace that lets the pointer cross from a trigger onto the card),
 * the data load through the shared cache, and the portal card itself.
 */
function usePeek() {
  const [state, dispatch] = useReducer(peekReducer, PEEK_IDLE);
  // Mirror of the reducer state, for the event handlers that must decide
  // against the *current* state without re-rendering first.
  const stateRef = useRef(state);
  stateRef.current = state;

  const [data, setData] = useState<PeekData | null>(null);
  const [failed, setFailed] = useState(false);

  const anchorRef = useRef<Element | null>(null);
  const cardRef = useRef<HTMLElement | null>(null);
  const intentTimerRef = useRef<number | null>(null);
  const graceTimerRef = useRef<number | null>(null);
  const shownByRef = useRef<ShownBy>("hover");
  /** Set by `focusEnter(…, focusCard)`: the next shown card takes focus. */
  const focusCardOnShowRef = useRef(false);
  /**
   * Set while `dismiss()` hands focus back to the trigger: both focus paths
   * would read that programmatic focus as a user focus and re-arm the card
   * over the dismissal just requested. Cleared after the synchronous dispatch
   * and the microtasks it queues — see `dismiss`.
   */
  const suppressFocusRearmRef = useRef(false);

  const clearIntentTimer = useCallback(() => {
    if (intentTimerRef.current !== null) window.clearTimeout(intentTimerRef.current);
    intentTimerRef.current = null;
  }, []);
  const clearGrace = useCallback(() => {
    if (graceTimerRef.current !== null) window.clearTimeout(graceTimerRef.current);
    graceTimerRef.current = null;
  }, []);

  /** A pointer entered a trigger: arm the intent delay unless it is showing. */
  const enter = useCallback(
    (nodeId: string, anchor: Element | null) => {
      clearGrace();
      anchorRef.current = anchor;
      const before = stateRef.current;
      if (before.phase !== "shown" || before.trigger !== nodeId) {
        clearIntentTimer();
        intentTimerRef.current = window.setTimeout(() => {
          if (stateRef.current.phase !== "pending" || stateRef.current.trigger !== nodeId) return;
          shownByRef.current = "hover";
          dispatch({ type: "confirm" });
        }, PEEK_DELAY_MS);
      }
      dispatch({ type: "enter", trigger: nodeId });
    },
    [clearGrace, clearIntentTimer, dispatch],
  );

  /** A trigger received focus: show immediately — keyboard users do not hover.
   *  With `focusCard`, focus moves into the card once it is on screen, making
   *  its actions the next tab stops after the trigger (the scope's anchors sit
   *  inside `innerHTML`, so their card would otherwise sit unreachable at the
   *  end of `document.body` in tab order). */
  const focusEnter = useCallback(
    (nodeId: string, anchor: Element | null, focusCard = false) => {
      // A focus returned to the trigger by an Esc-in-card dismissal is that
      // dismissal, not a new focus — re-arming would reopen the card over it.
      if (suppressFocusRearmRef.current) return;
      clearGrace();
      clearIntentTimer();
      anchorRef.current = anchor;
      shownByRef.current = "focus";
      focusCardOnShowRef.current = focusCard;
      dispatch({ type: "enter", trigger: nodeId });
      dispatch({ type: "confirm" });
    },
    [clearGrace, clearIntentTimer, dispatch],
  );

  /** A pointer left a trigger: hide after the grace window, unless the card
   *  was entered in time. A focus-shown card survives a stray mouseleave —
   *  the pointer is not the interaction that summoned it (mirror of the blur
   *  rule below). */
  const leave = useCallback(
    (nodeId: string) => {
      if (stateRef.current.trigger !== nodeId) return;
      if (shownByRef.current === "focus") return;
      clearGrace();
      graceTimerRef.current = window.setTimeout(() => {
        if (stateRef.current.trigger !== nodeId) return;
        clearIntentTimer();
        dispatch({ type: "leave", trigger: nodeId });
      }, PEEK_LEAVE_GRACE_MS);
    },
    [clearGrace, clearIntentTimer, dispatch],
  );

  /** Escape, a click outside, leaving the card: hide now. When the card held
   *  the focus, hand it back to the trigger — an Esc-in-card must not drop a
   *  keyboard user on `<body>`. That focus return fires `focusin` on the
   *  trigger, which the focus paths would otherwise read as a user focus and
   *  re-arm the card; the re-arm is suppressed for the dispatch and the
   *  microtasks it queues. */
  const dismiss = useCallback(() => {
    const card = cardRef.current;
    if (card !== null && card.contains(document.activeElement)) {
      const anchor = anchorRef.current;
      if (anchor instanceof HTMLElement && anchor.isConnected) {
        suppressFocusRearmRef.current = true;
        anchor.focus();
        // `focus()` dispatches `focusin` synchronously, and the scope's
        // title-resolution callback re-checks focus in a microtask — clear
        // after both, so a genuine later focus is never suppressed.
        queueMicrotask(() => {
          suppressFocusRearmRef.current = false;
        });
      }
    }
    clearGrace();
    clearIntentTimer();
    dispatch({ type: "dismiss" });
  }, [clearGrace, clearIntentTimer, dispatch]);

  /** The trigger lost focus: keep the card when focus moved into it, and keep
   *  a focus-shown card until Escape or until focus leaves the card — that is
   *  what makes its links reachable by keyboard. */
  const onTriggerBlur = useCallback(() => {
    window.setTimeout(() => {
      const card = cardRef.current;
      if (card !== null && card.contains(document.activeElement)) return;
      if (shownByRef.current === "focus") return;
      if (stateRef.current.trigger !== null) {
        clearIntentTimer();
        dispatch({ type: "leave", trigger: stateRef.current.trigger });
      }
    }, 0);
  }, [clearIntentTimer, dispatch]);

  const onCardEnter = useCallback(() => clearGrace(), [clearGrace]);
  const onCardLeave = useCallback(() => dismiss(), [dismiss]);
  const onCardBlur = useCallback(() => {
    window.setTimeout(() => {
      const card = cardRef.current;
      if (card !== null && card.contains(document.activeElement)) return;
      dismiss();
    }, 0);
  }, [dismiss]);

  /** A delegated focusout from the scope's container.
   *
   * The scope's anchors cannot host React handlers, so focus leaving one
   * routes here. Unlike the wrapper's blur rule, a focus-shown card does not
   * survive a move-on: in a rendered-Markdown container, Tab moving on means
   * the peek is over. The one survivor is focus landing inside the card
   * itself — the hop from the anchor onto its first action when a focus-show
   * summons it. */
  const scopeBlur = useCallback(
    (nodeId: string) => {
      window.setTimeout(() => {
        if (stateRef.current.trigger !== nodeId) return;
        const card = cardRef.current;
        if (card !== null && card.contains(document.activeElement)) return;
        clearIntentTimer();
        dispatch({ type: "leave", trigger: nodeId });
      }, 0);
    },
    [clearIntentTimer, dispatch],
  );

  /** Dismiss when the armed anchor left the DOM.
   *
   * The editor preview rewrites `container.innerHTML` on its debounce, which
   * detaches the wikilink anchors without firing mouseout; the open card
   * would otherwise keep a dead anchor and jump to the viewport corner on the
   * next reposition. The scope's MutationObserver calls this on every
   * mutation; the connection check makes it a no-op while the anchor is still
   * live. */
  const dismissIfAnchorDetached = useCallback(() => {
    const anchor = anchorRef.current;
    if (anchor !== null && !anchor.isConnected) dismiss();
  }, [dismiss]);

  // Load the peek's data when the state machine confirms. The data stays
  // cached per session, so a re-show of the same node renders immediately;
  // the render guard below keeps a stale entry from a previous node.
  useEffect(() => {
    const trigger = state.trigger;
    if (state.phase !== "shown" || trigger === null) return;
    let cancelled = false;
    setFailed(false);
    peekCache
      .getOrLoad(trigger)
      .then((loaded) => {
        if (!cancelled) setData(loaded);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [state]);

  const shown =
    state.phase === "shown" &&
    state.trigger !== null &&
    data !== null &&
    data.node.id === state.trigger &&
    !failed;

  // Place the card when it appears — and again if its content changes height
  // (the space name resolving flips the meta line). Painted before the
  // browser does, so the card never flashes at the wrong spot. A focus-show
  // that asked for it hands focus to the card's first action here.
  useLayoutEffect(() => {
    const card = cardRef.current;
    const anchor = anchorRef.current;
    if (!shown || card === null || anchor === null) return;
    if (!anchor.isConnected) {
      dismiss();
      return;
    }
    positionCard(anchor, card);
    if (focusCardOnShowRef.current) {
      focusCardOnShowRef.current = false;
      card.querySelector<HTMLElement>("a, button")?.focus();
    }
  }, [shown, data, dismiss]);

  // While shown: Escape dismisses, a click anywhere outside the card dismisses
  // (a focus-shown card survives focus moving elsewhere, not a pointer click),
  // and scroll/resize keeps the card pinned to its anchor.
  useEffect(() => {
    if (!shown) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") dismiss();
    };
    const onMouseDown = (event: MouseEvent) => {
      const card = cardRef.current;
      if (card !== null && card.contains(event.target as Node)) return;
      dismiss();
    };
    const reposition = () => {
      const card = cardRef.current;
      const anchor = anchorRef.current;
      if (card === null || anchor === null) return;
      // The trigger may be gone — the editor preview replaced its `innerHTML`
      // — without a mouseout. Positioned against a detached node, the card
      // would jump to the viewport corner; dismiss instead.
      if (!anchor.isConnected) {
        dismiss();
        return;
      }
      positionCard(anchor, card);
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onMouseDown);
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onMouseDown);
      window.removeEventListener("scroll", reposition, true);
      window.removeEventListener("resize", reposition);
    };
  }, [shown, dismiss]);

  // Unmount: never let a timer fire into a dead component.
  useEffect(
    () => () => {
      clearIntentTimer();
      clearGrace();
    },
    [clearIntentTimer, clearGrace],
  );

  const card =
    shown && data !== null
      ? createPortal(
          <PeekCard
            ref={cardRef}
            data={data}
            onMouseEnter={onCardEnter}
            onMouseLeave={onCardLeave}
            onBlur={onCardBlur}
          />,
          document.body,
        )
      : null;

  return {
    enter,
    focusEnter,
    leave,
    dismiss,
    onTriggerBlur,
    scopeBlur,
    dismissIfAnchorDetached,
    card,
  };
}

/* ------------------------------------------------------------------ */
/* The card                                                            */
/* ------------------------------------------------------------------ */

interface PeekCardProps {
  data: PeekData;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
  onBlur: () => void;
}

/**
 * The card itself: title and badge, plain-text excerpt, space and edge
 * counts, and the two actions. Mounted only while shown, so its space reads
 * run only when a card is actually on screen.
 */
const PeekCard = forwardRef<HTMLElement, PeekCardProps>(function PeekCard(
  { data, onMouseEnter, onMouseLeave, onBlur },
  ref,
) {
  const { spaces } = useSpaces();
  const spaceId = data.node.space_id;
  const needed = unresolvedSpaceIds(spaceId === null ? [] : [spaceId], spaces);
  const archivedSpaces = useArchivedSpaces(needed.length > 0);
  const spaceName = spaceId === null ? null : nameSpace(spaceId, spaces, archivedSpaces.spaces);
  const spaceNote = spaceName === null ? null : spaceNameNote(spaceName);

  const title = data.node.title?.trim() ? data.node.title : "(untitled)";
  const excerpt = peekExcerpt(data.node.content);

  return (
    <section
      ref={ref}
      className="nd-peek"
      role="dialog"
      aria-label={`Preview of ${title}`}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      onBlur={onBlur}
    >
      <header className="nd-row nd-peek__header">
        <strong className="nd-truncate nd-peek__title">{title}</strong>
        <NodeBadge type={data.node.type} state={data.node.state} />
      </header>
      {excerpt !== null ? <p className="nd-peek__excerpt">{excerpt}</p> : null}
      <p className="nd-meta nd-peek__meta" title={spaceNote ?? undefined}>
        {spaceName === null ? "—" : spaceName.label}
        {spaceName?.kind === "archived" ? " · archived" : ""} · updated{" "}
        {formatTimestamp(data.node.updated_at)} · {data.outCount} out · {data.inCount} in
      </p>
      <div className="nd-peek__actions">
        <Link
          className="nd-button nd-button--small nd-button--primary"
          to={`/node/${encodeURIComponent(data.node.id)}`}
        >
          Open
        </Link>
        <Link
          className="nd-button nd-button--small"
          to={`/editor/${encodeURIComponent(data.node.id)}`}
        >
          Edit
        </Link>
      </div>
    </section>
  );
});

/* ------------------------------------------------------------------ */
/* The two trigger surfaces                                            */
/* ------------------------------------------------------------------ */

interface NodePeekProps {
  /** The node to preview. */
  nodeId: string;
  /** The trigger element: hover or focus on it shows the card. */
  children: ReactNode;
}

/**
 * Wrap a trigger element in a peek: hover after the intent delay, or focus,
 * shows the card for `nodeId`.
 *
 * The wrapper is `display: contents` — it adds the enter/leave/focus
 * handlers without a box of its own, so the trigger's layout and styles are
 * untouched.
 *
 * @param nodeId The node to preview.
 * @param children The trigger element.
 */
export function NodePeek({ nodeId, children }: NodePeekProps) {
  const peek = usePeek();
  const triggerRef = useRef<HTMLSpanElement | null>(null);

  const anchor = () => triggerRef.current?.firstElementChild ?? triggerRef.current;

  return (
    <span
      ref={triggerRef}
      className="nd-peek-trigger"
      onMouseEnter={() => peek.enter(nodeId, anchor())}
      onMouseLeave={() => peek.leave(nodeId)}
      onFocus={() => peek.focusEnter(nodeId, anchor())}
      onBlur={peek.onTriggerBlur}
      onKeyDown={(event) => {
        if (event.key === "Escape") peek.dismiss();
      }}
    >
      {children}
      {peek.card}
    </span>
  );
}

interface NodePeekScopeProps {
  /** The container whose `a.nd-wikilink` anchors trigger the peek. */
  containerRef: RefObject<HTMLElement | null>;
  /**
   * The read-side space preference for title resolution — the same one the
   * reading view gives its click path.
   */
  space?: string;
}

/**
 * Attach a peek to the wikilinks inside a rendered-Markdown container.
 *
 * The anchors live in sanitised `innerHTML`, so they cannot carry React
 * props; the container can. Delegation mirrors `lib/wikilinks.ts`'s click
 * interceptor: `mouseover`/`mouseout`/`focusin`/`focusout` on the container,
 * matched through `closest("a.nd-wikilink")`, with the title resolved to a
 * node id before the peek arms. A focus-show moves focus into the card, so
 * its actions follow the anchor in tab order; `focusout` hides a focus-peek
 * on Tab-away. A MutationObserver dismisses the card when the armed anchor is
 * destroyed by a preview re-render.
 *
 * @param containerRef The rendered-content element.
 * @param space The title-resolution space preference.
 */
export function NodePeekScope({ containerRef, space }: NodePeekScopeProps) {
  const { enter, focusEnter, leave, scopeBlur, dismissIfAnchorDetached, card } = usePeek();

  useEffect(() => {
    const container = containerRef.current;
    if (container === null) return;

    const onMouseOver = (event: MouseEvent) => {
      const anchor = wikilinkAnchor(event.target);
      if (anchor === null) return;
      const title = titleFromWikilinkHref(anchor.getAttribute("href") ?? "");
      if (title === null) return;
      void resolveTitleId(title, space).then((nodeId) => {
        if (nodeId === null) return;
        if (!anchor.matches(":hover")) return; // the pointer moved on
        enter(nodeId, anchor);
      });
    };

    const onMouseOut = (event: MouseEvent) => {
      const anchor = wikilinkAnchor(event.target);
      if (anchor === null) return;
      const title = titleFromWikilinkHref(anchor.getAttribute("href") ?? "");
      if (title === null) return;
      // Only a wikilink that actually armed has an id to leave — the map is
      // what enter's resolution wrote, so an unarmed one is a no-op.
      const nodeId = titleToId.get(titleCacheKey(title, space));
      if (nodeId !== null && nodeId !== undefined) leave(nodeId);
    };

    const onFocusIn = (event: FocusEvent) => {
      const anchor = wikilinkAnchor(event.target);
      if (anchor === null) return;
      const title = titleFromWikilinkHref(anchor.getAttribute("href") ?? "");
      if (title === null) return;
      void resolveTitleId(title, space).then((nodeId) => {
        if (nodeId === null) return;
        if (!anchor.contains(document.activeElement)) return; // focus moved on
        focusEnter(nodeId, anchor, true);
      });
    };

    const onFocusOut = (event: FocusEvent) => {
      const anchor = wikilinkAnchor(event.target);
      if (anchor === null) return;
      const title = titleFromWikilinkHref(anchor.getAttribute("href") ?? "");
      if (title === null) return;
      const nodeId = titleToId.get(titleCacheKey(title, space));
      if (nodeId === null || nodeId === undefined) return;
      // A move onto another anchor in this container is the incoming
      // focusin's re-aim, not a Tab-away — leave the card to it.
      const movedTo = event.relatedTarget;
      if (movedTo instanceof Node && container.contains(movedTo)) return;
      scopeBlur(nodeId);
    };

    container.addEventListener("mouseover", onMouseOver);
    container.addEventListener("mouseout", onMouseOut);
    container.addEventListener("focusin", onFocusIn);
    container.addEventListener("focusout", onFocusOut);
    // The editor preview rewrites `innerHTML` per debounced keystroke,
    // detaching the armed anchor without a mouseout — the open card would
    // keep a dead anchor and jump to the viewport corner on the next
    // reposition. Dismiss as soon as the anchor is gone.
    const observer = new MutationObserver(() => dismissIfAnchorDetached());
    observer.observe(container, { childList: true, subtree: true });
    return () => {
      container.removeEventListener("mouseover", onMouseOver);
      container.removeEventListener("mouseout", onMouseOut);
      container.removeEventListener("focusin", onFocusIn);
      container.removeEventListener("focusout", onFocusOut);
      observer.disconnect();
    };
  }, [containerRef, space, enter, focusEnter, leave, scopeBlur, dismissIfAnchorDetached]);

  return card;
}

/**
 * The `a.nd-wikilink` under an event target, if there is one.
 *
 * @param target The event's target.
 */
function wikilinkAnchor(target: EventTarget | null): HTMLElement | null {
  if (!(target instanceof Element)) return null;
  const anchor = target.closest("a.nd-wikilink");
  return anchor instanceof HTMLElement ? anchor : null;
}
