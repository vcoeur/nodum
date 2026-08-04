/**
 * One proposal in the inbox, rendered for its kind.
 *
 * The three kinds are not interchangeable and are not rendered as if they were:
 *
 * - a **node** proposal is a row that would become live, so it shows its title,
 *   its type, its parent (from the server's `context`), and its Markdown;
 * - an **edge** proposal is a claim about two nodes, so it shows both endpoints
 *   by title and **by space** — the ids alone are unreviewable — plus the
 *   agent's self-reported confidence, labelled as self-reported;
 * - an **update** proposal is a staged version, and what matters is *which
 *   fields it named*, so it defers to {@link UpdateDiff}.
 *
 * The endpoint spaces are not decoration. A cross-space edge is filed under one
 * space (its source's) while accepting it needs `edit` on **both** — the queue
 * simplifies, and a card that listed source, target, type, confidence, author
 * and date while omitting the one dimension the filing simplified would be
 * hiding exactly the fact the reviewer is missing.
 */

import { NodeBadge } from "../../components";
import type { SpaceName } from "../../components";
import type { JsonObject, ProposalOut } from "../../api/types";
import { formatAbsolute, formatRelative } from "../../lib";
import { formatConfidence, formatProps, shortId, truncate } from "./format";
import { edgeCrossing, proposalKind } from "./grouping";
import type { EdgeCrossing } from "./grouping";
import { UpdateDiff } from "./UpdateDiff";
import {
  acceptConsequence,
  contextRef,
  refLabel,
  rejectConsequence,
  updateFields,
} from "./proposalText";

/** How much Markdown the collapsed card shows. */
const PREVIEW_CHARS = 220;

interface ProposalCardProps {
  proposal: ProposalOut;
  /** Names a space id — used for both ends of a cross-space edge. */
  spaceName: (spaceId: string) => SpaceName;
  selected: boolean;
  onToggleSelect: () => void;
  expanded: boolean;
  onToggleExpand: () => void;
  /** Accept this one item. Fired only by the card's own button. */
  onAccept: () => void;
  /** Open the reject dialog for this one item. */
  onReject: () => void;
  /** True while any review request is in flight. */
  busy: boolean;
}

/**
 * A single proposal card.
 *
 * @param proposal The queue entry.
 * @param spaceName Names a space id, for a crossing's two ends.
 * @param selected Whether it is in the multi-select set.
 * @param onToggleSelect Toggle multi-select.
 * @param expanded Whether the detail body is open.
 * @param onToggleExpand Toggle the detail body.
 * @param onAccept Accept just this item.
 * @param onReject Open the reject dialog for just this item.
 * @param busy Disable actions while a request is in flight.
 */
