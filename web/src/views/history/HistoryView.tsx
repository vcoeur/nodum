import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { diffVersions, getHistory, getNode } from "../../api/client";
import { EmptyState, NodeBadge, Spinner } from "../../components";
import type { DiffOut, NodeOut, VersionOut } from "../../api/types";
import { ExportJsonButton } from "../assets/ExportJsonButton";
import { describeFailure, type FailureDescription } from "../../lib";
import { DiffPane } from "./DiffPane";
import { VersionTimeline } from "./VersionTimeline";
import "./history.css";

/**
 * Version history for one node — route `/history/:nodeId`.
 *
 * Reached from the editor by URL, so the path is part of the contract between
 * the two slices and must not move.
 *
 * The view answers three questions in order: what happened to this node, what
 * exactly changed between any two points, and how do I get a copy. The third
 * is the thin JSON export and nothing more — Markdown Mirror is Phase 6, so
 * there is no format picker here and no export destination to grow into one.
 */

/** Loading / loaded / failed for the node + history pair. */
type LoadState =
  | { status: "loading" }
  | { status: "ready"; node: NodeOut | null; versions: VersionOut[] }
  | { status: "failed"; failure: FailureDescription };

/** Loading / loaded / failed for the comparison. */
type DiffState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; diff: DiffOut }
  | { status: "failed"; failure: FailureDescription };

/** The stable empty list a not-yet-loaded view reads: a literal `[]` would be
 * a fresh identity every render, re-running the `ordinals` memo for nothing. */
const EMPTY_VERSIONS: VersionOut[] = [];

export default function HistoryView() {
  const { nodeId } = useParams<{ nodeId: string }>();
  const [load, setLoad] = useState<LoadState>({ status: "loading" });
  const [selected, setSelected] = useState<number[]>([]);
  const [diff, setDiff] = useState<DiffState>({ status: "idle" });
  // Bumped by the retry button; the only thing that re-runs the load for an
  // unchanged node id.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!nodeId) return;
    const controller = new AbortController();
    setLoad({ status: "loading" });
    setSelected([]);
    setDiff({ status: "idle" });

    // The node itself is a nicety (title, type, state); the history is the
    // view. Fetching them together but only failing on the history keeps a
    // readable page when one of the two routes is not up yet.
    Promise.all([
      getNode(nodeId, controller.signal).catch(() => null),
      getHistory(nodeId, controller.signal),
    ])
      .then(([node, versions]) => setLoad({ status: "ready", node, versions }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoad({ status: "failed", failure: describeFailure(error, "this node") });
      });

    return () => controller.abort();
  }, [nodeId, attempt]);

  const versions = load.status === "ready" ? load.versions : EMPTY_VERSIONS;

  const ordinals = useMemo(
    () => new Map(versions.map((version, index) => [version.id, index + 1])),
    [versions],
  );

  /** Keep at most two picked; a third replaces the older selection. */
  const toggle = useCallback((id: number) => {
    setSelected((current) => {
      if (current.includes(id)) return current.filter((value) => value !== id);
      if (current.length < 2) return [...current, id];
      return [current[1] as number, id];
    });
  }, []);

  // Compare oldest → newest regardless of click order, so `+` always means
  // "added later" rather than "added in whichever one was clicked second".
  const pair = useMemo(() => (selected.length === 2 ? [...selected].sort((a, b) => a - b) : null), [
    selected,
  ]);

  useEffect(() => {
    if (!pair) {
      setDiff({ status: "idle" });
      return;
    }
    const controller = new AbortController();
    setDiff({ status: "loading" });
    diffVersions(pair[0] as number, pair[1] as number, controller.signal)
      .then((result) => setDiff({ status: "ready", diff: result }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setDiff({ status: "failed", failure: describeFailure(error, "that comparison") });
      });
    return () => controller.abort();
  }, [pair]);

  if (!nodeId) {
    return (
      <div className="nd-view">
        <EmptyState
          title="No node given"
          body="History is per node. Open one from the editor, search, or the graph and follow its history link."
        />
      </div>
    );
  }

  return (
    <div className="nd-view nd-history">
      <header className="nd-view__header">
        <div className="nd-history__heading">
          <h1>History</h1>
          <p className="nd-row nd-history__subject">
            <span className="nd-mono">{nodeId}</span>
            {load.status === "ready" && load.node ? (
              <NodeBadge type={load.node.type} state={load.node.state} />
            ) : null}
          </p>
          {load.status === "ready" && load.node?.title ? (
            <p className="nd-meta">{load.node.title}</p>
          ) : null}
          <p className="nd-history__links">
            <Link to={`/editor/${encodeURIComponent(nodeId)}`}>Open in the editor</Link>
            {" · "}
            <Link to={`/graph/${encodeURIComponent(nodeId)}`}>Show in the graph</Link>
          </p>
        </div>
        <ExportJsonButton nodeId={nodeId} />
      </header>

      {load.status === "loading" ? (
        <div className="nd-empty">
          <Spinner large label="Loading history" />
        </div>
      ) : null}

      {load.status === "failed" ? (
        <EmptyState
          title={load.failure.title}
          body={load.failure.body}
          action={
            load.failure.kind === "not-found" ? (
              <Link to="/search" className="nd-button">
                Find another node
              </Link>
            ) : (
              <button
                type="button"
                className="nd-button"
                onClick={() => setAttempt((count) => count + 1)}
              >
                Try again
              </button>
            )
          }
        />
      ) : null}

      {load.status === "ready" ? (
        versions.length === 0 ? (
          <EmptyState
            title="No versions recorded"
            body="This node has no version snapshots. Every write records one, so an empty history means nothing has been written since it was created."
          />
        ) : (
          <div className="nd-history__body">
            <section className="nd-history__timeline" aria-label="Version timeline">
              <div className="nd-history__timeline-head">
                <h2>
                  {versions.length} {versions.length === 1 ? "version" : "versions"}
                </h2>
                <p className="nd-meta">
                  {versions.length === 1
                    ? "Only one snapshot — there is nothing to compare it against yet."
                    : "Pick two versions to compare them side by side."}
                </p>
              </div>
              <VersionTimeline versions={versions} selected={selected} onToggle={toggle} />
            </section>

            <div className="nd-history__diff">
              {diff.status === "idle" ? (
                <p className="nd-history__hint nd-meta">
                  {selected.length === 1
                    ? "One version picked. Pick a second to see the diff."
                    : "No comparison selected."}
                </p>
              ) : null}
              {diff.status === "loading" ? (
                <div className="nd-empty">
                  <Spinner large label="Building the diff" />
                </div>
              ) : null}
              {diff.status === "failed" ? (
                <EmptyState title={diff.failure.title} body={diff.failure.body} />
              ) : null}
              {diff.status === "ready" ? (
                <DiffPane diff={diff.diff} ordinalOf={(id) => ordinals.get(id) ?? 0} />
              ) : null}
            </div>
          </div>
        )
      ) : null}
    </div>
  );
}

export { HistoryView };
