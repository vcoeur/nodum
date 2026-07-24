/**
 * "Here is exactly what this button will do", rendered from the proposals
 * themselves.
 *
 * Both destructive dialogs show one. A batch action's whole risk is that the
 * set is larger or different from what the reviewer pictured, so the set is
 * enumerated — by kind, then item by item, in the same language the cards use.
 */

import type { ProposalOut } from "../../api/types";
import { describeCounts, PROPOSAL_KINDS, proposalKind } from "./grouping";
import type { KindCounts } from "./grouping";
import { plural, shortId, truncate } from "./format";
import { contextLabel, updateFields } from "./proposalText";

/** How many items are listed before the rest are summarised as a count. */
const MAX_LISTED = 25;

interface ProposalManifestProps {
  proposals: readonly ProposalOut[];
  /** Which action the manifest is describing. */
  action: "accept" | "reject";
}

/** Count a set of proposals by kind. */
function countKinds(proposals: readonly ProposalOut[]): KindCounts {
  const counts: KindCounts = { node: 0, edge: 0, update: 0 };
  for (const proposal of proposals) {
    const kind = proposalKind(proposal);
    if (kind !== null) counts[kind] += 1;
  }
  return counts;
}

/** One line describing what a single proposal is. */
function manifestLine(proposal: ProposalOut): string {
  const kind = proposalKind(proposal);
  if (kind === "node" && proposal.node) {
    const title = proposal.node.title ?? "(untitled)";
    return `node · ${truncate(title, 60)}`;
  }
  if (kind === "edge" && proposal.edge) {
    const { source, target } = contextLabel(proposal);
    return `edge · ${truncate(source, 34)} —[${proposal.type}]→ ${truncate(target, 34)}`;
  }
  if (kind === "update" && proposal.version) {
    const fields = updateFields(proposal.version).join(", ");
    const { target } = contextLabel(proposal);
    return `update · ${fields || "no fields"} on ${truncate(target, 40)}`;
  }
  return `${proposal.kind} · ${shortId(proposal.id)}`;
}

/**
 * The manifest for a pending destructive action.
 *
 * @param proposals The exact set the action will be sent for.
 * @param action Accept or reject — changes the consequence wording only.
 */
export function ProposalManifest({ proposals, action }: ProposalManifestProps) {
  const counts = countKinds(proposals);
  const listed = proposals.slice(0, MAX_LISTED);
  const remainder = proposals.length - listed.length;

  return (
    <div className="nd-stack" style={{ ["--nd-stack-gap" as string]: "var(--nd-space-6)" }}>
      <p className="nd-rv-manifest__lead">
        {action === "accept" ? "Accepting" : "Rejecting"}{" "}
        <strong>{plural(proposals.length, "proposal")}</strong>
        {describeCounts(counts) ? ` — ${describeCounts(counts)}` : ""}.
      </p>

      <ul className="nd-rv-manifest">
        {listed.map((proposal) => (
          <li key={`${proposal.kind}:${proposal.id}`} className="nd-rv-manifest__item">
            <span className="nd-rv-manifest__text">{manifestLine(proposal)}</span>
            <span className="nd-mono" title={proposal.id}>
              {shortId(proposal.id)}
            </span>
          </li>
        ))}
        {remainder > 0 ? (
          <li className="nd-rv-manifest__item nd-rv-manifest__item--more">
            …and {plural(remainder, "more proposal")}
          </li>
        ) : null}
      </ul>

      <ul className="nd-rv-consequences">
        {action === "accept" ? <AcceptConsequences counts={counts} /> : <RejectConsequences />}
      </ul>
    </div>
  );
}

/** What accepting each kind actually changes, stated per kind present. */
function AcceptConsequences({ counts }: { counts: KindCounts }) {
  const lines: string[] = [];
  if (counts.node > 0) {
    lines.push(
      `${plural(counts.node, "node")} become active — and any pending 'mentions' edges their ` +
        "wikilinks created go live with them.",
    );
  }
  if (counts.edge > 0) {
    lines.push(
      `${plural(counts.edge, "edge")} become active — they start being followed by traversals, ` +
        "search expansion, and the graph view.",
    );
  }
  if (counts.update > 0) {
    lines.push(
      `${plural(counts.update, "version update")} apply only the fields each proposal named, to ` +
        "the node as it stands right now.",
    );
  }
  lines.push("Each transition is one event in the append-only log, attributed to 'human'.");
  return (
    <>
      {lines.map((line) => (
        <li key={line}>{line}</li>
      ))}
    </>
  );
}

/** What rejecting does, for every kind alike. */
function RejectConsequences() {
  const lines = [
    "Rejected nodes and edges move to 'archived'; a rejected version update is archived and never applied.",
    "Your reason is recorded verbatim in every reject event's payload — that is the audit trail.",
    "Nothing is deleted. An archived row stays in the graph and in the event log.",
  ];
  return (
    <>
      {lines.map((line) => (
        <li key={line}>{line}</li>
      ))}
    </>
  );
}

/** Which kinds a set contains, for a dialog subtitle. */
export function kindSummary(proposals: readonly ProposalOut[]): string {
  const counts = countKinds(proposals);
  return PROPOSAL_KINDS.filter((kind) => counts[kind] > 0).join(" + ") || "nothing";
}