export function ProposalCard({
  proposal,
  spaceName,
  selected,
  onToggleSelect,
  expanded,
  onToggleExpand,
  onAccept,
  onReject,
  busy,
}: ProposalCardProps) {
  const kind = proposalKind(proposal);
  const crossing = edgeCrossing(proposal);

  return (
    <article className={selected ? "nd-rv-card nd-rv-card--selected" : "nd-rv-card"}>
      <div className="nd-rv-card__bar">
        <span className="nd-rv-card__select">
          <input
            name={`select-${proposal.id}`}
            type="checkbox"
            checked={selected}
            onChange={onToggleSelect}
            aria-label={`Select this ${proposal.kind} proposal: ${headline(proposal)}`}
          />
        </span>

        <div className="nd-rv-card__heading">
          <div className="nd-row nd-rv-card__title-row">
            <span className={`nd-rv-kind nd-rv-kind--${kind ?? "unknown"}`}>
              {kind === "update" ? "version update" : (kind ?? proposal.kind)}
            </span>
            {proposal.node?.props?.synthesized === true ? (
              <span
                className="nd-badge nd-badge--proposed"
                title="Synthesized by the gardener's abstraction job: the title and content were generated from the members it names, and accepting files them as a concept"
              >
                synthesized
              </span>
            ) : null}
            {crossing ? (
              <span
                className="nd-rv-kind nd-rv-kind--crossing"
                title={`This edge starts in ${spaceName(crossing.from).label} and ends in ${spaceName(crossing.to).label}. It is queued under its source's space; accepting it needs edit on both.`}
              >
                cross-space
              </span>
            ) : null}
            <NodeBadge type={proposal.type} state="proposed" />
          </div>
          <h3 className="nd-rv-card__title">{headline(proposal)}</h3>
          <p className="nd-meta">
            <span title={proposal.id} className="nd-mono">
              {shortId(proposal.id)}
            </span>{" "}
            · <span title={formatAbsolute(proposal.created_at)}>{formatRelative(proposal.created_at)}</span>
          </p>
        </div>

        <div className="nd-row nd-rv-card__actions">
          <button
            type="button"
            className="nd-button nd-button--ghost nd-button--small"
            onClick={onToggleExpand}
            aria-expanded={expanded}
          >
            {expanded ? "Less" : "Inspect"}
          </button>
          <button
            type="button"
            className="nd-button nd-button--danger nd-button--small"
            onClick={onReject}
            disabled={busy}
            title={rejectConsequence(proposal)}
          >
            Reject…
          </button>
          <button
            type="button"
            className="nd-button nd-button--primary nd-button--small"
            onClick={onAccept}
            disabled={busy}
            title={acceptConsequence(proposal)}
          >
            Accept
          </button>
        </div>
      </div>

      <p className="nd-rv-card__consequence">{acceptConsequence(proposal)}</p>

      {proposal.annotation === null ? null : (
        <AnnotationLine annotation={proposal.annotation} kind={proposal.kind} />
      )}

      {expanded ? (
        <div className="nd-rv-card__body">
          {kind === "node" ? <NodeProposal proposal={proposal} /> : null}
          {kind === "edge" ? <EdgeProposal proposal={proposal} spaceName={spaceName} /> : null}
          {kind === "update" && proposal.version ? (
            <UpdateDiff proposal={proposal} version={proposal.version} />
          ) : null}
          {kind === null ? (
            <p className="nd-rv-flag nd-rv-flag--warn">
              Unrecognised proposal kind <code>{proposal.kind}</code>. This build
              of the UI does not know how to render it; review it through the CLI
              rather than guessing.
            </p>
          ) : null}
        </div>
      ) : (
        <CollapsedSummary proposal={proposal} crossing={crossing} spaceName={spaceName} />
      )}
    </article>
  );
}

/** The card's one-line title, per kind. */
function headline(proposal: ProposalOut): string {
  if (proposal.kind === "node") {
    return proposal.node?.title?.trim() || "(untitled node)";
  }
  if (proposal.kind === "edge") {
    const source = refLabel(contextRef(proposal.context, "src"));
    const target = refLabel(contextRef(proposal.context, "dst"));
    return `${source} → ${target}`;
  }
  if (proposal.kind === "update") {
    return refLabel(contextRef(proposal.context, "node"), proposal.version?.node_id ?? proposal.id);
  }
  return proposal.id;
}

/**
 * The learned-curation annotation on this item: the proposer's acceptance rate
 * on this item's type, and the signals their own props named.
 *
 * Rendered small and never as a judgement — the rate is the graph's measure of
 * the proposer's record, which is exactly what a reviewer deciding on this
 * item is entitled to; whether to act on it stays the reviewer's call.
 */
