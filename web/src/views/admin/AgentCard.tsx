/**
 * One agent account and what it can reach.
 *
 * The card carries the agent's lifecycle actions (rotate the token,
 * disable/enable) and its grant list (one row per space, plus the add-grant
 * row). Everything destructive goes through {@link ConfirmDialog}; creation
 * and rotation end in {@link TokenDialog}, the show-once hand-off.
 */

import { useState } from "react";
import { useToast } from "../../components";
import { api } from "../../api/client";
import type { AgentOut, GrantLevel, GrantOut, NodeOut } from "../../api/types";
import { GRANT_LEVELS, grantableSpaces, grantsForAgent, LEVEL_SUMMARY, spaceLabel } from "./grants";
import { ConfirmDialog } from "./ConfirmDialog";

/** A destructive action waiting on its confirm. */
interface PendingConfirm {
  title: string;
  body: string;
  confirmLabel: string;
  run: () => Promise<void>;
}

interface AgentCardProps {
  agent: AgentOut;
  /** Every grant row; the card takes its own slice. */
  grants: readonly GrantOut[];
  /** Every active space. */
  spaces: readonly NodeOut[];
  /** Reload the view's data after a mutation lands. */
  onChanged: () => Promise<void>;
  /** Hand a freshly minted token to the show-once dialog. */
  onToken: (agentName: string, token: string) => void;
}

/** One agent account: lifecycle actions plus its grants. */
export function AgentCard({ agent, grants, spaces, onChanged, onToken }: AgentCardProps) {
  const toast = useToast();
  const [confirm, setConfirm] = useState<PendingConfirm | null>(null);
  const [newSpace, setNewSpace] = useState("");
  const [newLevel, setNewLevel] = useState<GrantLevel>("read");
  const [busy, setBusy] = useState(false);

  const own = grantsForAgent(grants, agent.id);
  const available = grantableSpaces(spaces, grants, agent.id);
  const pick = available.some((space) => space.id === newSpace) ? newSpace : (available[0]?.id ?? "");

  /** Run a quiet mutation: toast on failure, reload on success. */
  const mutate = (action: () => Promise<unknown>, done?: () => void) => {
    setBusy(true);
    void action().then(
      async () => {
        await onChanged();
        setBusy(false);
        done?.();
      },
      (error: unknown) => {
        setBusy(false);
        toast.showError(error);
      },
    );
  };

  const addGrant = () => {
    if (pick === "") return;
    mutate(() => api.setGrant({ agent: agent.id, space: pick, level: newLevel }), () =>
      setNewLevel("read"),
    );
  };

  const rotate = () =>
    setConfirm({
      title: `Rotate token for ${agent.name}`,
      body: `The current token dies the moment the new one is issued — any MCP client still configured with it stops working until it is updated.`,
      confirmLabel: "Rotate",
      run: async () => {
        const rotated = await api.rotateAgentToken(agent.id);
        await onChanged();
        onToken(agent.name, rotated.token);
      },
    });

  const toggleDisabled = () => {
    if (agent.disabled) {
      mutate(() => api.enableAgent(agent.id));
      return;
    }
    setConfirm({
      title: `Disable ${agent.name}`,
      body: `The agent's token is refused from now on — every MCP server it drives stops at its next request. In-flight proposals stay in the review queue.`,
      confirmLabel: "Disable",
      run: () => api.disableAgent(agent.id).then(() => onChanged()),
    });
  };

  const revoke = (grant: GrantOut) =>
    setConfirm({
      title: `Revoke ${spaceLabel(spaces, grant.space_id)} from ${agent.name}`,
      body: `The agent loses its ${grant.level} access to the space immediately. Its rows stay in the graph; only its reach changes.`,
      confirmLabel: "Revoke",
      run: () => api.revokeGrant({ agent: agent.id, space: grant.space_id }).then(() => onChanged()),
    });

  return (
    <section className="nd-card nd-ad-agent">
      <header className="nd-ad-agent__header">
        <div>
          <h2 className="nd-ad-agent__name">
            {agent.name} <span className="nd-mono nd-meta">{agent.id}</span>
          </h2>
          <p className="nd-meta">
            {agent.kind}
            {agent.has_token ? "" : " · no token"}
            {agent.disabled ? " · disabled" : ""}
          </p>
        </div>
        <div className="nd-row">
          <button
            type="button"
            className="nd-button nd-button--small nd-button--ghost"
            onClick={rotate}
            disabled={busy || agent.disabled}
          >
            Rotate token
          </button>
          <button
            type="button"
            className={`nd-button nd-button--small ${agent.disabled ? "" : "nd-button--danger"}`}
            onClick={toggleDisabled}
            disabled={busy}
          >
            {agent.disabled ? "Enable" : "Disable"}
          </button>
        </div>
      </header>

      <table className="nd-ad-grants">
        <thead>
          <tr>
            <th>Space</th>
            <th>Level</th>
            <th aria-label="Actions" />
          </tr>
        </thead>
        <tbody>
          {own.map((grant) => (
            <tr key={grant.space_id}>
              <td>{spaceLabel(spaces, grant.space_id)}</td>
              <td>
                <span className="nd-badge nd-badge--type nd-mono">{grant.level}</span>
              </td>
              <td className="nd-ad-grants__actions">
                <button
                  type="button"
                  className="nd-button nd-button--small nd-button--ghost"
                  onClick={() => revoke(grant)}
                  disabled={busy}
                >
                  Revoke
                </button>
              </td>
            </tr>
          ))}
          {own.length === 0 ? (
            <tr>
              <td colSpan={3} className="nd-meta">
                No grants — the agent cannot read or write anywhere.
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>

      {available.length > 0 ? (
        <div className="nd-row nd-ad-agent__add">
          <select
            className="nd-select"
            value={pick}
            onChange={(event) => setNewSpace(event.target.value)}
            aria-label="Space to grant"
          >
            {available.map((space) => (
              <option key={space.id} value={space.id}>
                {space.title ?? space.id}
              </option>
            ))}
          </select>
          <select
            className="nd-select"
            value={newLevel}
            onChange={(event) => setNewLevel(event.target.value as GrantLevel)}
            aria-label="Grant level"
          >
            {GRANT_LEVELS.map((level) => (
              <option key={level} value={level}>
                {level} — {LEVEL_SUMMARY[level]}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="nd-button nd-button--small nd-button--primary"
            onClick={addGrant}
            disabled={busy || pick === ""}
          >
            Grant
          </button>
        </div>
      ) : null}

      {confirm ? (
        <ConfirmDialog
          title={confirm.title}
          body={confirm.body}
          confirmLabel={confirm.confirmLabel}
          onConfirm={confirm.run}
          onClose={() => setConfirm(null)}
        />
      ) : null}
    </section>
  );
}
