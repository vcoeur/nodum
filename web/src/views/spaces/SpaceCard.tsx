/**
 * One space: what lives in it, who can reach it, and its two lifecycle actions.
 *
 * The counts and the grant table are the substance of this screen rather than
 * decoration — a list of names would answer "what did I call things?" instead
 * of "what territory exists?", which is the question design decision D2 exists
 * to answer. So each card carries the live node count (`active` + `proposed`:
 * a space holding only an agent's proposals is not empty) and every agent
 * holding a grant, with its level.
 *
 * Rename is inline; archive goes through {@link ArchiveDialog}. `main` and
 * `meta` can be renamed — the id a rename leaves alone is what the schema
 * depends on — but never archived, and the disabled button says why rather
 * than leaving the human to discover it as a server error.
 */

import { useState } from "react";
import { Link } from "react-router-dom";
import { useToast } from "../../components";
import type { SpaceOut } from "../../api/types";
import { formatTimestamp } from "../../lib";
import { describeSpaceFailure, structuralReason, validateSpaceName } from "./spaces";
import type { SpaceRow } from "./spaces";
import { ArchiveDialog } from "./ArchiveDialog";

interface SpaceCardProps {
  /** The space, with everything the card shows already derived. */
  row: SpaceRow;
  /** Every active space — the vocabulary a new name must not collide with. */
  spaces: readonly SpaceOut[];
  /** Rename the space; throws on failure. */
  onRename: (row: SpaceRow, name: string) => Promise<void>;
  /** Archive the space; throws on failure. */
  onArchive: (row: SpaceRow) => Promise<void>;
}

/** One space's card. */
export function SpaceCard({ row, spaces, onRename, onArchive }: SpaceCardProps) {
  const toast = useToast();
  const [draftName, setDraftName] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [archiving, setArchiving] = useState(false);

  const renaming = draftName !== null;
  const nameError = renaming ? validateSpaceName(draftName, spaces, { renaming: row.id }) : null;
  const blockedReason = row.structural ? structuralReason(row.id) : null;

  const save = () => {
    if (draftName === null || nameError !== null) return;
    setBusy(true);
    void onRename(row, draftName.trim()).then(
      () => {
        setBusy(false);
        setDraftName(null);
      },
      (error: unknown) => {
        setBusy(false);
        const described = describeSpaceFailure(error, row.label);
        toast.show("error", described.title, described.body);
      },
    );
  };

  return (
    <section className="nd-card nd-sp-space">
      <header className="nd-sp-space__header">
        <div className="nd-sp-space__identity">
          {renaming ? (
            <div className="nd-sp-rename">
              <input
                name={`rename-${row.id}`}
                className="nd-input"
                type="text"
                value={draftName}
                aria-label={`New name for ${row.label}`}
                autoFocus
                onChange={(event) => setDraftName(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") save();
                  if (event.key === "Escape") setDraftName(null);
                }}
              />
              <button
                type="button"
                className="nd-button nd-button--small nd-button--primary"
                onClick={save}
                disabled={busy || nameError !== null}
              >
                {busy ? "Saving…" : "Save"}
              </button>
              <button
                type="button"
                className="nd-button nd-button--small nd-button--ghost"
                onClick={() => setDraftName(null)}
                disabled={busy}
              >
                Cancel
              </button>
            </div>
          ) : (
            <h2 className="nd-sp-space__name">
              {row.label} <span className="nd-mono nd-meta">{row.id}</span>
            </h2>
          )}

          <p className="nd-meta">
            {row.nodeCount === 1 ? "1 live node" : `${row.nodeCount} live nodes`}
            <span className="nd-sp-space__sep"> · </span>
            {row.holders.length === 0
              ? "no agent granted"
              : row.holders.length === 1
                ? "1 agent granted"
                : `${row.holders.length} agents granted`}
            <span className="nd-sp-space__sep"> · </span>
            created {formatTimestamp(row.space.created_at)}
          </p>
          <Link to={`/nodes?space=${encodeURIComponent(row.id)}`}>Browse nodes</Link>
        </div>

        <div className="nd-row nd-sp-space__actions">
          {row.writeTarget ? (
            <span className="nd-badge nd-badge--type" title="New nodes land here until you change the write target">
              write target
            </span>
          ) : null}
          {row.selfGoverning ? (
            <span
              className="nd-badge nd-sp-badge--governing"
              title="An agent holds edit here, so its writes may land active without reaching the review queue. A grant is a ceiling, not a mandate — it may still file a proposal below it."
            >
              writes directly
            </span>
          ) : null}
          {renaming ? null : (
            <button
              type="button"
              className="nd-button nd-button--small nd-button--ghost"
              onClick={() => setDraftName(row.label)}
              disabled={busy}
            >
              Rename
            </button>
          )}
          <button
            type="button"
            className={`nd-button nd-button--small ${blockedReason ? "" : "nd-button--danger"}`}
            onClick={() => setArchiving(true)}
            disabled={busy || blockedReason !== null}
            title={
              blockedReason ??
              "Retire the space from the vocabulary. Its nodes keep their space_id — nothing is deleted."
            }
          >
            Archive
          </button>
        </div>
      </header>

      {blockedReason ? <p className="nd-meta nd-sp-space__note">{blockedReason}</p> : null}
      {nameError ? <p className="nd-sp-space__error">{nameError}</p> : null}

      <table className="nd-sp-grants">
        <thead>
          <tr>
            <th>Agent</th>
            <th>Level</th>
          </tr>
        </thead>
        <tbody>
          {row.holders.map((holder) => (
            <tr key={holder.agent}>
              <td>{holder.agent}</td>
              <td>
                <span className="nd-badge nd-badge--type nd-mono">{holder.level}</span>
              </td>
            </tr>
          ))}
          {row.holders.length === 0 ? (
            <tr>
              <td colSpan={2} className="nd-meta">
                No agent holds a grant here — this space is yours alone.
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>

      {archiving ? (
        <ArchiveDialog
          row={row}
          onConfirm={() => onArchive(row)}
          onClose={() => setArchiving(false)}
        />
      ) : null}
    </section>
  );
}