function AnnotationLine({ annotation, kind }: { annotation: JsonObject; kind: string }) {
  const rate = typeof annotation.rate === "number" ? annotation.rate : null;
  const signals = Array.isArray(annotation.signals)
    ? annotation.signals.filter((signal): signal is string => typeof signal === "string")
    : [];
  const counts =
    annotation.counts !== null && typeof annotation.counts === "object"
      ? (annotation.counts as JsonObject)
      : null;
  const accepted = counts !== null && typeof counts.accepted === "number" ? counts.accepted : null;
  const rejected =
    counts !== null && typeof counts.rejected === "number" ? counts.rejected : null;
  if (rate === null && accepted === null) return null;

  const typeWord = kind === "edge" ? "edge type" : "node type";
  const parts: string[] = [];
  if (rate !== null) {
    parts.push(`this proposer accepts at ${Math.round(rate * 100)} % on this ${typeWord}`);
  }
  if (accepted !== null && rejected !== null) {
    parts.push(`${accepted} accepted, ${rejected} rejected`);
  }
  if (signals.length > 0) {
    parts.push(`signals: ${signals.join(", ")}`);
  }
  return (
    <p className="nd-meta nd-rv-card__annotation" title="Judged by the curation job over its proposals (row state for outcomes, the event log to classify them), last 90 days">
      {parts.join(" · ")}
    </p>
  );
}

/** The one-line "what is in here" shown while a card is collapsed. */
function CollapsedSummary({
  proposal,
  crossing,
  spaceName,
}: {
  proposal: ProposalOut;
  /** The crossing this edge makes, or null. */
  crossing: EdgeCrossing | null;
  spaceName: (spaceId: string) => SpaceName;
}) {
  if (proposal.kind === "node" && proposal.node) {
    const preview = truncate(proposal.node.content, PREVIEW_CHARS);
    return <p className="nd-rv-card__preview">{preview || "(no content)"}</p>;
  }
  if (proposal.kind === "edge" && proposal.edge) {
    return (
      <p className="nd-rv-card__preview">
        confidence {formatConfidence(proposal.edge.confidence)} (self-reported)
        {crossing ? (
          <>
            {" · "}
            <span className="nd-mono">{spaceName(crossing.from).label}</span> →{" "}
            <span className="nd-mono">{spaceName(crossing.to).label}</span>
          </>
        ) : null}
      </p>
    );
  }
  if (proposal.kind === "update" && proposal.version) {
    const fields = updateFields(proposal.version);
    return (
      <p className="nd-rv-card__preview">
        names {fields.length === 0 ? "no fields" : fields.join(", ")} · inspect to
        see exactly what accepting writes
      </p>
    );
  }
  return null;
}

/** A proposed node: what would become live, and where it would hang. */
function NodeProposal({ proposal }: { proposal: ProposalOut }) {
  const node = proposal.node;
  if (!node) return <p className="nd-rv-flag nd-rv-flag--warn">The queue entry carries no node row.</p>;
  const parent = contextRef(proposal.context, "parent");

  return (
    <div className="nd-rv-detail">
      <dl className="nd-rv-facts">
        <div>
          <dt className="nd-label">Type</dt>
          <dd className="nd-mono">{node.type}</dd>
        </div>
        <div>
          <dt className="nd-label">Parent</dt>
          <dd>
            {parent ? (
              <>
                {refLabel(parent)}{" "}
                <span className="nd-mono" title={parent.id}>
                  {shortId(parent.id)}
                </span>
              </>
            ) : (
              <span className="nd-meta">none — this would be a root node</span>
            )}
          </dd>
        </div>
        <div>
          <dt className="nd-label">Proposed by</dt>
          <dd className="nd-mono">{node.created_by}</dd>
        </div>
        <div>
          <dt className="nd-label">Created</dt>
          <dd className="nd-meta">{formatAbsolute(node.created_at)}</dd>
        </div>
      </dl>

      <div className="nd-rv-detail__block">
        <span className="nd-label">Content (Markdown source)</span>
        <pre className="nd-rv-source">{node.content || "(empty)"}</pre>
      </div>

      {formatProps(node.props) !== "—" ? (
        <div className="nd-rv-detail__block">
          <span className="nd-label">Props</span>
          <pre className="nd-rv-source">{formatProps(node.props)}</pre>
        </div>
      ) : null}
    </div>
  );
}

