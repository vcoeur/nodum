/**
 * The review inbox: every `proposed` item, grouped by agent and by batch.
 *
 * The grouping is the point. A proposal is rarely interesting alone — what a
 * human decides on is "this run of this agent", so the batch is the default
 * unit of accept and reject, and the per-item controls are the exception for
 * when a run is mostly right.
 *
 * Two deliberateness rules, chosen so that neither becomes a dialog people
 * learn to click through:
 *
 * - **A reject always opens the reason dialog**, single or batch. The service
 *   requires a reason and records it in every reject event; there is no path
 *   through this view that sends one without the reviewer typing it.
 * - **An accept is confirmed unless the card is already open.** Accepting a
 *   card you have expanded and read is one click. Accepting one you have not —
 *   or a whole batch — shows a manifest of exactly what will change first.
 *
 * Nothing here is bound to a key. Accept and reject are reachable only by
 * activating a labelled button.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Ref } from "react";
import { api } from "../../api/client";
import type { BatchTransitionOut, ProposalOut } from "../../api/types";
import { EmptyState, Spinner, useToast } from "../../components";
import { AcceptDialog } from "./AcceptDialog";
import { RejectDialog } from "./RejectDialog";
import { ProposalCard } from "./ProposalCard";
import { describeCounts, groupProposals, PROPOSAL_KINDS, proposalKind } from "./grouping";
import type { AgentGroup, ProposalBatch, ProposalKind } from "./grouping";
import { formatAbsolute, formatRelative } from "../../lib";
import { plural } from "./format";
import { classifyFailure, failureMessage, useReviewQueue } from "./useReviewQueue";

/** A destructive action waiting on confirmation. */
interface PendingAction {
  mode: "accept" | "reject";
  proposals: ProposalOut[];
  /** Where the set came from, shown in the dialog. */
  scope: string;
}

/** The outcome of the last accept/reject, kept on screen until superseded. */
interface ActionResult {
  action: string;
  actor: string;
  reason: string | null;
  transitioned: string[];
  failed: { id: string; error: string }[];
}

