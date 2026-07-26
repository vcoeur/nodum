/**
 * Route `/admin` — accounts and grants.
 *
 * Replaces the removed policy editor as the view where the human administers
 * what agents can reach. Three sections: agent accounts (create, rotate the
 * show-once token, disable/enable), each agent's grants (add, revoke), and a
 * read-only list of the human accounts. Everything here is a thin call into
 * the `/api/agents`, `/api/grants`, `/api/spaces`, and `/api/humans` routes,
 * which are thin delegates over the service's human-only admin surface — this
 * view adds no authority the CLI does not have.
 */

import { useCallback, useEffect, useState } from "react";
import { EmptyState, Spinner, useSpaces, useToast } from "../../components";
import { api } from "../../api/client";
import type { AgentOut, GrantOut, HumanOut } from "../../api/types";
import { describeFailure } from "../../lib";
import type { FailureDescription } from "../../lib";
import { formatTimestamp } from "../../lib";
import { AgentCard } from "./AgentCard";
import { TokenDialog } from "./TokenDialog";
import "./admin.css";

/**
 * Everything the view loads once and reloads after each mutation.
 *
 * The space list is not in here: it comes from the shared {@link useSpaces},
 * which every space surface reads. What this view does with a failed one is
 * still its own decision — see the failure derivation below.
 */
interface AdminData {
  agents: AgentOut[];
  grants: GrantOut[];
  humans: HumanOut[];
}

/** A freshly minted token waiting in the show-once dialog. */
interface FreshToken {
  agentName: string;
  token: string;
}

/** The admin route. */
export default function AdminView() {
  const toast = useToast();
  const [data, setData] = useState<AdminData | null>(null);
  const [accountsFailure, setAccountsFailure] = useState<FailureDescription | null>(null);
  const [freshToken, setFreshToken] = useState<FreshToken | null>(null);
  const [newAgentName, setNewAgentName] = useState("");
  const [creating, setCreating] = useState(false);
  const spaceList = useSpaces();

  const load = useCallback(async () => {
    const [agents, grants, humans] = await Promise.all([
      api.listAgents(),
      api.listGrants(),
      api.listHumans(),
    ]);
    setData({ agents, grants, humans });
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [agents, grants, humans] = await Promise.all([
          api.listAgents(),
          api.listGrants(),
          api.listHumans(),
        ]);
        if (!cancelled) setData({ agents, grants, humans });
      } catch (error) {
        if (!cancelled) setAccountsFailure(describeFailure(error, "the accounts"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // This view *escalates* a missing space list rather than degrading with it,
  // which is why the shared hook carries the error. Without the vocabulary the
  // add-grant picker would render empty — indistinguishable from "every space is
  // already granted" — on the one screen whose job is handing an agent a space.
  const failure =
    accountsFailure ?? (spaceList.failed ? describeFailure(spaceList.error, "the spaces") : null);
  // Bound to a local so the null check below narrows inside the render callbacks
  // too; a property access does not.
  const spaces = spaceList.spaces;

  const createAgent = () => {
    const name = newAgentName.trim();
    if (name === "") return;
    setCreating(true);
    void api.createAgent(name).then(
      async (created) => {
        setCreating(false);
        setNewAgentName("");
        await load();
        setFreshToken({ agentName: created.agent.name, token: created.token });
      },
      (error: unknown) => {
        setCreating(false);
        toast.showError(error, "Agent not created");
      },
    );
  };

  return (
    <div className="nd-view nd-ad">
      <header className="nd-view__header">
        <div>
          <h1>Admin</h1>
          <p className="nd-meta nd-ad__subtitle">
            Agent accounts and the spaces their grants let them reach. Humans
            are listed for reference; their passwords are set with the CLI.
          </p>
        </div>
      </header>

      {failure ? (
        <EmptyState title={failure.title} body={failure.body} />
      ) : data === null || spaces === null ? (
        <div className="nd-empty">
          <Spinner large label="Loading accounts" />
        </div>
      ) : (
        <>
          <section className="nd-ad-section">
            <h2 className="nd-ad-section__title">Agents</h2>

            <div className="nd-row nd-ad-create">
              <input
                name="new-agent-name"
                className="nd-input"
                type="text"
                placeholder="New agent name"
                value={newAgentName}
                onChange={(event) => setNewAgentName(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") createAgent();
                }}
              />
              <button
                type="button"
                className="nd-button nd-button--primary"
                onClick={createAgent}
                disabled={creating || newAgentName.trim() === ""}
              >
                {creating ? "Creating…" : "Create agent"}
              </button>
            </div>

            {data.agents.length === 0 ? (
              <EmptyState
                title="No agents yet"
                body="An agent is an MCP client's identity. Create one, put its token in the client's NODUM_AGENT_TOKEN, then grant it spaces here."
              />
            ) : (
              data.agents.map((agent) => (
                <AgentCard
                  key={agent.id}
                  agent={agent}
                  grants={data.grants}
                  spaces={spaces}
                  onChanged={load}
                  onToken={(agentName, token) => setFreshToken({ agentName, token })}
                />
              ))
            )}
          </section>

          <section className="nd-ad-section">
            <h2 className="nd-ad-section__title">Humans</h2>
            <table className="nd-ad-grants">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Id</th>
                  <th>Password</th>
                  <th>State</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {data.humans.map((human) => (
                  <tr key={human.id}>
                    <td>{human.name}</td>
                    <td className="nd-mono nd-meta">{human.id}</td>
                    <td className="nd-meta">{human.has_password ? "set" : "not set"}</td>
                    <td>
                      {human.disabled ? (
                        <span className="nd-badge nd-badge--archived">disabled</span>
                      ) : (
                        <span className="nd-badge nd-badge--active">active</span>
                      )}
                    </td>
                    <td className="nd-meta">{formatTimestamp(human.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}

      {freshToken ? (
        <TokenDialog
          agentName={freshToken.agentName}
          token={freshToken.token}
          onClose={() => setFreshToken(null)}
        />
      ) : null}
    </div>
  );
}

export { AdminView };