/**
 * A proposed edge: both endpoints by title **and space**, and the self-graded
 * confidence.
 *
 * The space of each end is on the same footing as its title here. An edge is a
 * claim about two nodes, and which spaces those nodes are in is what decides
 * who may accept it: `Store.edge_landing_state` needs `edit` on **both**, while
 * the queue files the proposal under the source's space alone. The panel that
 * listed source, target, type, confidence, author and date and left the spaces
 * out was silently endorsing that simplification.
 */
function EdgeProposal({
  proposal,
  spaceName,
}: {
  proposal: ProposalOut;
  spaceName: (spaceId: string) => SpaceName;
}) {
  const edge = proposal.edge;
  if (!edge) return <p className="nd-rv-flag nd-rv-flag--warn">The queue entry carries no edge row.</p>;
  const source = contextRef(proposal.context, "src");
  const target = contextRef(proposal.context, "dst");
  const crossing = edgeCrossing(proposal);

  return (
    <div className="nd-rv-detail">
      <div className="nd-rv-edge">
        <Endpoint
          label="Source"
          reference={source}
          fallbackId={edge.src_id}
          spaceName={spaceName}
        />
        <span className="nd-rv-edge__arrow" aria-hidden="true">
          —[{edge.type}]→
        </span>
        <Endpoint
          label="Target"
          reference={target}
          fallbackId={edge.dst_id}
          spaceName={spaceName}
        />
      </div>

      {crossing ? (
        <p className="nd-rv-flag nd-rv-flag--crossing">
          This edge crosses spaces: it starts in{" "}
          <span className="nd-mono">{spaceName(crossing.from).label}</span> and ends in{" "}
          <span className="nd-mono">{spaceName(crossing.to).label}</span>. The queue files it under
          the source's space, which is where you found it — but the edge lands only for a reviewer
          with authority on <em>both</em>, so an agent holding <code>edit</code> on one of them
          cannot accept it. As a human here you have both; the filing is a simplification of the
          grouping, not of the rule.
        </p>
      ) : null}

      <dl className="nd-rv-facts">
        <div>
          <dt className="nd-label">Edge type</dt>
          <dd className="nd-mono">{edge.type}</dd>
        </div>
        <div>
          <dt className="nd-label">Confidence</dt>
          <dd>
            {formatConfidence(edge.confidence)}
            <span className="nd-meta nd-rv-facts__caveat">
              {" "}
              self-reported by {edge.created_by}; the graph does not measure it
            </span>
          </dd>
        </div>
        <div>
          <dt className="nd-label">Proposed by</dt>
          <dd className="nd-mono">{edge.created_by}</dd>
        </div>
        <div>
          <dt className="nd-label">Created</dt>
          <dd className="nd-meta">{formatAbsolute(edge.created_at)}</dd>
        </div>
      </dl>

      {formatProps(edge.props) !== "—" ? (
        <div className="nd-rv-detail__block">
          <span className="nd-label">Props</span>
          <pre className="nd-rv-source">{formatProps(edge.props)}</pre>
        </div>
      ) : null}
    </div>
  );
}

/** One end of a proposed edge: what it is, where it is, and which row it is. */
function Endpoint({
  label,
  reference,
  fallbackId,
  spaceName,
}: {
  label: string;
  reference: { id: string; title: string | null; spaceId: string | null } | null;
  fallbackId: string;
  spaceName: (spaceId: string) => SpaceName;
}) {
  const id = reference?.id ?? fallbackId;
  const title = reference?.title;
  const space = reference?.spaceId ?? null;
  return (
    <div className="nd-rv-edge__end">
      <span className="nd-label">{label}</span>
      <span className="nd-rv-edge__title">
        {title && title.trim() !== "" ? title : <em className="nd-meta">untitled</em>}
      </span>
      {space === null ? null : (
        <span className="nd-meta nd-rv-edge__space">
          in <span className="nd-mono">{spaceName(space).label}</span>
        </span>
      )}
      <span className="nd-mono" title={id}>
        {shortId(id, 12)}
      </span>
      {reference === null ? (
        <span className="nd-rv-flag nd-rv-flag--warn">
          the server could not resolve this endpoint
        </span>
      ) : null}
    </div>
  );
}