/** The review inbox. */
export function ReviewInbox() {
  const toast = useToast();
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [result, setResult] = useState<ActionResult | null>(null);
  const [agentFilter, setAgentFilter] = useState<string>("");
  const [kindFilter, setKindFilter] = useState<ProposalKind | "">("");
  const outcomeRef = useRef<HTMLDivElement | null>(null);

  /**
   * Put the reader on the outcome panel once an action lands.
   *
   * The control that opened the dialog is usually gone by now — the selection
   * bar unmounts when the selection clears, the card unmounts when its proposal
   * leaves the queue — so `Modal` deliberately does not restore focus to it, and
   * without this a keyboard user is dropped on `<body>` after every batch
   * action. The outcome panel is where the answer is, so it is where they go.
   *
   * `result` is a fresh object per action, so this fires once per action and
   * not on an unrelated re-render; dismissing sets it to null and moves nothing.
   */
  useEffect(() => {
    if (result !== null) outcomeRef.current?.focus();
  }, [result]);

  // Polling pauses while a dialog is open or a request is in flight, so the
  // list never re-orders under a pointer that is about to click it.
  const queue = useReviewQueue(pending !== null || busy);

  const agents = useMemo(
    () => [...new Set(queue.proposals.map((proposal) => proposal.created_by))].sort(),
    [queue.proposals],
  );

  const visible = useMemo(
    () =>
      queue.proposals.filter(
        (proposal) =>
          (agentFilter === "" || proposal.created_by === agentFilter) &&
          (kindFilter === "" || proposalKind(proposal) === kindFilter),
      ),
    [queue.proposals, agentFilter, kindFilter],
  );

  const groups = useMemo(() => groupProposals(visible), [visible]);

  const selectedProposals = useMemo(
    () => visible.filter((proposal) => selected.has(proposal.id)),
    [visible, selected],
  );

  const toggleSelect = useCallback((id: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const toggleExpand = useCallback((id: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const setBatchSelection = useCallback((batch: ProposalBatch, include: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      for (const proposal of batch.proposals) {
        if (include) next.add(proposal.id);
        else next.delete(proposal.id);
      }
      return next;
    });
  }, []);

  /**
   * Send a review transition and report it honestly.
   *
   * `BatchTransitionOut.failed` carries every id the batch could not process —
   * an id that vanished or was already reviewed elsewhere. It is reported item
   * by item rather than folded into a success message, and the queue is
   * re-fetched afterwards so what is on screen is what the server holds.
   */
  const runAction = useCallback(
    async (mode: "accept" | "reject", proposals: readonly ProposalOut[], reason?: string) => {
      const ids = proposals.map((proposal) => proposal.id);
      if (ids.length === 0) return;
      setBusy(true);
      try {
        const outcome: BatchTransitionOut =
          mode === "accept"
            ? await api.acceptProposals({ ids })
            : await api.rejectProposals({ ids, reason: reason ?? "" });

        setResult({
          action: outcome.action,
          actor: outcome.actor,
          reason: outcome.reason,
          transitioned: outcome.transitioned,
          failed: outcome.failed,
        });

        if (outcome.failed.length === 0) {
          toast.show(
            "success",
            `${plural(outcome.transitioned.length, "proposal")} ${mode}ed`,
            `Recorded as actor "${outcome.actor}".`,
          );
        } else {
          toast.show(
            outcome.transitioned.length > 0 ? "info" : "error",
            `${outcome.transitioned.length} ${mode}ed, ${outcome.failed.length} failed`,
            "See the outcome panel at the top of the queue.",
          );
        }

        // Drop everything the server actually moved out of the selection; ids
        // that failed stay selected so they can be retried or inspected.
        const moved = new Set(outcome.transitioned);
        setSelected((current) => new Set([...current].filter((id) => !moved.has(id))));
        setPending(null);
        await queue.refresh();
      } catch (error) {
        toast.showError(error, mode === "accept" ? "Accept failed" : "Reject failed");
        if (classifyFailure(error) === "forbidden") {
          setResult({
            action: mode,
            actor: "(refused)",
            reason: reason ?? null,
            transitioned: [],
            failed: ids.map((id) => ({ id, error: failureMessage(error) })),
          });
        }
      } finally {
        setBusy(false);
      }
    },
    [queue, toast],
  );

  const askAccept = useCallback((proposals: ProposalOut[], scope: string) => {
    setPending({ mode: "accept", proposals, scope });
  }, []);

  const askReject = useCallback((proposals: ProposalOut[], scope: string) => {
    setPending({ mode: "reject", proposals, scope });
  }, []);

  /** A card's own Accept: direct when it has been read, confirmed otherwise. */
  const acceptOne = useCallback(
    (proposal: ProposalOut) => {
      if (expanded.has(proposal.id)) {
        void runAction("accept", [proposal]);
        return;
      }
      askAccept([proposal], "One proposal you have not opened");
    },
    [expanded, runAction, askAccept],
  );

  if (queue.status === "loading") {
    return (
      <div className="nd-empty">
        <Spinner large label="Loading the review queue" />
      </div>
    );
  }

  if (queue.status === "error") {
    return <QueueFailure error={queue.error} onRetry={() => void queue.refresh()} />;
  }

  return (
    <div className="nd-stack nd-rv-inbox">
      <QueueToolbar
        total={queue.proposals.length}
        shown={visible.length}
        agents={agents}
        agentFilter={agentFilter}
        onAgentFilter={setAgentFilter}
        kindFilter={kindFilter}
        onKindFilter={setKindFilter}
        loadedAt={queue.loadedAt}
        refreshing={queue.refreshing}
        onRefresh={() => void queue.refresh()}
        paused={pending !== null || busy}
      />

      {queue.error ? (
        <p className="nd-rv-flag nd-rv-flag--warn">
          The last refresh failed ({failureMessage(queue.error)}). Showing the
          queue as of {queue.loadedAt ? new Date(queue.loadedAt).toLocaleTimeString() : "the last successful load"}.
        </p>
      ) : null}

      {result ? (
        <ActionOutcome ref={outcomeRef} result={result} onDismiss={() => setResult(null)} />
      ) : null}

      {selectedProposals.length > 0 ? (
        <SelectionBar
          proposals={selectedProposals}
          busy={busy}
          onAccept={() => askAccept(selectedProposals, `${selectedProposals.length} selected`)}
          onReject={() => askReject(selectedProposals, `${selectedProposals.length} selected`)}
          onClear={() => setSelected(new Set())}
        />
      ) : null}

      {visible.length === 0 ? (
        <EmptyQueue
          filtered={queue.proposals.length > 0}
          loadedAt={queue.loadedAt}
          onClearFilters={() => {
            setAgentFilter("");
            setKindFilter("");
          }}
        />
      ) : (
        groups.map((group) => (
          <AgentSection
            key={group.agent}
            group={group}
            selected={selected}
            expanded={expanded}
            busy={busy}
            onToggleSelect={toggleSelect}
            onToggleExpand={toggleExpand}
            onSetBatchSelection={setBatchSelection}
            onAcceptOne={acceptOne}
            onRejectOne={(proposal) => askReject([proposal], "One proposal")}
            onAcceptBatch={(batch) => askAccept([...batch.proposals], batchScope(batch))}
            onRejectBatch={(batch) => askReject([...batch.proposals], batchScope(batch))}
          />
        ))
      )}

      {pending?.mode === "accept" ? (
        <AcceptDialog
          proposals={pending.proposals}
          scope={pending.scope}
          busy={busy}
          onConfirm={() => void runAction("accept", pending.proposals)}
          onCancel={() => setPending(null)}
        />
      ) : null}

      {pending?.mode === "reject" ? (
        <RejectDialog
          proposals={pending.proposals}
          scope={pending.scope}
          busy={busy}
          onConfirm={(reason) => void runAction("reject", pending.proposals, reason)}
          onCancel={() => setPending(null)}
        />
      ) : null}
    </div>
  );
}

/** How a batch is described in a dialog. */
function batchScope(batch: ProposalBatch): string {
  return `${batch.agent} · run of ${formatAbsolute(batch.startedAt)}`;
}

/** Filters, freshness, and the manual refresh. */
function QueueToolbar({
  total,
  shown,
  agents,
  agentFilter,
  onAgentFilter,
  kindFilter,
  onKindFilter,
  loadedAt,
  refreshing,
  onRefresh,
  paused,
}: {
  total: number;
  shown: number;
  agents: string[];
  agentFilter: string;
  onAgentFilter: (value: string) => void;
  kindFilter: ProposalKind | "";
  onKindFilter: (value: ProposalKind | "") => void;
  loadedAt: number | null;
  refreshing: boolean;
  onRefresh: () => void;
  paused: boolean;
}) {
  return (
    <div className="nd-rv-toolbar">
      <div className="nd-row nd-rv-toolbar__filters">
        <label className="nd-rv-toolbar__field">
          <span className="nd-label">Agent</span>
          <select
            className="nd-select"
            value={agentFilter}
            onChange={(event) => onAgentFilter(event.target.value)}
          >
            <option value="">all agents</option>
            {agents.map((agent) => (
              <option key={agent} value={agent}>
                {agent}
              </option>
            ))}
          </select>
        </label>

        <label className="nd-rv-toolbar__field">
          <span className="nd-label">Kind</span>
          <select
            className="nd-select"
            value={kindFilter}
            onChange={(event) => onKindFilter(event.target.value as ProposalKind | "")}
          >
            <option value="">all kinds</option>
            {PROPOSAL_KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {kind === "update" ? "version updates" : `${kind}s`}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="nd-row nd-rv-toolbar__status">
        <span className="nd-meta">
          {shown === total ? plural(total, "proposal") : `${shown} of ${total} proposals`} waiting
          {loadedAt !== null ? ` · checked ${new Date(loadedAt).toLocaleTimeString()}` : ""}
          {paused ? " · polling paused" : ""}
        </span>
        <button type="button" className="nd-button nd-button--small" onClick={onRefresh}>
          {refreshing ? <Spinner label="Refreshing" /> : null} Refresh
        </button>
      </div>
    </div>
  );
}

/** One agent's share of the queue. */
function AgentSection({
  group,
  selected,
  expanded,
  busy,
  onToggleSelect,
  onToggleExpand,
  onSetBatchSelection,
  onAcceptOne,
  onRejectOne,
  onAcceptBatch,
  onRejectBatch,
}: {
  group: AgentGroup;
  selected: ReadonlySet<string>;
  expanded: ReadonlySet<string>;
  busy: boolean;
  onToggleSelect: (id: string) => void;
  onToggleExpand: (id: string) => void;
  onSetBatchSelection: (batch: ProposalBatch, include: boolean) => void;
  onAcceptOne: (proposal: ProposalOut) => void;
  onRejectOne: (proposal: ProposalOut) => void;
  onAcceptBatch: (batch: ProposalBatch) => void;
  onRejectBatch: (batch: ProposalBatch) => void;
}) {
  return (
    <section className="nd-rv-agent">
      <header className="nd-rv-agent__head">
        <div>
          <h2 className="nd-rv-agent__name nd-mono">{group.agent}</h2>
          <p className="nd-meta">
            {describeCounts(group.counts)} · oldest {formatRelative(group.oldestAt)} ·{" "}
            {plural(group.batches.length, "run")}
          </p>
        </div>
      </header>

      {group.batches.map((batch) => (
        <BatchSection
          key={batch.key}
          batch={batch}
          selected={selected}
          expanded={expanded}
          busy={busy}
          onToggleSelect={onToggleSelect}
          onToggleExpand={onToggleExpand}
          onSetBatchSelection={onSetBatchSelection}
          onAcceptOne={onAcceptOne}
          onRejectOne={onRejectOne}
          onAcceptBatch={onAcceptBatch}
          onRejectBatch={onRejectBatch}
        />
      ))}
    </section>
  );
}

/** One derived batch — an agent's run — with its own accept/reject. */
function BatchSection({
  batch,
  selected,
  expanded,
  busy,
  onToggleSelect,
  onToggleExpand,
  onSetBatchSelection,
  onAcceptOne,
  onRejectOne,
  onAcceptBatch,
  onRejectBatch,
}: {
  batch: ProposalBatch;
  selected: ReadonlySet<string>;
  expanded: ReadonlySet<string>;
  busy: boolean;
  onToggleSelect: (id: string) => void;
  onToggleExpand: (id: string) => void;
  onSetBatchSelection: (batch: ProposalBatch, include: boolean) => void;
  onAcceptOne: (proposal: ProposalOut) => void;
  onRejectOne: (proposal: ProposalOut) => void;
  onAcceptBatch: (batch: ProposalBatch) => void;
  onRejectBatch: (batch: ProposalBatch) => void;
}) {
  const allSelected = batch.proposals.every((proposal) => selected.has(proposal.id));
  const spansTime = batch.startedAt !== batch.endedAt;

  return (
    <div className="nd-rv-batch">
      <header className="nd-rv-batch__head">
        <label className="nd-rv-batch__select">
          <input
            type="checkbox"
            checked={allSelected}
            onChange={(event) => onSetBatchSelection(batch, event.target.checked)}
            // The same instant the label beside it shows: a raw server
            // timestamp here would read out UTC to a screen reader while the
            // sighted label says local time.
            aria-label={`Select the whole run of ${batch.agent} from ${formatAbsolute(batch.startedAt)}`}
          />
          <span className="nd-rv-batch__label">
            Run · {formatAbsolute(batch.startedAt)}
            {spansTime ? ` → ${formatAbsolute(batch.endedAt)}` : ""}
          </span>
        </label>

        <span className="nd-meta nd-rv-batch__counts">{describeCounts(batch.counts)}</span>

        <div className="nd-row nd-rv-batch__actions">
          <button
            type="button"
            className="nd-button nd-button--danger nd-button--small"
            onClick={() => onRejectBatch(batch)}
            disabled={busy}
          >
            Reject run ({batch.proposals.length})
          </button>
          <button
            type="button"
            className="nd-button nd-button--primary nd-button--small"
            onClick={() => onAcceptBatch(batch)}
            disabled={busy}
          >
            Accept run ({batch.proposals.length})
          </button>
        </div>
      </header>

      <p className="nd-meta nd-rv-batch__derivation">
        A run is derived here from arrival times — proposals from one agent filed
        within two minutes of each other. Nothing in the schema records a batch id
        yet.
      </p>

      <div className="nd-stack nd-rv-batch__items">
        {batch.proposals.map((proposal) => (
          <ProposalCard
            key={`${proposal.kind}:${proposal.id}`}
            proposal={proposal}
            selected={selected.has(proposal.id)}
            onToggleSelect={() => onToggleSelect(proposal.id)}
            expanded={expanded.has(proposal.id)}
            onToggleExpand={() => onToggleExpand(proposal.id)}
            onAccept={() => onAcceptOne(proposal)}
            onReject={() => onRejectOne(proposal)}
            busy={busy}
          />
        ))}
      </div>
    </div>
  );
}

/** The sticky bar for a cross-batch multi-select. */
function SelectionBar({
  proposals,
  busy,
  onAccept,
  onReject,
  onClear,
}: {
  proposals: ProposalOut[];
  busy: boolean;
  onAccept: () => void;
  onReject: () => void;
  onClear: () => void;
}) {
  return (
    <div className="nd-rv-selection" role="region" aria-label="Selected proposals">
      <span>{plural(proposals.length, "proposal")} selected</span>
      <div className="nd-row">
        <button type="button" className="nd-button nd-button--small" onClick={onClear}>
          Clear
        </button>
        <button
          type="button"
          className="nd-button nd-button--danger nd-button--small"
          onClick={onReject}
          disabled={busy}
        >
          Reject selected…
        </button>
        <button
          type="button"
          className="nd-button nd-button--primary nd-button--small"
          onClick={onAccept}
          disabled={busy}
        >
          Accept selected…
        </button>
      </div>
    </div>
  );
}

/**
 * What the last action actually did.
 *
 * A batch never aborts on a bad id: the ids that could not transition come back
 * in `failed` with the server's reason. Reporting only the successes would be a
 * lie the reviewer has no other way to catch.
 *
 * `tabIndex={-1}` makes the panel a focus target without putting it in the Tab
 * order: the view moves focus here when an action lands, because the button
 * that started it has usually unmounted by then.
 */
function ActionOutcome({
  ref,
  result,
  onDismiss,
}: {
  ref: Ref<HTMLDivElement>;
  result: ActionResult;
  onDismiss: () => void;
}) {
  const clean = result.failed.length === 0;
  return (
    <div
      ref={ref}
      tabIndex={-1}
      className={clean ? "nd-rv-outcome" : "nd-rv-outcome nd-rv-outcome--partial"}
    >
      <div className="nd-rv-outcome__head">
        <strong>
          {result.action}: {plural(result.transitioned.length, "proposal")} moved
          {result.failed.length > 0 ? `, ${result.failed.length} failed` : ""}
        </strong>
        <button type="button" className="nd-button nd-button--ghost nd-button--small" onClick={onDismiss}>
          Dismiss
        </button>
      </div>
      <p className="nd-meta">
        Recorded as actor <code>{result.actor}</code>
        {result.reason ? (
          <>
            {" "}
            · reason: <q>{result.reason}</q>
          </>
        ) : null}
      </p>
      {result.failed.length > 0 ? (
        <ul className="nd-rv-outcome__failures">
          {result.failed.map((failure) => (
            <li key={failure.id}>
              <span className="nd-mono">{failure.id}</span> — {failure.error}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** The normal state: nothing waiting. */
function EmptyQueue({
  filtered,
  loadedAt,
  onClearFilters,
}: {
  filtered: boolean;
  loadedAt: number | null;
  onClearFilters: () => void;
}) {
  if (filtered) {
    return (
      <EmptyState
        title="Nothing matches these filters"
        body="There are proposals waiting, just not of this kind or from this agent."
        action={
          <button type="button" className="nd-button nd-button--small" onClick={onClearFilters}>
            Clear filters
          </button>
        }
      />
    );
  }
  return (
    <EmptyState
      title="Nothing waiting for review"
      body={
        <>
          An empty queue is the normal state. Proposals land here when an agent
          holding a <code>suggest</code> grant writes — over MCP, authenticated
          by its token. An <code>edit</code> grant lands those writes directly
          instead.
          Checked{" "}
          {loadedAt !== null ? new Date(loadedAt).toLocaleTimeString() : "just now"}; this
          view re-checks on a timer and whenever you come back to the window.
        </>
      }
    />
  );
}

/** The cold-load failure states. */
function QueueFailure({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const kind = classifyFailure(error);
  const message = failureMessage(error);

  if (kind === "unreachable") {
    return (
      <EmptyState
        title="No answer from the nodum server"
        body={
          <>
            The review queue could not be loaded — the API did not respond.
            Start it with <code>uv run nodum serve</code> and try again.
            <br />
            <span className="nd-mono">{message}</span>
          </>
        }
        action={
          <button type="button" className="nd-button nd-button--primary" onClick={onRetry}>
            Try again
          </button>
        }
      />
    );
  }

  if (kind === "busy") {
    return (
      <EmptyState
        title="The database is busy"
        body={
          <>
            SQLite has a single writer, and something is holding it — a large
            accept, an import, or another process writing to the same file. The
            queue itself is fine; nothing has been changed. Try again in a moment.
            <br />
            <span className="nd-mono">{message}</span>
          </>
        }
        action={
          <button type="button" className="nd-button nd-button--primary" onClick={onRetry}>
            Try again
          </button>
        }
      />
    );
  }

  if (kind === "forbidden") {
    return (
      <div className="nd-rv-forbidden">
        <h2>The server refused this as a non-human principal</h2>
        <p>
          Review on this surface is the human tier: <code>accept</code>,{" "}
          <code>reject</code>, <code>archive</code>, and <code>undo</code> need
          a human principal (an agent holding <code>edit</code> on the space can
          review over the service API, but never through this UI, and{" "}
          <code>undo</code> is human-only everywhere). This
          web surface <em>is</em> the human surface — it attributes every write
          to the session's human server-side and has no way to send another
          identity — so a 403 here means the server is not behaving as the surface
          promises, not that you picked the wrong identity.
        </p>
        <p className="nd-mono">{message}</p>
        <p className="nd-meta">
          Worth checking the process you are pointed at before reviewing anything
          through it.
        </p>
        <button type="button" className="nd-button" onClick={onRetry}>
          Try again
        </button>
      </div>
    );
  }

  return (
    <EmptyState
      title="The review queue could not be loaded"
      body={<span className="nd-mono">{message}</span>}
      action={
        <button type="button" className="nd-button nd-button--primary" onClick={onRetry}>
          Try again
        </button>
      }
    />
  );
}
