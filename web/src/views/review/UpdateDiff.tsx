/**
 * What accepting a proposed *version* will actually do.
 *
 * This is the subtle one, and the whole reason the review view exists rather
 * than a list of ids. An agent's `update_node` does **not** edit the node: it
 * stages a `proposed` version that carries a full title/content/props snapshot
 * plus `proposed_fields`, the list of fields the agent actually named. Accepting
 * applies *exactly those fields* to the node **as it stands then** — so:
 *
 * - a field in the snapshot that the agent did not name is reviewer context and
 *   is never written, even when it differs from the node's current value;
 * - a human edit made while the proposal waited is not reverted by the stale
 *   parts of the snapshot;
 * - the server's own `diff_versions` compares two whole snapshots, so its
 *   `changed_fields` can legitimately name a field this accept will not touch.
 *
 * All three are stated on screen, per field, because none of them are guessable
 * from a unified diff.
 */

import { useEffect, useState } from "react";
import { api } from "../../api/client";
import type { DiffOut, NodeOut, ProposalOut, VersionOut } from "../../api/types";
import { Spinner } from "../../components";
import { formatAbsolute } from "../../lib";
import { canonicalJson, formatProps, shortId } from "./format";
import { SideBySide } from "./SideBySide";
import { contextRef, refLabel, updateFields, VERSION_FIELDS } from "./proposalText";
import type { VersionField } from "./proposalText";
import { failureMessage } from "./useReviewQueue";

interface UpdateDiffProps {
  proposal: ProposalOut;
  version: VersionOut;
}

/** Everything the diff needs from the server, loaded when the card expands. */
interface DiffData {
  node: NodeOut | null;
  /** The latest `applied` version, the baseline the server diff is taken from. */
  baseline: VersionOut | null;
  serverDiff: DiffOut | null;
  /** Why the server diff is missing, when it is. */
  serverDiffError: string | null;
  loadError: string | null;
}

/** Pull one snapshot field out of a version as diffable text. */
function versionText(version: VersionOut, field: VersionField): string {
  if (field === "title") return version.title ?? "";
  if (field === "content") return version.content;
  return formatProps(version.props);
}

/** Pull the same field out of the live node. */
function nodeText(node: NodeOut, field: VersionField): string {
  if (field === "title") return node.title ?? "";
  if (field === "content") return node.content;
  return formatProps(node.props);
}

/** True when the node's value for a field has moved away from the snapshot's. */
function movedSinceProposal(node: NodeOut, version: VersionOut, field: VersionField): boolean {
  if (field === "props") return canonicalJson(node.props) !== canonicalJson(version.props);
  return nodeText(node, field) !== versionText(version, field);
}

/**
 * The proposed-update reviewer panel.
 *
 * @param proposal The queue entry (for its `context`).
 * @param version The proposed version it carries.
 */
