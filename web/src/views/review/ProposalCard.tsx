/**
 * One proposal in the inbox, rendered for its kind.
 *
 * The three kinds are not interchangeable and are not rendered as if they were:
 *
 * - a **node** proposal is a row that would become live, so it shows its title,
 *   its type, its parent (from the server's `context`), and its Markdown;
 * - an **edge** proposal is a claim about two nodes, so it shows both endpoints
 *   by title — the ids alone are unreviewable — plus the agent's self-reported
 *   confidence, labelled as self-reported;
 * - an **update** proposal is a staged version, and what matters is *which
 *   fields it named*, so it defers to {@link UpdateDiff}.
 */

import { NodeBadge } from "../../components";
import type { ProposalOut } from "../../api/types";
import { formatAbsolute, formatRelative } from "../../lib";
import { formatConfidence, formatProps, shortId, truncate } from "./format";
import { proposalKind } from "./grouping";
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
  selected,
  onToggleSelect,
  expanded,
  onToggleExpand,
  onAccept,
  onReject,
  busy,
}: ProposalCardProps) {
  const kind = proposalKind(proposal);

  return (
    <article className={selected ? "nd-rv-card nd-rv-card--selected" : "nd-rv-card"}>
      <div className="nd-rv-card__bar">
        <span className="nd-rv-card__select">
          <input
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

      {expanded ? (
        <div className="nd-rv-card__body">
          {kind === "node" ? <NodeProposal proposal={proposal} /> : null}
          {kind === "edge" ? <EdgeProposal proposal={proposal} /> : null}
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
        <CollapsedSummary proposal={proposal} />
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

/** The one-line "what is in here" shown while a card is collapsed. */
function CollapsedSummary({ proposal }: { proposal: ProposalOut }) {
  if (proposal.kind === "node" && proposal.node) {
    const preview = truncate(proposal.node.content, PREVIEW_CHARS);
    return <p className="nd-rv-card__preview">{preview || "(no content)"}</p>;
  }
  if (proposal.kind === "edge" && proposal.edge) {
    return (
      <p className="nd-rv-card__preview">
        confidence {formatConfidence(proposal.edge.confidence)} (self-reported)
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

/** A proposed edge: both endpoints by title, and the self-graded confidence. */
function EdgeProposal({ proposal }: { proposal: ProposalOut }) {
  const edge = proposal.edge;
  if (!edge) return <p className="nd-rv-flag nd-rv-flag--warn">The queue entry carries no edge row.</p>;
  const source = contextRef(proposal.context, "src");
  const target = contextRef(proposal.context, "dst");

  return (
    <div className="nd-rv-detail">
      <div className="nd-rv-edge">
        <Endpoint label="Source" reference={source} fallbackId={edge.src_id} />
        <span className="nd-rv-edge__arrow" aria-hidden="true">
          —[{edge.type}]→
        </span>
        <Endpoint label="Target" reference={target} fallbackId={edge.dst_id} />
      </div>

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

/** One end of a proposed edge. */
function Endpoint({
  label,
  reference,
  fallbackId,
}: {
  label: string;
  reference: { id: string; title: string | null } | null;
  fallbackId: string;
}) {
  const id = reference?.id ?? fallbackId;
  const title = reference?.title;
  return (
    <div className="nd-rv-edge__end">
      <span className="nd-label">{label}</span>
      <span className="nd-rv-edge__title">
        {title && title.trim() !== "" ? title : <em className="nd-meta">untitled</em>}
      </span>
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
