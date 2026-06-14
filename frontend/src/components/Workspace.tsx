// The authenticated app shell: header (with logout + a Graph/Schema view switch)
// plus the search / create / detail panels or the schema-administration view.
// Owns the currently-open node and a reload token that lets child mutations
// trigger a refresh of the detail view.

import { useState } from "react";

import { logout } from "../api";
import { useSchema } from "../schema";
import { CreateEdge } from "./CreateEdge";
import { CreateNode } from "./CreateNode";
import { NodeDetail } from "./NodeDetail";
import { SchemaAdmin } from "./SchemaAdmin";
import { Search } from "./Search";

type View = "graph" | "schema";

interface WorkspaceProps {
  onSignedOut: () => void;
}

export function Workspace({ onSignedOut }: WorkspaceProps) {
  const { schema } = useSchema();
  const [view, setView] = useState<View>("graph");
  const [current, setCurrent] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  function openNode(uuid: string) {
    setCurrent(uuid);
    setReloadToken((token) => token + 1);
  }

  function reload() {
    setReloadToken((token) => token + 1);
  }

  async function doLogout() {
    await logout();
    onSignedOut();
  }

  return (
    <>
      <header>
        <button id="logout-button" className="ghost" type="button" onClick={doLogout}>
          Logout
        </button>
        <h1 className="wordmark">
          nodum<span className="dot">.</span>
        </h1>
        <p className="tagline">
          A typed knowledge graph — search, create, edit, delete, and explore the subgraph.
        </p>
        <div className="stat-strip">
          <div className="stat">
            <span className="stat-num">{schema.node_kinds.length}</span>
            <span className="stat-label">node kinds</span>
          </div>
          <div className="stat">
            <span className="stat-num">{schema.edge_kinds.length}</span>
            <span className="stat-label">edge kinds</span>
          </div>
        </div>
        <nav className="view-nav">
          <button
            type="button"
            className={view === "graph" ? "active" : ""}
            onClick={() => setView("graph")}
          >
            Graph
          </button>
          <button
            type="button"
            className={view === "schema" ? "active" : ""}
            onClick={() => setView("schema")}
          >
            Schema
          </button>
        </nav>
      </header>
      <main>
        {view === "graph" ? (
          <>
            <Search onOpen={openNode} />
            <CreateNode onCreated={openNode} />
            <CreateEdge onCreated={openNode} />
            {current && (
              <NodeDetail
                key={current}
                uuid={current}
                reloadToken={reloadToken}
                onOpen={openNode}
                onReload={reload}
                onDeleted={() => setCurrent(null)}
              />
            )}
          </>
        ) : (
          <SchemaAdmin />
        )}
      </main>
    </>
  );
}