export function UpdateDiff({ proposal, version }: UpdateDiffProps) {
  const [data, setData] = useState<DiffData | null>(null);
  const [loading, setLoading] = useState(true);
  const [showServerDiff, setShowServerDiff] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    const load = async () => {
      setLoading(true);
      let node: NodeOut | null = null;
      let baseline: VersionOut | null = null;
      let loadError: string | null = null;
      try {
        node = await api.getNode(version.node_id, controller.signal);
      } catch (error) {
        if (controller.signal.aborted) return;
        loadError = failureMessage(error);
      }
      try {
        const history = await api.getHistory(version.node_id, controller.signal);
        // The baseline is the newest snapshot that was actually applied; a
        // node whose only versions are proposals has none.
        for (const entry of history) {
          if (entry.state !== "applied") continue;
          if (baseline === null || entry.id > baseline.id) baseline = entry;
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        loadError = loadError ?? failureMessage(error);
      }

      let serverDiff: DiffOut | null = null;
      let serverDiffError: string | null = null;
      if (baseline !== null) {
        try {
          serverDiff = await api.diffVersions(baseline.id, version.id, controller.signal);
        } catch (error) {
          if (controller.signal.aborted) return;
          serverDiffError = failureMessage(error);
        }
      } else {
        serverDiffError = "this node has no applied version yet, so there is nothing to diff against";
      }

      if (cancelled || controller.signal.aborted) return;
      setData({ node, baseline, serverDiff, serverDiffError, loadError });
      setLoading(false);
    };

    void load();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [version.node_id, version.id]);

  const applied = updateFields(version);
  const appliedSet = new Set<string>(applied);
  const targetRef = contextRef(proposal.context, "node");
  const targetLabel = refLabel(targetRef, version.node_id);

  return (
    <div className="nd-rv-update">
      <FieldLegend applied={applied} target={targetLabel} targetId={version.node_id} />

      {version.proposed_fields === null ? (
        <p className="nd-rv-flag nd-rv-flag--info">
          This proposal predates the <code>proposed_fields</code> column (migration
          0008), so the service reads it as naming all three fields. Accepting will
          write title, content, and props.
        </p>
      ) : null}

      {applied.length === 0 ? (
        <p className="nd-rv-flag nd-rv-flag--warn">
          This proposal names no fields — accepting it writes nothing to the node
          and simply marks the version applied.
        </p>
      ) : null}

      {loading ? (
        <p className="nd-rv-loading">
          <Spinner label="Loading the node this update targets" /> Loading the current node…
        </p>
      ) : null}

      {data?.loadError ? (
        <p className="nd-rv-flag nd-rv-flag--danger">
          Could not load the current node ({data.loadError}). The proposed snapshot
          is shown below on its own — without the node's current value there is
          nothing to compare it against, so do not accept from this screen alone.
        </p>
      ) : null}

      {data?.node
        ? VERSION_FIELDS.map((field) => (
            <FieldPanel
              key={field}
              field={field}
              willApply={appliedSet.has(field)}
              current={nodeText(data.node as NodeOut, field)}
              proposed={versionText(version, field)}
              moved={movedSinceProposal(data.node as NodeOut, version, field)}
            />
          ))
        : null}

      {data && !data.node ? (
        <div className="nd-rv-field">
          <span className="nd-label">Proposed snapshot</span>
          <pre className="nd-rv-diff__pane">{versionText(version, "content")}</pre>
        </div>
      ) : null}

      {data ? (
        <ServerDiff
          data={data}
          version={version}
          applied={applied}
          expanded={showServerDiff}
          onToggle={() => setShowServerDiff((current) => !current)}
        />
      ) : null}
    </div>
  );
}

/** The field chips: what accept writes, and what is only context. */
function FieldLegend({
  applied,
  target,
  targetId,
}: {
  applied: VersionField[];
  target: string;
  targetId: string;
}) {
  const appliedSet = new Set<string>(applied);
  return (
    <div className="nd-rv-legend">
      <p className="nd-rv-legend__lead">
        Accepting writes{" "}
        {applied.length === 0 ? (
          <strong>no fields</strong>
        ) : (
          applied.map((field, index) => (
            <span key={field}>
              {index > 0 ? " and " : ""}
              <strong className="nd-rv-legend__field">{field}</strong>
            </span>
          ))
        )}{" "}
        to <span className="nd-rv-legend__target">{target}</span>{" "}
        <span className="nd-mono" title={targetId}>
          {shortId(targetId)}
        </span>{" "}
        as it stands at the moment you click — not as it stood when the agent
        proposed this.
      </p>
      <div className="nd-row nd-rv-legend__chips">
        {VERSION_FIELDS.map((field) => (
          <span
            key={field}
            className={
              appliedSet.has(field)
                ? "nd-badge nd-badge--proposed nd-rv-chip"
                : "nd-badge nd-rv-chip nd-rv-chip--context"
            }
            title={
              appliedSet.has(field)
                ? `The proposal named ${field}; accepting writes it`
                : `The snapshot carries ${field} as reviewer context only; accepting will not write it`
            }
          >
            {field}
            {appliedSet.has(field) ? " · applied" : " · context only"}
          </span>
        ))}
      </div>
    </div>
  );
}

/** One field's before/after, labelled with whether accept will write it. */
function FieldPanel({
  field,
  willApply,
  current,
  proposed,
  moved,
}: {
  field: VersionField;
  willApply: boolean;
  current: string;
  proposed: string;
  moved: boolean;
}) {
  const differs = current !== proposed;

  // A field the proposal did not name and that nothing changed is noise.
  if (!willApply && !differs) return null;

  return (
    <section className={willApply ? "nd-rv-field nd-rv-field--applied" : "nd-rv-field"}>
      <header className="nd-rv-field__head">
        <h4 className="nd-rv-field__name">{field}</h4>
        <span className={willApply ? "nd-rv-field__verdict" : "nd-rv-field__verdict nd-rv-field__verdict--context"}>
          {willApply ? "will be written on accept" : "context only — will not be written"}
        </span>
      </header>

      {!willApply && differs ? (
        <p className="nd-rv-flag nd-rv-flag--warn">
          The snapshot's <code>{field}</code> differs from the node's current
          value, and accepting will <strong>not</strong> apply it. The agent did
          not name this field; the snapshot copied it as it looked at proposal
          time.
        </p>
      ) : null}

      {willApply && moved ? (
        <p className="nd-rv-flag nd-rv-flag--info">
          The node's <code>{field}</code> has changed since this was proposed.
          Accepting overwrites the current value below with the proposed one.
        </p>
      ) : null}

      <SideBySide
        before={current}
        after={proposed}
        beforeLabel="on the node now"
        afterLabel={willApply ? "proposed — will be written" : "in the snapshot — not written"}
        identicalNote={
          willApply
            ? "No change — accepting writes the value the node already holds."
            : "Unchanged from the node's current value."
        }
      />
    </section>
  );
}

/** The server's unified diff, cross-checked against `proposed_fields`. */
function ServerDiff({
  data,
  version,
  applied,
  expanded,
  onToggle,
}: {
  data: DiffData;
  version: VersionOut;
  applied: VersionField[];
  expanded: boolean;
  onToggle: () => void;
}) {
  const appliedSet = new Set<string>(applied);
  const changed = data.serverDiff?.changed_fields ?? [];
  const misleading = changed.filter((field) => !appliedSet.has(field));

  return (
    <section className="nd-rv-serverdiff">
      <button
        type="button"
        className="nd-button nd-button--ghost nd-button--small"
        onClick={onToggle}
        aria-expanded={expanded}
      >
        {expanded ? "Hide" : "Show"} the server's unified diff
        {data.baseline ? ` (v${data.baseline.id} → v${version.id})` : ""}
      </button>

      {expanded ? (
        <div className="nd-rv-serverdiff__body">
          {data.serverDiff ? (
            <>
              <p className="nd-meta">
                <code>diff_versions</code> compares two whole snapshots — the last
                applied version ({formatAbsolute(data.baseline?.created_at)}) against
                this proposal. It is the audit view, not the accept preview.
              </p>
              {misleading.length > 0 ? (
                <p className="nd-rv-flag nd-rv-flag--warn">
                  The server reports <code>{changed.join(", ")}</code> as changed
                  between the two snapshots, but accepting writes only{" "}
                  <code>{applied.join(", ") || "nothing"}</code>.{" "}
                  <strong>{misleading.join(", ")}</strong>{" "}
                  {misleading.length === 1 ? "differs" : "differ"} in the snapshot
                  and will not be applied.
                </p>
              ) : null}
              <pre className="nd-rv-unified">{data.serverDiff.diff || "(no textual difference)"}</pre>
            </>
          ) : (
            <p className="nd-rv-flag nd-rv-flag--info">
              No server diff: {data.serverDiffError ?? "unavailable"}. The
              field-by-field comparison above is computed in the browser from the
              node's current row and the proposed snapshot, which is what an
              accept actually consults.
            </p>
          )}
        </div>
      ) : null}
    </section>
  );
}
